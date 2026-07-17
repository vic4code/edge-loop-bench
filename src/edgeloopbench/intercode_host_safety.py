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
import stat
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .intercode_gate_manifest import HostSafetyPins
from .ollama_loopback_http import (
    OLLAMA_PS_URL,
    OllamaLoopbackHttpError,
    open_ollama_http,
    parse_strict_json_object,
    require_exact_ollama_response,
)


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
_TAGGED_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DOCKER_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
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


@dataclass(frozen=True, slots=True)
class DockerTelemetryPins:
    """Exact local Docker identities accepted by the telemetry boundary."""

    endpoint: str
    client_version: str
    server_version: str
    binary_sha256: str

    def __post_init__(self) -> None:
        _require_local_docker_endpoint(self.endpoint)
        for name in ("client_version", "server_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or _DOCKER_VERSION.fullmatch(value) is None:
                raise ValueError(f"Docker {name} pin is invalid")
        if (
            not isinstance(self.binary_sha256, str)
            or _TAGGED_DIGEST.fullmatch(self.binary_sha256) is None
        ):
            raise ValueError("Docker binary SHA-256 pin is invalid")

    @property
    def endpoint_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.endpoint.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DockerDaemonIdentity:
    """Path-free Docker client/endpoint/daemon identity for one sample."""

    binary_sha256: str
    endpoint_sha256: str
    client_version: str
    server_version: str

    def __post_init__(self) -> None:
        for name in ("binary_sha256", "endpoint_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or _TAGGED_DIGEST.fullmatch(value) is None:
                raise ValueError(f"Docker identity {name} is invalid")
        for name in ("client_version", "server_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or _DOCKER_VERSION.fullmatch(value) is None:
                raise ValueError(f"Docker identity {name} is invalid")

    def to_record(self) -> dict[str, str]:
        return {
            "binary_sha256": self.binary_sha256,
            "endpoint_sha256": self.endpoint_sha256,
            "client_version": self.client_version,
            "server_version": self.server_version,
        }


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
    docker_daemon: DockerDaemonIdentity | None = None

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
        if self.docker_daemon is not None and not isinstance(
            self.docker_daemon, DockerDaemonIdentity
        ):
            raise ValueError(
                "docker_daemon must be a DockerDaemonIdentity value or None"
            )
        _require_models(self.resident_models, "resident models")
        _require_container_ids(self.running_container_ids, "running containers")

    def _content_record(self) -> dict[str, object]:
        record: dict[str, object] = {
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
        if self.docker_daemon is not None:
            record["schema"] = "edgeloopbench.host-safety-sample.v2"
            record["docker_daemon"] = self.docker_daemon.to_record()
        return record

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(
            _canonical_json(self._content_record())
        ).hexdigest()

    def to_record(self) -> dict[str, object]:
        record = self._content_record()
        record["sample_sha256"] = self.sha256
        return record


def parse_host_safety_sample(record: object) -> HostSafetySample:
    """Reconstruct one exact path-free sample and verify its content digest."""

    if not isinstance(record, Mapping):
        raise ValueError("host safety sample record must be a mapping")
    values = dict(record)
    schema = values.get("schema")
    base_fields = {
        "schema",
        "sample_sha256",
        "captured_unix_ns",
        "captured_monotonic_ns",
        "boot_time_unix_microseconds",
        "on_ac_power",
        "low_power_mode_enabled",
        "vm_pressure_level",
        "free_memory_percent",
        "swap_used_bytes",
        "thermal_warning",
        "performance_warning",
        "disk_free_bytes",
        "resident_models",
        "running_container_ids",
    }
    if schema == "edgeloopbench.host-safety-sample.v1":
        expected_fields = base_fields
        daemon = None
    elif schema == "edgeloopbench.host-safety-sample.v2":
        expected_fields = base_fields | {"docker_daemon"}
        raw_daemon = values.get("docker_daemon")
        if not isinstance(raw_daemon, Mapping) or set(raw_daemon) != {
            "binary_sha256",
            "endpoint_sha256",
            "client_version",
            "server_version",
        }:
            raise ValueError("host safety Docker identity record is invalid")
        daemon = DockerDaemonIdentity(**dict(raw_daemon))  # type: ignore[arg-type]
    else:
        raise ValueError("host safety sample schema is invalid")
    if set(values) != expected_fields:
        raise ValueError("host safety sample fields differ from the schema")

    raw_models = values.get("resident_models")
    if not isinstance(raw_models, list):
        raise ValueError("host safety resident models record is invalid")
    models: list[ResidentModel] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, Mapping) or set(raw_model) != {"model", "digest"}:
            raise ValueError("host safety resident model record is invalid")
        models.append(ResidentModel(**dict(raw_model)))  # type: ignore[arg-type]
    raw_containers = values.get("running_container_ids")
    if not isinstance(raw_containers, list):
        raise ValueError("host safety running containers record is invalid")

    sample = HostSafetySample(
        captured_unix_ns=values.get("captured_unix_ns"),  # type: ignore[arg-type]
        captured_monotonic_ns=values.get("captured_monotonic_ns"),  # type: ignore[arg-type]
        boot_time_unix_microseconds=values.get("boot_time_unix_microseconds"),  # type: ignore[arg-type]
        on_ac_power=values.get("on_ac_power"),  # type: ignore[arg-type]
        low_power_mode_enabled=values.get("low_power_mode_enabled"),  # type: ignore[arg-type]
        vm_pressure_level=values.get("vm_pressure_level"),  # type: ignore[arg-type]
        free_memory_percent=values.get("free_memory_percent"),  # type: ignore[arg-type]
        swap_used_bytes=values.get("swap_used_bytes"),  # type: ignore[arg-type]
        thermal_warning=values.get("thermal_warning"),  # type: ignore[arg-type]
        performance_warning=values.get("performance_warning"),  # type: ignore[arg-type]
        disk_free_bytes=values.get("disk_free_bytes"),  # type: ignore[arg-type]
        resident_models=tuple(models),
        running_container_ids=tuple(raw_containers),
        docker_daemon=daemon,
    )
    digest = values.get("sample_sha256")
    if type(digest) is not str or _TAGGED_DIGEST.fullmatch(digest) is None:
        raise ValueError("host safety sample digest is invalid")
    if sample.sha256 != digest:
        raise ValueError("host safety sample digest differs from its content")
    return sample


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
    DOCKER_IDENTITY = "docker_identity"


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
        docker_pins: DockerTelemetryPins | None = None,
        environment: Mapping[str, str] | None = None,
        docker_binary_sha256: Callable[[Path], str] | None = None,
        runner: CommandRunner = subprocess.run,
        urlopen: UrlOpen = open_ollama_http,
        statvfs: StatVfs = os.statvfs,
        time_ns: Callable[[], int] = time.time_ns,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if (
            not isinstance(docker_binary, Path)
            or not docker_binary.is_absolute()
            or Path(os.path.normpath(os.fspath(docker_binary))) != docker_binary
            or "\x00" in os.fspath(docker_binary)
        ):
            raise ValueError("docker_binary must be a canonical absolute Path")
        if (
            not isinstance(docker_data_path, Path)
            or not docker_data_path.is_absolute()
            or Path(os.path.normpath(os.fspath(docker_data_path))) != docker_data_path
            or "\x00" in os.fspath(docker_data_path)
        ):
            raise ValueError("docker_data_path must be a canonical absolute Path")
        if docker_pins is not None and not isinstance(
            docker_pins, DockerTelemetryPins
        ):
            raise ValueError("docker_pins must be a DockerTelemetryPins value or None")
        inherited: dict[str, str] | None = None
        if docker_pins is not None or environment is not None:
            inherited = dict(os.environ if environment is None else environment)
            if "DOCKER_HOST" in inherited or "DOCKER_CONTEXT" in inherited:
                raise HostTelemetryError(
                    "inherited DOCKER_HOST or DOCKER_CONTEXT is not admitted"
                )
            if any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or "=" in key
                or "\x00" in key
                or "\x00" in value
                for key, value in inherited.items()
            ):
                raise ValueError("telemetry subprocess environment is invalid")
        if docker_binary_sha256 is not None and not callable(docker_binary_sha256):
            raise ValueError("docker_binary_sha256 must be callable")
        self._docker_binary = docker_binary
        self._docker_pins = docker_pins
        self._docker_data_path = docker_data_path
        self._environment = inherited
        self._docker_binary_sha256 = (
            docker_binary_sha256 or attest_docker_executable
        )
        self._runner = runner
        self._urlopen = urlopen
        self._statvfs = statvfs
        self._time_ns = time_ns
        self._monotonic_ns = monotonic_ns

    def collect(self) -> HostSafetySample:
        docker_binary_sha256: str | None = None
        if self._docker_pins is not None:
            docker_binary_sha256 = self._read_docker_binary_sha256()
        outputs = {argv: self._run_probe(argv) for argv in _FIXED_PROBE_ARGV}
        docker_daemon: DockerDaemonIdentity | None = None
        if self._docker_pins is None:
            docker_argv = (
                str(self._docker_binary),
                "ps",
                "--quiet",
                "--no-trunc",
            )
        else:
            assert docker_binary_sha256 is not None
            docker_daemon = self._read_docker_daemon_identity(
                docker_binary_sha256
            )
            docker_argv = (
                str(self._docker_binary),
                "--host",
                self._docker_pins.endpoint,
                "container",
                "ls",
                "--quiet",
                "--no-trunc",
                "--filter",
                "status=running",
            )
        docker_output = self._run_probe(docker_argv)
        models = self._read_ollama_models()
        disk_free = self._read_disk_free_bytes()
        if (
            docker_binary_sha256 is not None
            and self._read_docker_binary_sha256() != docker_binary_sha256
        ):
            raise HostTelemetryError(
                "Docker binary changed during telemetry collection"
            )
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
            docker_daemon=docker_daemon,
        )

    def _run_probe(self, argv: tuple[str, ...]) -> bytes:
        kwargs: dict[str, object] = {
            "shell": False,
            "capture_output": True,
            "check": False,
            "timeout": _PROBE_TIMEOUT_SECONDS,
        }
        if self._environment is not None:
            kwargs["env"] = dict(self._environment)
        try:
            completed = self._runner(list(argv), **kwargs)
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

    def _read_docker_binary_sha256(self) -> str:
        assert self._docker_pins is not None
        try:
            value = self._docker_binary_sha256(self._docker_binary)
        except (OSError, ValueError) as error:
            raise HostTelemetryError("Docker binary identity probe failed") from error
        if not isinstance(value, str) or _TAGGED_DIGEST.fullmatch(value) is None:
            raise HostTelemetryError("Docker binary identity probe is invalid")
        if value != self._docker_pins.binary_sha256:
            raise HostTelemetryError("Docker binary identity differs from its pin")
        return value

    def _read_docker_daemon_identity(
        self, binary_sha256: str
    ) -> DockerDaemonIdentity:
        assert self._docker_pins is not None
        payload = self._run_probe(
            (
                str(self._docker_binary),
                "--host",
                self._docker_pins.endpoint,
                "version",
                "--format",
                "{{json .}}",
            )
        )
        client_version, server_version = _parse_docker_versions(payload)
        if (
            client_version != self._docker_pins.client_version
            or server_version != self._docker_pins.server_version
        ):
            raise HostTelemetryError(
                "Docker client or server version differs from its pin"
            )
        return DockerDaemonIdentity(
            binary_sha256=binary_sha256,
            endpoint_sha256=self._docker_pins.endpoint_sha256,
            client_version=client_version,
            server_version=server_version,
        )

    def _read_ollama_models(self) -> tuple[ResidentModel, ...]:
        request = urllib.request.Request(
            OLLAMA_PS_URL,
            method="GET",
            headers={"Accept": "application/json"},
        )
        if request.full_url != OLLAMA_PS_URL:
            raise HostTelemetryError("Ollama residency probe URL is invalid")
        try:
            with self._urlopen(request, _PROBE_TIMEOUT_SECONDS) as response:
                require_exact_ollama_response(response, expected_url=OLLAMA_PS_URL)
                payload = response.read(_MAX_OLLAMA_OUTPUT_BYTES + 1)
        except (
            OllamaLoopbackHttpError,
            OSError,
            TimeoutError,
            ValueError,
            urllib.error.URLError,
        ) as error:
            raise HostTelemetryError("Ollama residency probe failed") from error
        if not isinstance(payload, bytes):
            raise HostTelemetryError("Ollama residency probe returned non-byte output")
        if len(payload) > _MAX_OLLAMA_OUTPUT_BYTES:
            raise HostTelemetryError("Ollama residency output exceeded its bound")
        try:
            parsed = parse_strict_json_object(payload)
        except OllamaLoopbackHttpError as error:
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
        expected: ExpectedHostResources | None = None,
    ) -> HostSafetyDecision:
        if expected is None:
            expected = ExpectedHostResources()
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
        _append_docker_identity_reason(sample, self._pins, reasons)
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
        _append_docker_identity_reason(sample, self._pins, reasons)
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
            _append_docker_identity_reason(
                value,
                self._pins,
                reasons,
                unique=True,
            )
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


