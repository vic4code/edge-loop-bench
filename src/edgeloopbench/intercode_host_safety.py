"""Pinned, read-only macOS telemetry and outcome-independent safety policy.

The collector intentionally exposes no generic command runner.  It executes a
small fixed argv set with ``shell=False``, accepts bounded outputs, and returns
only path-free scalar evidence.  Policy evaluation is pure: callers append the
sample record before acting on a stop or recovery decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Protocol

from .intercode_gate_manifest import HostSafetyPins


OLLAMA_PS_URL = "http://127.0.0.1:11434/api/ps"
PMSET_BATTERY_ARGV = ("/usr/bin/pmset", "-g", "batt")
PMSET_CUSTOM_ARGV = ("/usr/bin/pmset", "-g", "custom")
VM_PRESSURE_ARGV = (
    "/usr/sbin/sysctl",
    "-n",
    "kern.memorystatus_vm_pressure_level",
)
SWAP_USAGE_ARGV = ("/usr/sbin/sysctl", "-n", "vm.swapusage")
FREE_MEMORY_ARGV = ("/usr/bin/memory_pressure", "-Q")
THERMAL_ARGV = ("/usr/bin/pmset", "-g", "therm")
BOOT_TIME_ARGV = ("/usr/sbin/sysctl", "-n", "kern.boottime")
_FIXED_PROBE_ARGV = (
    PMSET_BATTERY_ARGV,
    PMSET_CUSTOM_ARGV,
    VM_PRESSURE_ARGV,
    SWAP_USAGE_ARGV,
    FREE_MEMORY_ARGV,
    THERMAL_ARGV,
    BOOT_TIME_ARGV,
)
_MAX_PROBE_OUTPUT_BYTES = 65_536
_MAX_OLLAMA_OUTPUT_BYTES = 1_048_576
_PROBE_TIMEOUT_SECONDS = 5.0
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_BOOT_TIME = re.compile(
    rb"^\{ sec = ([0-9]+), usec = ([0-9]+) \}(?: .*)?\n?$"
)
_SWAP_USAGE = re.compile(
    rb"^total = ([0-9]+(?:\.[0-9]+)?)([KMGT])\s+"
    rb"used = ([0-9]+(?:\.[0-9]+)?)([KMGT])\s+"
    rb"free = ([0-9]+(?:\.[0-9]+)?)([KMGT])\s+"
    rb"\(encrypted\)\n?$"
)
_FREE_PERCENT = re.compile(
    rb"^System-wide memory free percentage: ([0-9]{1,3})%$"
)


class CommandRunner(Protocol):
    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]: ...


class UrlOpen(Protocol):
    def __call__(self, request: object, timeout: float) -> object: ...


class StatVfs(Protocol):
    def __call__(self, path: os.PathLike[str]) -> object: ...


class HostTelemetryError(RuntimeError):
    """A required probe failed, exceeded its bound, or was unparseable."""


@dataclass(frozen=True, slots=True, order=True)
class ResidentModel:
    model: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not _MODEL_NAME.fullmatch(self.model):
            raise ValueError("resident model name is invalid")
        if not isinstance(self.digest, str) or not _DIGEST.fullmatch(self.digest):
            raise ValueError("resident model digest is invalid")

    def to_record(self) -> dict[str, str]:
        return {"model": self.model, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class ExpectedHostResources:
    resident_models: tuple[ResidentModel, ...] = ()
    running_container_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_models(self.resident_models, "expected resident models")
        if len(self.resident_models) > 1:
            raise ValueError("at most one exact resident model may be expected")
        _require_container_ids(
            self.running_container_ids, "expected running containers"
        )


@dataclass(frozen=True, slots=True)
class HostSafetySample:
    captured_unix_ns: int
    captured_monotonic_ns: int
    boot_time_unix_microseconds: int
    on_ac_power: bool
    low_power_mode_enabled: bool
    vm_pressure_level: int
    free_memory_percent: int
    swap_used_bytes: int
    thermal_warning: bool
    performance_warning: bool
    disk_free_bytes: int
    resident_models: tuple[ResidentModel, ...]
    running_container_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "captured_unix_ns",
            "captured_monotonic_ns",
            "boot_time_unix_microseconds",
            "swap_used_bytes",
            "disk_free_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "on_ac_power",
            "low_power_mode_enabled",
            "thermal_warning",
            "performance_warning",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if type(self.vm_pressure_level) is not int or not 0 <= self.vm_pressure_level <= 4:
            raise ValueError("vm_pressure_level must be an integer from zero to four")
        if (
            type(self.free_memory_percent) is not int
            or not 0 <= self.free_memory_percent <= 100
        ):
            raise ValueError("free_memory_percent must be an integer percentage")
        _require_models(self.resident_models, "resident models")
        _require_container_ids(self.running_container_ids, "running containers")

    def _content_record(self) -> dict[str, object]:
        return {
            "schema": "edgeloopbench.host-safety-sample.v1",
            "captured_unix_ns": self.captured_unix_ns,
            "captured_monotonic_ns": self.captured_monotonic_ns,
            "boot_time_unix_microseconds": self.boot_time_unix_microseconds,
            "on_ac_power": self.on_ac_power,
            "low_power_mode_enabled": self.low_power_mode_enabled,
            "vm_pressure_level": self.vm_pressure_level,
            "free_memory_percent": self.free_memory_percent,
            "swap_used_bytes": self.swap_used_bytes,
            "thermal_warning": self.thermal_warning,
            "performance_warning": self.performance_warning,
            "disk_free_bytes": self.disk_free_bytes,
            "resident_models": [model.to_record() for model in self.resident_models],
            "running_container_ids": list(self.running_container_ids),
        }

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(
            _canonical_json(self._content_record())
        ).hexdigest()

    def to_record(self) -> dict[str, object]:
        record = self._content_record()
        record["sample_sha256"] = self.sha256
        return record


class HostSafetyAction(str, Enum):
    CONTINUE = "continue"
    STOP = "stop"
    RECOVER = "recover"


class HostSafetyReason(str, Enum):
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
    BLOCK_SWAP_GROWTH = "block_swap_growth"
    COOLDOWN_SWAP_GROWTH = "cooldown_swap_growth"
    SAMPLE_INTERVAL = "sample_interval"
    SAMPLE_ORDER = "sample_order"
    COOLDOWN_TIMEOUT = "cooldown_timeout"
    BOOT_IDENTITY = "boot_identity"


@dataclass(frozen=True, slots=True)
class HostSafetyDecision:
    action: HostSafetyAction
    reasons: tuple[HostSafetyReason, ...]

    @property
    def allowed(self) -> bool:
        return self.action is HostSafetyAction.CONTINUE


class HostTelemetryCollector:
    """Collect one complete sample or fail closed without partial evidence."""

    def __init__(
        self,
        *,
        docker_binary: Path,
        docker_data_path: Path,
        runner: CommandRunner = subprocess.run,
        urlopen: UrlOpen = urllib.request.urlopen,
        statvfs: StatVfs = os.statvfs,
        time_ns: Callable[[], int] = time.time_ns,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not isinstance(docker_binary, Path) or not docker_binary.is_absolute():
            raise ValueError("docker_binary must be an absolute Path")
        if not isinstance(docker_data_path, Path) or not docker_data_path.is_absolute():
            raise ValueError("docker_data_path must be an absolute Path")
        self._docker_binary = docker_binary
        self._docker_data_path = docker_data_path
        self._runner = runner
        self._urlopen = urlopen
        self._statvfs = statvfs
        self._time_ns = time_ns
        self._monotonic_ns = monotonic_ns

    def collect(self) -> HostSafetySample:
        outputs = {argv: self._run_probe(argv) for argv in _FIXED_PROBE_ARGV}
        docker_argv = (
            str(self._docker_binary),
            "ps",
            "--quiet",
            "--no-trunc",
        )
        docker_output = self._run_probe(docker_argv)
        models = self._read_ollama_models()
        disk_free = self._read_disk_free_bytes()
        on_ac_power = _parse_ac_power(outputs[PMSET_BATTERY_ARGV])
        low_power = _parse_low_power_mode(
            outputs[PMSET_CUSTOM_ARGV], on_ac_power=on_ac_power
        )
        thermal_warning, performance_warning = _parse_thermal(
            outputs[THERMAL_ARGV]
        )
        return HostSafetySample(
            captured_unix_ns=_call_nonnegative_int(self._time_ns, "wall clock"),
            captured_monotonic_ns=_call_nonnegative_int(
                self._monotonic_ns, "monotonic clock"
            ),
            boot_time_unix_microseconds=_parse_boot_time(outputs[BOOT_TIME_ARGV]),
            on_ac_power=on_ac_power,
            low_power_mode_enabled=low_power,
            vm_pressure_level=_parse_vm_pressure(outputs[VM_PRESSURE_ARGV]),
            free_memory_percent=_parse_free_percent(outputs[FREE_MEMORY_ARGV]),
            swap_used_bytes=_parse_swap_used(outputs[SWAP_USAGE_ARGV]),
            thermal_warning=thermal_warning,
            performance_warning=performance_warning,
            disk_free_bytes=disk_free,
            resident_models=models,
            running_container_ids=_parse_container_ids(docker_output),
        )

    def _run_probe(self, argv: tuple[str, ...]) -> bytes:
        try:
            completed = self._runner(
                list(argv),
                shell=False,
                capture_output=True,
                check=False,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise HostTelemetryError("required host telemetry probe failed") from error
        if type(completed.returncode) is not int or completed.returncode != 0:
            raise HostTelemetryError("required host telemetry probe returned non-zero")
        if not isinstance(completed.stdout, bytes) or not isinstance(
            completed.stderr, bytes
        ):
            raise HostTelemetryError("host telemetry probe returned non-byte output")
        if (
            len(completed.stdout) > _MAX_PROBE_OUTPUT_BYTES
            or len(completed.stderr) > _MAX_PROBE_OUTPUT_BYTES
        ):
            raise HostTelemetryError("host telemetry probe exceeded its output bound")
        if completed.stderr:
            raise HostTelemetryError("host telemetry probe wrote unexpected stderr")
        return completed.stdout

    def _read_ollama_models(self) -> tuple[ResidentModel, ...]:
        request = urllib.request.Request(
            OLLAMA_PS_URL,
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with self._urlopen(request, _PROBE_TIMEOUT_SECONDS) as response:
                payload = response.read(_MAX_OLLAMA_OUTPUT_BYTES + 1)
        except (OSError, TimeoutError, ValueError) as error:
            raise HostTelemetryError("Ollama residency probe failed") from error
        if not isinstance(payload, bytes):
            raise HostTelemetryError("Ollama residency probe returned non-byte output")
        if len(payload) > _MAX_OLLAMA_OUTPUT_BYTES:
            raise HostTelemetryError("Ollama residency output exceeded its bound")
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HostTelemetryError("Ollama residency output is invalid JSON") from error
        if not isinstance(parsed, dict) or set(parsed) != {"models"}:
            raise HostTelemetryError("Ollama residency response shape is invalid")
        raw_models = parsed["models"]
        if not isinstance(raw_models, list) or len(raw_models) > 2:
            raise HostTelemetryError("Ollama residency model list is invalid")
        models: list[ResidentModel] = []
        try:
            for raw in raw_models:
                if not isinstance(raw, dict):
                    raise ValueError("model entry must be an object")
                model = raw.get("model")
                digest = raw.get("digest")
                models.append(ResidentModel(model=model, digest=digest))
        except (TypeError, ValueError) as error:
            raise HostTelemetryError("Ollama residency model identity is invalid") from error
        result = tuple(sorted(models))
        try:
            _require_models(result, "Ollama resident models")
        except ValueError as error:
            raise HostTelemetryError("Ollama residency model list is invalid") from error
        return result

    def _read_disk_free_bytes(self) -> int:
        try:
            value = self._statvfs(self._docker_data_path)
            block_size = value.f_frsize
            available = value.f_bavail
        except (AttributeError, OSError, TypeError, ValueError) as error:
            raise HostTelemetryError("Docker data filesystem probe failed") from error
        if (
            type(block_size) is not int
            or type(available) is not int
            or block_size <= 0
            or available < 0
        ):
            raise HostTelemetryError("Docker data filesystem probe is invalid")
        return block_size * available


class HostSafetyPolicy:
    """Pure evaluator for the manifest-pinned admission and run thresholds."""

    def __init__(self, pins: HostSafetyPins) -> None:
        if not isinstance(pins, HostSafetyPins):
            raise ValueError("pins must be a HostSafetyPins value")
        self._pins = pins

    def evaluate_admission(
        self,
        sample: HostSafetySample,
        expected: ExpectedHostResources,
    ) -> HostSafetyDecision:
        _require_policy_inputs(sample, expected)
        reasons: list[HostSafetyReason] = []
        if self._pins.require_ac_power and not sample.on_ac_power:
            reasons.append(HostSafetyReason.AC_POWER_REQUIRED)
        if (
            self._pins.require_low_power_mode_off
            and sample.low_power_mode_enabled
        ):
            reasons.append(HostSafetyReason.LOW_POWER_MODE_ENABLED)
        if sample.vm_pressure_level != self._pins.required_vm_pressure_level:
            reasons.append(HostSafetyReason.VM_PRESSURE)
        if sample.free_memory_percent < self._pins.admission_free_percent_minimum:
            reasons.append(HostSafetyReason.FREE_MEMORY)
        if sample.disk_free_bytes < self._pins.admission_disk_free_bytes_minimum:
            reasons.append(HostSafetyReason.DISK_SPACE)
        _append_warning_reasons(sample, reasons)
        _append_resource_reasons(sample, expected, reasons)
        return _decision(reasons)

    def evaluate_running(
        self,
        sample: HostSafetySample,
        *,
        phase_baseline: HostSafetySample,
        block_baseline: HostSafetySample,
        expected: ExpectedHostResources,
    ) -> HostSafetyDecision:
        _require_policy_inputs(sample, expected)
        if not isinstance(phase_baseline, HostSafetySample) or not isinstance(
            block_baseline, HostSafetySample
        ):
            raise ValueError("running baselines must be HostSafetySample values")
        boot = phase_baseline.boot_time_unix_microseconds
        if (
            block_baseline.boot_time_unix_microseconds != boot
            or sample.boot_time_unix_microseconds != boot
        ):
            return HostSafetyDecision(
                HostSafetyAction.RECOVER,
                (HostSafetyReason.BOOT_IDENTITY,),
            )
        reasons: list[HostSafetyReason] = []
        if (
            phase_baseline.captured_monotonic_ns > sample.captured_monotonic_ns
            or block_baseline.captured_monotonic_ns > sample.captured_monotonic_ns
        ):
            reasons.append(HostSafetyReason.SAMPLE_ORDER)
        if self._pins.require_ac_power and not sample.on_ac_power:
            reasons.append(HostSafetyReason.AC_POWER_REQUIRED)
        if (
            self._pins.require_low_power_mode_off
            and sample.low_power_mode_enabled
        ):
            reasons.append(HostSafetyReason.LOW_POWER_MODE_ENABLED)
        if sample.vm_pressure_level != self._pins.required_vm_pressure_level:
            reasons.append(HostSafetyReason.VM_PRESSURE)
        if sample.free_memory_percent < self._pins.abort_free_percent_below:
            reasons.append(HostSafetyReason.FREE_MEMORY)
        if sample.disk_free_bytes < self._pins.abort_disk_free_bytes_below:
            reasons.append(HostSafetyReason.DISK_SPACE)
        if (
            sample.swap_used_bytes - phase_baseline.swap_used_bytes
            > self._pins.max_phase_swap_growth_bytes
        ):
            reasons.append(HostSafetyReason.PHASE_SWAP_GROWTH)
        if (
            sample.swap_used_bytes - block_baseline.swap_used_bytes
            > self._pins.max_block_swap_growth_bytes
        ):
            reasons.append(HostSafetyReason.BLOCK_SWAP_GROWTH)
        _append_warning_reasons(sample, reasons)
        _append_resource_reasons(sample, expected, reasons)
        return _decision(reasons)

    def evaluate_cooldown_pair(
        self,
        first: HostSafetySample,
        second: HostSafetySample,
        *,
        cooldown_started_monotonic_ns: int,
        admission_boot_time_unix_microseconds: int,
        expected: ExpectedHostResources,
    ) -> HostSafetyDecision:
        _require_policy_inputs(first, expected)
        _require_policy_inputs(second, expected)
        if (
            type(cooldown_started_monotonic_ns) is not int
            or cooldown_started_monotonic_ns < 0
            or type(admission_boot_time_unix_microseconds) is not int
            or admission_boot_time_unix_microseconds < 0
        ):
            raise ValueError("cooldown clocks must be non-negative integers")
        if (
            first.boot_time_unix_microseconds
            != admission_boot_time_unix_microseconds
            or second.boot_time_unix_microseconds
            != admission_boot_time_unix_microseconds
        ):
            return HostSafetyDecision(
                HostSafetyAction.RECOVER,
                (HostSafetyReason.BOOT_IDENTITY,),
            )
        reasons: list[HostSafetyReason] = []
        if (
            first.captured_monotonic_ns < cooldown_started_monotonic_ns
            or second.captured_monotonic_ns < first.captured_monotonic_ns
        ):
            reasons.append(HostSafetyReason.SAMPLE_ORDER)
        interval = second.captured_monotonic_ns - first.captured_monotonic_ns
        minimum_interval = self._pins.sample_interval_seconds * 1_000_000_000
        if interval < minimum_interval:
            reasons.append(HostSafetyReason.SAMPLE_INTERVAL)
        elapsed = second.captured_monotonic_ns - cooldown_started_monotonic_ns
        timeout = self._pins.cooldown_timeout_seconds * 1_000_000_000
        if elapsed > timeout:
            reasons.append(HostSafetyReason.COOLDOWN_TIMEOUT)
        for value in (first, second):
            if value.vm_pressure_level != self._pins.required_vm_pressure_level:
                _append_once(reasons, HostSafetyReason.VM_PRESSURE)
            if value.free_memory_percent < self._pins.cooldown_free_percent_minimum:
                _append_once(reasons, HostSafetyReason.FREE_MEMORY)
            _append_warning_reasons(value, reasons, unique=True)
            _append_resource_reasons(value, expected, reasons, unique=True)
        if (
            second.swap_used_bytes - first.swap_used_bytes
            >= self._pins.cooldown_max_swap_growth_bytes
        ):
            reasons.append(HostSafetyReason.COOLDOWN_SWAP_GROWTH)
        return _decision(reasons)


def _parse_ac_power(output: bytes) -> bool:
    lines = output.splitlines()
    if not lines:
        raise HostTelemetryError("power-source output is empty")
    if lines[0] == b"Now drawing from 'AC Power'":
        return True
    if lines[0] == b"Now drawing from 'Battery Power'":
        return False
    raise HostTelemetryError("power-source output is unparseable")


def _parse_low_power_mode(output: bytes, *, on_ac_power: bool) -> bool:
    try:
        text = output.decode("ascii")
    except UnicodeDecodeError as error:
        raise HostTelemetryError("power-settings output is not ASCII") from error
    target = "AC Power" if on_ac_power else "Battery Power"
    section: str | None = None
    values: list[int] = []
    for line in text.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            section = line[:-1]
            continue
        if section == target:
            match = re.fullmatch(r"\s*lowpowermode\s+([01])\s*", line)
            if match:
                values.append(int(match.group(1)))
    if len(values) != 1:
        raise HostTelemetryError("current low-power-mode setting is unparseable")
    return values[0] == 1


def _parse_vm_pressure(output: bytes) -> int:
    try:
        value = int(output.strip())
    except ValueError as error:
        raise HostTelemetryError("VM pressure level is unparseable") from error
    if not 0 <= value <= 4:
        raise HostTelemetryError("VM pressure level is outside its domain")
    return value


def _parse_swap_used(output: bytes) -> int:
    match = _SWAP_USAGE.fullmatch(output)
    if match is None:
        raise HostTelemetryError("swap usage is unparseable")
    try:
        used = Decimal(match.group(3).decode("ascii"))
    except (InvalidOperation, UnicodeDecodeError) as error:
        raise HostTelemetryError("swap usage is unparseable") from error
    multipliers = {
        b"K": 1 << 10,
        b"M": 1 << 20,
        b"G": 1 << 30,
        b"T": 1 << 40,
    }
    value = int(used * multipliers[match.group(4)])
    if value < 0:
        raise HostTelemetryError("swap usage is negative")
    return value


def _parse_free_percent(output: bytes) -> int:
    matches = [
        _FREE_PERCENT.fullmatch(line)
        for line in output.splitlines()
        if line.startswith(b"System-wide memory free percentage:")
    ]
    if len(matches) != 1 or matches[0] is None:
        raise HostTelemetryError("free-memory percentage is unparseable")
    value = int(matches[0].group(1))
    if not 0 <= value <= 100:
        raise HostTelemetryError("free-memory percentage is outside its domain")
    return value


def _parse_thermal(output: bytes) -> tuple[bool, bool]:
    lowered = output.lower()
    thermal_clear = b"no thermal warning level has been recorded" in lowered
    performance_clear = b"no performance warning level has been recorded" in lowered
    if not thermal_clear and b"thermal warning" not in lowered:
        raise HostTelemetryError("thermal-warning state is unparseable")
    if not performance_clear and b"performance warning" not in lowered:
        raise HostTelemetryError("performance-warning state is unparseable")
    return not thermal_clear, not performance_clear


def _parse_boot_time(output: bytes) -> int:
    match = _BOOT_TIME.fullmatch(output)
    if match is None:
        raise HostTelemetryError("boot-time identity is unparseable")
    seconds = int(match.group(1))
    microseconds = int(match.group(2))
    if microseconds >= 1_000_000:
        raise HostTelemetryError("boot-time microseconds are outside their domain")
    return seconds * 1_000_000 + microseconds


def _parse_container_ids(output: bytes) -> tuple[str, ...]:
    try:
        values = tuple(sorted(line.decode("ascii") for line in output.splitlines()))
    except UnicodeDecodeError as error:
        raise HostTelemetryError("Docker container IDs are not ASCII") from error
    try:
        _require_container_ids(values, "Docker container IDs")
    except ValueError as error:
        raise HostTelemetryError("Docker container IDs are invalid") from error
    return values


def _require_models(models: object, label: str) -> None:
    if not isinstance(models, tuple) or any(
        not isinstance(model, ResidentModel) for model in models
    ):
        raise ValueError(f"{label} must be a tuple of ResidentModel values")
    if tuple(sorted(models)) != models or len(set(models)) != len(models):
        raise ValueError(f"{label} must be sorted and unique")
    if len(models) > 2:
        raise ValueError(f"{label} exceeds the two-model safety bound")


def _require_container_ids(values: object, label: str) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(value, str) or not _CONTAINER_ID.fullmatch(value)
        for value in values
    ):
        raise ValueError(f"{label} must contain full lowercase container IDs")
    if tuple(sorted(values)) != values or len(set(values)) != len(values):
        raise ValueError(f"{label} must be sorted and unique")
    if len(values) > 16:
        raise ValueError(f"{label} exceeds the running-container safety bound")


def _require_policy_inputs(
    sample: HostSafetySample, expected: ExpectedHostResources
) -> None:
    if not isinstance(sample, HostSafetySample):
        raise ValueError("sample must be a HostSafetySample value")
    if not isinstance(expected, ExpectedHostResources):
        raise ValueError("expected must be an ExpectedHostResources value")


def _append_warning_reasons(
    sample: HostSafetySample,
    reasons: list[HostSafetyReason],
    *,
    unique: bool = False,
) -> None:
    append = _append_once if unique else list.append
    if sample.thermal_warning:
        append(reasons, HostSafetyReason.THERMAL_WARNING)
    if sample.performance_warning:
        append(reasons, HostSafetyReason.PERFORMANCE_WARNING)


def _append_resource_reasons(
    sample: HostSafetySample,
    expected: ExpectedHostResources,
    reasons: list[HostSafetyReason],
    *,
    unique: bool = False,
) -> None:
    append = _append_once if unique else list.append
    if sample.resident_models != expected.resident_models:
        append(reasons, HostSafetyReason.RESIDENT_MODELS)
    if sample.running_container_ids != expected.running_container_ids:
        append(reasons, HostSafetyReason.RUNNING_CONTAINERS)


def _append_once(
    values: list[HostSafetyReason], value: HostSafetyReason
) -> None:
    if value not in values:
        values.append(value)


def _decision(reasons: list[HostSafetyReason]) -> HostSafetyDecision:
    return HostSafetyDecision(
        HostSafetyAction.STOP if reasons else HostSafetyAction.CONTINUE,
        tuple(reasons),
    )


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _call_nonnegative_int(function: Callable[[], int], label: str) -> int:
    try:
        value = function()
    except Exception as error:
        raise HostTelemetryError(f"{label} probe failed") from error
    if type(value) is not int or value < 0:
        raise HostTelemetryError(f"{label} probe returned an invalid value")
    return value
