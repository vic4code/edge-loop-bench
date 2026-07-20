"""In-memory replay checkpoints for the bounded v0.7 InterCode adapter.

This module contains no Docker, model, filesystem, or network operation.  The
runtime-specific boundary is injected.  Checkpoints expose only digests; the
candidate material and action history remain in an episode-private registry.
"""

from __future__ import annotations

import hashlib
import re
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .interactive_environment import (
    ActionExecution,
    AttemptEvaluation,
    AttemptEvaluator,
    AttemptEvaluationKind,
    EnvironmentCheckpoint,
    StrictEvaluation,
    StrictEvaluator,
    TerminalFinalization,
    TerminalSelection,
)
from .intercode_evaluator import (
    MAX_NORMALIZED_OUTPUT_BYTES,
    CanonicalStateSnapshot,
)
from .intercode_v07_protocol import candidate_progress_evaluation


_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKPOINT_DOMAIN = b"edgeloopbench.v0.7.replay-checkpoint.v1\0"
_OUTPUT_DOMAIN = b"edgeloopbench.v0.7.normalized-streams.v1\0"
_MATERIAL_DOMAIN = b"edgeloopbench.v0.7.candidate-material.v1\0"

V07_STRICT_REPLAY_EVALUATOR_SHA256 = (
    "sha256:e3ce3f3785e1ec6ad5bad87d14632834d5af0307a2143442987d48410787b3d1"
)


class ReplayInfrastructureError(RuntimeError):
    """A replay or private-registry invariant failed without model blame."""


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 reference")
    return value


