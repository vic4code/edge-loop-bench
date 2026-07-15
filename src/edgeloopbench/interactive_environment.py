"""Typed trust boundary for stateful interactive benchmark environments."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


SHA256_REFERENCE_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _require_sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or not SHA256_REFERENCE_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 reference")


@dataclass(frozen=True)
class EnvironmentCheckpoint:
    """Opaque checkpoint identity plus the state digest it is bound to.

    ``reference_sha256`` identifies adapter-private checkpoint metadata. It is
    deliberately a digest rather than a host path or container identifier.
    """

    reference_sha256: str
    state_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.reference_sha256, "checkpoint.reference_sha256")
        _require_sha256(self.state_sha256, "checkpoint.state_sha256")


class ActionPolicyFailureKind(str, Enum):
    """Frozen model-caused outcomes with no admissible candidate state."""

    TIMEOUT = "timeout"
    OUTPUT_OVERFLOW = "output_overflow"
    INVALID_TEXT = "invalid_text"
    RESIDUAL_PROCESS = "residual_process"
    CONTAINER_TERMINATED = "container_terminated"


ACTION_POLICY_OBSERVATIONS = {
    ActionPolicyFailureKind.TIMEOUT: "Command timed out.",
    ActionPolicyFailureKind.OUTPUT_OVERFLOW: (
        "Command output exceeded the safety limit."
    ),
    ActionPolicyFailureKind.INVALID_TEXT: "Command output violated text policy.",
    ActionPolicyFailureKind.RESIDUAL_PROCESS: (
        "Command left a residual process."
    ),
    ActionPolicyFailureKind.CONTAINER_TERMINATED: (
        "Command terminated the task container."
    ),
}


@dataclass(frozen=True)
class ActionExecution:
    """The bounded, agent-visible portion of one environment action."""

    observation: str
    exit_code: int | None
    state_sha256: str
    output_sha256: str
    admissible: bool
    state_changed: bool
    policy_failure: ActionPolicyFailureKind | None = None
    safety_recovery_performed: bool = False
    safety_recovery_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.observation, str):
            raise ValueError("action observation must be text")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ValueError("action exit_code must be an integer or null")
        _require_sha256(self.state_sha256, "action.state_sha256")
        _require_sha256(self.output_sha256, "action.output_sha256")
        if not isinstance(self.admissible, bool):
            raise ValueError("action admissible must be boolean")
        if not isinstance(self.state_changed, bool):
            raise ValueError("action state_changed must be boolean")
        if self.policy_failure is not None and not isinstance(
            self.policy_failure,
            ActionPolicyFailureKind,
        ):
            raise ValueError("action policy failure must be typed")
        if not isinstance(self.safety_recovery_performed, bool):
            raise ValueError("action safety recovery marker must be boolean")
        if self.safety_recovery_evidence_sha256 is not None:
            _require_sha256(
                self.safety_recovery_evidence_sha256,
                "action.safety_recovery_evidence_sha256",
            )
        if self.admissible:
            if (
                self.exit_code is None
                or self.policy_failure is not None
                or self.safety_recovery_performed
                or self.safety_recovery_evidence_sha256 is not None
            ):
                raise ValueError("admissible action accounting is contradictory")
        elif (
            self.policy_failure is None
            or self.state_changed
            or self.observation != ACTION_POLICY_OBSERVATIONS[self.policy_failure]
            or not self.safety_recovery_performed
            or self.safety_recovery_evidence_sha256 is None
        ):
            raise ValueError(
                "policy failure must expose only its frozen restored-state result"
            )


class AttemptEvaluationKind(str, Enum):
    """Public provenance class without evaluator diagnostics."""

    EVALUATOR_DERIVED = "evaluator_derived"
    CANDIDATE_SURFACE_FAILURE = "candidate_surface_failure"
    ACTION_POLICY_FAILURE = "action_policy_failure"


@dataclass(frozen=True)
class AttemptEvaluation:
    """Public stop signal from the isolated attempt-level evaluator.

    No diagnostic text or evaluator path is represented by this type, making
    it impossible for the controller to accidentally forward either one.
    """

    reward: float
    official_success: bool
    evaluation_kind: AttemptEvaluationKind = AttemptEvaluationKind.EVALUATOR_DERIVED

    def __post_init__(self) -> None:
        if isinstance(self.reward, bool) or not isinstance(self.reward, (int, float)):
            raise ValueError("attempt reward must be numeric")
        if not math.isfinite(float(self.reward)) or not 0.0 <= float(self.reward) <= 1.0:
            raise ValueError("attempt reward must be finite and between 0 and 1")
        if not isinstance(self.official_success, bool):
            raise ValueError("attempt official_success must be boolean")
        if self.official_success != (float(self.reward) == 1.0):
            raise ValueError("official_success must equal reward == 1.0")
        if not isinstance(self.evaluation_kind, AttemptEvaluationKind):
            raise ValueError("attempt evaluation kind must be typed")
        if (
            self.evaluation_kind
            in {
                AttemptEvaluationKind.CANDIDATE_SURFACE_FAILURE,
                AttemptEvaluationKind.ACTION_POLICY_FAILURE,
            }
            and (float(self.reward) != 0.0 or self.official_success)
        ):
            raise ValueError(
                "candidate surface failures must use the frozen zero score"
            )


@dataclass(frozen=True)
class StrictEvaluation:
    """Final selected-checkpoint result with no model-visible diagnostics."""

    strict_success: bool
    evaluator_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.strict_success, bool):
            raise ValueError("strict_success must be boolean")
        _require_sha256(self.evaluator_sha256, "strict.evaluator_sha256")


@dataclass(frozen=True)
class TerminalSelection:
    """Controller-safe terminal provenance passed to the trusted finalizer.

    The finalizer owns the private evaluation session and resource lifetime.
    ``aborted`` distinguishes ordinary checkpoint-free completion from a path
    that must be invalidated and requeued.
    """

    checkpoint: EnvironmentCheckpoint | None
    selected_attempt: int | None
    evaluation_kind: AttemptEvaluationKind | None
    official_success: bool
    aborted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.official_success, bool):
            raise ValueError("terminal official_success must be boolean")
        if not isinstance(self.aborted, bool):
            raise ValueError("terminal aborted must be boolean")
        if self.checkpoint is None:
            if (
                self.selected_attempt is not None
                or self.evaluation_kind is not None
                or self.official_success
            ):
                raise ValueError("checkpoint-free terminal selection is contradictory")
            return
        if type(self.checkpoint) is not EnvironmentCheckpoint:
            raise ValueError("terminal checkpoint capability is invalid")
        if (
            isinstance(self.selected_attempt, bool)
            or not isinstance(self.selected_attempt, int)
            or self.selected_attempt <= 0
        ):
            raise ValueError("terminal selected_attempt must be positive")
        if not isinstance(self.evaluation_kind, AttemptEvaluationKind):
            raise ValueError("terminal evaluation provenance must be typed")


@dataclass(frozen=True)
class TerminalFinalization:
    """Trusted terminal outcome and exact fresh evaluator invocation count."""

    strict_evaluation: StrictEvaluation | None
    strict_evaluator_calls: int
    posthoc_evaluator_calls: int

    def __post_init__(self) -> None:
        if self.strict_evaluation is not None and type(self.strict_evaluation) is not StrictEvaluation:
            raise ValueError("terminal strict evaluation must be typed")
        for field, value in (
            ("strict_evaluator_calls", self.strict_evaluator_calls),
            ("posthoc_evaluator_calls", self.posthoc_evaluator_calls),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"terminal {field} must be a non-negative integer")
        if self.strict_evaluator_calls not in {0, 1}:
            raise ValueError("terminal strict evaluator calls must be zero or one")
        if (self.strict_evaluation is None) != (self.strict_evaluator_calls == 0):
            raise ValueError("terminal strict result and call count are contradictory")

    @property
    def evaluator_calls(self) -> int:
        return self.strict_evaluator_calls + self.posthoc_evaluator_calls


class InteractiveEnvironment(Protocol):
    """Minimal adapter surface used by the controller state machine."""

    def execute(self, action: str) -> ActionExecution: ...

    def checkpoint(self) -> EnvironmentCheckpoint: ...

    def restore(self, checkpoint: EnvironmentCheckpoint) -> None: ...

    def close(self) -> None: ...


EnvironmentFactory = Callable[[], InteractiveEnvironment]
AttemptEvaluator = Callable[[EnvironmentCheckpoint], AttemptEvaluation]
StrictEvaluator = Callable[[EnvironmentCheckpoint], StrictEvaluation]
TerminalFinalizer = Callable[
    [TerminalSelection, StrictEvaluator | None, int],
    TerminalFinalization,
]
