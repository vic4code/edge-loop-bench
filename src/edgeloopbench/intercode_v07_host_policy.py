"""Executable v0.7 host-safety policy and admission capability.

The pre-calibration manifest stores v0.7-only safety pins.  This module turns
those pins into a pure policy and one factory-issued phase session.  A session
owns the exact production telemetry collector and freezes the model/container
resources expected for the phase.  Every collection is bracketed by the
caller's managed-runtime liveness proof.

The caller must complete and separately account for the operational model
unload/preload transition before opening a phase.  That control request is not
a benchmark-model prompt.  A phase admits exactly one expected resident model;
model switching therefore requires a new phase session.

No Docker, Ollama, network, or host probe runs at import time.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass
from enum import Enum
from pathlib import Path
from typing import TypeVar

from .intercode_host_safety import (
    ExpectedHostResources,
    HostSafetySample,
    HostTelemetryCollector,
    ResidentModel,
    parse_host_safety_sample,
)
from .intercode_v07_manifest import V07HostSafetyPins
from .intercode_v07_runtime_factory import V07ResidencyReceipt
from .journal import JournalError, append_journal_event, inspect_journal, seal_journal
from .model_adapter import PHI4_MINI_RAW_PROFILE, QWEN35_RAW_PROFILE


V07_HOST_POLICY_REVISION = (
    "intercode-v0.7-host-safety-policy-v2-model-preload-stabilization"
)
V07_MODEL_PRELOAD_ADMISSION_JOURNAL_REVISION = (
    "intercode-v0.7-model-preload-admission-journal-v1"
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_JOURNAL_INSTANCE_ID = re.compile(r"[0-9a-f]{32}\Z")
_SESSION_AUTHORITY = object()
_HOOK_AUTHORITY = object()
_EVIDENCE_AUTHORITY = object()
_ALLOWED_PHASE_MODEL_SETS = frozenset(
    (
        (
            ResidentModel(
                profile.model,
                profile.model_manifest_sha256.removeprefix("sha256:"),
            ),
        )
        for profile in (QWEN35_RAW_PROFILE, PHI4_MINI_RAW_PROFILE)
    )
)
_ALLOWED_TRANSITION_RESOURCE_SETS = _ALLOWED_PHASE_MODEL_SETS | frozenset(((),))
_PRELOAD_PHASES = frozenset(("calibration", "confirmatory"))
_JOURNAL_CHAIN_FIELDS = frozenset(
    ("sequence", "previous_event_sha256", "event_sha256")
)
_MAX_PRELOAD_JOURNAL_BYTES = 8 << 20


class V07HostSafetyError(RuntimeError):
    """The v0.7 host admission capability could not prove safe execution."""


class V07HostSafetyAction(str, Enum):
    CONTINUE = "continue"
    STOP = "stop"
    RECOVER = "recover"


class V07HostSafetyReason(str, Enum):
    AC_POWER_REQUIRED = "ac_power_required"
    LOW_POWER_MODE_ENABLED = "low_power_mode_enabled"
    VM_PRESSURE = "vm_pressure"
    FREE_MEMORY = "free_memory"
    DISK_SPACE = "disk_space"
    THERMAL_WARNING = "thermal_warning"
    PERFORMANCE_WARNING = "performance_warning"
    RESIDENT_MODELS = "resident_models"
    RUNNING_CONTAINERS = "running_containers"
    PHASE_SWAP_GROWTH = "phase_swap_growth"
    EPISODE_SWAP_GROWTH = "episode_swap_growth"
    SAMPLE_ORDER = "sample_order"
    SAMPLE_INTERVAL = "sample_interval"
    COOLDOWN_TIMEOUT = "cooldown_timeout"
    COOLDOWN_SWAP_GROWTH = "cooldown_swap_growth"
    BOOT_IDENTITY = "boot_identity"
    DOCKER_IDENTITY = "docker_identity"


@dataclass(frozen=True, slots=True)
class V07HostSafetyDecision:
    action: V07HostSafetyAction
    reasons: tuple[V07HostSafetyReason, ...]

    def __post_init__(self) -> None:
        if type(self.action) is not V07HostSafetyAction:
            raise ValueError("v0.7 host-safety action is invalid")
        if (
            type(self.reasons) is not tuple
            or any(type(item) is not V07HostSafetyReason for item in self.reasons)
            or len(set(self.reasons)) != len(self.reasons)
        ):
            raise ValueError("v0.7 host-safety reasons are invalid")
        if (self.action is V07HostSafetyAction.CONTINUE) != (not self.reasons):
            raise ValueError("v0.7 host-safety decision is inconsistent")

    @property
    def allowed(self) -> bool:
        return self.action is V07HostSafetyAction.CONTINUE


class V07HostSafetyDenied(V07HostSafetyError):
    """One admitted phase or episode sample failed the frozen policy."""

    def __init__(
        self,
        stage: str,
        decision: V07HostSafetyDecision,
    ) -> None:
        if stage not in {"phase", "before_episode", "after_episode", "cooldown"}:
            raise ValueError("v0.7 host-safety denial stage is invalid")
        if type(decision) is not V07HostSafetyDecision or decision.allowed:
            raise ValueError("v0.7 host-safety denial decision is invalid")
        self.stage = stage
        self.decision = decision
        reason = ",".join(item.value for item in decision.reasons)
        super().__init__(f"v0.7 host safety denied {stage}: {reason}")


@dataclass(frozen=True, slots=True)
class _PrivatePreloadJournalIdentity:
    parent_device: int
    parent_inode: int
    file_device: int
    file_inode: int


def _declare_preload_journal(
    path: Path,
    *,
    phase: str,
    transition_index: int,
) -> _PrivatePreloadJournalIdentity:
    if (
        type(path) is not type(Path())
        or not path.is_absolute()
        or Path(os.path.normpath(path)) != path
        or phase not in _PRELOAD_PHASES
        or type(transition_index) is not int
        or transition_index not in (1, 2)
        or path.name != f"{phase}-{transition_index:02d}.jsonl"
    ):
        raise V07HostSafetyError("v0.7 preload journal path is invalid")
    parent_descriptor = -1
    descriptor = -1
    identity: _PrivatePreloadJournalIdentity | None = None
    parent_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    parent_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    file_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_descriptor = os.open(path.parent, parent_flags)
        parent = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise OSError("unsafe preload journal parent")
        descriptor = os.open(
            path.name,
            file_flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != 0
        ):
            raise OSError("unsafe preload journal")
        identity = _PrivatePreloadJournalIdentity(
            parent_device=parent.st_dev,
            parent_inode=parent.st_ino,
            file_device=metadata.st_dev,
            file_inode=metadata.st_ino,
        )
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
    except OSError:
        raise V07HostSafetyError(
            "v0.7 preload admission journal could not be declared"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    assert identity is not None
    _validate_preload_journal(path, identity)
    return identity


def _validate_preload_journal(
    path: Path,
    identity: _PrivatePreloadJournalIdentity,
) -> None:
    parent_descriptor = -1
    descriptor = -1
    parent_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    parent_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        parent_descriptor = os.open(path.parent, parent_flags)
        parent = os.fstat(parent_descriptor)
        descriptor = os.open(path.name, file_flags, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or (parent.st_dev, parent.st_ino)
            != (identity.parent_device, identity.parent_inode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (metadata.st_dev, metadata.st_ino)
            != (identity.file_device, identity.file_inode)
        ):
            raise OSError("preload journal identity changed")
    except OSError:
        raise V07HostSafetyError(
            "v0.7 preload admission journal identity changed"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _append_preload_event(
    path: Path,
    event: Mapping[str, object],
    identity: _PrivatePreloadJournalIdentity,
) -> None:
    try:
        _validate_preload_journal(path, identity)
        append_journal_event(path, event)
        _validate_preload_journal(path, identity)
    except (JournalError, OSError, ValueError):
        raise V07HostSafetyError(
            "v0.7 preload admission journal append failed"
        ) from None


def _seal_preload_journal(
    path: Path,
    identity: _PrivatePreloadJournalIdentity,
) -> None:
    try:
        _validate_preload_journal(path, identity)
        seal_journal(path)
        _validate_preload_journal(path, identity)
        inspect_journal(path, require_sealed=True)
    except (JournalError, OSError, ValueError):
        raise V07HostSafetyError(
            "v0.7 preload admission journal seal failed"
        ) from None


def _read_preload_records(
    path: Path,
    identity: _PrivatePreloadJournalIdentity,
) -> list[dict[str, object]]:
    _validate_preload_journal(path, identity)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        metadata = os.fstat(descriptor)
        if (
            (metadata.st_dev, metadata.st_ino)
            != (identity.file_device, identity.file_inode)
            or metadata.st_size > _MAX_PRELOAD_JOURNAL_BYTES
        ):
            raise OSError("preload journal identity or size changed")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_PRELOAD_JOURNAL_BYTES:
                raise OSError("preload journal exceeds read bound")
            chunks.append(chunk)
        payload = b"".join(chunks)
        inspection = inspect_journal(path, require_sealed=True)
        final = os.fstat(descriptor)
        if (
            final.st_size != metadata.st_size
            or final.st_mtime_ns != metadata.st_mtime_ns
            or final.st_ctime_ns != metadata.st_ctime_ns
            or inspection.file_byte_length != len(payload)
            or inspection.record_count != len(payload.splitlines())
        ):
            raise OSError("preload journal changed during verification")
        records = [json.loads(line) for line in payload.splitlines()]
        if any(not isinstance(record, dict) for record in records):
            raise ValueError("preload record is not an object")
    except (JournalError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise V07HostSafetyError(
            "v0.7 preload admission journal could not be verified"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _validate_preload_journal(path, identity)
    return records


def _preload_domain_record(record: Mapping[str, object]) -> dict[str, object]:
    if not _JOURNAL_CHAIN_FIELDS.issubset(record):
        raise V07HostSafetyError("v0.7 preload journal framing is invalid")
    return {
        key: value
        for key, value in record.items()
        if key not in _JOURNAL_CHAIN_FIELDS
    }


class V07HostSafetyPolicy:
    """Pure evaluator for the exact manifest-sealed v0.7 threshold subset."""

    def __init__(self, pins: V07HostSafetyPins) -> None:
        if type(pins) is not V07HostSafetyPins:
            raise ValueError("v0.7 host policy requires exact v0.7 pins")
        try:
            pins.canonical_record()
        except (TypeError, ValueError) as error:
            raise ValueError("v0.7 host policy pins are invalid") from error
        self._pins = pins

    @property
    def pins(self) -> V07HostSafetyPins:
        return self._pins

    def evaluate_admission(
        self,
        sample: HostSafetySample,
        expected: ExpectedHostResources,
    ) -> V07HostSafetyDecision:
        _require_inputs(sample, expected)
        reasons: list[V07HostSafetyReason] = []
        self._append_common_reasons(
            sample,
            expected,
            free_memory_minimum=self._pins.admission_free_percent_minimum,
            disk_free_minimum=self._pins.admission_disk_free_bytes_minimum,
            reasons=reasons,
        )
        return _decision(reasons)

    def evaluate_running(
        self,
        sample: HostSafetySample,
        *,
        phase_baseline: HostSafetySample,
        episode_baseline: HostSafetySample,
        expected: ExpectedHostResources,
    ) -> V07HostSafetyDecision:
        _require_inputs(sample, expected)
        _require_sample(phase_baseline, "phase baseline")
        _require_sample(episode_baseline, "episode baseline")
        boot = phase_baseline.boot_time_unix_microseconds
        if (
            episode_baseline.boot_time_unix_microseconds != boot
            or sample.boot_time_unix_microseconds != boot
        ):
            return V07HostSafetyDecision(
                V07HostSafetyAction.RECOVER,
                (V07HostSafetyReason.BOOT_IDENTITY,),
            )

        reasons: list[V07HostSafetyReason] = []
        if not (
            phase_baseline.captured_monotonic_ns
            <= episode_baseline.captured_monotonic_ns
            <= sample.captured_monotonic_ns
        ):
            reasons.append(V07HostSafetyReason.SAMPLE_ORDER)
        self._append_common_reasons(
            sample,
            expected,
            free_memory_minimum=self._pins.abort_free_percent_below,
            disk_free_minimum=self._pins.abort_disk_free_bytes_below,
            reasons=reasons,
        )
        for baseline in (phase_baseline, episode_baseline):
            self._append_daemon_reason(baseline, reasons)
            _append_resource_reasons(baseline, expected, reasons)
        if (
            sample.swap_used_bytes - phase_baseline.swap_used_bytes
            > self._pins.max_phase_swap_growth_bytes
        ):
            _append_once(reasons, V07HostSafetyReason.PHASE_SWAP_GROWTH)
        if (
            sample.swap_used_bytes - episode_baseline.swap_used_bytes
            > self._pins.max_episode_swap_growth_bytes
        ):
            _append_once(reasons, V07HostSafetyReason.EPISODE_SWAP_GROWTH)
        return _decision(reasons)

    def evaluate_cooldown_pair(
        self,
        first: HostSafetySample,
        second: HostSafetySample,
        *,
        cooldown_started_monotonic_ns: int,
        admission_boot_time_unix_microseconds: int,
        expected: ExpectedHostResources,
    ) -> V07HostSafetyDecision:
        """Require the exact two-sample v0.7 stabilization gate.

        Passing this gate is only recovery evidence.  It does not requeue an
        episode or reactivate a failed phase session.
        """

        _require_inputs(first, expected)
        _require_inputs(second, expected)
        if (
            type(cooldown_started_monotonic_ns) is not int
            or cooldown_started_monotonic_ns < 0
            or type(admission_boot_time_unix_microseconds) is not int
            or admission_boot_time_unix_microseconds < 0
        ):
            raise ValueError("v0.7 cooldown clocks must be non-negative integers")
        if self._pins.cooldown_consecutive_samples != 2:
            raise ValueError("v0.7 cooldown requires exactly two samples")
        if (
            first.boot_time_unix_microseconds
            != admission_boot_time_unix_microseconds
            or second.boot_time_unix_microseconds
            != admission_boot_time_unix_microseconds
        ):
            return V07HostSafetyDecision(
                V07HostSafetyAction.RECOVER,
                (V07HostSafetyReason.BOOT_IDENTITY,),
            )

        reasons: list[V07HostSafetyReason] = []
        if not (
            cooldown_started_monotonic_ns
            <= first.captured_monotonic_ns
            <= second.captured_monotonic_ns
        ):
            reasons.append(V07HostSafetyReason.SAMPLE_ORDER)
        minimum_interval_ns = self._pins.sample_interval_seconds * 1_000_000_000
        if (
            second.captured_monotonic_ns - first.captured_monotonic_ns
            < minimum_interval_ns
        ):
            reasons.append(V07HostSafetyReason.SAMPLE_INTERVAL)
        timeout_ns = self._pins.cooldown_timeout_seconds * 1_000_000_000
        if second.captured_monotonic_ns - cooldown_started_monotonic_ns > timeout_ns:
            reasons.append(V07HostSafetyReason.COOLDOWN_TIMEOUT)
        for sample in (first, second):
            if sample.vm_pressure_level != self._pins.required_vm_pressure_level:
                _append_once(reasons, V07HostSafetyReason.VM_PRESSURE)
            if sample.free_memory_percent < self._pins.cooldown_free_percent_minimum:
                _append_once(reasons, V07HostSafetyReason.FREE_MEMORY)
            if self._pins.require_no_thermal_warnings and sample.thermal_warning:
                _append_once(reasons, V07HostSafetyReason.THERMAL_WARNING)
            if (
                self._pins.require_no_performance_warnings
                and sample.performance_warning
            ):
                _append_once(reasons, V07HostSafetyReason.PERFORMANCE_WARNING)
            self._append_daemon_reason(sample, reasons)
            _append_resource_reasons(sample, expected, reasons)
        if (
            second.swap_used_bytes - first.swap_used_bytes
            > self._pins.cooldown_max_swap_growth_bytes
        ):
            reasons.append(V07HostSafetyReason.COOLDOWN_SWAP_GROWTH)
        return _decision(reasons)

    def _append_common_reasons(
        self,
        sample: HostSafetySample,
        expected: ExpectedHostResources,
        *,
        free_memory_minimum: int,
        disk_free_minimum: int,
        reasons: list[V07HostSafetyReason],
    ) -> None:
        if self._pins.require_ac_power and not sample.on_ac_power:
            reasons.append(V07HostSafetyReason.AC_POWER_REQUIRED)
        if (
            self._pins.require_low_power_mode_off
            and sample.low_power_mode_enabled
        ):
            reasons.append(V07HostSafetyReason.LOW_POWER_MODE_ENABLED)
        if sample.vm_pressure_level != self._pins.required_vm_pressure_level:
            reasons.append(V07HostSafetyReason.VM_PRESSURE)
        if sample.free_memory_percent < free_memory_minimum:
            reasons.append(V07HostSafetyReason.FREE_MEMORY)
        if sample.disk_free_bytes < disk_free_minimum:
            reasons.append(V07HostSafetyReason.DISK_SPACE)
        if self._pins.require_no_thermal_warnings and sample.thermal_warning:
            reasons.append(V07HostSafetyReason.THERMAL_WARNING)
        if self._pins.require_no_performance_warnings and sample.performance_warning:
            reasons.append(V07HostSafetyReason.PERFORMANCE_WARNING)
        self._append_daemon_reason(sample, reasons)
        _append_resource_reasons(sample, expected, reasons)

    def _append_daemon_reason(
        self,
        sample: HostSafetySample,
        reasons: list[V07HostSafetyReason],
    ) -> None:
        expected = self._pins.host_identity
        daemon = sample.docker_daemon
        if daemon is None or (
            daemon.binary_sha256,
            daemon.endpoint_sha256,
            daemon.client_version,
            daemon.server_version,
        ) != (
            expected.docker_binary_sha256,
            expected.docker_endpoint_sha256,
            expected.docker_client_version,
            expected.docker_server_version,
        ):
            _append_once(reasons, V07HostSafetyReason.DOCKER_IDENTITY)


@dataclass(frozen=True, slots=True)
class V07EpisodeHostEvidence:
    phase_baseline_sha256: str
    before: HostSafetySample
    after: HostSafetySample
    _authority: InitVar[object | None] = None

    def __post_init__(self, _authority: object | None) -> None:
        if _authority is not _EVIDENCE_AUTHORITY:
            raise V07HostSafetyError("v0.7 host evidence must be admission-issued")
        if (
            type(self.phase_baseline_sha256) is not str
            or _SHA256.fullmatch(self.phase_baseline_sha256) is None
        ):
            raise V07HostSafetyError("v0.7 phase baseline root is invalid")
        _require_sample(self.before, "before evidence")
        _require_sample(self.after, "after evidence")
        if (
            self.before.boot_time_unix_microseconds
            != self.after.boot_time_unix_microseconds
            or self.before.captured_monotonic_ns > self.after.captured_monotonic_ns
        ):
            raise V07HostSafetyError("v0.7 host evidence order is invalid")


@dataclass(frozen=True, slots=True)
class V07CooldownEvidence:
    phase_baseline_sha256: str
    cooldown_started_monotonic_ns: int
    first: HostSafetySample
    second: HostSafetySample
    _authority: InitVar[object | None] = None

    def __post_init__(self, _authority: object | None) -> None:
        if _authority is not _EVIDENCE_AUTHORITY:
            raise V07HostSafetyError("v0.7 cooldown evidence must be session-issued")
        if (
            type(self.phase_baseline_sha256) is not str
            or _SHA256.fullmatch(self.phase_baseline_sha256) is None
            or type(self.cooldown_started_monotonic_ns) is not int
            or self.cooldown_started_monotonic_ns < 0
        ):
            raise V07HostSafetyError("v0.7 cooldown evidence identity is invalid")
        _require_sample(self.first, "first cooldown evidence")
        _require_sample(self.second, "second cooldown evidence")
        if (
            self.first.boot_time_unix_microseconds
            != self.second.boot_time_unix_microseconds
            or not (
                self.cooldown_started_monotonic_ns
                <= self.first.captured_monotonic_ns
                <= self.second.captured_monotonic_ns
            )
        ):
            raise V07HostSafetyError("v0.7 cooldown evidence order is invalid")


RequireLiveRuntime = Callable[[], object]
T = TypeVar("T")


class V07HostSafetySession:
    """One factory-issued, single-boot host-safety phase capability."""

    __slots__ = (
        "_active",
        "_collector",
        "_cooldown_attempted",
        "_cooldown_evidence",
        "_cooldown_started_monotonic_ns",
        "_expected",
        "_failed",
        "_lock",
        "_phase_baseline",
        "_policy",
        "_require_live_runtime",
        "__weakref__",
    )

    def __init__(
        self,
        *,
        policy: V07HostSafetyPolicy,
        collector: HostTelemetryCollector,
        expected: ExpectedHostResources,
        require_live_runtime: RequireLiveRuntime,
        _authority: object,
        _initial_baseline: HostSafetySample | None = None,
    ) -> None:
        if _authority is not _SESSION_AUTHORITY:
            raise V07HostSafetyError("v0.7 host sessions must be factory-issued")
        self._policy = policy
        self._collector = collector
        self._expected = expected
        self._require_live_runtime = require_live_runtime
        if _initial_baseline is not None:
            _require_sample(_initial_baseline, "verified preload baseline")
            initial_decision = policy.evaluate_admission(
                _initial_baseline,
                expected,
            )
            if not initial_decision.allowed:
                raise V07HostSafetyDenied("phase", initial_decision)
        self._phase_baseline = _initial_baseline
        self._active: V07EpisodeHostAdmission | None = None
        self._failed = False
        self._cooldown_attempted = False
        self._cooldown_evidence: V07CooldownEvidence | None = None
        self._cooldown_started_monotonic_ns: int | None = None
        self._lock = threading.RLock()

    @property
    def phase_baseline(self) -> HostSafetySample:
        _require_issued_session(self)
        assert self._phase_baseline is not None
        return self._phase_baseline

    @property
    def expected_resources(self) -> ExpectedHostResources:
        _require_issued_session(self)
        return self._expected

    @property
    def policy_pins(self) -> V07HostSafetyPins:
        """Return the exact immutable pins that issued this phase session."""

        _require_issued_session(self)
        return self._policy.pins

    def issue_episode_admission(self) -> V07EpisodeHostAdmission:
        _require_issued_session(self)
        with self._lock:
            if self._failed:
                raise V07HostSafetyError("v0.7 host-safety session is invalid")
            if self._active is not None:
                raise V07HostSafetyError(
                    "v0.7 host-safety session already has an active episode"
                )
            admission = V07EpisodeHostAdmission(
                session=self,
                _authority=_HOOK_AUTHORITY,
            )
            _ISSUED_HOOKS.add(admission)
            self._active = admission
            return admission

    def collect_cooldown_evidence(
        self,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> V07CooldownEvidence:
        """Collect one terminal two-sample recovery proof after a denial.

        The failed phase remains terminal even when this returns successfully;
        production must journal the recovery and open a newly admitted phase.
        """

        _require_issued_session(self)
        if not callable(sleeper):
            raise V07HostSafetyError("v0.7 cooldown sleeper is invalid")
        with self._lock:
            if (
                not self._failed
                or self._active is not None
                or self._cooldown_started_monotonic_ns is None
            ):
                raise V07HostSafetyError(
                    "v0.7 cooldown requires one sample-backed terminal denial"
                )
            if self._cooldown_attempted:
                raise V07HostSafetyError("v0.7 cooldown was already attempted")
            self._cooldown_attempted = True
            first = self._collect_runtime_bound_sample()
            try:
                sleeper(float(self._policy.pins.sample_interval_seconds))
            except Exception as error:
                raise V07HostSafetyError("v0.7 cooldown wait failed") from error
            second = self._collect_runtime_bound_sample()
            decision = self._policy.evaluate_cooldown_pair(
                first,
                second,
                cooldown_started_monotonic_ns=(
                    self._cooldown_started_monotonic_ns
                ),
                admission_boot_time_unix_microseconds=(
                    self.phase_baseline.boot_time_unix_microseconds
                ),
                expected=self._expected,
            )
            if not decision.allowed:
                raise V07HostSafetyDenied("cooldown", decision)
            evidence = V07CooldownEvidence(
                phase_baseline_sha256=self.phase_baseline.sha256,
                cooldown_started_monotonic_ns=(
                    self._cooldown_started_monotonic_ns
                ),
                first=first,
                second=second,
                _authority=_EVIDENCE_AUTHORITY,
            )
            self._cooldown_evidence = evidence
            return evidence

    def _start(self) -> None:
        if self._phase_baseline is not None:
            raise V07HostSafetyError("v0.7 host session baseline is already set")
        sample = self._collect_runtime_bound_sample()
        decision = self._policy.evaluate_admission(sample, self._expected)
        if not decision.allowed:
            self._failed = True
            raise V07HostSafetyDenied("phase", decision)
        self._phase_baseline = sample

    def _collect_runtime_bound_sample(self) -> HostSafetySample:
        self._require_runtime_live()
        try:
            sample = self._collector.collect()
        except Exception as error:
            raise V07HostSafetyError("v0.7 host telemetry collection failed") from error
        self._require_runtime_live()
        _require_sample(sample, "collected sample")
        return sample

    def _require_runtime_live(self) -> None:
        try:
            self._require_live_runtime()
        except Exception as error:
            raise V07HostSafetyError(
                "v0.7 runtime liveness proof failed"
            ) from error

    def _require_active(self, admission: V07EpisodeHostAdmission) -> None:
        _require_issued_session(self)
        if self._failed or self._active is not admission:
            raise V07HostSafetyError("v0.7 episode admission capability is inactive")

    def _complete(self, admission: V07EpisodeHostAdmission) -> None:
        with self._lock:
            self._require_active(admission)
            self._active = None

    def _release_unstarted(self, admission: V07EpisodeHostAdmission) -> None:
        with self._lock:
            self._require_active(admission)
            self._active = None

    def _invalidate(
        self,
        admission: V07EpisodeHostAdmission,
        observed: HostSafetySample | None,
    ) -> None:
        with self._lock:
            if self._active is admission:
                self._active = None
            self._failed = True
            if observed is not None:
                self._cooldown_started_monotonic_ns = observed.captured_monotonic_ns


class V07EpisodeHostAdmission:
    """Before/after hooks for exactly one episode in an admitted phase."""

    __slots__ = (
        "_after",
        "_before",
        "_evidence",
        "_lock",
        "_session",
        "_state",
        "__weakref__",
    )

    def __init__(
        self,
        *,
        session: V07HostSafetySession,
        _authority: object,
    ) -> None:
        if _authority is not _HOOK_AUTHORITY:
            raise V07HostSafetyError("v0.7 episode hooks must be session-issued")
        self._session = session
        self._before: HostSafetySample | None = None
        self._after: HostSafetySample | None = None
        self._evidence: V07EpisodeHostEvidence | None = None
        self._state = "issued"
        self._lock = threading.RLock()

    @property
    def evidence(self) -> V07EpisodeHostEvidence:
        _require_issued_hook(self)
        if self._state != "complete" or self._evidence is None:
            raise V07HostSafetyError("v0.7 episode host evidence is not complete")
        return self._evidence

    def abort(self) -> None:
        """Close a composition failure without leaving an active capability.

        An admission that never sampled the host may be released for another
        episode.  Once the before sample exists, abandoning the matching after
        sample terminally invalidates the phase instead.
        """

        _require_issued_hook(self)
        with self._lock:
            if self._state == "issued":
                self._session._release_unstarted(self)
                self._state = "aborted"
                return
            if self._state == "before":
                self._state = "failed"
                self._session._invalidate(self, self._before)
                return
            if self._state in {"aborted", "complete", "failed"}:
                return
            raise V07HostSafetyError("v0.7 episode admission state is invalid")

    def before_episode_admission(self) -> HostSafetySample:
        _require_issued_hook(self)
        with self._lock:
            if self._state != "issued":
                self._state = "failed"
                self._session._invalidate(self, None)
                raise V07HostSafetyError(
                    "v0.7 before-episode admission is out of order"
                )
            self._session._require_active(self)
            try:
                sample = self._session._collect_runtime_bound_sample()
                admission = self._session._policy.evaluate_admission(
                    sample,
                    self._session._expected,
                )
                if not admission.allowed:
                    raise V07HostSafetyDenied("before_episode", admission)
                running = self._session._policy.evaluate_running(
                    sample,
                    phase_baseline=self._session.phase_baseline,
                    episode_baseline=sample,
                    expected=self._session._expected,
                )
                if not running.allowed:
                    raise V07HostSafetyDenied("before_episode", running)
            except BaseException:
                self._state = "failed"
                self._session._invalidate(
                    self,
                    sample if "sample" in locals() else None,
                )
                raise
            self._before = sample
            self._state = "before"
            return sample

    def after_episode_admission(self) -> HostSafetySample:
        _require_issued_hook(self)
        with self._lock:
            if self._state != "before" or self._before is None:
                self._state = "failed"
                self._session._invalidate(self, None)
                raise V07HostSafetyError(
                    "v0.7 after-episode admission requires before evidence"
                )
            self._session._require_active(self)
            try:
                sample = self._session._collect_runtime_bound_sample()
                decision = self._session._policy.evaluate_running(
                    sample,
                    phase_baseline=self._session.phase_baseline,
                    episode_baseline=self._before,
                    expected=self._session._expected,
                )
                if not decision.allowed:
                    raise V07HostSafetyDenied("after_episode", decision)
                evidence = V07EpisodeHostEvidence(
                    phase_baseline_sha256=self._session.phase_baseline.sha256,
                    before=self._before,
                    after=sample,
                    _authority=_EVIDENCE_AUTHORITY,
                )
            except BaseException:
                self._after = sample if "sample" in locals() else None
                self._state = "failed"
                self._session._invalidate(self, self._after)
                raise
            self._after = sample
            self._evidence = evidence
            self._state = "complete"
            self._session._complete(self)
            return sample

    def execute(self, work: Callable[[], T]) -> T:
        """Run caller work only after admission and always attempt post evidence."""

        if not callable(work):
            raise V07HostSafetyError("v0.7 admitted work must be callable")
        self.before_episode_admission()
        result: T | None = None
        work_error: BaseException | None = None
        after_error: BaseException | None = None
        try:
            result = work()
        except BaseException as error:
            work_error = error
        try:
            self.after_episode_admission()
        except BaseException as error:
            after_error = error
        if work_error is not None:
            raise work_error.with_traceback(work_error.__traceback__)
        if after_error is not None:
            raise after_error.with_traceback(after_error.__traceback__)
        return result  # type: ignore[return-value]


_ISSUED_SESSIONS: weakref.WeakSet[V07HostSafetySession] = weakref.WeakSet()
_ISSUED_HOOKS: weakref.WeakSet[V07EpisodeHostAdmission] = weakref.WeakSet()


def _resource_record(expected: ExpectedHostResources) -> dict[str, object]:
    return {
        "resident_models": [model.to_record() for model in expected.resident_models],
        "running_container_ids": list(expected.running_container_ids),
    }


def _validate_preload_transition_record(
    value: object,
    *,
    previous_expected: ExpectedHostResources,
    expected: ExpectedHostResources,
    expected_runtime_receipt_sha256: str,
) -> dict[str, object]:
    if type(value) is not V07ResidencyReceipt:
        raise V07HostSafetyError("v0.7 residency transition receipt is invalid")
    try:
        record = value.canonical_record()
    except Exception as error:
        raise V07HostSafetyError(
            "v0.7 residency transition receipt is invalid"
        ) from error
    if (
        type(expected_runtime_receipt_sha256) is not str
        or _SHA256.fullmatch(expected_runtime_receipt_sha256) is None
    ):
        raise V07HostSafetyError("v0.7 residency transition receipt is invalid")
    previous_model = (
        None
        if not previous_expected.resident_models
        else previous_expected.resident_models[0]
    )
    target_model = expected.resident_models[0]
    profile_by_model = {
        profile.model: profile
        for profile in (QWEN35_RAW_PROFILE, PHI4_MINI_RAW_PROFILE)
    }
    previous_profile = (
        None if previous_model is None else profile_by_model[previous_model.model]
    )
    target_profile = profile_by_model[target_model.model]
    previous = record.get("previous")
    target = record.get("target")
    expected_previous = (
        None
        if previous_profile is None
        else {
            "model_artifact_sha256": previous_profile.model_artifact_sha256,
            "model_id": previous_profile.model,
            "model_manifest_sha256": previous_profile.model_manifest_sha256,
        }
    )
    expected_target = {
        "model_artifact_sha256": target_profile.model_artifact_sha256,
        "model_id": target_profile.model,
        "model_manifest_sha256": target_profile.model_manifest_sha256,
    }
    if (
        previous != expected_previous
        or target != expected_target
        or record.get("schema")
        != "edgeloopbench.v07-model-residency-transition.v1"
        or record.get("runtime_receipt_sha256")
        != expected_runtime_receipt_sha256
        or _SHA256.fullmatch(str(record.get("transition_sha256"))) is None
    ):
        raise V07HostSafetyError("v0.7 residency transition receipt is invalid")
    try:
        json.dumps(record, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise V07HostSafetyError(
            "v0.7 residency transition receipt is invalid"
        ) from None
    return record


def _collect_preload_sample(
    collector: HostTelemetryCollector,
    require_live_runtime: RequireLiveRuntime,
) -> HostSafetySample:
    try:
        require_live_runtime()
        sample = collector.collect()
        require_live_runtime()
        return _require_sample(sample, "preload sample")
    except V07HostSafetyError:
        raise
    except Exception as error:
        raise V07HostSafetyError(
            "v0.7 preload telemetry or runtime liveness failed"
        ) from error


def _preload_denial_is_retryable(
    decision: V07HostSafetyDecision,
    sample: HostSafetySample,
) -> bool:
    return (
        decision.reasons == (V07HostSafetyReason.VM_PRESSURE,)
        and sample.vm_pressure_level == 2
    )


def _stop_preload_admission(
    path: Path,
    identity: _PrivatePreloadJournalIdentity,
    *,
    stop_reason: str,
    sample_count: int,
) -> None:
    _append_preload_event(
        path,
        {
            "type": "model_preload_admission_stopped",
            "stop_reason": stop_reason,
            "sample_count": sample_count,
        },
        identity,
    )
    _seal_preload_journal(path, identity)


def _preload_sample_event(
    *,
    sample: HostSafetySample,
    admission: V07HostSafetyDecision,
    retryable: bool,
    candidate: HostSafetySample | None,
    pair: V07HostSafetyDecision | None,
    transition_swap_growth_bytes: int,
    transition_swap_allowed: bool,
) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "model_preload_admission_sample",
        "sample": sample.to_record(),
        "admission_action": admission.action.value,
        "admission_reasons": [reason.value for reason in admission.reasons],
        "retryable_denial": retryable,
        "allowed_streak": (
            2 if pair is not None and pair.allowed and transition_swap_allowed
            else int(admission.allowed and transition_swap_allowed)
        ),
        "transition_swap_growth_bytes": transition_swap_growth_bytes,
        "transition_swap_allowed": transition_swap_allowed,
    }
    if candidate is not None:
        event["candidate_sample_sha256"] = candidate.sha256
    if pair is not None:
        event["pair_action"] = pair.action.value
        event["pair_reasons"] = [reason.value for reason in pair.reasons]
    return event


def _verify_completed_preload_journal(
    path: Path,
    identity: _PrivatePreloadJournalIdentity,
    *,
    pins: V07HostSafetyPins,
    previous_expected: ExpectedHostResources,
    expected: ExpectedHostResources,
    phase: str,
    transition_index: int,
    transition_record: Mapping[str, object],
    expected_runtime_receipt_sha256: str,
) -> HostSafetySample:
    """Replay a sealed successful journal and return only its accepted sample."""

    records = _read_preload_records(path, identity)
    if len(records) < 7:
        raise V07HostSafetyError("v0.7 preload journal record count is invalid")
    declaration, baseline_record, started_record = records[:3]
    completed, sealed = records[-2:]
    instance_id = declaration.get("journal_instance_id")
    expected_declaration = {
        "type": "model_preload_admission_declared",
        "journal_revision": V07_MODEL_PRELOAD_ADMISSION_JOURNAL_REVISION,
        "host_policy_revision": V07_HOST_POLICY_REVISION,
        "journal_instance_id": instance_id,
        "phase": phase,
        "transition_index": transition_index,
        "policy_source_sha256": pins.policy_source_sha256,
        "telemetry_collector_source_sha256": pins.telemetry_collector_source_sha256,
        "runtime_receipt_sha256": expected_runtime_receipt_sha256,
        "previous_expected_resources": _resource_record(previous_expected),
        "expected_resources": _resource_record(expected),
        "sample_interval_seconds": pins.sample_interval_seconds,
        "required_consecutive_samples": pins.cooldown_consecutive_samples,
        "required_free_memory_percent_minimum": (
            pins.admission_free_percent_minimum
        ),
        "retryable_vm_pressure_levels": [2],
        "timeout_seconds": pins.cooldown_timeout_seconds,
        "max_transition_swap_growth_bytes": pins.max_phase_swap_growth_bytes,
        "max_pair_swap_growth_bytes": pins.cooldown_max_swap_growth_bytes,
    }
    if (
        not isinstance(instance_id, str)
        or _JOURNAL_INSTANCE_ID.fullmatch(instance_id) is None
        or _preload_domain_record(declaration) != expected_declaration
    ):
        raise V07HostSafetyError("v0.7 preload declaration did not reproduce")

    try:
        transition_baseline = parse_host_safety_sample(baseline_record.get("sample"))
    except (TypeError, ValueError):
        raise V07HostSafetyError(
            "v0.7 preload transition baseline is invalid"
        ) from None
    policy = V07HostSafetyPolicy(pins)
    baseline_decision = policy.evaluate_admission(
        transition_baseline,
        previous_expected,
    )
    expected_baseline_record = {
        "type": "model_preload_transition_baseline",
        "sample": transition_baseline.to_record(),
        "admission_action": baseline_decision.action.value,
        "admission_reasons": [reason.value for reason in baseline_decision.reasons],
    }
    if (
        not baseline_decision.allowed
        or _preload_domain_record(baseline_record) != expected_baseline_record
    ):
        raise V07HostSafetyError(
            "v0.7 preload transition baseline did not reproduce"
        )
    admission_started = started_record.get("admission_started_monotonic_ns")
    expected_started_record = {
        "type": "model_preload_admission_started",
        "admission_started_monotonic_ns": admission_started,
        "residency_transition": dict(transition_record),
    }
    if (
        type(admission_started) is not int
        or admission_started < transition_baseline.captured_monotonic_ns
        or _preload_domain_record(started_record) != expected_started_record
    ):
        raise V07HostSafetyError("v0.7 preload start did not reproduce")

    sample_records = records[3:-2]
    if not sample_records or any(
        record.get("type") != "model_preload_admission_sample"
        for record in sample_records
    ):
        raise V07HostSafetyError("v0.7 preload sample sequence is invalid")
    candidate: HostSafetySample | None = None
    previous_sample: HostSafetySample | None = None
    accepted: HostSafetySample | None = None
    timeout_ns = pins.cooldown_timeout_seconds * 1_000_000_000
    try:
        for index, record in enumerate(sample_records):
            sample = parse_host_safety_sample(record.get("sample"))
            elapsed_ns = sample.captured_monotonic_ns - admission_started
            if (
                sample.boot_time_unix_microseconds
                != transition_baseline.boot_time_unix_microseconds
                or elapsed_ns < 0
                or elapsed_ns > timeout_ns
                or (
                    previous_sample is not None
                    and sample.captured_monotonic_ns
                    < previous_sample.captured_monotonic_ns
                )
            ):
                raise ValueError("preload sample identity or order differs")
            admission = policy.evaluate_admission(sample, expected)
            transition_swap_growth = (
                sample.swap_used_bytes - transition_baseline.swap_used_bytes
            )
            transition_swap_allowed = (
                transition_swap_growth <= pins.max_phase_swap_growth_bytes
            )
            retryable = (
                transition_swap_allowed
                and _preload_denial_is_retryable(admission, sample)
            )
            pair = None
            if admission.allowed and candidate is not None:
                pair = policy.evaluate_cooldown_pair(
                    candidate,
                    sample,
                    cooldown_started_monotonic_ns=admission_started,
                    admission_boot_time_unix_microseconds=(
                        transition_baseline.boot_time_unix_microseconds
                    ),
                    expected=expected,
                )
            expected_event = _preload_sample_event(
                sample=sample,
                admission=admission,
                retryable=retryable,
                candidate=candidate,
                pair=pair,
                transition_swap_growth_bytes=transition_swap_growth,
                transition_swap_allowed=transition_swap_allowed,
            )
            if _preload_domain_record(record) != expected_event:
                raise ValueError("preload sample event differs")
            if not transition_swap_allowed:
                raise ValueError("transition swap denial appears in success")
            if not admission.allowed and not retryable:
                raise ValueError("hard denial appears in successful journal")
            if pair is not None:
                if not pair.allowed or not transition_swap_allowed:
                    raise ValueError("preload pair denial appears in success")
                if index != len(sample_records) - 1:
                    raise ValueError("accepted preload pair is not terminal")
                accepted = sample
            candidate = sample if admission.allowed else None
            previous_sample = sample
    except (TypeError, ValueError):
        raise V07HostSafetyError(
            "v0.7 preload sample evidence did not reproduce"
        ) from None

    expected_completed = {
        "type": "model_preload_admission_completed",
        "accepted_sample_sha256": None if accepted is None else accepted.sha256,
        "sample_count": len(sample_records),
        "transition_baseline_sha256": transition_baseline.sha256,
    }
    expected_sealed = {
        "type": "journal_sealed",
        "sealed_event_count": len(records) - 1,
    }
    if (
        accepted is None
        or _preload_domain_record(completed) != expected_completed
        or _preload_domain_record(sealed) != expected_sealed
        or len(sample_records)
        > pins.cooldown_timeout_seconds // pins.sample_interval_seconds + 1
    ):
        raise V07HostSafetyError("v0.7 preload completion did not reproduce")
    return accepted


def open_v07_preload_stabilized_host_safety_session(
    *,
    pins: V07HostSafetyPins,
    collector: HostTelemetryCollector,
    previous_expected: ExpectedHostResources,
    expected: ExpectedHostResources,
    require_live_before: RequireLiveRuntime,
    perform_transition: Callable[[], V07ResidencyReceipt],
    require_live_runtime: RequireLiveRuntime,
    expected_runtime_receipt_sha256: str,
    journal_path: Path,
    phase: str,
    transition_index: int,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    sleeper: Callable[[float], None] = time.sleep,
) -> V07HostSafetySession:
    """Transition once, stabilize, seal/replay, and issue one phase session."""

    policy = V07HostSafetyPolicy(pins)
    if type(collector) is not HostTelemetryCollector:
        raise V07HostSafetyError(
            "v0.7 preload stabilization requires the production collector"
        )
    for resources, label in (
        (previous_expected, "previous"),
        (expected, "target"),
    ):
        if type(resources) is not ExpectedHostResources:
            raise V07HostSafetyError(f"v0.7 preload {label} resources are invalid")
        resources.__post_init__()
        if resources.running_container_ids:
            raise V07HostSafetyError("v0.7 preload cannot expect running containers")
    if previous_expected.resident_models not in _ALLOWED_TRANSITION_RESOURCE_SETS:
        raise V07HostSafetyError("v0.7 preload previous model is invalid")
    if expected.resident_models not in _ALLOWED_PHASE_MODEL_SETS:
        raise V07HostSafetyError("v0.7 preload target model is invalid")
    if any(
        not callable(value)
        for value in (
            require_live_before,
            perform_transition,
            require_live_runtime,
            monotonic_ns,
            sleeper,
        )
    ):
        raise V07HostSafetyError("v0.7 preload execution boundary is invalid")
    if (
        type(expected_runtime_receipt_sha256) is not str
        or _SHA256.fullmatch(expected_runtime_receipt_sha256) is None
    ):
        raise V07HostSafetyError("v0.7 preload runtime receipt is invalid")

    identity = _declare_preload_journal(
        journal_path,
        phase=phase,
        transition_index=transition_index,
    )
    instance_id = secrets.token_hex(16)
    declaration = {
        "type": "model_preload_admission_declared",
        "journal_revision": V07_MODEL_PRELOAD_ADMISSION_JOURNAL_REVISION,
        "host_policy_revision": V07_HOST_POLICY_REVISION,
        "journal_instance_id": instance_id,
        "phase": phase,
        "transition_index": transition_index,
        "policy_source_sha256": pins.policy_source_sha256,
        "telemetry_collector_source_sha256": pins.telemetry_collector_source_sha256,
        "runtime_receipt_sha256": expected_runtime_receipt_sha256,
        "previous_expected_resources": _resource_record(previous_expected),
        "expected_resources": _resource_record(expected),
        "sample_interval_seconds": pins.sample_interval_seconds,
        "required_consecutive_samples": pins.cooldown_consecutive_samples,
        "required_free_memory_percent_minimum": (
            pins.admission_free_percent_minimum
        ),
        "retryable_vm_pressure_levels": [2],
        "timeout_seconds": pins.cooldown_timeout_seconds,
        "max_transition_swap_growth_bytes": pins.max_phase_swap_growth_bytes,
        "max_pair_swap_growth_bytes": pins.cooldown_max_swap_growth_bytes,
    }
    _append_preload_event(journal_path, declaration, identity)

    try:
        transition_baseline = _collect_preload_sample(
            collector,
            require_live_before,
        )
    except (KeyboardInterrupt, SystemExit):
        _stop_preload_admission(
            journal_path,
            identity,
            stop_reason="transition_baseline_collection_interrupted",
            sample_count=0,
        )
        raise
    except Exception:
        _stop_preload_admission(
            journal_path,
            identity,
            stop_reason="transition_baseline_collection_failed",
            sample_count=0,
        )
        raise
    baseline_decision = policy.evaluate_admission(
        transition_baseline,
        previous_expected,
    )
    _append_preload_event(
        journal_path,
        {
            "type": "model_preload_transition_baseline",
            "sample": transition_baseline.to_record(),
            "admission_action": baseline_decision.action.value,
            "admission_reasons": [
                reason.value for reason in baseline_decision.reasons
            ],
        },
        identity,
    )
    if not baseline_decision.allowed:
        _stop_preload_admission(
            journal_path,
            identity,
            stop_reason="transition_baseline_denied",
            sample_count=0,
        )
        raise V07HostSafetyDenied("phase", baseline_decision)

    try:
        transition_record = _validate_preload_transition_record(
            perform_transition(),
            previous_expected=previous_expected,
            expected=expected,
            expected_runtime_receipt_sha256=expected_runtime_receipt_sha256,
        )
    except (KeyboardInterrupt, SystemExit):
        _stop_preload_admission(
            journal_path,
            identity,
            stop_reason="transition_interrupted",
            sample_count=0,
        )
        raise
    except Exception as error:
        _stop_preload_admission(
            journal_path,
            identity,
            stop_reason="transition_failed",
            sample_count=0,
        )
        raise V07HostSafetyError("v0.7 model preload transition failed") from error
    try:
        admission_started_monotonic_ns = monotonic_ns()
    except Exception as error:
        _stop_preload_admission(
            journal_path,
            identity,
            stop_reason="transition_clock_failed",
            sample_count=0,
        )
        raise V07HostSafetyError("v0.7 preload monotonic clock failed") from error
    if (
        type(admission_started_monotonic_ns) is not int
        or admission_started_monotonic_ns < transition_baseline.captured_monotonic_ns
    ):
        _stop_preload_admission(
            journal_path,
            identity,
            stop_reason="transition_clock_failed",
            sample_count=0,
        )
        raise V07HostSafetyError("v0.7 preload monotonic clock is invalid")
    _append_preload_event(
        journal_path,
        {
            "type": "model_preload_admission_started",
            "admission_started_monotonic_ns": admission_started_monotonic_ns,
            "residency_transition": transition_record,
        },
        identity,
    )

    candidate: HostSafetySample | None = None
    previous_sample: HostSafetySample | None = None
    maximum_samples = (
        pins.cooldown_timeout_seconds // pins.sample_interval_seconds + 1
    )
    timeout_ns = pins.cooldown_timeout_seconds * 1_000_000_000
    for sample_index in range(1, maximum_samples + 1):
        try:
            sample = _collect_preload_sample(collector, require_live_runtime)
        except (KeyboardInterrupt, SystemExit):
            _stop_preload_admission(
                journal_path,
                identity,
                stop_reason="sample_collection_interrupted",
                sample_count=sample_index - 1,
            )
            raise
        except Exception:
            _stop_preload_admission(
                journal_path,
                identity,
                stop_reason="sample_collection_failed",
                sample_count=sample_index - 1,
            )
            raise
        admission = policy.evaluate_admission(sample, expected)
        elapsed_ns = sample.captured_monotonic_ns - admission_started_monotonic_ns
        boot_matches = (
            sample.boot_time_unix_microseconds
            == transition_baseline.boot_time_unix_microseconds
        )
        ordered = (
            elapsed_ns >= 0
            and (
                previous_sample is None
                or sample.captured_monotonic_ns
                >= previous_sample.captured_monotonic_ns
            )
        )
        transition_swap_growth = (
            sample.swap_used_bytes - transition_baseline.swap_used_bytes
        )
        transition_swap_allowed = (
            transition_swap_growth <= pins.max_phase_swap_growth_bytes
        )
        retryable = (
            boot_matches
            and ordered
            and elapsed_ns <= timeout_ns
            and transition_swap_allowed
            and _preload_denial_is_retryable(admission, sample)
        )
        pair = None
        if boot_matches and ordered and admission.allowed and candidate is not None:
            pair = policy.evaluate_cooldown_pair(
                candidate,
                sample,
                cooldown_started_monotonic_ns=admission_started_monotonic_ns,
                admission_boot_time_unix_microseconds=(
                    transition_baseline.boot_time_unix_microseconds
                ),
                expected=expected,
            )
        _append_preload_event(
            journal_path,
            _preload_sample_event(
                sample=sample,
                admission=admission,
                retryable=retryable,
                candidate=candidate,
                pair=pair,
                transition_swap_growth_bytes=transition_swap_growth,
                transition_swap_allowed=transition_swap_allowed,
            ),
            identity,
        )

        hard_decision: V07HostSafetyDecision | None = None
        if not boot_matches:
            hard_decision = V07HostSafetyDecision(
                V07HostSafetyAction.RECOVER,
                (V07HostSafetyReason.BOOT_IDENTITY,),
            )
        elif not ordered:
            hard_decision = V07HostSafetyDecision(
                V07HostSafetyAction.STOP,
                (V07HostSafetyReason.SAMPLE_ORDER,),
            )
        elif elapsed_ns > timeout_ns:
            _stop_preload_admission(
                journal_path,
                identity,
                stop_reason="timeout",
                sample_count=sample_index,
            )
            raise V07HostSafetyError("v0.7 model preload admission timed out")
        elif not transition_swap_allowed:
            hard_decision = V07HostSafetyDecision(
                V07HostSafetyAction.STOP,
                (V07HostSafetyReason.PHASE_SWAP_GROWTH,),
            )
        elif not admission.allowed and not retryable:
            hard_decision = admission
        elif pair is not None and not pair.allowed:
            hard_decision = pair
        if hard_decision is not None:
            _stop_preload_admission(
                journal_path,
                identity,
                stop_reason="hard_denial",
                sample_count=sample_index,
            )
            raise V07HostSafetyDenied("phase", hard_decision)
        if pair is not None and pair.allowed:
            _append_preload_event(
                journal_path,
                {
                    "type": "model_preload_admission_completed",
                    "accepted_sample_sha256": sample.sha256,
                    "sample_count": sample_index,
                    "transition_baseline_sha256": transition_baseline.sha256,
                },
                identity,
            )
            _seal_preload_journal(journal_path, identity)
            verified = _verify_completed_preload_journal(
                journal_path,
                identity,
                pins=pins,
                previous_expected=previous_expected,
                expected=expected,
                phase=phase,
                transition_index=transition_index,
                transition_record=transition_record,
                expected_runtime_receipt_sha256=expected_runtime_receipt_sha256,
            )
            session = V07HostSafetySession(
                policy=policy,
                collector=collector,
                expected=expected,
                require_live_runtime=require_live_runtime,
                _authority=_SESSION_AUTHORITY,
                _initial_baseline=verified,
            )
            _ISSUED_SESSIONS.add(session)
            return session

        candidate = sample if admission.allowed else None
        previous_sample = sample
        if sample_index == maximum_samples:
            _stop_preload_admission(
                journal_path,
                identity,
                stop_reason="timeout",
                sample_count=sample_index,
            )
            raise V07HostSafetyError("v0.7 model preload admission timed out")
        try:
            sleeper(float(pins.sample_interval_seconds))
        except Exception as error:
            _stop_preload_admission(
                journal_path,
                identity,
                stop_reason="wait_failed",
                sample_count=sample_index,
            )
            raise V07HostSafetyError("v0.7 model preload wait failed") from error
    raise AssertionError("unreachable v0.7 preload admission state")


def open_v07_host_safety_session(
    *,
    pins: V07HostSafetyPins,
    collector: HostTelemetryCollector,
    expected: ExpectedHostResources,
    require_live_runtime: RequireLiveRuntime,
) -> V07HostSafetySession:
    """Collect and admit one phase baseline, returning a sealed capability."""

    policy = V07HostSafetyPolicy(pins)
    if type(collector) is not HostTelemetryCollector:
        raise V07HostSafetyError(
            "v0.7 host session requires the production telemetry collector"
        )
    if type(expected) is not ExpectedHostResources:
        raise V07HostSafetyError("v0.7 expected host resources are invalid")
    expected.__post_init__()
    if expected.resident_models not in _ALLOWED_PHASE_MODEL_SETS:
        raise V07HostSafetyError(
            "v0.7 phase requires one exact preloaded resident model; "
            "the operational preload must be accounted before opening the phase"
        )
    if expected.running_container_ids:
        raise V07HostSafetyError(
            "v0.7 episode phase cannot admit expected running containers"
        )
    if not callable(require_live_runtime):
        raise V07HostSafetyError("v0.7 runtime liveness boundary is invalid")
    session = V07HostSafetySession(
        policy=policy,
        collector=collector,
        expected=expected,
        require_live_runtime=require_live_runtime,
        _authority=_SESSION_AUTHORITY,
    )
    _ISSUED_SESSIONS.add(session)
    try:
        session._start()
    except BaseException:
        _ISSUED_SESSIONS.discard(session)
        raise
    return session


def _require_issued_session(value: V07HostSafetySession) -> None:
    if type(value) is not V07HostSafetySession or value not in _ISSUED_SESSIONS:
        raise V07HostSafetyError("v0.7 host-safety session was not issued")


def _require_issued_hook(value: V07EpisodeHostAdmission) -> None:
    if type(value) is not V07EpisodeHostAdmission or value not in _ISSUED_HOOKS:
        raise V07HostSafetyError("v0.7 episode admission hook was not issued")


def _require_sample(value: object, label: str) -> HostSafetySample:
    if type(value) is not HostSafetySample:
        raise ValueError(f"v0.7 {label} must be an exact HostSafetySample")
    value.__post_init__()
    return value


def _require_inputs(
    sample: HostSafetySample,
    expected: ExpectedHostResources,
) -> None:
    _require_sample(sample, "policy sample")
    if type(expected) is not ExpectedHostResources:
        raise ValueError("v0.7 expected resources must use the exact type")
    expected.__post_init__()


def _append_resource_reasons(
    sample: HostSafetySample,
    expected: ExpectedHostResources,
    reasons: list[V07HostSafetyReason],
) -> None:
    if sample.resident_models != expected.resident_models:
        _append_once(reasons, V07HostSafetyReason.RESIDENT_MODELS)
    if sample.running_container_ids != expected.running_container_ids:
        _append_once(reasons, V07HostSafetyReason.RUNNING_CONTAINERS)


def _append_once(
    values: list[V07HostSafetyReason],
    value: V07HostSafetyReason,
) -> None:
    if value not in values:
        values.append(value)


def _decision(reasons: list[V07HostSafetyReason]) -> V07HostSafetyDecision:
    return V07HostSafetyDecision(
        V07HostSafetyAction.STOP if reasons else V07HostSafetyAction.CONTINUE,
        tuple(reasons),
    )


__all__ = (
    "V07_HOST_POLICY_REVISION",
    "V07_MODEL_PRELOAD_ADMISSION_JOURNAL_REVISION",
    "V07EpisodeHostAdmission",
    "V07EpisodeHostEvidence",
    "V07CooldownEvidence",
    "V07HostSafetyAction",
    "V07HostSafetyDecision",
    "V07HostSafetyDenied",
    "V07HostSafetyError",
    "V07HostSafetyPolicy",
    "V07HostSafetyReason",
    "V07HostSafetySession",
    "open_v07_preload_stabilized_host_safety_session",
    "open_v07_host_safety_session",
)
