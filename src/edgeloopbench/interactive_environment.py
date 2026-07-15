"""Typed trust boundary for stateful interactive benchmark environments."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ActionExecution:
    """The bounded, agent-visible portion of one environment action."""

    observation: str
    exit_code: int
    state_sha256: str
    output_sha256: str
    admissible: bool
    state_changed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.observation, str):
            raise ValueError("action observation must be text")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ValueError("action exit_code must be an integer")
        _require_sha256(self.state_sha256, "action.state_sha256")
        _require_sha256(self.output_sha256, "action.output_sha256")
        if not isinstance(self.admissible, bool):
            raise ValueError("action admissible must be boolean")
        if not isinstance(self.state_changed, bool):
            raise ValueError("action state_changed must be boolean")


@dataclass(frozen=True)
class AttemptEvaluation:
    """Public stop signal from the isolated attempt-level evaluator.

    No diagnostic text or evaluator path is represented by this type, making
    it impossible for the controller to accidentally forward either one.
    """

    reward: float
    official_success: bool

    def __post_init__(self) -> None:
        if isinstance(self.reward, bool) or not isinstance(self.reward, (int, float)):
            raise ValueError("attempt reward must be numeric")
        if not math.isfinite(float(self.reward)) or not 0.0 <= float(self.reward) <= 1.0:
            raise ValueError("attempt reward must be finite and between 0 and 1")
        if not isinstance(self.official_success, bool):
            raise ValueError("attempt official_success must be boolean")
        if self.official_success != (float(self.reward) == 1.0):
            raise ValueError("official_success must equal reward == 1.0")


@dataclass(frozen=True)
class StrictEvaluation:
    """Final selected-checkpoint result with no model-visible diagnostics."""

    strict_success: bool
    evaluator_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.strict_success, bool):
            raise ValueError("strict_success must be boolean")
        _require_sha256(self.evaluator_sha256, "strict.evaluator_sha256")


class InteractiveEnvironment(Protocol):
    """Minimal adapter surface used by the controller state machine."""

    def execute(self, action: str) -> ActionExecution: ...

    def checkpoint(self) -> EnvironmentCheckpoint: ...

    def restore(self, checkpoint: EnvironmentCheckpoint) -> None: ...

    def close(self) -> None: ...


EnvironmentFactory = Callable[[], InteractiveEnvironment]
AttemptEvaluator = Callable[[EnvironmentCheckpoint], AttemptEvaluation]
StrictEvaluator = Callable[[EnvironmentCheckpoint], StrictEvaluation]

