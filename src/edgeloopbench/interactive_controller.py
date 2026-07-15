"""Four-arm interactive controller with explicit causal and budget semantics."""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .interactive_environment import (
    ActionExecution,
    AttemptEvaluator,
    EnvironmentCheckpoint,
    EnvironmentFactory,
    InteractiveEnvironment,
    StrictEvaluator,
)
from .journal import append_journal_event, inspect_journal, seal_journal


INTERACTIVE_STRATEGIES = (
    "direct",
    "independent_verified_sampling",
    "raw_feedback_loop",
    "engineered_loop",
)
MAX_ACTION_BYTES = 8 * 1024
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
    per_call_context_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for field, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"interactive budget {field} must be a positive integer")


@dataclass(frozen=True)
class InteractiveModelRequest:
    prompt: str
    seed: int
    context_id: str
    max_output_tokens: int

    def __post_init__(self) -> None:
        if not self.prompt or not self.context_id:
            raise ValueError("interactive request prompt and context_id must not be empty")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("interactive request seed must be an integer")
        if self.max_output_tokens <= 0:
            raise ValueError("interactive request max_output_tokens must be positive")


@dataclass(frozen=True)
class InteractiveModelOutput:
    text: str
    prompt_tokens: int
    completion_tokens: int
    total_duration_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("interactive model output text must be a string")
        for field in ("prompt_tokens", "completion_tokens", "total_duration_ns"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"interactive model output {field} must be non-negative")


InteractiveModel = Callable[[InteractiveModelRequest], InteractiveModelOutput]


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
    parser_failures: int
    initial_prompts: int
    independent_sample_prompts: int
    feedback_followups: int
    human_prompts: int = 0

    @property
    def maintenance_operations(self) -> int:
        return self.checkpoint_creates + self.checkpoint_restores


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
    parser_failures: int = 0
    initial_prompts: int = 0
    independent_sample_prompts: int = 0
    feedback_followups: int = 0