def _normalized_text_bytes(value: object, field: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{field} must be valid UTF-8 text") from None
    if "\r" in value:
        raise ValueError(f"{field} must use normalized newlines")
    if any(
        character not in {"\n", "\t"}
        and (
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
        )
        for character in value
    ):
        raise ValueError(f"{field} contains unsafe control text")
    return encoded


@dataclass(frozen=True, slots=True, repr=False)
class CandidateMaterial:
    """Private normalized result of one admissible boundary action."""

    state: CanonicalStateSnapshot
    collector_state_sha256: str
    exit_code: int
    normalized_stdout: str
    normalized_stderr: str
    agent_observation: str
    state_changed: bool

    def __post_init__(self) -> None:
        if type(self.state) is not CanonicalStateSnapshot:
            raise ValueError("candidate state must be a CanonicalStateSnapshot")
        _require_sha256(
            self.collector_state_sha256,
            "candidate collector state digest",
        )
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ValueError("candidate exit code must be an integer")
        stdout = _normalized_text_bytes(
            self.normalized_stdout,
            "candidate normalized stdout",
        )
        stderr = _normalized_text_bytes(
            self.normalized_stderr,
            "candidate normalized stderr",
        )
        if len(stdout) + len(stderr) > MAX_NORMALIZED_OUTPUT_BYTES:
            raise ValueError("candidate normalized streams exceed the safety limit")
        observation_bytes = _normalized_text_bytes(
            self.agent_observation,
            "candidate agent observation",
        )
        if len(observation_bytes) > MAX_NORMALIZED_OUTPUT_BYTES:
            raise ValueError("candidate agent observation exceeds the safety limit")
        if type(self.state_changed) is not bool:
            raise ValueError("candidate state_changed must be boolean")

    def __repr__(self) -> str:
        return "<CandidateMaterial redacted>"

    def __reduce__(self) -> object:
        raise TypeError("candidate material cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("candidate material cannot be serialized")


class AttemptBoundary(Protocol):
    """Injected owner of one fresh, bounded attempt environment."""

    def execute(self, action: str) -> CandidateMaterial | ActionExecution: ...

    def close(self) -> None: ...


AttemptBoundaryFactory = Callable[[], AttemptBoundary]


@dataclass(frozen=True, slots=True, repr=False)
class _ReplayStep:
    action: str
    material: CandidateMaterial

    def __repr__(self) -> str:
        return "<_ReplayStep redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class _CheckpointRecord:
    scope: int
    steps: tuple[_ReplayStep, ...]

    @property
    def material(self) -> CandidateMaterial:
        return self.steps[-1].material

    def __repr__(self) -> str:
        return f"<_CheckpointRecord steps={len(self.steps)}>"


class _HashUpdater(Protocol):
    def update(self, payload: bytes, /) -> None: ...


def _update_length_prefixed(hasher: _HashUpdater, payload: bytes) -> None:
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stream_sha256(stdout: str, stderr: str) -> str:
    hasher = hashlib.sha256(_OUTPUT_DOMAIN)
    _update_length_prefixed(hasher, stdout.encode("utf-8"))
    _update_length_prefixed(hasher, stderr.encode("utf-8"))
    return "sha256:" + hasher.hexdigest()


def _material_sha256(material: CandidateMaterial) -> str:
    hasher = hashlib.sha256(_MATERIAL_DOMAIN)
    for value in (
        material.collector_state_sha256,
        str(material.exit_code),
        _text_sha256(material.normalized_stdout),
        _text_sha256(material.normalized_stderr),
        _text_sha256(material.agent_observation),
        "1" if material.state_changed else "0",
    ):
        _update_length_prefixed(hasher, value.encode("ascii"))
    return "sha256:" + hasher.hexdigest()


class EpisodeCheckpointRegistry:
    """Episode-private checkpoint capabilities and replay records.

    The registry stores neither gold material nor host/evaluator paths.  It has
    no serialization surface; trusted evaluator helpers resolve candidates in
    memory by opaque checkpoint capability.
    """

    __slots__ = (
        "_lock",
        "_next_checkpoint",
        "_next_scope",
        "_records",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_checkpoint = 1
        self._next_scope = 1
        self._records: dict[str, _CheckpointRecord] = {}

    def _allocate_scope(self) -> int:
        with self._lock:
            scope = self._next_scope
            self._next_scope += 1
        return scope

    def _register(
        self,
        scope: int,
        steps: tuple[_ReplayStep, ...],
    ) -> EnvironmentCheckpoint:
        if not steps:
            raise ReplayInfrastructureError(
                "cannot register an empty replay checkpoint"
            )
        with self._lock:
            sequence = self._next_checkpoint
            self._next_checkpoint += 1
            hasher = hashlib.sha256(_CHECKPOINT_DOMAIN)
            for value in (str(scope), str(sequence), str(len(steps))):
                _update_length_prefixed(hasher, value.encode("ascii"))
            for step in steps:
                _update_length_prefixed(
                    hasher,
                    _text_sha256(step.action).encode("ascii"),
                )
                _update_length_prefixed(
                    hasher,
                    _material_sha256(step.material).encode("ascii"),
                )
            reference_sha256 = "sha256:" + hasher.hexdigest()
            if reference_sha256 in self._records:  # pragma: no cover - SHA invariant
                raise ReplayInfrastructureError("checkpoint identity collision")
            checkpoint = EnvironmentCheckpoint(
                reference_sha256=reference_sha256,
                state_sha256=steps[-1].material.collector_state_sha256,
            )
            self._records[reference_sha256] = _CheckpointRecord(scope, steps)
            return checkpoint

    def _resolve(self, checkpoint: EnvironmentCheckpoint) -> _CheckpointRecord:
        if type(checkpoint) is not EnvironmentCheckpoint:
            raise ReplayInfrastructureError("checkpoint capability is invalid")
        with self._lock:
            record = self._records.get(checkpoint.reference_sha256)
        if (
            record is None
            or checkpoint.state_sha256 != record.material.collector_state_sha256
        ):
            raise ReplayInfrastructureError("checkpoint capability is unknown")
        return record

    def candidate_material(
        self,
        checkpoint: EnvironmentCheckpoint,
    ) -> CandidateMaterial:
        """Resolve candidate material for a trusted evaluator only."""

        return self._resolve(checkpoint).material

    def __repr__(self) -> str:
        return f"<EpisodeCheckpointRegistry private_entries={len(self._records)}>"

    def __reduce__(self) -> object:
        raise TypeError("episode checkpoint registries cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("episode checkpoint registries cannot be serialized")


class ReplayEnvironment:
    """Interactive environment backed by fresh-boundary deterministic replay."""

    def __init__(
        self,
        registry: EpisodeCheckpointRegistry,
        boundary_factory: AttemptBoundaryFactory,
    ) -> None:
        if type(registry) is not EpisodeCheckpointRegistry:
            raise ValueError("replay environment requires an episode registry")
        if not callable(boundary_factory):
            raise ValueError("replay environment boundary factory must be callable")
        self._registry = registry
        self._boundary_factory = boundary_factory
        self._scope = registry._allocate_scope()
        self._history: tuple[_ReplayStep, ...] = ()
        self._latest_checkpoint: EnvironmentCheckpoint | None = None
        self._checkpoint_available = False
        self._closed = False
        self._failed = False
        try:
            self._boundary: AttemptBoundary | None = boundary_factory()
        except Exception:
            self._failed = True
            raise ReplayInfrastructureError(
                "fresh attempt boundary creation failed"
            ) from None

    def _require_open(self) -> AttemptBoundary:
        if self._closed:
            raise ReplayInfrastructureError("replay environment is closed")
        if self._failed or self._boundary is None:
            raise ReplayInfrastructureError("replay environment is unavailable")
        return self._boundary

    def _retire_boundary(self) -> None:
        boundary = self._boundary
        self._boundary = None
        if boundary is None:
            return
        try:
            boundary.close()
        except Exception:
            self._failed = True
            raise ReplayInfrastructureError("attempt boundary close failed") from None

    def execute(self, action: str) -> ActionExecution:
        boundary = self._require_open()
        if not isinstance(action, str) or not action:
            raise ValueError("replay action must be non-empty text")
        try:
            outcome = boundary.execute(action)
        except Exception:
            self._failed = True
            self._checkpoint_available = False
            raise ReplayInfrastructureError("attempt boundary execution failed") from None

        if type(outcome) is ActionExecution:
            if outcome.admissible or outcome.policy_failure is None:
                self._failed = True
                raise ReplayInfrastructureError(
                    "attempt boundary returned an invalid policy failure"
                )
            if (
                self._history
                and outcome.state_sha256
                != self._history[-1].material.collector_state_sha256
            ):
                self._failed = True
                raise ReplayInfrastructureError(
                    "policy recovery did not preserve the recorded state"
                )
            if (
                outcome.safety_recovery_replayed_environment_actions
                != len(self._history)
            ):
                self._failed = True
                raise ReplayInfrastructureError(
                    "policy recovery replay accounting is invalid"
                )
            self._checkpoint_available = False
            return outcome

        if type(outcome) is not CandidateMaterial:
            self._failed = True
            raise ReplayInfrastructureError(
                "attempt boundary returned an invalid action result"
            )

        step = _ReplayStep(action, outcome)
        self._history = (*self._history, step)
        self._latest_checkpoint = self._registry._register(
            self._scope,
            self._history,
        )
        self._checkpoint_available = True
        return ActionExecution(
            observation=outcome.agent_observation,
            exit_code=outcome.exit_code,
            state_sha256=outcome.collector_state_sha256,
            output_sha256=_stream_sha256(
                outcome.normalized_stdout,
                outcome.normalized_stderr,
            ),
            admissible=True,
            state_changed=outcome.state_changed,
        )

    def checkpoint(self) -> EnvironmentCheckpoint:
        self._require_open()
        if not self._checkpoint_available or self._latest_checkpoint is None:
            raise ReplayInfrastructureError(
                "no admissible action is available for checkpointing"
            )
        return self._latest_checkpoint

    def restore(
        self,
        checkpoint: EnvironmentCheckpoint,
        *,
        action_limit: int,
    ) -> int:
        self._require_open()
        record = self._registry._resolve(checkpoint)
        if record.scope != self._scope:
            raise ReplayInfrastructureError(
                "checkpoint belongs to a different replay environment"
            )
        if (
            isinstance(action_limit, bool)
            or not isinstance(action_limit, int)
            or action_limit < 0
        ):
            raise ValueError("replay action limit must be a non-negative integer")
        replayed_environment_actions = len(record.steps)
        if replayed_environment_actions > action_limit:
            raise ReplayInfrastructureError("checkpoint replay action limit exceeded")

        self._checkpoint_available = False
        self._retire_boundary()
        try:
            boundary = self._boundary_factory()
        except Exception:
            self._failed = True
            raise ReplayInfrastructureError(
                "fresh replay boundary creation failed"
            ) from None
        self._boundary = boundary

        try:
            for step in record.steps:
                replayed = boundary.execute(step.action)
                if (
                    type(replayed) is not CandidateMaterial
                    or not _same_replay_material(replayed, step.material)
                ):
                    raise ReplayInfrastructureError(
                        "checkpoint replay did not reproduce recorded material"
                    )
        except Exception:
            self._failed = True
            try:
                self._retire_boundary()
            except ReplayInfrastructureError:
                pass
            raise ReplayInfrastructureError(
                "checkpoint replay did not reproduce recorded material"
            ) from None

        self._history = record.steps
        self._latest_checkpoint = checkpoint
        self._checkpoint_available = True
        return replayed_environment_actions

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._checkpoint_available = False
        self._retire_boundary()


def _same_replay_material(
    replayed: CandidateMaterial,
    recorded: CandidateMaterial,
) -> bool:
    return bool(
        replayed.state == recorded.state
        and replayed.collector_state_sha256 == recorded.collector_state_sha256
        and replayed.exit_code == recorded.exit_code
        and replayed.normalized_stdout == recorded.normalized_stdout
        and replayed.normalized_stderr == recorded.normalized_stderr
        and replayed.agent_observation == recorded.agent_observation
        and replayed.state_changed == recorded.state_changed
    )


def make_candidate_progress_evaluator(
    registry: EpisodeCheckpointRegistry,
) -> AttemptEvaluator:
    """Bind the frozen, gold-free candidate-progress evaluator to a registry."""

    if type(registry) is not EpisodeCheckpointRegistry:
        raise ValueError("candidate progress evaluator requires an episode registry")

    def evaluate(checkpoint: EnvironmentCheckpoint) -> AttemptEvaluation:
        material = registry.candidate_material(checkpoint)
        return candidate_progress_evaluation(
            parsed_single_action=True,
            action_admissible=True,
            exit_code=material.exit_code,
            state_changed=material.state_changed,
            normalized_output_nonempty=bool(
                material.normalized_stdout or material.normalized_stderr
            ),
        )

    return evaluate


def make_strict_evaluator(
    registry: EpisodeCheckpointRegistry,
    gold: CandidateMaterial,
) -> StrictEvaluator:
    """Bind one injected private gold material to the frozen strict endpoint."""

    if type(registry) is not EpisodeCheckpointRegistry:
        raise ValueError("strict evaluator requires an episode registry")
    if type(gold) is not CandidateMaterial:
        raise ValueError("strict evaluator requires private gold material")

    def evaluate(checkpoint: EnvironmentCheckpoint) -> StrictEvaluation:
        candidate = registry.candidate_material(checkpoint)
        success = bool(
            candidate.state == gold.state
            and candidate.collector_state_sha256 == gold.collector_state_sha256
            and candidate.exit_code == gold.exit_code
            and candidate.normalized_stdout == gold.normalized_stdout
            and candidate.normalized_stderr == gold.normalized_stderr
        )
        return StrictEvaluation(
            strict_success=success,
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
        )

    return evaluate


def finalize_v07_terminal(
    selection: TerminalSelection,
    strict_evaluate: StrictEvaluator | None,
    evaluator_call_limit: int,
) -> TerminalFinalization:
    """Invoke strict exactly once for an authorized evaluator-derived selection."""

    if type(selection) is not TerminalSelection:
        raise ValueError("terminal selection must be typed")
    if (
        isinstance(evaluator_call_limit, bool)
        or not isinstance(evaluator_call_limit, int)
        or evaluator_call_limit < 0
    ):
        raise ValueError("terminal evaluator call limit must be non-negative")
    authorized = bool(
        selection.checkpoint is not None
        and selection.evaluation_kind is AttemptEvaluationKind.EVALUATOR_DERIVED
        and not selection.aborted
    )
    if not authorized:
        return TerminalFinalization(None, 0, 0)
    if strict_evaluate is None or evaluator_call_limit < 1:
        raise ReplayInfrastructureError(
            "authorized terminal selection lacks strict evaluator authority"
        )
    result = strict_evaluate(selection.checkpoint)
    if type(result) is not StrictEvaluation:
        raise ReplayInfrastructureError("strict evaluator returned an invalid result")
    return TerminalFinalization(result, 1, 0)
