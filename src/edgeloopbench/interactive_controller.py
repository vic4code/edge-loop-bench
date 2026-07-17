"""Four-arm interactive controller with explicit causal and budget semantics."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .interactive_environment import (
    ActionExecution,
    AttemptEvaluation,
    AttemptEvaluator,
    AttemptEvaluationKind,
    EnvironmentCheckpoint,
    EnvironmentFactory,
    InteractiveEnvironment,
    StrictEvaluation,
    StrictEvaluator,
    TerminalFinalization,
    TerminalFinalizer,
    TerminalSelection,
)
from .journal import append_journal_event, inspect_journal, seal_journal
from .model_adapter import (
    InteractiveModel,
    InteractiveModelOutput,
    InteractiveModelRequest,
    PromptPreparer,
    RenderedPromptByteLimitExceeded,
    TranscriptMessage,
)


INTERACTIVE_STRATEGIES = (
    "direct",
    "independent_verified_sampling",
    "raw_feedback_loop",
    "engineered_loop",
)
INTERACTIVE_CONTROLLER_REVISION = "interactive-controller-v4-v07-preregistered-topology"
MAX_ACTION_BYTES = 8 * 1024
_SHA256_REFERENCE = re.compile(r"sha256:[0-9a-f]{64}\Z")
PARSER_RETRY_OBSERVATION = (
    "Invalid response. Return exactly one JSON object with one non-empty "
    'single-line string field named "command".'
)
COMMON_PROMPT_TEMPLATE = """Complete the Bash filesystem task below.
Return exactly one JSON object with one non-empty single-line string field named \"command\".
The command runs in an isolated offline task container. Do not include Markdown or explanation.

