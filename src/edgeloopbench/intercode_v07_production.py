"""Fail-closed outer control surface for the v0.7 local study.

The prelaunch probe is intentionally separate from the production executor: it
can be run repeatedly without creating an artifact directory, starting Docker
work, or launching Ollama.  The complete single-process executor is composed
below from the already sealed image, runtime, calibration, formal, and
publication authorities.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .docker_action_executor import DockerActionExecutor
from .docker_cli import DockerCli
from .intercode_campaign_ledger import CAMPAIGN_TASK_IDS, CampaignSpec
from .intercode_gate_manifest import HostSafetyPins
from .intercode_host_safety import (
    DockerDaemonIdentity,
    DockerTelemetryPins,
    ExpectedHostResources,
    HostSafetyPolicy,
    HostSafetyReason,
    HostSafetySample,
    HostTelemetryError,
    HostTelemetryCollector,
    attest_docker_executable,
    parse_host_safety_sample,
)
from .intercode_image_build import (
    InterCodeImageBuildRequest,
    create_intercode_image_build_plan,
    execute_intercode_image_build,
    verify_intercode_image_build_result,
)
from .intercode_local_model import attest_local_ollama_model
from .intercode_managed_ollama import (
    ManagedOllamaRuntime,
    launch_managed_v07_ollama,
    require_live_managed_ollama_receipt,
)
from .intercode_replay_environment import V07_STRICT_REPLAY_EVALUATOR_SHA256
from .intercode_source import load_intercode_source
from .intercode_source_inventory import (
    VerifiedSourceInventory,
    build_verified_source_inventory,
    derive_source_subset_sha256,
)
from .intercode_v07_analysis import V07EffectivenessAnalysis
from .intercode_v07_authorization import build_v07_campaign_authorization
from .intercode_v07_calibration import (
    build_v07_calibration_design,
    evaluate_v07_calibration,
    evaluate_v07_planning_gate,
)
from .intercode_v07_calibration_executor import execute_v07_calibration
from .intercode_v07_calibration_runtime import (
    build_v07_calibration_runtime_composer,
)
from .intercode_v07_docker_qualification import (
    run_v07_docker_calibration_gold,
    run_v07_docker_qualification,
)
from .intercode_v07_formal_executor import (
    V07FormalCampaignRun,
    build_v07_formal_phase_executor,
    run_v07_formal_campaign,
)
from .intercode_v07_image_provenance import verify_v07_image_set
from .intercode_v07_interventions import (
    V07InterventionPhase,
    append_operational_action,
    append_orchestrator_operator_instruction,
    declare_v07_intervention_journal,
    seal_v07_intervention_journal,
    verify_v07_intervention_declaration,
)
from .intercode_v07_manifest import (
    V07BudgetPins,
    V07DesignPins,
    build_v07_artifact_pins,
    build_v07_execution_pins,
    build_v07_precalibration_manifest,
)
from .intercode_v07_model_phase import build_v07_model_phase_manager
from .intercode_v07_runtime_factory import (
    attest_v07_tokenizer_helper,
    build_v07_host_identity,
    build_v07_model_runtime,
    build_v07_runtime_session,
    issue_v07_managed_residency_boundary,
)
from .intercode_v07_study_binding import prepare_v07_study
from .intercode_v07_study_evidence import (
    VerifiedV07StudyEvidence,
    analyze_v07_study_effectiveness,
    verify_v07_study_evidence,
)
from .journal import (
    JournalError,
    JournalInspection,
    append_journal_event,
    inspect_journal,
    seal_journal,
)
from .model_adapter import PHI4_MINI_RAW_PROFILE, QWEN35_RAW_PROFILE


V07_PRODUCTION_RUNNER_REVISION = (
    "intercode-v0.7-production-runner-v6-bounded-pressure-cooldown"
)
V07_PREFLIGHT_VM_PRESSURE_LEVEL = 1
V07_PREFLIGHT_FREE_MEMORY_PERCENT_MINIMUM = 25
V07_PREFLIGHT_DISK_FREE_BYTES_MINIMUM = 32 << 30
V07_IMAGE_ADMISSION_INTERVAL_SECONDS = 30
V07_IMAGE_ADMISSION_CONSECUTIVE_SAMPLES = 2
V07_IMAGE_ADMISSION_TIMEOUT_SECONDS = 600
V07_IMAGE_ADMISSION_RETRYABLE_VM_PRESSURE_LEVELS = (2,)
V07_IMAGE_ADMISSION_JOURNAL_REVISION = (
    "intercode-v0.7-image-build-admission-journal-v2"
)

_VM_PRESSURE_ARGV = (
    "/usr/sbin/sysctl",
    "-n",
    "kern.memorystatus_vm_pressure_level",
)
_FREE_MEMORY_ARGV = ("/usr/bin/memory_pressure", "-Q")
_FREE_MEMORY = re.compile(
    rb"(?:The system has ([0-9]{1,20}) \(([0-9]{1,20}) pages with a "
    rb"page size of ([0-9]{1,20})\)\.\n)?"
    rb"System-wide memory free percentage: ([0-9]{1,3})%\n?\Z"
)
_MAX_PROBE_BYTES = 65_536
_MAX_ADMISSION_JOURNAL_BYTES = 8 << 20
_JOURNAL_CHAIN_FIELDS = frozenset(
    {"sequence", "previous_event_sha256", "event_sha256"}
)


class V07ProductionError(RuntimeError):
    """The production run is unsafe, incomplete, or internally inconsistent."""


class _CommandRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]: ...


class _StatVfs(Protocol):
    def __call__(self, path: os.PathLike[str]) -> object: ...


@dataclass(frozen=True, slots=True)
class V07ProductionConfig:
    """Local-only path inputs; no path is copied into published evidence."""

    repository_root: Path
    artifact_root: Path
    docker_binary: Path
    docker_endpoint: str
    docker_data_path: Path
    ollama_binary: Path
    ollama_models_root: Path
    tokenizer_helper: Path
    stewarded_container_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _existing_path(self.repository_root, directory=True, executable=False)
        _future_private_root(self.artifact_root)
        _existing_path(self.docker_binary, directory=False, executable=True)
        _local_docker_endpoint(self.docker_endpoint)
        _existing_path(self.docker_data_path, directory=True, executable=False)
        _existing_path(self.ollama_binary, directory=False, executable=True)
        _existing_path(self.ollama_models_root, directory=True, executable=False)
        _planned_executable_path(self.tokenizer_helper)
        try:
            ExpectedHostResources(
                running_container_ids=self.stewarded_container_ids
            )
        except ValueError:
            raise V07ProductionError(
                "v0.7 stewarded container identities are invalid"
            ) from None
        if len(self.stewarded_container_ids) not in (0, 2):
            raise V07ProductionError(
                "v0.7 requires zero or two stewarded container identities"
            )


@dataclass(frozen=True, slots=True)
class _PrivateAdmissionJournalIdentity:
    parent_device: int
    parent_inode: int
    file_device: int
    file_inode: int


@dataclass(frozen=True, slots=True)
class V07ProductionPreflight:
    """Read-only, path-free decision collected before any runtime launch."""

    vm_pressure_level: int
    free_memory_percent: int
    disk_free_bytes: int
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.vm_pressure_level) is not int
            or not 1 <= self.vm_pressure_level <= 4
            or type(self.free_memory_percent) is not int
            or not 0 <= self.free_memory_percent <= 100
            or type(self.disk_free_bytes) is not int
            or self.disk_free_bytes < 0
        ):
            raise V07ProductionError("v0.7 prelaunch sample is invalid")
        expected: list[str] = []
        if self.vm_pressure_level != V07_PREFLIGHT_VM_PRESSURE_LEVEL:
            expected.append("vm_pressure")
        if self.free_memory_percent < V07_PREFLIGHT_FREE_MEMORY_PERCENT_MINIMUM:
            expected.append("free_memory")
        if self.disk_free_bytes < V07_PREFLIGHT_DISK_FREE_BYTES_MINIMUM:
            expected.append("disk_space")
        if self.reasons != tuple(expected):
            raise V07ProductionError("v0.7 prelaunch decision differs from thresholds")

    @property
    def allowed(self) -> bool:
        return not self.reasons

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "disk_free_bytes": self.disk_free_bytes,
            "free_memory_percent": self.free_memory_percent,
            "reasons": list(self.reasons),
            "required_disk_free_bytes_minimum": (
                V07_PREFLIGHT_DISK_FREE_BYTES_MINIMUM
            ),
            "required_free_memory_percent_minimum": (
                V07_PREFLIGHT_FREE_MEMORY_PERCENT_MINIMUM
            ),
            "required_vm_pressure_level": V07_PREFLIGHT_VM_PRESSURE_LEVEL,
            "runner_revision": V07_PRODUCTION_RUNNER_REVISION,
            "schema": "edgeloopbench.intercode-v0.7-production-preflight.v1",
            "vm_pressure_level": self.vm_pressure_level,
        }


@dataclass(frozen=True, slots=True)
class V07ProductionResult:
    """Terminal verified result; local artifact paths remain operational only."""

    preflight: V07ProductionPreflight
    formal_run: V07FormalCampaignRun
    study_evidence: VerifiedV07StudyEvidence
    analysis: V07EffectivenessAnalysis
    artifact_root: Path

    def __post_init__(self) -> None:
        if (
            type(self.preflight) is not V07ProductionPreflight
            or not self.preflight.allowed
            or type(self.formal_run) is not V07FormalCampaignRun
            or not self.formal_run.progress.sealed
            or type(self.study_evidence) is not VerifiedV07StudyEvidence
            or type(self.analysis) is not V07EffectivenessAnalysis
            or type(self.artifact_root) is not type(Path())
            or not self.artifact_root.is_absolute()
        ):
            raise V07ProductionError("v0.7 terminal production result is invalid")

    def canonical_record(self) -> dict[str, object]:
        """Return only path-free publication-safe terminal identities."""

        self.__post_init__()
        return {
            "analysis": self.analysis.to_dict(),
            "preflight": self.preflight.canonical_record(),
            "runner_revision": V07_PRODUCTION_RUNNER_REVISION,
            "schema": "edgeloopbench.intercode-v0.7-production-result.v3",
            "study_evidence": self.study_evidence.canonical_record(),
        }


def inspect_v07_production_preflight(
    config: V07ProductionConfig,
    *,
    runner: _CommandRunner = subprocess.run,
    statvfs: _StatVfs = os.statvfs,
) -> V07ProductionPreflight:
    """Collect the minimum gate needed before launching an empty Ollama server."""

    if type(config) is not V07ProductionConfig:
        raise V07ProductionError("v0.7 production config type is invalid")
    config.__post_init__()
    if not callable(runner) or not callable(statvfs):
        raise V07ProductionError("v0.7 prelaunch probe boundary is invalid")
    pressure_payload = _run_probe(runner, _VM_PRESSURE_ARGV)
    free_payload = _run_probe(runner, _FREE_MEMORY_ARGV)
    try:
        pressure = int(pressure_payload.strip().decode("ascii"))
    except (UnicodeError, ValueError):
        raise V07ProductionError("v0.7 VM pressure probe is invalid") from None
    match = _FREE_MEMORY.fullmatch(free_payload)
    if match is None:
        raise V07ProductionError("v0.7 free-memory probe is invalid")
    if match.group(1) is not None:
        total_bytes, page_count, page_size = (
            int(match.group(index)) for index in range(1, 4)
        )
        if (
            total_bytes <= 0
            or page_count <= 0
            or page_size <= 0
            or total_bytes != page_count * page_size
        ):
            raise V07ProductionError("v0.7 free-memory geometry is invalid")
    free_percent = int(match.group(4))
    try:
        disk = statvfs(config.docker_data_path)
        block_size = disk.f_frsize
        available = disk.f_bavail
    except (AttributeError, OSError, TypeError, ValueError):
        raise V07ProductionError("v0.7 Docker-data disk probe failed") from None
    if (
        type(block_size) is not int
        or block_size <= 0
        or type(available) is not int
        or available < 0
    ):
        raise V07ProductionError("v0.7 Docker-data disk probe is invalid")
    disk_free = block_size * available
    reasons: list[str] = []
    if pressure != V07_PREFLIGHT_VM_PRESSURE_LEVEL:
        reasons.append("vm_pressure")
    if free_percent < V07_PREFLIGHT_FREE_MEMORY_PERCENT_MINIMUM:
        reasons.append("free_memory")
    if disk_free < V07_PREFLIGHT_DISK_FREE_BYTES_MINIMUM:
        reasons.append("disk_space")
    return V07ProductionPreflight(
        vm_pressure_level=pressure,
        free_memory_percent=free_percent,
        disk_free_bytes=disk_free,
        reasons=tuple(reasons),
    )


def _await_image_build_admission(
    collector: HostTelemetryCollector,
    pins: HostSafetyPins,
    journal_path: Path,
    *,
    stewarded_container_ids: tuple[str, ...] = (),
    require_live_runtime: Callable[[], object],
    sleep: Callable[[float], object] = time.sleep,
) -> HostSafetySample:
    """Journal a bounded, read-only stabilization before any Docker mutation."""

    if (
        not callable(getattr(collector, "collect", None))
        or type(pins) is not HostSafetyPins
        or type(journal_path) is not type(Path())
        or not journal_path.is_absolute()
        or not callable(require_live_runtime)
        or not callable(sleep)
    ):
        raise V07ProductionError("v0.7 image admission boundary is invalid")
    try:
        stewarded = ExpectedHostResources(
            running_container_ids=stewarded_container_ids
        )
    except ValueError:
        raise V07ProductionError(
            "v0.7 stewarded container identities are invalid"
        ) from None
    if len(stewarded.running_container_ids) not in (0, 2):
        raise V07ProductionError(
            "v0.7 requires zero or two stewarded container identities"
        )
    frozen_timing = (
        pins.sample_interval_seconds,
        pins.cooldown_consecutive_samples,
        pins.cooldown_timeout_seconds,
    )
    if frozen_timing != (
        V07_IMAGE_ADMISSION_INTERVAL_SECONDS,
        V07_IMAGE_ADMISSION_CONSECUTIVE_SAMPLES,
        V07_IMAGE_ADMISSION_TIMEOUT_SECONDS,
    ):
        raise V07ProductionError("v0.7 image admission timing pins drifted")
    expected = ExpectedHostResources()
    policy = HostSafetyPolicy(pins)
    identity = _declare_private_journal(journal_path)
    _append_admission_event(
        journal_path,
        {
            "type": "image_build_admission_declared",
            "expected_resources": {
                "resident_models": [],
                "running_container_ids": [],
            },
            "journal_revision": V07_IMAGE_ADMISSION_JOURNAL_REVISION,
            "journal_instance_id": secrets.token_hex(16),
            "policy_sha256": pins.policy_sha256,
            "runner_revision": V07_PRODUCTION_RUNNER_REVISION,
            "sample_interval_seconds": V07_IMAGE_ADMISSION_INTERVAL_SECONDS,
            "required_consecutive_samples": (
                V07_IMAGE_ADMISSION_CONSECUTIVE_SAMPLES
            ),
            "retryable_vm_pressure_levels": list(
                V07_IMAGE_ADMISSION_RETRYABLE_VM_PRESSURE_LEVELS
            ),
            "stewarded_container_ids": list(
                stewarded.running_container_ids
            ),
            "telemetry_collector_sha256": pins.telemetry_collector_sha256,
            "timeout_seconds": V07_IMAGE_ADMISSION_TIMEOUT_SECONDS,
        },
        identity,
    )
    started_monotonic_ns: int | None = None
    initial_boot_time: int | None = None
    previous_monotonic_ns: int | None = None
    candidate: HostSafetySample | None = None
    sample_count = 0
    maximum_samples = (
        V07_IMAGE_ADMISSION_TIMEOUT_SECONDS
        // V07_IMAGE_ADMISSION_INTERVAL_SECONDS
        + 1
    )

    while sample_count < maximum_samples:
        try:
            require_live_runtime()
        except Exception:
            _stop_image_admission(
                journal_path,
                stop_reason="runtime_liveness_error",
                sample_count=sample_count,
                identity=identity,
            )
            raise V07ProductionError(
                "v0.7 managed Ollama runtime became unavailable"
            ) from None
        try:
            sample = collector.collect()
        except HostTelemetryError:
            _stop_image_admission(
                journal_path,
                stop_reason="telemetry_error",
                sample_count=sample_count,
                identity=identity,
            )
            raise V07ProductionError(
                "v0.7 image admission telemetry failed"
            ) from None
        if type(sample) is not HostSafetySample:
            _stop_image_admission(
                journal_path,
                stop_reason="telemetry_error",
                sample_count=sample_count,
                identity=identity,
            )
            raise V07ProductionError("v0.7 image admission sample is invalid")
        try:
            require_live_runtime()
        except Exception:
            _stop_image_admission(
                journal_path,
                stop_reason="runtime_liveness_error",
                sample_count=sample_count,
                identity=identity,
            )
            raise V07ProductionError(
                "v0.7 managed Ollama runtime became unavailable"
            ) from None

        sample_count += 1
        if started_monotonic_ns is None:
            started_monotonic_ns = sample.captured_monotonic_ns
            initial_boot_time = sample.boot_time_unix_microseconds
        assert initial_boot_time is not None
        assert started_monotonic_ns is not None
        boot_changed = sample.boot_time_unix_microseconds != initial_boot_time
        sample_order_changed = (
            previous_monotonic_ns is not None
            and sample.captured_monotonic_ns < previous_monotonic_ns
        )
        try:
            admission = policy.evaluate_admission(sample, expected)
            pair = None
            if admission.allowed and candidate is not None and not boot_changed:
                pair = policy.evaluate_cooldown_pair(
                    candidate,
                    sample,
                    cooldown_started_monotonic_ns=started_monotonic_ns,
                    admission_boot_time_unix_microseconds=initial_boot_time,
                    expected=expected,
                )
        except ValueError:
            _stop_image_admission(
                journal_path,
                stop_reason="policy_error",
                sample_count=sample_count,
                identity=identity,
            )
            raise V07ProductionError(
                "v0.7 image admission policy evaluation failed"
            ) from None
        retryable_denial = _image_admission_denial_is_retryable(
            admission.reasons,
            vm_pressure_level=sample.vm_pressure_level,
            observed_container_ids=sample.running_container_ids,
            stewarded_container_ids=stewarded.running_container_ids,
        )
        sample_event: dict[str, object] = {
            "type": "image_build_admission_sample",
            "sample": sample.to_record(),
            "admission_action": admission.action.value,
            "admission_reasons": [reason.value for reason in admission.reasons],
            "retryable_denial": retryable_denial,
            "allowed_streak": (
                2 if pair is not None and pair.allowed else int(admission.allowed)
            ),
        }
        if candidate is not None:
            sample_event["candidate_sample_sha256"] = candidate.sha256
        if pair is not None:
            sample_event["pair_action"] = pair.action.value
            sample_event["pair_reasons"] = [reason.value for reason in pair.reasons]
        _append_admission_event(journal_path, sample_event, identity)

        elapsed_ns = sample.captured_monotonic_ns - started_monotonic_ns
        hard_denial = not admission.allowed and not retryable_denial
        pair_denied = pair is not None and not pair.allowed
        if boot_changed or sample_order_changed or hard_denial or pair_denied:
            _stop_image_admission(
                journal_path,
                stop_reason="hard_denial",
                sample_count=sample_count,
                identity=identity,
            )
            raise V07ProductionError("v0.7 image admission was denied")
        if pair is not None and pair.allowed:
            _append_admission_event(
                journal_path,
                {
                    "type": "image_build_admission_completed",
                    "accepted_sample_sha256": sample.sha256,
                    "sample_count": sample_count,
                },
                identity,
            )
            _seal_admission_journal(journal_path, identity)
            return _verify_completed_admission_journal(
                journal_path,
                identity,
                pins=pins,
                stewarded_container_ids=stewarded.running_container_ids,
            )
        if (
            elapsed_ns >= V07_IMAGE_ADMISSION_TIMEOUT_SECONDS * 1_000_000_000
            or sample_count >= maximum_samples
        ):
            _stop_image_admission(
                journal_path,
                stop_reason="timeout",
                sample_count=sample_count,
                identity=identity,
            )
            raise V07ProductionError("v0.7 image admission did not stabilize")

        candidate = sample if admission.allowed else None
        previous_monotonic_ns = sample.captured_monotonic_ns
        try:
            sleep(float(V07_IMAGE_ADMISSION_INTERVAL_SECONDS))
        except (OSError, ValueError):
            _stop_image_admission(
                journal_path,
                stop_reason="sleep_error",
                sample_count=sample_count,
                identity=identity,
            )
            raise V07ProductionError(
                "v0.7 image admission wait failed"
            ) from None

    raise AssertionError("bounded image admission loop exhausted unexpectedly")


def _image_admission_denial_is_retryable(
    reasons: tuple[HostSafetyReason, ...],
    *,
    vm_pressure_level: int,
    observed_container_ids: tuple[str, ...],
    stewarded_container_ids: tuple[str, ...],
) -> bool:
    if reasons == (HostSafetyReason.VM_PRESSURE,):
        return (
            vm_pressure_level
            in V07_IMAGE_ADMISSION_RETRYABLE_VM_PRESSURE_LEVELS
        )
    if reasons not in (
        (HostSafetyReason.RUNNING_CONTAINERS,),
        (
            HostSafetyReason.VM_PRESSURE,
            HostSafetyReason.RUNNING_CONTAINERS,
        ),
    ):
        return False
    if (
        HostSafetyReason.VM_PRESSURE in reasons
        and vm_pressure_level
        not in V07_IMAGE_ADMISSION_RETRYABLE_VM_PRESSURE_LEVELS
    ):
        return False
    observed = set(observed_container_ids)
    return bool(observed) and observed.issubset(stewarded_container_ids)


def _declare_private_journal(path: Path) -> _PrivateAdmissionJournalIdentity:
    parent_descriptor = -1
    descriptor = -1
    parent_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    parent_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    identity: _PrivateAdmissionJournalIdentity | None = None
    try:
        parent_descriptor = os.open(path.parent, parent_flags)
        parent = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise OSError("unsafe parent")
        descriptor = os.open(
            path.name,
            flags,
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
            raise OSError("unsafe journal")
        identity = _PrivateAdmissionJournalIdentity(
            parent_device=parent.st_dev,
            parent_inode=parent.st_ino,
            file_device=metadata.st_dev,
            file_inode=metadata.st_ino,
        )
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
    except OSError:
        raise V07ProductionError(
            "v0.7 image admission journal could not be declared"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    assert identity is not None
    _validate_private_journal(path, identity)
    return identity


def _validate_private_journal(
    path: Path,
    identity: _PrivateAdmissionJournalIdentity,
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
            or parent.st_dev != identity.parent_device
            or parent.st_ino != identity.parent_inode
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_dev != identity.file_device
            or metadata.st_ino != identity.file_inode
        ):
            raise OSError("journal identity changed")
    except OSError:
        raise V07ProductionError(
            "v0.7 image admission journal identity changed"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _append_admission_event(
    path: Path,
    event: Mapping[str, object],
    identity: _PrivateAdmissionJournalIdentity,
) -> None:
    try:
        _validate_private_journal(path, identity)
        append_journal_event(path, event)
        _validate_private_journal(path, identity)
    except (JournalError, OSError, ValueError):
        raise V07ProductionError(
            "v0.7 image admission journal append failed"
        ) from None


def _seal_admission_journal(
    path: Path,
    identity: _PrivateAdmissionJournalIdentity,
) -> None:
    try:
        _validate_private_journal(path, identity)
        seal_journal(path)
        _validate_private_journal(path, identity)
        inspect_journal(path, require_sealed=True)
        _validate_private_journal(path, identity)
    except (JournalError, OSError, ValueError):
        raise V07ProductionError(
            "v0.7 image admission journal seal failed"
        ) from None


def _read_private_admission_records(
    path: Path,
    identity: _PrivateAdmissionJournalIdentity,
) -> tuple[list[dict[str, object]], JournalInspection]:
    _validate_private_journal(path, identity)
    parent_descriptor = -1
    descriptor = -1
    parent_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    parent_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        parent_descriptor = os.open(path.parent, parent_flags)
        descriptor = os.open(path.name, file_flags, dir_fd=parent_descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        metadata = os.fstat(descriptor)
        if (
            metadata.st_dev != identity.file_device
            or metadata.st_ino != identity.file_inode
            or metadata.st_size > _MAX_ADMISSION_JOURNAL_BYTES
        ):
            raise OSError("journal identity or size changed")
        snapshots = tuple(
            _read_bounded_admission_snapshot(descriptor)
            for _ in range(3)
        )
        inspection = inspect_journal(path, require_sealed=True)
        final_metadata = os.fstat(descriptor)
        if (
            snapshots[0] != snapshots[1]
            or snapshots[1] != snapshots[2]
            or metadata.st_size != len(snapshots[0])
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_ctime_ns != metadata.st_ctime_ns
            or inspection.file_byte_length != len(snapshots[0])
            or inspection.complete_byte_length != len(snapshots[0])
        ):
            raise OSError("journal changed during verification")
        payload = snapshots[0]
    except (JournalError, OSError, ValueError):
        raise V07ProductionError(
            "v0.7 image admission journal could not be verified"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
    _validate_private_journal(path, identity)
    try:
        parsed = [json.loads(line) for line in payload.splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise V07ProductionError(
            "v0.7 image admission journal is not valid JSONL"
        ) from None
    if any(not isinstance(record, dict) for record in parsed):
        raise V07ProductionError(
            "v0.7 image admission journal record is invalid"
        )
    return parsed, inspection


def _read_bounded_admission_snapshot(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 65_536)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_ADMISSION_JOURNAL_BYTES:
            raise OSError("journal exceeds read bound")
        chunks.append(chunk)
    return b"".join(chunks)


def _verify_completed_admission_journal(
    path: Path,
    identity: _PrivateAdmissionJournalIdentity,
    *,
    pins: HostSafetyPins,
    stewarded_container_ids: tuple[str, ...],
) -> HostSafetySample:
    """Re-derive the accepted baseline only from sealed raw evidence."""

    records, inspection = _read_private_admission_records(path, identity)
    if inspection.record_count != len(records) or len(records) < 4:
        raise V07ProductionError(
            "v0.7 image admission journal record count is invalid"
        )
    declaration = records[0]
    completed = records[-2]
    sealed = records[-1]
    instance_id = declaration.get("journal_instance_id")
    expected_declaration = {
        "type": "image_build_admission_declared",
        "journal_revision": V07_IMAGE_ADMISSION_JOURNAL_REVISION,
        "runner_revision": V07_PRODUCTION_RUNNER_REVISION,
        "policy_sha256": pins.policy_sha256,
        "telemetry_collector_sha256": pins.telemetry_collector_sha256,
        "expected_resources": {
            "resident_models": [],
            "running_container_ids": [],
        },
        "stewarded_container_ids": list(stewarded_container_ids),
        "sample_interval_seconds": pins.sample_interval_seconds,
        "required_consecutive_samples": pins.cooldown_consecutive_samples,
        "retryable_vm_pressure_levels": list(
            V07_IMAGE_ADMISSION_RETRYABLE_VM_PRESSURE_LEVELS
        ),
        "timeout_seconds": pins.cooldown_timeout_seconds,
        "journal_instance_id": instance_id,
    }
    if _journal_domain_record(declaration) != expected_declaration:
        raise V07ProductionError(
            "v0.7 image admission declaration differs from its pins"
        )
    if (
        type(instance_id) is not str
        or re.fullmatch(r"[0-9a-f]{32}", instance_id) is None
        or completed.get("type") != "image_build_admission_completed"
        or sealed.get("type") != "journal_sealed"
    ):
        raise V07ProductionError(
            "v0.7 image admission terminal records are invalid"
        )

    sample_records = records[1:-2]
    if not sample_records or any(
        record.get("type") != "image_build_admission_sample"
        for record in sample_records
    ):
        raise V07ProductionError(
            "v0.7 image admission sample sequence is invalid"
        )
    policy = HostSafetyPolicy(pins)
    expected = ExpectedHostResources()
    samples: list[HostSafetySample] = []
    candidate: HostSafetySample | None = None
    accepted_pair = None
    first_monotonic_ns: int | None = None
    boot_time: int | None = None
    try:
        for index, record in enumerate(sample_records):
            sample = parse_host_safety_sample(record.get("sample"))
            samples.append(sample)
            if first_monotonic_ns is None:
                first_monotonic_ns = sample.captured_monotonic_ns
                boot_time = sample.boot_time_unix_microseconds
            assert first_monotonic_ns is not None
            assert boot_time is not None
            admission = policy.evaluate_admission(sample, expected)
            retryable = _image_admission_denial_is_retryable(
                admission.reasons,
                vm_pressure_level=sample.vm_pressure_level,
                observed_container_ids=sample.running_container_ids,
                stewarded_container_ids=stewarded_container_ids,
            )
            if (
                (not admission.allowed and not retryable)
                or sample.boot_time_unix_microseconds != boot_time
                or (
                    index > 0
                    and sample.captured_monotonic_ns
                    < samples[index - 1].captured_monotonic_ns
                )
            ):
                raise ValueError("sample decision differs")
            pair = None
            if admission.allowed and candidate is not None:
                pair = policy.evaluate_cooldown_pair(
                    candidate,
                    sample,
                    cooldown_started_monotonic_ns=first_monotonic_ns,
                    admission_boot_time_unix_microseconds=boot_time,
                    expected=expected,
                )
            streak = 2 if pair is not None and pair.allowed else int(admission.allowed)
            expected_sample_record: dict[str, object] = {
                "type": "image_build_admission_sample",
                "sample": sample.to_record(),
                "admission_action": admission.action.value,
                "admission_reasons": [
                    reason.value for reason in admission.reasons
                ],
                "retryable_denial": retryable,
                "allowed_streak": streak,
            }
            if candidate is not None:
                expected_sample_record["candidate_sample_sha256"] = (
                    candidate.sha256
                )
            if pair is not None:
                expected_sample_record["pair_action"] = pair.action.value
                expected_sample_record["pair_reasons"] = [
                    reason.value for reason in pair.reasons
                ]
            if _journal_domain_record(record) != expected_sample_record:
                raise ValueError("sample event differs")
            if pair is not None and not pair.allowed:
                raise ValueError("pair denial was not terminal")
            if pair is not None and pair.allowed:
                if index != len(sample_records) - 1:
                    raise ValueError("completed pair is not terminal")
                accepted_pair = pair
            candidate = sample if admission.allowed else None
    except (TypeError, ValueError):
        raise V07ProductionError(
            "v0.7 image admission evidence did not reproduce"
        ) from None

    accepted = samples[-1]
    expected_completed = {
        "type": "image_build_admission_completed",
        "accepted_sample_sha256": accepted.sha256,
        "sample_count": len(samples),
    }
    expected_sealed = {
        "type": "journal_sealed",
        "sealed_event_count": len(records) - 1,
    }
    if (
        accepted_pair is None
        or _journal_domain_record(completed) != expected_completed
        or _journal_domain_record(sealed) != expected_sealed
        or len(samples) > (
            pins.cooldown_timeout_seconds // pins.sample_interval_seconds + 1
        )
        or accepted.captured_monotonic_ns - samples[0].captured_monotonic_ns
        > pins.cooldown_timeout_seconds * 1_000_000_000
    ):
        raise V07ProductionError(
            "v0.7 image admission completion did not reproduce"
        )
    return accepted


def _journal_domain_record(record: Mapping[str, object]) -> dict[str, object]:
    if not _JOURNAL_CHAIN_FIELDS.issubset(record):
        raise V07ProductionError(
            "v0.7 image admission journal framing is invalid"
        )
    return {
        key: value
        for key, value in record.items()
        if key not in _JOURNAL_CHAIN_FIELDS
    }


def _stop_image_admission(
    path: Path,
    *,
    stop_reason: str,
    sample_count: int,
    identity: _PrivateAdmissionJournalIdentity,
) -> None:
    _append_admission_event(
        path,
        {
            "type": "image_build_admission_stopped",
            "stop_reason": stop_reason,
            "sample_count": sample_count,
        },
        identity,
    )
    _seal_admission_journal(path, identity)


def execute_v07_production(
    config: V07ProductionConfig,
) -> V07ProductionResult:
    """Execute qualification, calibration, formal scoring, and verification.

    This function intentionally has no resume mode. A process or host failure
    preserves its append-only raw directory, but a later production attempt
    must use a fresh artifact root and receive a new intervention-journal
    instance identity.
    """

    preflight = inspect_v07_production_preflight(config)
    if not preflight.allowed:
        raise V07ProductionError(
            "v0.7 prelaunch safety gate denied production execution"
        )
    _existing_path(config.tokenizer_helper, directory=False, executable=True)
    _existing_path(
        config.tokenizer_helper.with_name(
            config.tokenizer_helper.name + ".provenance.json"
        ),
        directory=False,
        executable=False,
    )

    stage = "source inventory"
    managed: ManagedOllamaRuntime | None = None
    try:
        inventory = build_verified_source_inventory(config.repository_root)
        paths = _create_artifact_tree(config.artifact_root)
        _write_record(paths["records"] / "preflight.json", preflight.canonical_record())
        _write_record(
            paths["records"] / "source-inventory.json",
            inventory.canonical_record(),
        )

        intervention_path = config.artifact_root / "interventions.jsonl"
        declare_v07_intervention_journal(intervention_path)
        append_orchestrator_operator_instruction(
            intervention_path,
            phase=V07InterventionPhase.PREPARATION,
        )

        stage = "managed Ollama launch"
        append_operational_action(
            intervention_path,
            phase=V07InterventionPhase.PREPARATION,
        )
        ollama_sha256 = _sha256_executable(config.ollama_binary)
        managed = launch_managed_v07_ollama(
            runtime_binary=config.ollama_binary,
            expected_runtime_binary_sha256=ollama_sha256,
        )

        stage = "Docker identity"
        docker_pins = _inspect_docker_pins(config)
        environment = _docker_environment()
        collector = HostTelemetryCollector(
            docker_binary=config.docker_binary,
            docker_data_path=config.docker_data_path,
            docker_pins=docker_pins,
            environment=environment,
        )
        legacy_safety = _image_build_safety_pins(
            inventory=inventory,
            docker_pins=docker_pins,
            managed=managed,
        )
        image_policy = HostSafetyPolicy(legacy_safety)
        baseline = _await_image_build_admission(
            collector,
            legacy_safety,
            paths["records"] / "image-build-admission.jsonl",
            stewarded_container_ids=config.stewarded_container_ids,
            require_live_runtime=lambda: require_live_managed_ollama_receipt(
                managed.receipt
            ),
            sleep=time.sleep,
        )
        if type(baseline.docker_daemon) is not DockerDaemonIdentity:
            raise V07ProductionError("v0.7 Docker daemon identity is unavailable")
        _write_record(
            paths["records"] / "docker-identity.json",
            baseline.docker_daemon.to_record(),
        )

        stage = "Docker image build"
        append_operational_action(
            intervention_path,
            phase=V07InterventionPhase.PREPARATION,
        )
        build_plan = create_intercode_image_build_plan(
            InterCodeImageBuildRequest(
                repo_root=config.repository_root,
                docker_binary=config.docker_binary,
                docker_pins=docker_pins,
            )
        )
        build_manifest_path = paths["images"] / "image-build.jsonl"
        build_result = execute_intercode_image_build(
            build_plan,
            manifest_path=build_manifest_path,
            collector=collector,
            policy=image_policy,
            environment=environment,
        )
        verified_build = verify_intercode_image_build_result(
            build_plan,
            manifest_path=build_manifest_path,
            result=build_result,
            environment=environment,
        )
        image_set = verify_v07_image_set(
            source_inventory=inventory,
            repository_root=config.repository_root,
            verified_build=verified_build,
        )
        _write_record(
            paths["records"] / "image-set.json",
            image_set.canonical_record(),
        )

        stage = "small-model and tokenizer attestation"
        tokenizer = attest_v07_tokenizer_helper(
            helper_path=config.tokenizer_helper,
            provenance_path=config.tokenizer_helper.with_name(
                config.tokenizer_helper.name + ".provenance.json"
            ),
        )
        profiles = (QWEN35_RAW_PROFILE, PHI4_MINI_RAW_PROFILE)
        attestations = tuple(
            attest_local_ollama_model(
                profile=profile,
                models_root=config.ollama_models_root,
                runtime_binary=config.ollama_binary,
                runtime_receipt=managed.receipt,
            )
            for profile in profiles
        )
        model_runtimes = tuple(
            build_v07_model_runtime(
                model_id=profile.model,
                attestation=attestation,
                runtime_receipt=managed.receipt,
                tokenizer_attestation=tokenizer,
                model_artifact_path=(
                    config.ollama_models_root
                    / "blobs"
                    / profile.model_artifact_sha256.replace(":", "-")
                ),
            )
            for profile, attestation in zip(profiles, attestations, strict=True)
        )
        host_identity = build_v07_host_identity(
            docker_pins=docker_pins,
            docker_daemon=baseline.docker_daemon,
            runtime_receipt=managed.receipt,
        )
        runtime_session = build_v07_runtime_session(
            models=model_runtimes,
            host_identity=host_identity,
        )
        execution_pins = build_v07_execution_pins(
            source_inventory=inventory,
            host_identity=host_identity,
        )
        _write_record(
            paths["records"] / "runtime-session.json",
            runtime_session.canonical_record(),
        )
        _write_record(
            paths["records"] / "execution-pins.json",
            execution_pins.canonical_record(),
        )

        stage = "Docker qualification"
        docker_cli = DockerCli(
            expected_context="desktop-linux",
            expected_endpoint=config.docker_endpoint,
            docker_binary=os.fspath(config.docker_binary),
            env=environment,
        )
        action_executor = DockerActionExecutor(
            boundary=docker_cli,
            expected_docker_binary=os.fspath(config.docker_binary),
            expected_endpoint=config.docker_endpoint,
        )
        append_operational_action(
            intervention_path,
            phase=V07InterventionPhase.QUALIFICATION,
        )
        source = load_intercode_source(config.repository_root)
        qualification = run_v07_docker_qualification(
            source=source,
            journal_path=paths["qualification"] / "selected-sample.jsonl",
            image_set=image_set,
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
            execution_pins=execution_pins,
            docker_cli=docker_cli,
            action_executor=action_executor,
        )
        append_operational_action(
            intervention_path,
            phase=V07InterventionPhase.QUALIFICATION,
        )
        calibration_gold = run_v07_docker_calibration_gold(
            source=source,
            image_set=image_set,
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
            execution_pins=execution_pins,
            docker_cli=docker_cli,
            action_executor=action_executor,
        )
        _write_record(
            paths["records"] / "qualification-evidence.json",
            qualification.evidence.to_public_record(),
        )

        stage = "precalibration manifest"
        artifacts = build_v07_artifact_pins(
            source_inventory=inventory,
            qualification_evidence=qualification.evidence,
        )
        manifest = build_v07_precalibration_manifest(
            artifacts=artifacts,
            models=tuple(runtime.model_identity for runtime in model_runtimes),
            host_identity=host_identity,
            execution=execution_pins,
            budgets=V07BudgetPins(),
            design=V07DesignPins(),
        )
        _write_record(
            paths["records"] / "precalibration-manifest.json",
            manifest.canonical_record(),
        )

        stage = "calibration"
        residency = issue_v07_managed_residency_boundary(managed.receipt)
        phase_manager = build_v07_model_phase_manager(
            runtime_session=runtime_session,
            execution_pins=execution_pins,
            collector=collector,
            residency_boundary=residency,
            intervention_journal_path=intervention_path,
        )
        calibration_design = build_v07_calibration_design(source)
        calibration_runtime = build_v07_calibration_runtime_composer(
            runtime_session=runtime_session,
            source=source,
            calibration_gold=calibration_gold,
            manifest=manifest,
            open_phase=phase_manager.open_calibration_phase,
            docker_cli=docker_cli,
            action_executor=action_executor,
        )
        calibration = execute_v07_calibration(
            design=calibration_design,
            source=source,
            calibration_gold=calibration_gold,
            precalibration_manifest=manifest,
            calibration_journal_path=paths["calibration"] / "calibration.jsonl",
            artifact_directory=paths["calibration"] / "episodes",
            runtime_factory=calibration_runtime,
        )
        dispositions = tuple(
            evaluate_v07_calibration(calibration.evidence, model_id)
            for model_id in runtime_session.model_ids
        )
        planning_gate = evaluate_v07_planning_gate(dispositions)
        _write_record(
            paths["records"] / "calibration-evidence.json",
            {
                "calibration_campaign_sha256": (
                    calibration.evidence.calibration_campaign_sha256
                ),
                "calibration_journal_sha256": (
                    calibration.evidence.calibration_journal_sha256
                ),
                "controller_log_set_sha256": (
                    calibration.evidence.controller_log_set_sha256
                ),
                "design_sha256": calibration.evidence.design_sha256,
                "episode_count": calibration.evidence.episode_count,
                "evidence_sha256": calibration.evidence.evidence_sha256,
                "precalibration_manifest_sha256": (
                    calibration.evidence.precalibration_manifest_sha256
                ),
                "schedule_sha256": calibration.evidence.schedule_sha256,
                "schema": (
                    "edgeloopbench.intercode-v0.7-calibration-summary.v1"
                ),
                "total_model_prompts": (
                    calibration.evidence.total_model_prompts
                ),
            },
        )
        _write_record(
            paths["records"] / "calibration-planning-gate.json",
            asdict(planning_gate),
        )

        stage = "formal authorization"
        authorization = build_v07_campaign_authorization(
            manifest=manifest,
            qualification_evidence=qualification.evidence,
            calibration_evidence=calibration.evidence,
            dispositions=dispositions,
            planning_gate=planning_gate,
            source_inventory=inventory,
        )
        intervention_declaration = verify_v07_intervention_declaration(
            intervention_path
        )
        prepared = prepare_v07_study(
            authorization=authorization,
            qualification=qualification,
            calibration_gold=calibration_gold,
            calibration_evidence=calibration.evidence,
            runtime_session=runtime_session,
            intervention_declaration=intervention_declaration,
            manifest=manifest,
            execution_pins=execution_pins,
            campaign_spec=CampaignSpec(CAMPAIGN_TASK_IDS),
        )
        _write_record(
            paths["records"] / "authorization.json",
            authorization.canonical_record(),
        )
        _write_record(
            paths["records"] / "study-binding.json",
            prepared.canonical_record(),
        )

        stage = "formal campaign"

        def open_formal_phase(
            previous_model_id: str | None,
            target_model_id: str,
        ):  # type: ignore[no-untyped-def]
            host_session = phase_manager.open_formal_phase(
                previous_model_id,
                target_model_id,
            )
            return build_v07_formal_phase_executor(
                prepared_study=prepared,
                source=source,
                manifest=manifest,
                phase_model_id=target_model_id,
                host_session=host_session,
                docker_cli=docker_cli,
                action_executor=action_executor,
                artifact_root=paths["formal"],
            )

        formal = run_v07_formal_campaign(
            spec=prepared.bound_campaign_spec,
            open_phase=open_formal_phase,
            campaign_journal_path=paths["formal"] / "campaign.jsonl",
            repository_root=config.repository_root,
            intervention_journal_path=intervention_path,
        )

        stage = "aggregate evidence"
        seal_v07_intervention_journal(intervention_path)
        evidence = verify_v07_study_evidence(
            prepared,
            campaign_journal_path=paths["formal"] / "campaign.jsonl",
            episode_log_directory=paths["formal"] / "controllers",
            execution_envelope_directory=paths["formal"] / "envelopes",
            intervention_journal_path=intervention_path,
        )
        analysis = analyze_v07_study_effectiveness(evidence)
        _write_record(
            paths["records"] / "study-evidence.json",
            evidence.canonical_record(),
        )
        _write_record(
            paths["records"] / "analysis.json",
            analysis.to_dict(),
        )
        result = V07ProductionResult(
            preflight=preflight,
            formal_run=formal,
            study_evidence=evidence,
            analysis=analysis,
            artifact_root=config.artifact_root,
        )
        _write_record(
            paths["records"] / "production-result.json",
            result.canonical_record(),
        )
        return result
    except (KeyboardInterrupt, SystemExit):
        raise
    except V07ProductionError:
        raise
    except Exception:
        raise V07ProductionError(
            f"v0.7 production run failed during {stage}"
        ) from None
    finally:
        if managed is not None:
            managed.close()


def _create_artifact_tree(root: Path) -> dict[str, Path]:
    names = ("images", "qualification", "calibration", "formal", "records")
    try:
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        paths = {name: root / name for name in names}
        for path in paths.values():
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700)
    except OSError:
        raise V07ProductionError("v0.7 artifact tree could not be created") from None
    for path in (root, *paths.values()):
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise V07ProductionError("v0.7 artifact directory identity is unsafe")
    return paths


def _write_record(path: Path, record: Mapping[str, object]) -> None:
    if not isinstance(record, Mapping):
        raise V07ProductionError("v0.7 derived record is not a mapping")
    try:
        payload = (
            json.dumps(
                dict(record),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise V07ProductionError("v0.7 derived record is not canonical JSON") from None
    if len(payload) > 16 << 20:
        raise V07ProductionError("v0.7 derived record exceeds its safety bound")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
    except OSError:
        raise V07ProductionError("v0.7 derived record could not be written") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise V07ProductionError("v0.7 derived record identity is unsafe")


def _sha256_executable(path: Path) -> str:
    try:
        return attest_docker_executable(path)
    except Exception:
        raise V07ProductionError("v0.7 executable attestation failed") from None


def _inspect_docker_pins(config: V07ProductionConfig) -> DockerTelemetryPins:
    binary_sha256 = _sha256_executable(config.docker_binary)
    argv = [
        os.fspath(config.docker_binary),
        "--host",
        config.docker_endpoint,
        "version",
        "--format",
        "{{json .}}",
    ]
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            check=False,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError):
        raise V07ProductionError("v0.7 Docker identity probe failed") from None
    if (
        completed.returncode != 0
        or not isinstance(completed.stdout, bytes)
        or not isinstance(completed.stderr, bytes)
        or completed.stderr
        or len(completed.stdout) > _MAX_PROBE_BYTES
    ):
        raise V07ProductionError("v0.7 Docker identity probe was invalid")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        parsed = json.loads(completed.stdout, object_pairs_hook=reject_duplicates)
        client = parsed["Client"]
        server = parsed["Server"]
        client_version = client["Version"]
        server_version = server["Version"]
        pins = DockerTelemetryPins(
            endpoint=config.docker_endpoint,
            client_version=client_version,
            server_version=server_version,
            binary_sha256=binary_sha256,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise V07ProductionError("v0.7 Docker version identity is invalid") from None
    if _sha256_executable(config.docker_binary) != binary_sha256:
        raise V07ProductionError("v0.7 Docker binary changed during identity probe")
    return pins


def _docker_environment() -> dict[str, str]:
    if "DOCKER_HOST" in os.environ or "DOCKER_CONTEXT" in os.environ:
        raise V07ProductionError("v0.7 inherited Docker override is not admitted")
    environment = dict(os.environ)
    if any(
        not key
        or "=" in key
        or "\x00" in key
        or "\x00" in value
        for key, value in environment.items()
    ):
        raise V07ProductionError("v0.7 inherited environment is invalid")
    return environment


def _image_build_safety_pins(
    *,
    inventory: VerifiedSourceInventory,
    docker_pins: DockerTelemetryPins,
    managed: ManagedOllamaRuntime,
) -> HostSafetyPins:
    source_sha256 = derive_source_subset_sha256(
        inventory,
        ("src/edgeloopbench/intercode_host_safety.py",),
    )
    receipt = managed.receipt
    return HostSafetyPins(
        policy_sha256=source_sha256,
        telemetry_collector_sha256=source_sha256,
        docker_binary_sha256=docker_pins.binary_sha256,
        docker_endpoint_sha256=docker_pins.endpoint_sha256,
        docker_client_version=docker_pins.client_version,
        docker_server_version=docker_pins.server_version,
        ollama_runtime_binary_sha256=receipt.runtime_binary_sha256,
        ollama_server_version=receipt.runtime_version,
        ollama_launch_environment_sha256=receipt.launch_environment_sha256,
        ollama_generation_endpoint_sha256=(
            receipt.generation_endpoint_sha256
        ),
    )


def _run_probe(runner: _CommandRunner, argv: tuple[str, ...]) -> bytes:
    try:
        completed = runner(
            list(argv),
            shell=False,
            capture_output=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        raise V07ProductionError("v0.7 prelaunch probe failed") from None
    if (
        type(completed.returncode) is not int
        or completed.returncode != 0
        or not isinstance(completed.stdout, bytes)
        or not isinstance(completed.stderr, bytes)
        or completed.stderr
        or len(completed.stdout) > _MAX_PROBE_BYTES
    ):
        raise V07ProductionError("v0.7 prelaunch probe returned invalid output")
    return completed.stdout


def _existing_path(path: Path, *, directory: bool, executable: bool) -> None:
    if (
        type(path) is not type(Path())
        or not path.is_absolute()
        or Path(os.path.normpath(path)) != path
    ):
        raise V07ProductionError("v0.7 production path is not canonical and absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError:
        raise V07ProductionError("v0.7 production input is unavailable") from None
    if resolved != path or stat.S_ISLNK(metadata.st_mode):
        raise V07ProductionError("v0.7 production input contains a symlink")
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(
        metadata.st_mode
    )
    if not expected or (executable and not os.access(path, os.X_OK)):
        raise V07ProductionError("v0.7 production input type is invalid")


def _planned_executable_path(path: Path) -> None:
    """Validate an existing helper or a canonical location provisioned later."""

    if (
        type(path) is not type(Path())
        or not path.is_absolute()
        or Path(os.path.normpath(path)) != path
    ):
        raise V07ProductionError("v0.7 production path is not canonical and absolute")
    if path.is_symlink():
        raise V07ProductionError("v0.7 tokenizer helper location is unsafe")
    if path.exists():
        _existing_path(path, directory=False, executable=True)


def _future_private_root(path: Path) -> None:
    if (
        type(path) is not type(Path())
        or not path.is_absolute()
        or Path(os.path.normpath(path)) != path
        or path.exists()
        or path.is_symlink()
    ):
        raise V07ProductionError("v0.7 artifact root must be a fresh absolute path")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        raise V07ProductionError("v0.7 artifact parent is unavailable") from None
    if parent != path.parent:
        raise V07ProductionError("v0.7 artifact parent contains a symlink")


def _local_docker_endpoint(value: str) -> None:
    if type(value) is not str or not value or "\x00" in value:
        raise V07ProductionError("v0.7 Docker endpoint is invalid")
    parsed = urlsplit(value)
    valid_unix = (
        parsed.scheme == "unix"
        and not parsed.netloc
        and parsed.path.startswith("/")
        and os.path.normpath(parsed.path) == parsed.path
        and value == "unix://" + parsed.path
        and not parsed.query
        and not parsed.fragment
    )
    if not valid_unix:
        raise V07ProductionError("v0.7 Docker endpoint must be a local Unix socket")


def main(argv: list[str] | None = None) -> int:
    """Inspect the gate by default; execute only with the explicit flag."""

    parser = argparse.ArgumentParser(
        prog="python -m edgeloopbench.intercode_v07_production",
        allow_abbrev=False,
    )
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--docker-binary", required=True)
    parser.add_argument("--docker-endpoint", required=True)
    parser.add_argument("--docker-data-path", required=True)
    parser.add_argument("--ollama-binary", required=True)
    parser.add_argument("--ollama-models-root", required=True)
    parser.add_argument("--tokenizer-helper", required=True)
    parser.add_argument(
        "--stewarded-container-id",
        action="append",
        default=[],
    )
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        config = V07ProductionConfig(
            repository_root=_command_path(arguments.repository_root),
            artifact_root=_command_path(arguments.artifact_root),
            docker_binary=_command_path(arguments.docker_binary),
            docker_endpoint=arguments.docker_endpoint,
            docker_data_path=_command_path(arguments.docker_data_path),
            ollama_binary=_command_path(arguments.ollama_binary),
            ollama_models_root=_command_path(arguments.ollama_models_root),
            tokenizer_helper=_command_path(arguments.tokenizer_helper),
            stewarded_container_ids=tuple(
                sorted(arguments.stewarded_container_id)
            ),
        )
        if arguments.execute:
            payload = execute_v07_production(config).canonical_record()
            mode = "execute"
        else:
            payload = inspect_v07_production_preflight(config).canonical_record()
            mode = "preflight"
        print(
            json.dumps(
                {"mode": mode, "result": payload},
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, ValueError, V07ProductionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _command_path(value: str) -> Path:
    if type(value) is not str or not value or "\x00" in value:
        raise V07ProductionError("v0.7 command path is invalid")
    return Path(os.path.abspath(os.path.expanduser(value)))


__all__ = (
    "V07_PREFLIGHT_DISK_FREE_BYTES_MINIMUM",
    "V07_PREFLIGHT_FREE_MEMORY_PERCENT_MINIMUM",
    "V07_PREFLIGHT_VM_PRESSURE_LEVEL",
    "V07_PRODUCTION_RUNNER_REVISION",
    "V07ProductionConfig",
    "V07ProductionError",
    "V07ProductionPreflight",
    "V07ProductionResult",
    "execute_v07_production",
    "inspect_v07_production_preflight",
    "main",
)


if __name__ == "__main__":
    raise SystemExit(main())
