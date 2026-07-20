"""Single-process model-residency authority shared by v0.7 phases.

Calibration ends with Phi resident, while the formal campaign begins with
Qwen.  This module retains the actual live residency across that boundary so
the first formal transition unloads Phi instead of pretending the server is
empty.  It performs no work at import time.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .intercode_campaign_ledger import CAMPAIGN_MODELS
from .intercode_host_safety import ExpectedHostResources, HostTelemetryCollector
from .intercode_v07_host_policy import (
    V07HostSafetySession,
    open_v07_preload_stabilized_host_safety_session,
)
from .intercode_v07_interventions import (
    V07InterventionPhase,
    append_operational_action,
    verify_v07_intervention_declaration,
)
from .intercode_v07_manifest import V07ExecutionPins
from .intercode_v07_runtime_factory import (
    V07ManagedResidencyBoundary,
    V07ModelRuntime,
    V07ResidencyReceipt,
    V07RuntimeSession,
    transition_v07_model_residency,
)


V07_MODEL_PHASE_MANAGER_REVISION = (
    "intercode-v0.7-model-phase-manager-v2-model-preload-stabilization"
)

_CONSTRUCTION_SEAL = object()


class V07ModelPhaseError(RuntimeError):
    """The live model-major transition state is stale or inconsistent."""


class V07ModelPhaseManager:
    """Retain actual residency across calibration and formal model blocks."""

    __slots__ = (
        "_active_model",
        "_collector",
        "_execution_pins",
        "_failed",
        "_formal_models",
        "_intervention_journal_path",
        "_lock",
        "_monotonic_ns",
        "_preload_admission_directory",
        "_residency_boundary",
        "_runtime_session",
        "_sleeper",
    )

    def __init__(
        self,
        *,
        runtime_session: V07RuntimeSession,
        execution_pins: V07ExecutionPins,
        collector: HostTelemetryCollector,
        residency_boundary: V07ManagedResidencyBoundary,
        intervention_journal_path: Path,
        preload_admission_directory: Path,
        monotonic_ns: Callable[[], int],
        sleeper: Callable[[float], None],
        _construction_seal: object | None = None,
    ) -> None:
        if _construction_seal is not _CONSTRUCTION_SEAL:
            raise V07ModelPhaseError("model phase managers must be builder-issued")
        self._runtime_session = runtime_session
        self._execution_pins = execution_pins
        self._collector = collector
        self._residency_boundary = residency_boundary
        self._intervention_journal_path = intervention_journal_path
        self._preload_admission_directory = preload_admission_directory
        self._monotonic_ns = monotonic_ns
        self._sleeper = sleeper
        self._active_model: V07ModelRuntime | None = None
        self._formal_models: list[str] = []
        self._failed = False
        self._lock = threading.RLock()

    @property
    def active_model_id(self) -> str | None:
        with self._lock:
            return None if self._active_model is None else self._active_model.model_id

    @property
    def formal_models(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._formal_models)

    def open_calibration_phase(
        self,
        previous: V07ModelRuntime | None,
        target: V07ModelRuntime,
    ) -> V07HostSafetySession:
        """Open one exact calibration model block in Qwen-then-Phi order."""

        with self._lock:
            self._require_usable()
            if self._formal_models:
                raise V07ModelPhaseError("calibration cannot resume after formal start")
            if previous is not self._active_model:
                raise V07ModelPhaseError(
                    "calibration callback previous model differs from live residency"
                )
            expected_index = 0 if previous is None else 1
            if (
                expected_index >= len(CAMPAIGN_MODELS)
                or target is not self._exact_runtime(CAMPAIGN_MODELS[expected_index])
            ):
                raise V07ModelPhaseError("calibration model-major order drifted")
            return self._open_phase(target, V07InterventionPhase.CALIBRATION)

    def open_formal_phase(
        self,
        previous_model_id: str | None,
        target_model_id: str,
    ) -> V07HostSafetySession:
        """Open one formal block while honoring post-calibration residency."""

        with self._lock:
            self._require_usable()
            expected_previous = (
                None if not self._formal_models else self._formal_models[-1]
            )
            if previous_model_id != expected_previous:
                raise V07ModelPhaseError("formal callback previous model drifted")
            position = len(self._formal_models)
            if (
                position >= len(CAMPAIGN_MODELS)
                or target_model_id != CAMPAIGN_MODELS[position]
            ):
                raise V07ModelPhaseError("formal model-major order drifted")
            target = self._exact_runtime(target_model_id)
            session = self._open_phase(
                target,
                V07InterventionPhase.CONFIRMATORY,
            )
            self._formal_models.append(target_model_id)
            return session

    def _open_phase(
        self,
        target: V07ModelRuntime,
        phase: V07InterventionPhase,
    ) -> V07HostSafetySession:
        if target is self._active_model:
            raise V07ModelPhaseError("model-major phase cannot reuse live residency")
        try:
            self._runtime_session.require_live()
            previous = self._active_model
            previous_expected = ExpectedHostResources(
                resident_models=(
                    () if previous is None else (previous.expected_resident_model,)
                ),
            )
            transition_index = CAMPAIGN_MODELS.index(target.model_id) + 1

            def perform_transition() -> V07ResidencyReceipt:
                append_operational_action(
                    self._intervention_journal_path,
                    phase=phase,
                    model_id=target.model_id,
                )
                receipt = transition_v07_model_residency(
                    previous=previous,
                    target=target,
                    boundary=self._residency_boundary,
                )
                if type(receipt) is not V07ResidencyReceipt:
                    raise V07ModelPhaseError(
                        "model residency transition returned invalid authority"
                    )
                receipt.canonical_record()
                if (
                    receipt.previous_model_id
                    != (None if previous is None else previous.model_id)
                    or receipt.target_model_id != target.model_id
                    or receipt.runtime_receipt_sha256
                    != target.runtime_receipt_sha256
                ):
                    raise V07ModelPhaseError(
                        "model residency transition receipt drifted"
                    )
                return receipt

            session = open_v07_preload_stabilized_host_safety_session(
                pins=self._execution_pins.host_safety,
                collector=self._collector,
                previous_expected=previous_expected,
                expected=ExpectedHostResources(
                    resident_models=(target.expected_resident_model,),
                ),
                require_live_before=(
                    self._runtime_session.require_live
                    if previous is None
                    else previous.require_live
                ),
                perform_transition=perform_transition,
                require_live_runtime=target.require_live,
                expected_runtime_receipt_sha256=target.runtime_receipt_sha256,
                journal_path=(
                    self._preload_admission_directory
                    / f"{phase.value}-{transition_index:02d}.jsonl"
                ),
                phase=phase.value,
                transition_index=transition_index,
                monotonic_ns=self._monotonic_ns,
                sleeper=self._sleeper,
            )
            if type(session) is not V07HostSafetySession:
                raise V07ModelPhaseError(
                    "host phase factory returned invalid authority"
                )
            self._active_model = target
            return session
        except (KeyboardInterrupt, SystemExit):
            self._failed = True
            raise
        except V07ModelPhaseError:
            self._failed = True
            raise
        except Exception:
            self._failed = True
            raise V07ModelPhaseError(
                "v0.7 model phase transition failed closed"
            ) from None

    def _exact_runtime(self, model_id: str) -> V07ModelRuntime:
        runtime = self._runtime_session.model_runtime(model_id)
        if type(runtime) is not V07ModelRuntime:
            raise V07ModelPhaseError("runtime session returned invalid model authority")
        return runtime

    def _require_usable(self) -> None:
        if self._failed:
            raise V07ModelPhaseError("model phase manager is terminally invalid")


def build_v07_model_phase_manager(
    *,
    runtime_session: V07RuntimeSession,
    execution_pins: V07ExecutionPins,
    collector: HostTelemetryCollector,
    residency_boundary: V07ManagedResidencyBoundary,
    intervention_journal_path: Path,
    preload_admission_directory: Path,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    sleeper: Callable[[float], None] = time.sleep,
) -> V07ModelPhaseManager:
    """Validate all static authorities before the first residency mutation."""

    try:
        if (
            type(runtime_session) is not V07RuntimeSession
            or type(execution_pins) is not V07ExecutionPins
            or type(collector) is not HostTelemetryCollector
            or type(residency_boundary) is not V07ManagedResidencyBoundary
            or not callable(monotonic_ns)
            or not callable(sleeper)
        ):
            raise ValueError("model phase authority type differs")
        runtime_session.require_live()
        execution_pins.canonical_record()
        if runtime_session.host_identity != execution_pins.host_safety.host_identity:
            raise ValueError("model phase runtime and execution host differ")
        runtime_record = runtime_session.canonical_record()
        managed = runtime_record.get("managed_runtime")
        if (
            not isinstance(managed, dict)
            or residency_boundary.runtime_receipt_sha256
            != managed.get("receipt_sha256")
        ):
            raise ValueError("model phase residency boundary differs from runtime")
        path = _absolute_path(intervention_journal_path)
        preload_directory = _absolute_private_directory(
            preload_admission_directory
        )
        verify_v07_intervention_declaration(path)
        return V07ModelPhaseManager(
            runtime_session=runtime_session,
            execution_pins=execution_pins,
            collector=collector,
            residency_boundary=residency_boundary,
            intervention_journal_path=path,
            preload_admission_directory=preload_directory,
            monotonic_ns=monotonic_ns,
            sleeper=sleeper,
            _construction_seal=_CONSTRUCTION_SEAL,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except V07ModelPhaseError:
        raise
    except Exception:
        raise V07ModelPhaseError(
            "v0.7 model phase manager construction failed closed"
        ) from None


def _absolute_path(value: Path) -> Path:
    if (
        type(value) is not type(Path())
        or not value.is_absolute()
        or Path(os.path.normpath(value)) != value
    ):
        raise ValueError("intervention journal path must be canonical and absolute")
    return value


def _absolute_private_directory(value: Path) -> Path:
    if (
        type(value) is not type(Path())
        or not value.is_absolute()
        or Path(os.path.normpath(value)) != value
    ):
        raise ValueError("preload admission directory must be canonical and absolute")
    metadata = value.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("preload admission directory identity is unsafe")
    return value


__all__ = (
    "V07_MODEL_PHASE_MANAGER_REVISION",
    "V07ModelPhaseError",
    "V07ModelPhaseManager",
    "build_v07_model_phase_manager",
)