@dataclass(frozen=True)
class _SelectedCheckpoint:
    checkpoint: EnvironmentCheckpoint
    reward: float
    official_success: bool
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
    environment_factory: EnvironmentFactory,
    attempt_evaluate: AttemptEvaluator,
    strict_evaluate: StrictEvaluator,
    budget: InteractiveBudget,
    replicate_seed: int,
    event_log: str | Path,
) -> InteractiveResult:
    """Execute one preregistered interactive arm and select one checkpoint."""

    if strategy not in INTERACTIVE_STRATEGIES:
        raise ValueError(f"unsupported interactive strategy: {strategy}")
    if isinstance(replicate_seed, bool) or not isinstance(replicate_seed, int):
        raise ValueError("replicate_seed must be an integer")

    counters = _Counters()
    environment: InteractiveEnvironment | None = None
    independent_environments: list[InteractiveEnvironment] = []
    selected: _SelectedCheckpoint | None = None
    best: _SelectedCheckpoint | None = None
    transcript = _common_prompt(task)
    context_id = _context_id(task, strategy, replicate_seed, 1)
    stop_reason = "attempt_budget_exhausted"
    signature_counts: dict[str, int] = {}

    existing_journal = inspect_journal(event_log)
    if existing_journal.record_count or existing_journal.partial_tail is not None:
        raise ValueError("interactive event journal must be empty before an episode starts")

    def record(event_type: str, **fields: object) -> None:
        append_journal_event(
            event_log,
            {
                "type": event_type,
                "task_id": task.task_id,
                "strategy": strategy,
                "replicate_seed": replicate_seed,
                **fields,
            },
        )

    def close_environment(
        target: InteractiveEnvironment, *, scope: str
    ) -> None:
        record("environment_close_requested", scope=scope)
        target.close()
        record("environment_closed", scope=scope)

    record("controller_started", controller_revision="interactive-controller-v1")
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
                prompt = transcript
                counters.initial_prompts += 1
            elif strategy == "independent_verified_sampling":
                prompt = _common_prompt(task)
                counters.independent_sample_prompts += 1
            else:
                prompt = transcript
                counters.feedback_followups += 1

            request_context_id = (
                _context_id(task, strategy, replicate_seed, attempt)
                if strategy == "independent_verified_sampling"
                else context_id
            )
            remaining_completion = budget.completion_tokens - counters.completion_tokens
            output_limit = min(budget.max_output_tokens, remaining_completion)
            if output_limit <= 0:
                stop_reason = "logical_completion_token_budget_exhausted"
                break
            request = InteractiveModelRequest(
                prompt=prompt,
                seed=candidate_seed(replicate_seed, attempt),
                context_id=request_context_id,
                max_output_tokens=output_limit,
            )
            record(
                "model_requested",
                attempt=attempt,
                prompt_sha256=_text_digest(prompt),
                candidate_seed=request.seed,
                context_sha256=_text_digest(request_context_id),
                max_output_tokens=output_limit,
            )
            output = model(request)
            counters.attempts += 1
            counters.model_calls += 1
            counters.prompt_tokens += output.prompt_tokens
            counters.completion_tokens += output.completion_tokens
            record(
                "model_completed",
                attempt=attempt,
                response_sha256=_text_digest(output.text),
                prompt_tokens=output.prompt_tokens,
                completion_tokens=output.completion_tokens,
                total_duration_ns=output.total_duration_ns,
            )
            call_context = output.prompt_tokens + output.completion_tokens
            if (
                counters.prompt_tokens > budget.prompt_tokens
                or counters.completion_tokens > budget.completion_tokens
                or output.completion_tokens > output_limit
                or call_context > budget.per_call_context_tokens
            ):
                stop_reason = "logical_token_budget_exhausted"
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
                    transcript = _append_feedback(
                        transcript,
                        output.text,
                        PARSER_RETRY_OBSERVATION,
                        0.0,
                    )
                    if strategy == "engineered_loop":
                        packet = _engineered_packet(
                            attempt=attempt,
                            budget=budget,
                            counters=counters,
                            action=None,
                            execution=None,
                            reward=0.0,
                            best_reward=best.reward if best is not None else 0.0,
                            score_delta=-(best.reward) if best is not None else 0.0,
                            rollback_performed=False,
                            repeated_signature_count=0,
                            restored_state_sha256=None,
                        )
                        transcript += "\n" + packet
                continue

            if (
                counters.environment_actions >= budget.environment_actions
                or counters.evaluator_calls >= budget.evaluator_calls
                or counters.checkpoint_creates >= budget.checkpoint_creates
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
            )
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
            )
            if strategy == "independent_verified_sampling":
                close_environment(current_environment, scope=f"attempt-{attempt}")
                independent_environments.remove(current_environment)
            candidate = _SelectedCheckpoint(
                checkpoint=checkpoint,
                reward=float(evaluation.reward),
                official_success=evaluation.official_success,
                attempt=attempt,
            )
            selected = candidate

            rollback_performed = False
            restored_state_sha256: str | None = None
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
                signature = _progress_signature(action, execution, candidate.reward)
                signature_counts[signature] = signature_counts.get(signature, 0) + 1
                repeated_signature_count = signature_counts[signature]

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
                transcript = _append_feedback(
                    transcript,
                    output.text,
                    execution.observation,
                    float(evaluation.reward),
                )
                if strategy == "engineered_loop":
                    assert best is not None
                    packet = _engineered_packet(
                        attempt=attempt,
                        budget=budget,
                        counters=counters,
                        action=action,
                        execution=execution,
                        reward=float(evaluation.reward),
                        best_reward=best.reward,
                        score_delta=float(evaluation.reward) - prior_best_reward,
                        rollback_performed=rollback_performed,
                        repeated_signature_count=repeated_signature_count,
                        restored_state_sha256=restored_state_sha256,
                    )
                    transcript += "\n" + packet
                    if repeated_signature_count >= 3:
                        selected = best
                        stop_reason = "no_progress_guard"
                        break
        else:
            stop_reason = "attempt_budget_exhausted"
    finally:
        if environment is not None:
            close_environment(environment, scope="episode")
        for index, independent_environment in enumerate(independent_environments, 1):
            close_environment(independent_environment, scope=f"incomplete-attempt-{index}")

    if strategy == "engineered_loop" and best is not None:
        selected = best
    official_success = selected.official_success if selected is not None else False
    if selected is not None:
        record(
            "strict_evaluation_planned",
            selected_attempt=selected.attempt,
            state_sha256=selected.checkpoint.state_sha256,
        )
    record(
        "controller_stopped",
        stop_reason=stop_reason,
        selected_attempt=selected.attempt if selected is not None else None,
        official_success=official_success,
    )

    strict_success = False
    if selected is not None:
        strict = strict_evaluate(selected.checkpoint)
        strict_success = strict.strict_success
        record(
            "strict_evaluation_completed",
            strict_success=strict.strict_success,
            evaluator_sha256=strict.evaluator_sha256,
        )
    seal_journal(event_log)

    run_status = (
        "budget_exhausted" if "budget_exhausted" in stop_reason else "completed"
    )
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
        parser_failures=counters.parser_failures,
        initial_prompts=counters.initial_prompts,
        independent_sample_prompts=counters.independent_sample_prompts,
        feedback_followups=counters.feedback_followups,
    )


def _common_prompt(task: InteractiveTask) -> str:
    return COMMON_PROMPT_TEMPLATE.format(task_id=task.task_id, query=task.query)


def _append_feedback(
    transcript: str,
    model_text: str,
    observation: str,
    reward: float,
) -> str:
    rendered_reward = _render_reward(reward)
    return (
        f"{transcript}\nAssistant response: {model_text}\n"
        f"Output: {observation}\nReward: {rendered_reward}"
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
        "output_sha256": execution.output_sha256 if execution is not None else None,
        "admissible": execution.admissible if execution is not None else False,
        "state_changed": execution.state_changed if execution is not None else False,
        "state_sha256": execution.state_sha256 if execution is not None else None,
        "reward": reward,
        "best_reward": best_reward,
        "score_delta": score_delta,
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
    normalized_action = " ".join(action.split())
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