Task ID: {task_id}
Task: {query}"""


class ActionParseError(ValueError):
    """Raised when model output does not match the frozen action grammar."""


@dataclass(frozen=True)
class InteractiveTask:
    task_id: str
    query: str

    def __post_init__(self) -> None:
        if not self.task_id or not self.query:
            raise ValueError("interactive task id and query must not be empty")


@dataclass(frozen=True)
class InteractiveBudget:
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    model_calls: int
    environment_actions: int
    evaluator_calls: int
    checkpoint_creates: int
    checkpoint_restores: int
    safety_recoveries: int
    per_call_context_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for field, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"interactive budget {field} must be a positive integer")


@dataclass(frozen=True)
class InteractiveResult:
    run_status: str
    official_success: bool
    strict_success: bool
    stop_reason: str
    attempts: int
    model_calls: int
    logical_prompt_tokens: int
    logical_completion_tokens: int
    environment_actions: int
    evaluator_calls: int
    checkpoint_creates: int
    checkpoint_restores: int
    safety_recoveries: int
    parser_failures: int
    initial_prompts: int
    independent_sample_prompts: int
    feedback_followups: int
    human_prompts: int = 0

    @property
    def maintenance_operations(self) -> int:
        return (
            self.checkpoint_creates
            + self.checkpoint_restores
            + self.safety_recoveries
        )


@dataclass
class _Counters:
    attempts: int = 0
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    environment_actions: int = 0
    evaluator_calls: int = 0
    checkpoint_creates: int = 0
    checkpoint_restores: int = 0
    safety_recoveries: int = 0
    parser_failures: int = 0
    initial_prompts: int = 0
    independent_sample_prompts: int = 0
    feedback_followups: int = 0


@dataclass(frozen=True)
class _SelectedCheckpoint:
    checkpoint: EnvironmentCheckpoint
    reward: float
    official_success: bool
    evaluation_kind: AttemptEvaluationKind
    attempt: int


def parse_action(model_text: str) -> str:
    """Parse the frozen one-command JSON action without invoking a shell."""

    try:
        raw = json.loads(model_text, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ActionParseError("model response is not valid action JSON") from error
    if not isinstance(raw, dict) or set(raw) != {"command"}:
        raise ActionParseError("action JSON must contain only command")
    command = raw["command"]
    if not isinstance(command, str) or not command or not command.strip():
        raise ActionParseError("command must be a non-empty string")
    if command != command.strip():
        raise ActionParseError("command must not have leading or trailing whitespace")
    try:
        encoded = command.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ActionParseError("command contains invalid Unicode") from error
    if len(encoded) > MAX_ACTION_BYTES:
        raise ActionParseError("command exceeds action byte limit")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in command
    ):
        raise ActionParseError("command must be one line without control characters")
    return command


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ActionParseError(f"action JSON repeats field {key!r}")
        result[key] = value
    return result


def candidate_seed(replicate_seed: int, attempt: int) -> int:
    """Return the shared deterministic candidate schedule for every arm."""

    if attempt <= 0:
        raise ValueError("attempt must be positive")
    if attempt == 1:
        return replicate_seed
    payload = f"edge-loop-interactive-seed-v1:{replicate_seed}:{attempt}".encode()
    return int.from_bytes(sha256(payload).digest()[:4], "big") & 0x7FFFFFFF


def run_interactive_strategy(
    *,
    strategy: str,
    task: InteractiveTask,
    model: InteractiveModel,
    prompt_preparer: PromptPreparer,
    environment_factory: EnvironmentFactory,
    attempt_evaluate: AttemptEvaluator,
    strict_evaluate: StrictEvaluator,
    terminal_finalize: TerminalFinalizer,
    budget: InteractiveBudget,
    replicate_seed: int,
    event_log: str | Path,
    execution_authority_sha256: str | None = None,
) -> InteractiveResult:
    """Execute one preregistered interactive arm and select one checkpoint."""

    if strategy not in INTERACTIVE_STRATEGIES:
        raise ValueError(f"unsupported interactive strategy: {strategy}")
    if isinstance(replicate_seed, bool) or not isinstance(replicate_seed, int):
        raise ValueError("replicate_seed must be an integer")
    if execution_authority_sha256 is not None and (
        type(execution_authority_sha256) is not str
        or _SHA256_REFERENCE.fullmatch(execution_authority_sha256) is None
    ):
        raise ValueError("execution authority must be a lowercase SHA-256")

    counters = _Counters()
    environment: InteractiveEnvironment | None = None
    independent_environments: list[InteractiveEnvironment] = []
    selected: _SelectedCheckpoint | None = None
    best: _SelectedCheckpoint | None = None
    initial_transcript = (TranscriptMessage("user", _common_prompt(task)),)
    transcript = list(initial_transcript)
    context_id = _context_id(task, strategy, replicate_seed, 1)
    stop_reason = "attempt_budget_exhausted"
    infrastructure_reason: str | None = None
    action_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    signature_counts: dict[str, int] = {}
    episode_error: BaseException | None = None

    existing_journal = inspect_journal(event_log)
    if existing_journal.record_count or existing_journal.partial_tail is not None:
        raise ValueError("interactive event journal must be empty before an episode starts")

    event_identity: dict[str, object] = {
        "task_id": task.task_id,
        "strategy": strategy,
        "replicate_seed": replicate_seed,
    }
    if execution_authority_sha256 is not None:
        event_identity["execution_authority_sha256"] = (
            execution_authority_sha256
        )

    def record(event_type: str, **fields: object) -> None:
        append_journal_event(
            event_log,
            {
                "type": event_type,
                **event_identity,
                **fields,
            },
        )

    def close_environment(
        target: InteractiveEnvironment, *, scope: str
    ) -> None:
        request_error: BaseException | None = None
        try:
            record("environment_close_requested", scope=scope)
        except BaseException as error:
            request_error = error
        target.close()
        if request_error is not None:
            raise request_error
        record("environment_closed", scope=scope)

    record("controller_started", controller_revision=INTERACTIVE_CONTROLLER_REVISION)
    try:
        maximum_attempts = min(budget.attempts, budget.model_calls)
        for attempt in range(1, maximum_attempts + 1):
            if counters.prompt_tokens >= budget.prompt_tokens:
                stop_reason = "logical_prompt_token_budget_exhausted"
                break
            if counters.completion_tokens >= budget.completion_tokens:
                stop_reason = "logical_completion_token_budget_exhausted"
                break

            if attempt == 1:
                messages = tuple(transcript)
            elif strategy == "independent_verified_sampling":
                messages = initial_transcript
            else:
                messages = tuple(transcript)

            try:
                prepared_prompt = prompt_preparer(messages)
            except RenderedPromptByteLimitExceeded as error:
                stop_reason = "rendered_prompt_byte_budget_exhausted"
                record(
                    "model_request_rejected",
                    attempt=attempt,
                    reason="rendered_prompt_byte_budget",
                    prompt_sha256=error.prompt_sha256,
                    renderer_profile_sha256=error.renderer_profile_sha256,
                    observed_prompt_bytes=error.observed_bytes,
                    prompt_byte_limit=error.limit_bytes,
                )
                break
            record(
                "model_preflighted",
                attempt=attempt,
                prompt_sha256=prepared_prompt.prompt_sha256,
                prompt_tokens=prepared_prompt.prompt_tokens,
                token_ids_sha256=prepared_prompt.token_ids_sha256,
                renderer_profile_sha256=prepared_prompt.renderer_profile_sha256,
                tokenizer_artifact_sha256=(
                    prepared_prompt.tokenizer_artifact_sha256
                ),
                model_artifact_sha256=prepared_prompt.model_artifact_sha256,
            )
            remaining_prompt = budget.prompt_tokens - counters.prompt_tokens
            if prepared_prompt.prompt_tokens > remaining_prompt:
                stop_reason = "logical_prompt_token_budget_exhausted"
                record(
                    "model_request_rejected",
                    attempt=attempt,
                    reason="prompt_budget",
                    prompt_sha256=prepared_prompt.prompt_sha256,
                    prompt_tokens=prepared_prompt.prompt_tokens,
                    remaining_prompt_tokens=remaining_prompt,
                )
                break

            request_context_id = (
                _context_id(task, strategy, replicate_seed, attempt)
                if strategy == "independent_verified_sampling"
                else context_id
            )
            remaining_completion = budget.completion_tokens - counters.completion_tokens
            remaining_context = (
                budget.per_call_context_tokens - prepared_prompt.prompt_tokens
            )
            if remaining_context <= 0:
                stop_reason = "per_call_context_token_budget_exhausted"
                record(
                    "model_request_rejected",
                    attempt=attempt,
                    reason="per_call_context_budget",
                    prompt_sha256=prepared_prompt.prompt_sha256,
                    prompt_tokens=prepared_prompt.prompt_tokens,
                    remaining_context_tokens=max(0, remaining_context),
                )
                break
            output_limit = min(
                budget.max_output_tokens,
                remaining_completion,
                remaining_context,
            )
            if output_limit <= 0:
                stop_reason = "logical_completion_token_budget_exhausted"
                break
            request = InteractiveModelRequest(
                prepared_prompt=prepared_prompt,
                seed=candidate_seed(replicate_seed, attempt),
                context_id=request_context_id,
                max_output_tokens=output_limit,
            )
            if attempt == 1:
                counters.initial_prompts += 1
            elif strategy == "independent_verified_sampling":
                counters.independent_sample_prompts += 1
            else:
                counters.feedback_followups += 1
            record(
                "model_requested",
                attempt=attempt,
                prompt_sha256=prepared_prompt.prompt_sha256,
                logical_model_calls_after=counters.model_calls + 1,
                logical_prompt_tokens_after=(
                    counters.prompt_tokens + prepared_prompt.prompt_tokens
                ),
                candidate_seed=request.seed,
                context_sha256=_text_digest(request_context_id),
                max_output_tokens=output_limit,
            )
            counters.attempts += 1
            counters.model_calls += 1
            counters.prompt_tokens += prepared_prompt.prompt_tokens
            output = model(request)
            counters.completion_tokens += output.completion_tokens
            record(
                "model_completed",
                attempt=attempt,
                response_sha256=_text_digest(output.text),
                prompt_tokens=output.prompt_tokens,
                completion_tokens=output.completion_tokens,
                total_duration_ns=output.total_duration_ns,
            )
            if output.prompt_tokens != prepared_prompt.prompt_tokens:
                stop_reason = "prompt_token_telemetry_mismatch"
                infrastructure_reason = stop_reason
                record(
                    "infrastructure_invalid",
                    attempt=attempt,
                    reason=stop_reason,
                    preflight_prompt_tokens=prepared_prompt.prompt_tokens,
                    telemetry_prompt_tokens=output.prompt_tokens,
                )
                break
            call_context = output.prompt_tokens + output.completion_tokens
            if (
                output.completion_tokens > output_limit
                or call_context > budget.per_call_context_tokens
            ):
                stop_reason = "generation_telemetry_budget_violation"
                infrastructure_reason = stop_reason
                record(
                    "infrastructure_invalid",
                    attempt=attempt,
                    reason=stop_reason,
                    allowed_completion_tokens=output_limit,
                    telemetry_completion_tokens=output.completion_tokens,
                    allowed_context_tokens=budget.per_call_context_tokens,
                    telemetry_context_tokens=call_context,
                )
                break

            try:
                action = parse_action(output.text)
            except ActionParseError:
                counters.parser_failures += 1
                record("action_rejected", attempt=attempt, reason="parser_failure")
                if strategy == "direct":
                    stop_reason = "direct_parser_failure"
                    break
                if strategy in {"raw_feedback_loop", "engineered_loop"}:
                    controller_packet: str | None = None
                    if strategy == "engineered_loop":
                        controller_packet = _engineered_packet(
                            attempt=attempt,
                            budget=budget,
                            counters=counters,
                            action=None,
                            execution=None,
                            reward=0.0,
                            best_reward=best.reward if best is not None else 0.0,
                            score_delta=-(best.reward) if best is not None else 0.0,
                            rollback_performed=False,
                            repeated_action_count=0,
                            repeated_state_count=0,
                            repeated_signature_count=0,
                            restored_state_sha256=None,
                        )
                    _append_feedback(
                        transcript,
                        output.text,
                        PARSER_RETRY_OBSERVATION,
                        0.0,
                        exit_code=None,
                        controller_packet=controller_packet,
                    )
                continue

            if (
                counters.environment_actions >= budget.environment_actions
                # Reserve one evaluator slot for a possible selected strict
                # endpoint.  The trusted terminal hook separately budgets any
                # preregistered posthoc trajectory calls.
                or counters.evaluator_calls >= budget.evaluator_calls - 1
                or counters.checkpoint_creates >= budget.checkpoint_creates
                or counters.safety_recoveries >= budget.safety_recoveries
            ):
                stop_reason = "action_pipeline_budget_exhausted"
                break

            if strategy == "independent_verified_sampling":
                record("environment_create_requested", attempt=attempt, scope="attempt")
                current_environment = environment_factory()
                independent_environments.append(current_environment)
                record("environment_created", attempt=attempt, scope="attempt")
            else:
                if environment is None:
                    record("environment_create_requested", attempt=attempt, scope="episode")
                    environment = environment_factory()
                    record("environment_created", attempt=attempt, scope="episode")
                current_environment = environment

            record("action_requested", attempt=attempt, action_sha256=_text_digest(action))
            execution = current_environment.execute(action)
            counters.environment_actions += 1
            record(
                "action_completed",
                attempt=attempt,
                action_sha256=_text_digest(action),
                output_sha256=execution.output_sha256,
                state_sha256=execution.state_sha256,
                exit_code=execution.exit_code,
                admissible=execution.admissible,
                state_changed=execution.state_changed,
                policy_failure=(
                    None
                    if execution.policy_failure is None
                    else execution.policy_failure.value
                ),
                safety_recovery_performed=execution.safety_recovery_performed,
            )
            if not execution.admissible:
                counters.safety_recoveries += 1
                record(
                    "safety_recovery_completed",
                    attempt=attempt,
                    state_sha256=execution.state_sha256,
                    recovery_evidence_sha256=(
                        execution.safety_recovery_evidence_sha256
                    ),
                )
                evaluation = AttemptEvaluation(
                    reward=0.0,
                    official_success=False,
                    evaluation_kind=AttemptEvaluationKind.ACTION_POLICY_FAILURE,
                )
                record(
                    "attempt_defaulted",
                    attempt=attempt,
                    reward=0.0,
                    official_success=False,
                    evaluation_kind=evaluation.evaluation_kind.value,
                    policy_failure=execution.policy_failure.value,
                )
                if strategy == "independent_verified_sampling":
                    close_environment(current_environment, scope=f"attempt-{attempt}")
                    independent_environments.remove(current_environment)

                repeated_action_count = 0
                repeated_state_count = 0
                repeated_signature_count = 0
                if strategy == "engineered_loop":
                    (
                        repeated_action_count,
                        repeated_state_count,
                        repeated_signature_count,
                    ) = _increment_repeat_counts(
                        action_counts=action_counts,
                        state_counts=state_counts,
                        signature_counts=signature_counts,
                        action=action,
                        execution=execution,
                        reward=0.0,
                    )

                if strategy == "direct":
                    stop_reason = "direct_action_policy_failure"
                    break
                if strategy in {"raw_feedback_loop", "engineered_loop"}:
                    controller_packet = None
                    if strategy == "engineered_loop":
                        best_reward = best.reward if best is not None else 0.0
                        controller_packet = _engineered_packet(
                            attempt=attempt,
                            budget=budget,
                            counters=counters,
                            action=action,
                            execution=execution,
                            reward=0.0,
                            best_reward=best_reward,
                            score_delta=-best_reward,
                            rollback_performed=False,
                            repeated_action_count=repeated_action_count,
                            repeated_state_count=repeated_state_count,
                            repeated_signature_count=repeated_signature_count,
                            restored_state_sha256=execution.state_sha256,
                        )
                    _append_feedback(
                        transcript,
                        output.text,
                        execution.observation,
                        0.0,
                        exit_code=execution.exit_code,
                        controller_packet=controller_packet,
                    )
                    if strategy == "engineered_loop" and repeated_signature_count >= 3:
                        selected = best
                        stop_reason = "no_progress_guard"
                        break
                continue
            record("checkpoint_create_requested", attempt=attempt)
            checkpoint = current_environment.checkpoint()
            counters.checkpoint_creates += 1
            record(
                "checkpoint_created",
                attempt=attempt,
                state_sha256=checkpoint.state_sha256,
            )
            record(
                "attempt_evaluation_requested",
                attempt=attempt,
                state_sha256=checkpoint.state_sha256,
            )
            evaluation = attempt_evaluate(checkpoint)
            counters.evaluator_calls += 1
            record(
                "attempt_evaluated",
                attempt=attempt,
                reward=float(evaluation.reward),
                official_success=evaluation.official_success,
                evaluation_kind=evaluation.evaluation_kind.value,
            )
            if strategy == "independent_verified_sampling":
                close_environment(current_environment, scope=f"attempt-{attempt}")
                independent_environments.remove(current_environment)
            candidate = _SelectedCheckpoint(
                checkpoint=checkpoint,
                reward=float(evaluation.reward),
                official_success=evaluation.official_success,
                evaluation_kind=evaluation.evaluation_kind,
                attempt=attempt,
            )
            if strategy == "independent_verified_sampling":
                if selected is None or candidate.reward > selected.reward:
                    selected = candidate
            else:
                selected = candidate

            rollback_performed = False
            restored_state_sha256: str | None = None
            repeated_action_count = 0
            repeated_state_count = 0
            repeated_signature_count = 0
            if strategy == "engineered_loop":
                prior_best_reward = best.reward if best is not None else 0.0
                if best is None or candidate.reward >= best.reward:
                    best = candidate
                else:
                    if counters.checkpoint_restores >= budget.checkpoint_restores:
                        selected = best
                        stop_reason = "checkpoint_restore_budget_exhausted"
                        break
                    record(
                        "checkpoint_restore_requested",
                        attempt=attempt,
                        state_sha256=best.checkpoint.state_sha256,
                    )
                    current_environment.restore(best.checkpoint)
                    counters.checkpoint_restores += 1
                    rollback_performed = True
                    restored_state_sha256 = best.checkpoint.state_sha256
                    selected = best
                    record(
                        "checkpoint_restored",
                        attempt=attempt,
                        state_sha256=best.checkpoint.state_sha256,
                    )
                (
                    repeated_action_count,
                    repeated_state_count,
                    repeated_signature_count,
                ) = _increment_repeat_counts(
                    action_counts=action_counts,
                    state_counts=state_counts,
                    signature_counts=signature_counts,
                    action=action,
                    execution=execution,
                    reward=candidate.reward,
                )

            if evaluation.official_success:
                selected = candidate
                if strategy == "engineered_loop":
                    best = candidate
                stop_reason = "official_success"
                break
            if strategy == "direct":
                stop_reason = "direct_complete"
                break

            if strategy in {"raw_feedback_loop", "engineered_loop"}:
                controller_packet = None
                if strategy == "engineered_loop":
                    assert best is not None
                    controller_packet = _engineered_packet(
                        attempt=attempt,
                        budget=budget,
                        counters=counters,
                        action=action,
                        execution=execution,
                        reward=float(evaluation.reward),
                        best_reward=best.reward,
                        score_delta=float(evaluation.reward) - prior_best_reward,
                        rollback_performed=rollback_performed,
                        repeated_action_count=repeated_action_count,
                        repeated_state_count=repeated_state_count,
                        repeated_signature_count=repeated_signature_count,
                        restored_state_sha256=restored_state_sha256,
                    )
                _append_feedback(
                    transcript,
                    output.text,
                    execution.observation,
                    float(evaluation.reward),
                    exit_code=execution.exit_code,
                    controller_packet=controller_packet,
                )
                if strategy == "engineered_loop":
                    if repeated_signature_count >= 3:
                        selected = best
                        stop_reason = "no_progress_guard"
                        break
        else:
            stop_reason = "attempt_budget_exhausted"
    except BaseException as error:
        episode_error = error
    finally:
        if environment is not None:
            try:
                close_environment(environment, scope="episode")
            except BaseException as error:
                if episode_error is None:
                    episode_error = error
        for index, independent_environment in enumerate(independent_environments, 1):
            try:
                close_environment(
                    independent_environment,
                    scope=f"incomplete-attempt-{index}",
                )
            except BaseException as error:
                if episode_error is None:
                    episode_error = error

    terminal_aborted = episode_error is not None or infrastructure_reason is not None
    if terminal_aborted:
        selected = None
    elif strategy == "engineered_loop" and best is not None:
        selected = best
    official_success = selected.official_success if selected is not None else False
    strict_is_allowed = bool(
        selected is not None
        and selected.evaluation_kind is AttemptEvaluationKind.EVALUATOR_DERIVED
        and not terminal_aborted
    )
    terminal_selection = TerminalSelection(
        checkpoint=None if selected is None else selected.checkpoint,
        selected_attempt=None if selected is None else selected.attempt,
        evaluation_kind=None if selected is None else selected.evaluation_kind,
        official_success=official_success,
        aborted=terminal_aborted,
    )
    remaining_evaluator_calls = budget.evaluator_calls - counters.evaluator_calls
    try:
        if strict_is_allowed:
            assert selected is not None
            record(
                "strict_evaluation_planned",
                selected_attempt=selected.attempt,
                state_sha256=selected.checkpoint.state_sha256,
            )
        elif selected is not None:
            record(
                "strict_evaluation_defaulted",
                selected_attempt=selected.attempt,
                reason=selected.evaluation_kind.value,
                strict_success=False,
            )
        record(
            "terminal_finalization_requested",
            selected_attempt=terminal_selection.selected_attempt,
            evaluation_kind=(
                None
                if terminal_selection.evaluation_kind is None
                else terminal_selection.evaluation_kind.value
            ),
            aborted=terminal_selection.aborted,
            remaining_evaluator_calls=remaining_evaluator_calls,
        )
    except BaseException as error:
        if episode_error is None:
            episode_error = error
        terminal_aborted = True
        selected = None
        official_success = False
        strict_is_allowed = False
        terminal_selection = TerminalSelection(
            checkpoint=None,
            selected_attempt=None,
            evaluation_kind=None,
            official_success=False,
            aborted=True,
        )
    strict_calls = 0

    def counted_strict_evaluate(
        checkpoint: EnvironmentCheckpoint,
    ) -> StrictEvaluation:
        nonlocal strict_calls
        if (
            not strict_is_allowed
            or selected is None
            or checkpoint != selected.checkpoint
        ):
            raise RuntimeError("terminal strict evaluator checkpoint is invalid")
        if strict_calls >= 1 or strict_calls >= remaining_evaluator_calls:
            raise RuntimeError("terminal evaluator call budget is exhausted")
        strict_calls += 1
        return strict_evaluate(checkpoint)

    terminal_outcome = terminal_finalize(
        terminal_selection,
        counted_strict_evaluate if strict_is_allowed else None,
        remaining_evaluator_calls,
    )
    if type(terminal_outcome) is not TerminalFinalization:
        raise RuntimeError("terminal finalizer returned an invalid result")
    if terminal_outcome.strict_evaluator_calls != strict_calls:
        raise RuntimeError("terminal strict evaluator accounting is contradictory")
    if strict_is_allowed:
        if strict_calls != 1 or terminal_outcome.strict_evaluation is None:
            raise RuntimeError("selected evaluator-derived checkpoint lacks strict result")
    elif terminal_outcome.strict_evaluation is not None or strict_calls:
        raise RuntimeError("default or empty selection cannot have a strict result")
    if terminal_outcome.evaluator_calls > remaining_evaluator_calls:
        raise RuntimeError("terminal evaluator call budget was exceeded")
    counters.evaluator_calls += terminal_outcome.evaluator_calls
    record(
        "terminal_finalized",
        strict_evaluator_calls=terminal_outcome.strict_evaluator_calls,
        posthoc_evaluator_calls=terminal_outcome.posthoc_evaluator_calls,
    )

    strict_success = False
    if terminal_outcome.strict_evaluation is not None:
        strict_success = terminal_outcome.strict_evaluation.strict_success
        record(
            "strict_evaluation_completed",
            strict_success=terminal_outcome.strict_evaluation.strict_success,
            evaluator_sha256=terminal_outcome.strict_evaluation.evaluator_sha256,
        )
    if episode_error is not None:
        raise episode_error
    record(
        "controller_stopped",
        stop_reason=stop_reason,
        selected_attempt=selected.attempt if selected is not None else None,
        official_success=official_success,
    )
    seal_journal(event_log)

    if infrastructure_reason is not None:
        run_status = "infrastructure_error"
    elif "budget_exhausted" in stop_reason:
        run_status = "budget_exhausted"
    else:
        run_status = "completed"
    return InteractiveResult(
        run_status=run_status,
        official_success=official_success,
        strict_success=strict_success,
        stop_reason=stop_reason,
        attempts=counters.attempts,
        model_calls=counters.model_calls,
        logical_prompt_tokens=counters.prompt_tokens,
        logical_completion_tokens=counters.completion_tokens,
        environment_actions=counters.environment_actions,
        evaluator_calls=counters.evaluator_calls,
        checkpoint_creates=counters.checkpoint_creates,
        checkpoint_restores=counters.checkpoint_restores,
        safety_recoveries=counters.safety_recoveries,
        parser_failures=counters.parser_failures,
        initial_prompts=counters.initial_prompts,
        independent_sample_prompts=counters.independent_sample_prompts,
        feedback_followups=counters.feedback_followups,
    )


def _common_prompt(task: InteractiveTask) -> str:
    return COMMON_PROMPT_TEMPLATE.format(task_id=task.task_id, query=task.query)


def _append_feedback(
    transcript: list[TranscriptMessage],
    model_text: str,
    observation: str,
    reward: float,
    *,
    exit_code: int | None,
    controller_packet: str | None = None,
) -> None:
    rendered_reward = _render_reward(reward)
    rendered_exit_code = "null" if exit_code is None else str(exit_code)
    feedback = (
        f"Output: {observation}\n"
        f"Exit status: {rendered_exit_code}\n"
        f"Reward: {rendered_reward}"
    )
    if controller_packet is not None:
        feedback += "\n" + controller_packet
    transcript.extend(
        (
            TranscriptMessage("assistant", model_text),
            TranscriptMessage("user", feedback),
        )
    )


def _engineered_packet(
    *,
    attempt: int,
    budget: InteractiveBudget,
    counters: _Counters,
    action: str | None,
    execution: ActionExecution | None,
    reward: float,
    best_reward: float,
    score_delta: float,
    rollback_performed: bool,
    repeated_action_count: int,
    repeated_state_count: int,
    repeated_signature_count: int,
    restored_state_sha256: str | None,
) -> str:
    state = {
        "attempt": attempt,
        "remaining": {
            "attempts": max(0, budget.attempts - counters.attempts),
            "model_calls": max(0, budget.model_calls - counters.model_calls),
            "environment_actions": max(
                0, budget.environment_actions - counters.environment_actions
            ),
            "prompt_tokens": max(0, budget.prompt_tokens - counters.prompt_tokens),
            "completion_tokens": max(
                0, budget.completion_tokens - counters.completion_tokens
            ),
        },
        "last_command": action,
        "last_command_sha256": _text_digest(action) if action is not None else None,
        "last_output": execution.observation if execution is not None else None,
        "exit_status": execution.exit_code if execution is not None else None,
        "output_sha256": execution.output_sha256 if execution is not None else None,
        "admissible": execution.admissible if execution is not None else False,
        "state_changed": execution.state_changed if execution is not None else False,
        "state_sha256": execution.state_sha256 if execution is not None else None,
        "safety_recovery_performed": (
            execution.safety_recovery_performed if execution is not None else False
        ),
        "reward": reward,
        "best_reward": best_reward,
        "score_delta": score_delta,
        "repeated_action_count": repeated_action_count,
        "repeated_state_count": repeated_state_count,
        "repeated_signature_count": repeated_signature_count,
        "rollback_performed": rollback_performed,
        "restored_state_sha256": restored_state_sha256,
    }
    instruction = "Use this evidence to choose a meaningfully different next command."
    if repeated_signature_count >= 2:
        instruction = (
            "Form a new failure hypothesis and issue a meaningfully different command."
        )
    return (
        "Controller state: "
        + json.dumps(state, sort_keys=True)
        + "\nController instruction: "
        + instruction
    )


def _progress_signature(
    action: str,
    execution: ActionExecution,
    reward: float,
) -> str:
    normalized_action = _normalized_action(action)
    payload = json.dumps(
        {
            "action": normalized_action,
            "state_sha256": execution.state_sha256,
            "reward": _render_reward(reward),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_digest(payload)


def _increment_repeat_counts(
    *,
    action_counts: dict[str, int],
    state_counts: dict[str, int],
    signature_counts: dict[str, int],
    action: str,
    execution: ActionExecution,
    reward: float,
) -> tuple[int, int, int]:
    normalized_action = _normalized_action(action)
    action_counts[normalized_action] = action_counts.get(normalized_action, 0) + 1
    state_counts[execution.state_sha256] = (
        state_counts.get(execution.state_sha256, 0) + 1
    )
    signature = _progress_signature(action, execution, reward)
    signature_counts[signature] = signature_counts.get(signature, 0) + 1
    return (
        action_counts[normalized_action],
        state_counts[execution.state_sha256],
        signature_counts[signature],
    )


def _normalized_action(action: str) -> str:
    return " ".join(action.split())


def _context_id(
    task: InteractiveTask,
    strategy: str,
    replicate_seed: int,
    attempt: int,
) -> str:
    return _text_digest(
        f"interactive-context-v1:{task.task_id}:{strategy}:{replicate_seed}:{attempt}"
    )


def _text_digest(value: str | None) -> str:
    payload = "" if value is None else value
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()


def _render_reward(reward: float) -> str:
    value = float(reward)
    if not math.isfinite(value):
        raise ValueError("reward must be finite")
    return str(value)
