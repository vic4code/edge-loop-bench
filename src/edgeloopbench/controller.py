"""Small explicit strategy state machines with logical-token accounting."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import LogicalBudget
from .runner import (
    EDIT_RESPONSE_SCHEMA,
    CandidatePatchError,
    append_event,
    apply_candidate_edits,
    build_agent_prompt,
    run_public_tests,
)
from .tasks import TaskManifest


VERIFIER_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REJECT", "ESCALATE"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["requirement", "correctness", "edge_case", "regression"],
                    },
                    "location": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["category", "location", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "findings"],
    "additionalProperties": False,
}

CHECKER_NAMES = (
    "requirement_coverage",
    "boundary_conditions",
    "state_and_side_effects",
    "cross_file_contract",
    "regression_risk",
)

CHECKER_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "object",
            "properties": {
                name: {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["PASS", "FAIL", "UNKNOWN"]},
                        "location": {"type": "string", "minLength": 1, "maxLength": 96},
                        "evidence": {"type": "string", "minLength": 1, "maxLength": 180},
                    },
                    "required": ["status", "location", "evidence"],
                    "additionalProperties": False,
                }
                for name in CHECKER_NAMES
            },
            "required": list(CHECKER_NAMES),
            "additionalProperties": False,
        },
    },
    "required": ["checks"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ModelOutput:
    text: str
    thinking: str
    prompt_tokens: int
    completion_tokens: int
    total_duration_ns: int


@dataclass(frozen=True)
class ModelRequest:
    """A role-explicit model request with a role-specific output contract."""

    role: str
    prompt: str
    seed: int
    max_output_tokens: int
    response_schema: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.role not in {"maker", "verifier"}:
            raise ValueError(f"unsupported model role: {self.role}")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True)
class RunContext:
    experiment_id: str
    budget_tier: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.budget_tier:
            raise ValueError("run context identifiers must not be empty")
        if not self.manifest_sha256.startswith("sha256:") or len(self.manifest_sha256) != 71:
            raise ValueError("run context manifest_sha256 must be a SHA-256 reference")


ModelCall = Callable[[ModelRequest], ModelOutput]
Evaluator = Callable[[Path, TaskManifest], bool]


@dataclass(frozen=True)
class StrategyResult:
    run_status: str
    objective_success: bool
    failure_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    model_calls: int
    tool_calls: int
    public_test_runs: int
    max_call_context_tokens: int
    wall_seconds: float
    verifier_verdict: str | None = None
    verifier_protocol_error: bool = False
    fallback_used: bool = False
    candidate_a_success: bool | None = None
    candidate_b_success: bool | None = None


@dataclass
class _Counters:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    public_test_runs: int = 0
    max_call_context_tokens: int = 0


@dataclass(frozen=True)
class _Verdict:
    verdict: str
    findings: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class _ChecklistVerdict:
    verdict: str
    checks: tuple[dict[str, str], ...]


Snapshot = dict[str, bytes]


def run_strategy(
    strategy: str,
    worktree: str | Path,
    task: TaskManifest,
    model: ModelCall,
    budget: LogicalBudget,
    *,
    seed: int,
    event_log: str | Path,
    evaluate: Evaluator,
    context: RunContext,
) -> StrategyResult:
    """Run one strategy without returning private evaluation feedback."""

    if strategy not in {
        "direct",
        "bounded_retry",
        "maker_verifier",
        "evidence_gated_loop",
        "goal_skill_loop",
    }:
        raise ValueError(f"unsupported runnable strategy: {strategy}")
    root = Path(worktree).resolve()
    started_ns = time.monotonic_ns()
    counters = _Counters()
    sequence = 0
    failure_reason: str | None = None
    verifier_verdict: str | None = None
    verifier_protocol_error = False
    fallback_used = False
    candidate_a: Snapshot | None = None
    candidate_b: Snapshot | None = None
    final_candidate: Snapshot | None = None

    # The first maker call has the same cap in every arm. Maker-Verifier also
    # keeps a distinct 25% verifier reserve inside the shared episode budget.
    maker_call_cap = max(1, budget.completion_tokens * 3 // 4)
    verifier_cap = max(1, budget.completion_tokens - maker_call_cap)
    maker_used = 0
    verifier_used = 0

    def record(event_type: str, **fields: object) -> None:
        nonlocal sequence
        sequence += 1
        append_event(
            event_log,
            {
                "sequence": sequence,
                "type": event_type,
                "task_id": task.id,
                "strategy": strategy,
                "seed": seed,
                "experiment_id": context.experiment_id,
                "budget_tier": context.budget_tier,
                "manifest_sha256": context.manifest_sha256,
                **fields,
            },
        )

    def call(role: str, prompt: str) -> ModelOutput | None:
        nonlocal maker_used, verifier_used, failure_reason
        if counters.model_calls >= budget.model_calls:
            failure_reason = "model_call_budget_exhausted"
            return None
        remaining_total = budget.completion_tokens - counters.completion_tokens
        if role == "verifier":
            remaining_role = verifier_cap - verifier_used
            schema = (
                CHECKER_RESPONSE_SCHEMA
                if strategy == "evidence_gated_loop"
                else VERIFIER_RESPONSE_SCHEMA
            )
            per_call_cap = (
                max(1, verifier_cap // 2)
                if strategy == "evidence_gated_loop"
                else verifier_cap
            )
        else:
            remaining_role = (
                maker_call_cap - maker_used
                if strategy in {"maker_verifier", "evidence_gated_loop"}
                else remaining_total
            )
            schema = EDIT_RESPONSE_SCHEMA
            per_call_cap = maker_call_cap
        limit = min(remaining_total, remaining_role, per_call_cap)
        if limit <= 0:
            failure_reason = f"{role}_completion_token_budget_exhausted"
            return None
        output = model(ModelRequest(role, prompt, seed, limit, schema))
        counters.model_calls += 1
        counters.prompt_tokens += output.prompt_tokens
        counters.completion_tokens += output.completion_tokens
        if role == "verifier":
            verifier_used += output.completion_tokens
        else:
            maker_used += output.completion_tokens
        call_context = output.prompt_tokens + output.completion_tokens
        counters.max_call_context_tokens = max(counters.max_call_context_tokens, call_context)
        record(
            "model_completed",
            role=role,
            model_call=counters.model_calls,
            text=output.text,
            thinking=output.thinking,
            prompt_tokens=output.prompt_tokens,
            completion_tokens=output.completion_tokens,
            max_output_tokens=limit,
            total_duration_ns=output.total_duration_ns,
        )
        if (
            counters.prompt_tokens > budget.prompt_tokens
            or counters.completion_tokens > budget.completion_tokens
            or call_context > budget.per_call_context_tokens
            or output.completion_tokens > limit
        ):
            failure_reason = "logical_token_budget_exhausted"
            return None
        return output

    def apply_and_test(output: ModelOutput) -> tuple[bool, str]:
        nonlocal failure_reason
        if counters.tool_calls >= budget.tool_calls:
            failure_reason = "tool_call_budget_exhausted"
            return False, "Tool-call budget exhausted."
        counters.tool_calls += 1
        try:
            apply_candidate_edits(root, task, output.text)
        except CandidatePatchError as error:
            failure_reason = "candidate_edit_rejected"
            feedback = str(error)
            record("candidate_rejected", reason=feedback)
            return False, feedback
        record("candidate_applied", model_call=counters.model_calls)
        if (
            counters.public_test_runs >= budget.public_test_runs
            or counters.tool_calls >= budget.tool_calls
        ):
            failure_reason = "public_test_budget_exhausted"
            return False, "Public-test budget exhausted."
        public = run_public_tests(root, task)
        counters.public_test_runs += 1
        counters.tool_calls += 1
        record(
            "public_test_completed",
            passed=public.passed,
            returncode=public.returncode,
            output=public.output,
        )
        failure_reason = None if public.passed else "public_tests_failed"
        return public.passed, public.output

    record(
        "run_started",
        initial_commit=task.initial_commit,
        controller=(
            "evidence-gated-checker-v5"
            if strategy == "evidence_gated_loop"
            else "goal-skill-loop-v1"
            if strategy == "goal_skill_loop"
            else "read-only-verifier-v2"
        ),
    )
    base_prompt = build_agent_prompt(root, task)
    attempt = 0
    public_passed = False
    while True:
        if attempt > 0 and (
            counters.public_test_runs >= budget.public_test_runs
            or counters.tool_calls + 2 > budget.tool_calls
        ):
            failure_reason = "public_test_budget_exhausted"
            break
        attempt += 1
        if strategy == "goal_skill_loop":
            maker_prompt = _goal_skill_prompt(
                root,
                task,
                attempt,
                failure_reason if attempt > 1 else None,
                feedback if attempt > 1 else None,
            )
        else:
            maker_prompt = (
                base_prompt
                if attempt == 1
                else _retry_prompt(root, task, attempt, failure_reason, feedback)
            )
        output = call("maker", maker_prompt)
        if output is None:
            break
        public_passed, feedback = apply_and_test(output)
        if public_passed:
            final_candidate = _snapshot(root)
            break
        attempt_cap = 5 if strategy == "goal_skill_loop" else 3
        if strategy == "direct" or attempt >= attempt_cap or counters.model_calls >= budget.model_calls:
            break

    if strategy == "maker_verifier" and public_passed and final_candidate is not None:
        candidate_a = final_candidate
        record("candidate_checkpointed", candidate="A", sha256=_snapshot_digest(candidate_a))
        verifier_output = call("verifier", _verifier_prompt(root, task))
        if verifier_output is None:
            verifier_verdict = "ESCALATE"
            fallback_used = True
            record("candidate_fallback", candidate="A", reason=failure_reason)
        else:
            try:
                verdict = _parse_verdict(verifier_output.text)
            except ValueError as error:
                verifier_protocol_error = True
                verifier_verdict = "ESCALATE"
                fallback_used = True
                failure_reason = None
                record("verifier_protocol_error", reason=str(error))
                record("candidate_fallback", candidate="A", reason="verifier_protocol_error")
            else:
                verifier_verdict = verdict.verdict
                record(
                    "verifier_completed",
                    verdict=verdict.verdict,
                    findings=list(verdict.findings),
                )
                if verdict.verdict == "REJECT":
                    revision = call("maker", _revision_prompt(root, task, verdict))
                    if revision is None:
                        fallback_used = True
                        record("candidate_fallback", candidate="A", reason=failure_reason)
                    else:
                        revision_passed, _revision_feedback = apply_and_test(revision)
                        if revision_passed:
                            candidate_b = _snapshot(root)
                            final_candidate = candidate_b
                            record(
                                "candidate_checkpointed",
                                candidate="B",
                                sha256=_snapshot_digest(candidate_b),
                            )
                        else:
                            fallback_used = True
                            final_candidate = candidate_a
                            _restore(root, candidate_a)
                            record("candidate_fallback", candidate="A", reason=failure_reason)
                elif verdict.verdict == "ESCALATE":
                    fallback_used = True
                    record("candidate_fallback", candidate="A", reason="verifier_escalated")

    if strategy == "evidence_gated_loop" and public_passed and final_candidate is not None:
        candidate_a = final_candidate
        record("candidate_checkpointed", candidate="A", sha256=_snapshot_digest(candidate_a))
        checker_output = call("verifier", _checker_prompt(root, task, feedback))
        checklist = _parse_or_escalate_checklist(checker_output)
        if checklist is None:
            verifier_protocol_error = checker_output is not None
            verifier_verdict = "ESCALATE"
            fallback_used = True
            failure_reason = None
            if verifier_protocol_error:
                record("checker_protocol_error", phase="initial")
            record("candidate_fallback", candidate="A", reason="checker_unavailable")
        else:
            verifier_verdict = checklist.verdict
            record("checker_completed", verdict=checklist.verdict, checks=list(checklist.checks))
            if checklist.verdict == "REJECT" and attempt < 3:
                attempt += 1
                revision = call("maker", _checklist_revision_prompt(root, task, checklist))
                if revision is not None:
                    revision_passed, revision_feedback = apply_and_test(revision)
                    if revision_passed:
                        candidate_b = _snapshot(root)
                        record(
                            "candidate_checkpointed",
                            candidate="B",
                            sha256=_snapshot_digest(candidate_b),
                            selected=False,
                        )
                        recheck_output = call("verifier", _checker_prompt(root, task, revision_feedback))
                        recheck = _parse_or_escalate_checklist(recheck_output)
                        if recheck is None and recheck_output is not None:
                            verifier_protocol_error = True
                            record("checker_protocol_error", phase="revision")
                        if recheck is not None:
                            verifier_verdict = recheck.verdict
                            record("checker_completed", verdict=recheck.verdict, checks=list(recheck.checks))
                        if recheck is not None and recheck.verdict == "APPROVE":
                            final_candidate = candidate_b
                            record("candidate_selected", candidate="B")
                        else:
                            fallback_used = True
                            final_candidate = candidate_a
                            _restore(root, candidate_a)
                            record("candidate_fallback", candidate="A", reason="revision_not_approved")
                    else:
                        fallback_used = True
                        final_candidate = candidate_a
                        _restore(root, candidate_a)
                        record("candidate_fallback", candidate="A", reason=failure_reason)
                else:
                    fallback_used = True
                    record("candidate_fallback", candidate="A", reason=failure_reason)
            elif checklist.verdict != "APPROVE":
                fallback_used = True
                record("candidate_fallback", candidate="A", reason="checker_escalated_or_attempt_cap")

    objective_success = False
    candidate_a_success: bool | None = None
    candidate_b_success: bool | None = None
    if final_candidate is not None:
        try:
            if candidate_a is not None:
                _restore(root, candidate_a)
                candidate_a_success = bool(evaluate(root, task))
                record("candidate_evaluation_completed", candidate="A", passed=candidate_a_success)
            if candidate_b is not None:
                _restore(root, candidate_b)
                candidate_b_success = bool(evaluate(root, task))
                record("candidate_evaluation_completed", candidate="B", passed=candidate_b_success)
            _restore(root, final_candidate)
            objective_success = (
                candidate_b_success
                if final_candidate is candidate_b
                else candidate_a_success
                if final_candidate is candidate_a
                else bool(evaluate(root, task))
            )
            record("evaluation_completed", passed=objective_success)
            failure_reason = None if objective_success else "isolated_evaluation_failed"
        except Exception:
            _restore(root, final_candidate)
            failure_reason = "isolated_evaluation_error"
            return _finish(
                record, started_ns, "infrastructure_error", False, failure_reason,
                counters, verifier_verdict, verifier_protocol_error, fallback_used,
                candidate_a_success, candidate_b_success,
            )

    status = "completed"
    if failure_reason is not None and failure_reason.endswith("budget_exhausted"):
        status = "budget_exhausted"
    return _finish(
        record, started_ns, status, objective_success, failure_reason, counters,
        verifier_verdict, verifier_protocol_error, fallback_used,
        candidate_a_success, candidate_b_success,
    )


def _retry_prompt(
    root: Path,
    task: TaskManifest,
    attempt: int,
    failure_class: str | None,
    feedback: str,
) -> str:
    return build_agent_prompt(root, task) + (
        "\n\nRetry packet\n"
        f"Attempt: {attempt}\n"
        f"Failure class: {(failure_class or 'unknown').upper()}\n"
        "Sanitized public evidence:\n"
        f"{feedback}\n"
        "Return corrected JSON only."
    )


def _goal_skill_prompt(
    root: Path,
    task: TaskManifest,
    attempt: int,
    failure_class: str | None,
    feedback: str | None,
) -> str:
    prompt = build_agent_prompt(root, task) + (
        "\n\nDeterministic goal\n"
        "Make the agent-visible public tests pass. Stop immediately when they pass. "
        "The controller permits at most five maker attempts.\n\n"
        "Verification skill\n"
        "Before returning edits, inspect the visible requirements and source. Check "
        "requirement coverage, boundary conditions, state and side effects, cross-file "
        "contracts, and regression risk. Then return only the required full-file edit JSON."
    )
    if feedback is None:
        return prompt + f"\nCurrent attempt: {attempt}."
    return prompt + (
        "\n\nPrevious attempt evidence\n"
        f"Current attempt: {attempt}.\n"
        f"Failure class: {(failure_class or 'unknown').upper()}.\n"
        "Sanitized public evidence:\n"
        f"{feedback}\n"
        "Use this evidence and repeat the verification skill before returning corrected JSON."
    )


def _verifier_prompt(root: Path, task: TaskManifest) -> str:
    maker_prompt = build_agent_prompt(root, task)
    public_snapshot = "\n".join(maker_prompt.splitlines()[4:])
    return (
        "Act as a read-only verifier for Candidate A. Do not propose or return file edits.\n\n"
        + public_snapshot
        +
        "\n\nCandidate A passed all public tests. Judge the stated requirements and visible "
        "source for blocking correctness problems. Return only a verdict object with "
        "verdict APPROVE, REJECT, or ESCALATE and structured findings."
    )


def _checker_prompt(root: Path, task: TaskManifest, public_feedback: str) -> str:
    maker_prompt = build_agent_prompt(root, task)
    public_snapshot = "\n".join(maker_prompt.splitlines()[4:])
    return (
        "Act as a fresh read-only checker. Do not return edits and do not assume the maker is correct.\n\n"
        + public_snapshot
        + "\n\nSanitized public-test evidence:\n"
        + public_feedback
        + "\nEvaluate every required checklist item using only visible evidence. "
        "The checks object has exactly five fixed keys: requirement_coverage, "
        "boundary_conditions, state_and_side_effects, cross_file_contract, and regression_risk. "
        "Fill each key exactly once. Keep location under 96 characters and evidence under 180 characters. "
        "Missing public tests alone is not UNKNOWN: inspect the stated requirements and visible source. "
        "Return FAIL only for a concrete visible defect with an actionable location; use UNKNOWN only "
        "when the requirement or visible source is genuinely ambiguous."
    )


def _checklist_revision_prompt(
    root: Path, task: TaskManifest, checklist: _ChecklistVerdict,
) -> str:
    failures = [check for check in checklist.checks if check["status"] == "FAIL"]
    return build_agent_prompt(root, task) + (
        "\n\nA read-only checker found these blocking issues in the current candidate:\n"
        + json.dumps(failures, sort_keys=True)
        + "\nReturn a complete corrected edit JSON only."
    )


def _revision_prompt(root: Path, task: TaskManifest, verdict: _Verdict) -> str:
    return build_agent_prompt(root, task) + (
        "\n\nA read-only verifier rejected Candidate A with these agent-visible findings:\n"
        + json.dumps(list(verdict.findings), sort_keys=True)
        + "\nReturn a complete corrected edit JSON only."
    )


def _parse_verdict(text: str) -> _Verdict:
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ValueError("verifier response is not valid JSON") from error
    if not isinstance(raw, dict) or set(raw) != {"verdict", "findings"}:
        raise ValueError("verifier response must contain only verdict and findings")
    verdict = raw["verdict"]
    findings = raw["findings"]
    if verdict not in {"APPROVE", "REJECT", "ESCALATE"} or not isinstance(findings, list):
        raise ValueError("verifier response has an invalid verdict or findings list")
    parsed: list[dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"category", "location", "reason"}:
            raise ValueError("verifier finding has an invalid shape")
        if finding["category"] not in {"requirement", "correctness", "edge_case", "regression"}:
            raise ValueError("verifier finding has an invalid category")
        if not all(isinstance(finding[key], str) and finding[key].strip() for key in ("location", "reason")):
            raise ValueError("verifier finding text must not be empty")
        parsed.append(dict(finding))
    if verdict == "REJECT" and not parsed:
        raise ValueError("REJECT requires at least one actionable finding")
    return _Verdict(verdict, tuple(parsed))


def _parse_or_escalate_checklist(output: ModelOutput | None) -> _ChecklistVerdict | None:
    if output is None:
        return None
    try:
        raw = json.loads(output.text)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return None
    if not isinstance(raw, dict) or set(raw) != {"checks"} or not isinstance(raw["checks"], dict):
        return None
    if set(raw["checks"]) != set(CHECKER_NAMES):
        return None
    parsed: list[dict[str, str]] = []
    for name in CHECKER_NAMES:
        check = raw["checks"][name]
        if not isinstance(check, dict) or set(check) != {"status", "location", "evidence"}:
            return None
        if check["status"] not in {"PASS", "FAIL", "UNKNOWN"}:
            return None
        if not all(isinstance(check[key], str) and check[key].strip() for key in ("location", "evidence")):
            return None
        parsed.append({"name": name, **check})
    verdict = "REJECT" if any(check["status"] == "FAIL" for check in parsed) else (
        "ESCALATE" if any(check["status"] == "UNKNOWN" for check in parsed) else "APPROVE"
    )
    return _ChecklistVerdict(verdict, tuple(parsed))


def _snapshot(root: Path) -> Snapshot:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


def _restore(root: Path, snapshot: Snapshot) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and ".git" not in path.parts:
            relative = path.relative_to(root).as_posix()
            if relative not in snapshot:
                path.unlink()
    for relative, payload in snapshot.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _snapshot_digest(snapshot: Snapshot) -> str:
    from hashlib import sha256

    digest = sha256()
    for relative, payload in sorted(snapshot.items()):
        digest.update(relative.encode("utf-8") + b"\0" + payload + b"\0")
    return "sha256:" + digest.hexdigest()


def _finish(
    record: Callable[..., None],
    started_ns: int,
    run_status: str,
    objective_success: bool,
    failure_reason: str | None,
    counters: _Counters,
    verifier_verdict: str | None,
    verifier_protocol_error: bool,
    fallback_used: bool,
    candidate_a_success: bool | None,
    candidate_b_success: bool | None,
) -> StrategyResult:
    wall_seconds = (time.monotonic_ns() - started_ns) / 1_000_000_000
    record(
        "run_completed",
        run_status=run_status,
        objective_success=objective_success,
        failure_reason=failure_reason,
        prompt_tokens=counters.prompt_tokens,
        completion_tokens=counters.completion_tokens,
        model_calls=counters.model_calls,
        tool_calls=counters.tool_calls,
        public_test_runs=counters.public_test_runs,
        max_call_context_tokens=counters.max_call_context_tokens,
        wall_seconds=wall_seconds,
        verifier_verdict=verifier_verdict,
        verifier_protocol_error=verifier_protocol_error,
        fallback_used=fallback_used,
        candidate_a_success=candidate_a_success,
        candidate_b_success=candidate_b_success,
    )
    return StrategyResult(
        run_status,
        objective_success,
        failure_reason,
        counters.prompt_tokens,
        counters.completion_tokens,
        counters.model_calls,
        counters.tool_calls,
        counters.public_test_runs,
        counters.max_call_context_tokens,
        wall_seconds,
        verifier_verdict,
        verifier_protocol_error,
        fallback_used,
        candidate_a_success,
        candidate_b_success,
    )