def _parse_docker_versions(output: bytes) -> tuple[str, str]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate Docker version field")
            value[key] = item
        return value

    try:
        parsed = json.loads(output, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HostTelemetryError("Docker version output is invalid JSON") from error
    if not isinstance(parsed, dict):
        raise HostTelemetryError("Docker version output shape is invalid")
    client = parsed.get("Client")
    server = parsed.get("Server")
    if not isinstance(client, dict) or not isinstance(server, dict):
        raise HostTelemetryError("Docker version output shape is invalid")
    client_version = client.get("Version")
    server_version = server.get("Version")
    if (
        not isinstance(client_version, str)
        or _DOCKER_VERSION.fullmatch(client_version) is None
        or not isinstance(server_version, str)
        or _DOCKER_VERSION.fullmatch(server_version) is None
    ):
        raise HostTelemetryError("Docker version identity is invalid")
    return client_version, server_version


def _require_local_docker_endpoint(endpoint: object) -> None:
    if not isinstance(endpoint, str) or "\x00" in endpoint or "%" in endpoint:
        raise ValueError("Docker endpoint must be an absolute local Unix socket")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
        or os.path.normpath(parsed.path) != parsed.path
        or endpoint != "unix://" + parsed.path
    ):
        raise ValueError("Docker endpoint must be an absolute local Unix socket")


