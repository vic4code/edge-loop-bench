"""Small explicit strategy state machines with logical-token accounting."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import LogicalBudget
from .runner import (
    CandidatePatchError,
    append_event,
    apply_candidate_edits,
    build_agent_prompt,
    run_public_tests,
)
from .tasks import TaskManifest


@dataclass(frozen=True)
class ModelOutput:
    text: str
    thinking: str
    prompt_tokens: int
    completion_tokens: int
    total_duration_ns: int


ModelCall = Callable[[str, int, int], ModelOutput]
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
) -> StrategyResult:
    """Run direct or bounded-retry using only sanitized public feedback."""

    if strategy not in {"direct", "bounded_retry"}:
        raise ValueError(f"unsupported runnable strategy: {strategy}")
    root = Path(worktree).resolve()
    started_ns = time.monotonic_ns()
    sequence = 0

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
                **fields,
            },
        )

    record("run_started", initial_commit=task.initial_commit)
    prompt_tokens = 0
    completion_tokens = 0
    model_calls = 0
    tool_calls = 0
    public_test_runs = 0
    max_call_context_tokens = 0
    prompt = build_agent_prompt(root, task)
    failure_reason: str | None = None
    objective_success = False

    call_limit = 1 if strategy == "direct" else budget.model_calls
    for _attempt in range(call_limit):
        remaining_completion = budget.completion_tokens - completion_tokens
        if remaining_completion <= 0:
            failure_reason = "completion_token_budget_exhausted"
            break
        output = model(prompt, seed, remaining_completion)
        model_calls += 1
        prompt_tokens += output.prompt_tokens
        completion_tokens += output.completion_tokens
        call_context = output.prompt_tokens + output.completion_tokens
        max_call_context_tokens = max(max_call_context_tokens, call_context)
        record(
            "model_completed",
            model_call=model_calls,
            text=output.text,
            thinking=output.thinking,
            prompt_tokens=output.prompt_tokens,
            completion_tokens=output.completion_tokens,
            total_duration_ns=output.total_duration_ns,
        )
        if (
            prompt_tokens > budget.prompt_tokens
            or completion_tokens > budget.completion_tokens
            or call_context > budget.per_call_context_tokens
        ):
            failure_reason = "logical_token_budget_exhausted"
            break

        tool_calls += 1
        if tool_calls > budget.tool_calls:
            failure_reason = "tool_call_budget_exhausted"
            break
        try:
            apply_candidate_edits(root, task, output.text)
        except CandidatePatchError as error:
            failure_reason = "candidate_edit_rejected"
            feedback = str(error)
            record("candidate_rejected", reason=feedback)
            prompt = build_agent_prompt(root, task) + (
                "\n\nThe previous candidate was rejected: " + feedback +
                "\nReturn corrected JSON only."
            )
            continue
        record("candidate_applied", model_call=model_calls)

        if public_test_runs >= budget.public_test_runs or tool_calls >= budget.tool_calls:
            failure_reason = "public_test_budget_exhausted"
            break
        public = run_public_tests(root, task)
        public_test_runs += 1
        tool_calls += 1
        record(
            "public_test_completed",
            passed=public.passed,
            returncode=public.returncode,
            output=public.output,
        )
        if public.passed:
            try:
                objective_success = bool(evaluate(root, task))
            except Exception:
                return _finish(
                    record, started_ns, "infrastructure_error", False,
                    "isolated_evaluation_error", prompt_tokens, completion_tokens,
                    model_calls, tool_calls, public_test_runs, max_call_context_tokens,
                )
            record("evaluation_completed", passed=objective_success)
            failure_reason = None if objective_success else "isolated_evaluation_failed"
            break
        failure_reason = "public_tests_failed"
        prompt = build_agent_prompt(root, task) + (
            "\n\nThe previous edit failed public tests. Sanitized output:\n" + public.output +
            "\nReturn corrected JSON only."
        )

    status = "completed"
    if failure_reason is not None and failure_reason.endswith("budget_exhausted"):
        status = "budget_exhausted"
    elif not objective_success and model_calls >= call_limit and strategy == "bounded_retry":
        status = "budget_exhausted"
    return _finish(
        record, started_ns, status, objective_success, failure_reason,
        prompt_tokens, completion_tokens, model_calls, tool_calls,
        public_test_runs, max_call_context_tokens,
    )


def _finish(
    record: Callable[..., None],
    started_ns: int,
    run_status: str,
    objective_success: bool,
    failure_reason: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    model_calls: int,
    tool_calls: int,
    public_test_runs: int,
    max_call_context_tokens: int,
) -> StrategyResult:
    wall_seconds = (time.monotonic_ns() - started_ns) / 1_000_000_000
    record(
        "run_completed", run_status=run_status,
        objective_success=objective_success, failure_reason=failure_reason,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        model_calls=model_calls, tool_calls=tool_calls,
        public_test_runs=public_test_runs,
        max_call_context_tokens=max_call_context_tokens,
        wall_seconds=wall_seconds,
    )
    return StrategyResult(
        run_status, objective_success, failure_reason,
        prompt_tokens, completion_tokens, model_calls, tool_calls,
        public_test_runs, max_call_context_tokens, wall_seconds,
    )