def attest_docker_executable(path: Path) -> str:
    """Hash one safe executable through the telemetry boundary's exact policy."""

    if type(path) is not type(Path()) or not path.is_absolute():
        raise ValueError("Docker binary attestation path is invalid")
    if path.is_symlink():
        raise ValueError("Docker binary path is a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HostTelemetryError("Docker binary identity probe failed") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > 256 << 20
            or not before.st_mode & stat.S_IXUSR
            or before.st_mode & stat.S_IWOTH
        ):
            raise HostTelemetryError("Docker binary identity is unsafe")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
            raise HostTelemetryError("Docker binary changed while hashing")
        link = os.stat(path, follow_symlinks=False)
        if (link.st_dev, link.st_ino) != (after.st_dev, after.st_ino):
            raise HostTelemetryError("Docker binary path identity changed")
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


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


def _append_docker_identity_reason(
    sample: HostSafetySample,
    pins: HostSafetyPins,
    reasons: list[HostSafetyReason],
    *,
    unique: bool = False,
) -> None:
    expected = (
        pins.docker_binary_sha256,
        pins.docker_endpoint_sha256,
        pins.docker_client_version,
        pins.docker_server_version,
    )
    if expected == (None, None, None, None):
        return
    daemon = sample.docker_daemon
    if daemon is None or (
        daemon.binary_sha256,
        daemon.endpoint_sha256,
        daemon.client_version,
        daemon.server_version,
    ) != expected:
        (_append_once if unique else list.append)(
            reasons,
            HostSafetyReason.DOCKER_IDENTITY,
        )


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
