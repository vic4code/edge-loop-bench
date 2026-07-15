from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import unittest
from pathlib import Path

from edgeloopbench.intercode_gate_manifest import HostSafetyPins
from edgeloopbench.intercode_host_safety import (
    ExpectedHostResources,
    HostSafetyAction,
    HostSafetyPolicy,
    HostSafetyReason,
    HostSafetySample,
    HostTelemetryCollector,
    HostTelemetryError,
    ResidentModel,
)


SHA = "sha256:" + "a" * 64
QWEN_DIGEST = "b" * 64
CONTAINER_ID = "c" * 64


def pins() -> HostSafetyPins:
    return HostSafetyPins(policy_sha256=SHA, telemetry_collector_sha256=SHA)


class FakeRunner:
    def __init__(self, outputs: dict[tuple[str, ...], tuple[int, bytes, bytes]]) -> None:
        self.outputs = dict(outputs)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        key = tuple(argv)
        self.calls.append((key, dict(kwargs)))
        if key not in self.outputs:
            raise AssertionError(f"unexpected command: {key!r}")
        returncode, stdout, stderr = self.outputs[key]
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeUrlOpen:
    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> FakeUrlOpen.Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return self.payload[:limit]

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request: object, timeout: float) -> FakeUrlOpen.Response:
        self.calls.append((request, timeout))
        return self.Response(self.payload)


@dataclasses.dataclass
class FakeStatVfs:
    f_bavail: int = 20_000_000
    f_frsize: int = 4096


def command_outputs(
    *, docker_stdout: bytes = b"", vm_pressure: bytes = b"1\n"
) -> dict[tuple[str, ...], tuple[int, bytes, bytes]]:
    return {
        ("/usr/bin/pmset", "-g", "batt"): (
            0,
            b"Now drawing from 'AC Power'\n"
            b" -InternalBattery-0\t80%; AC attached; not charging present: true\n",
            b"",
        ),
        ("/usr/bin/pmset", "-g", "custom"): (
            0,
            b"Battery Power:\n lowpowermode 1\n sleep 1\n"
            b"AC Power:\n lowpowermode 0\n sleep 1\n",
            b"",
        ),
        ("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"): (
            0,
            vm_pressure,
            b"",
        ),
        ("/usr/sbin/sysctl", "-n", "vm.swapusage"): (
            0,
            b"total = 5120.00M  used = 4454.12M  free = 665.88M  (encrypted)\n",
            b"",
        ),
        ("/usr/bin/memory_pressure", "-Q"): (
            0,
            b"The system has 17179869184 (1048576 pages with a page size of 16384).\n"
            b"System-wide memory free percentage: 47%\n",
            b"",
        ),
        ("/usr/bin/pmset", "-g", "therm"): (
            0,
            b"Note: No thermal warning level has been recorded\n"
            b"Note: No performance warning level has been recorded\n"
            b"Note: No CPU power status has been recorded\n",
            b"",
        ),
        ("/usr/sbin/sysctl", "-n", "kern.boottime"): (
            0,
            b"{ sec = 1784098183, usec = 710968 } Wed Jul 15 14:49:43 2026\n",
            b"",
        ),
        ("/usr/local/bin/docker", "ps", "--quiet", "--no-trunc"): (
            0,
            docker_stdout,
            b"",
        ),
    }


def ollama_payload(*, with_model: bool = True) -> bytes:
    models: list[dict[str, object]] = []
    if with_model:
        models.append(
            {
                "name": "qwen3.5:4b",
                "model": "qwen3.5:4b",
                "digest": QWEN_DIGEST,
                "size": 3_000_000_000,
                "details": {"quantization_level": "Q4_K_M"},
            }
        )
    return json.dumps({"models": models}).encode("utf-8")


def collect(
    *,
    outputs: dict[tuple[str, ...], tuple[int, bytes, bytes]] | None = None,
    ollama: bytes | None = None,
) -> tuple[HostSafetySample, FakeRunner, FakeUrlOpen, list[Path]]:
    runner = FakeRunner(outputs or command_outputs())
    urlopen = FakeUrlOpen(ollama or ollama_payload())
    stat_paths: list[Path] = []

    def statvfs(path: os.PathLike[str]) -> FakeStatVfs:
        stat_paths.append(Path(path))
        return FakeStatVfs()

    collector = HostTelemetryCollector(
        docker_binary=Path("/usr/local/bin/docker"),
        docker_data_path=Path("/Users/test/Library/Containers/com.docker.docker"),
        runner=runner,
        urlopen=urlopen,
        statvfs=statvfs,
        time_ns=lambda: 1_800_000_000_000_000_000,
        monotonic_ns=lambda: 55_000_000_000,
    )
    return collector.collect(), runner, urlopen, stat_paths


def sample(**changes: object) -> HostSafetySample:
    values: dict[str, object] = {
        "captured_unix_ns": 1_800_000_000_000_000_000,
        "captured_monotonic_ns": 55_000_000_000,
        "boot_time_unix_microseconds": 1_784_098_183_710_968,
        "on_ac_power": True,
        "low_power_mode_enabled": False,
        "vm_pressure_level": 1,
        "free_memory_percent": 47,
        "swap_used_bytes": int(4454.12 * 1024 * 1024),
        "thermal_warning": False,
        "performance_warning": False,
        "disk_free_bytes": 20_000_000 * 4096,
        "resident_models": (ResidentModel("qwen3.5:4b", QWEN_DIGEST),),
        "running_container_ids": (CONTAINER_ID,),
    }
    values.update(changes)
    return HostSafetySample(**values)  # type: ignore[arg-type]


def resources() -> ExpectedHostResources:
    return ExpectedHostResources(
        resident_models=(ResidentModel("qwen3.5:4b", QWEN_DIGEST),),
        running_container_ids=(CONTAINER_ID,),
    )


class HostTelemetryCollectorTests(unittest.TestCase):
    def test_collects_strict_bounded_fixed_argv_telemetry(self) -> None:
        value, runner, urlopen, stat_paths = collect(
            outputs=command_outputs(docker_stdout=(CONTAINER_ID + "\n").encode())
        )

        self.assertTrue(value.on_ac_power)
        self.assertFalse(value.low_power_mode_enabled)
        self.assertEqual(value.vm_pressure_level, 1)
        self.assertEqual(value.free_memory_percent, 47)
        self.assertEqual(value.swap_used_bytes, int(4454.12 * 1024 * 1024))
        self.assertFalse(value.thermal_warning)
        self.assertFalse(value.performance_warning)
        self.assertEqual(value.boot_time_unix_microseconds, 1_784_098_183_710_968)
        self.assertEqual(value.running_container_ids, (CONTAINER_ID,))
        self.assertEqual(
            value.resident_models,
            (ResidentModel("qwen3.5:4b", QWEN_DIGEST),),
        )
        self.assertEqual(value.disk_free_bytes, 81_920_000_000)
        self.assertEqual(stat_paths, [Path("/Users/test/Library/Containers/com.docker.docker")])

        self.assertEqual(len(runner.calls), 8)
        for _argv, kwargs in runner.calls:
            self.assertIs(kwargs["shell"], False)
            self.assertIs(kwargs["capture_output"], True)
            self.assertIs(kwargs["check"], False)
            self.assertEqual(kwargs["timeout"], 5.0)
        request, timeout = urlopen.calls[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/ps")
        self.assertEqual(request.method, "GET")
        self.assertEqual(timeout, 5.0)

    def test_record_is_path_free_and_content_addressed(self) -> None:
        value, *_ = collect()
        record = value.to_record()
        self.assertEqual(record["schema"], "edgeloopbench.host-safety-sample.v1")
        self.assertEqual(record["sample_sha256"], value.sha256)
        encoded = json.dumps(record, sort_keys=True)
        self.assertNotIn("Users/test", encoded)
        self.assertNotIn("docker_data_path", encoded)

    def test_probe_failure_oversize_and_bad_parse_fail_closed(self) -> None:
        cases = []
        failed = command_outputs()
        failed[("/usr/bin/memory_pressure", "-Q")] = (1, b"", b"failed")
        cases.append(failed)
        oversized = command_outputs()
        oversized[("/usr/bin/pmset", "-g", "batt")] = (0, b"x" * 65_537, b"")
        cases.append(oversized)
        malformed = command_outputs(vm_pressure=b"green\n")
        cases.append(malformed)

        for outputs in cases:
            with self.subTest(outputs=outputs):
                with self.assertRaises(HostTelemetryError):
                    collect(outputs=outputs)

    def test_network_and_resource_identifiers_are_strict(self) -> None:
        malformed_model = json.dumps(
            {"models": [{"model": "qwen3.5:4b", "digest": "not-a-digest"}]}
        ).encode()
        with self.assertRaises(HostTelemetryError):
            collect(ollama=malformed_model)

        with self.assertRaises(HostTelemetryError):
            collect(outputs=command_outputs(docker_stdout=b"short-id\n"))

    def test_absolute_binary_and_data_paths_are_required(self) -> None:
        with self.assertRaises(ValueError):
            HostTelemetryCollector(
                docker_binary=Path("docker"),
                docker_data_path=Path("/tmp/docker"),
            )
        with self.assertRaises(ValueError):
            HostTelemetryCollector(
                docker_binary=Path("/usr/local/bin/docker"),
                docker_data_path=Path("relative/docker"),
            )

    def test_expected_resources_cannot_bless_two_resident_models(self) -> None:
        with self.assertRaises(ValueError):
            ExpectedHostResources(
                resident_models=(
                    ResidentModel("phi4-mini:3.8b", "a" * 64),
                    ResidentModel("qwen3.5:4b", QWEN_DIGEST),
                )
            )


class HostSafetyPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = HostSafetyPolicy(pins())

    def test_admission_accepts_exact_boundaries_and_expected_resources(self) -> None:
        value = sample(
            free_memory_percent=25,
            disk_free_bytes=32 << 30,
            running_container_ids=(),
        )
        expected = ExpectedHostResources(
            resident_models=value.resident_models,
            running_container_ids=(),
        )
        decision = self.policy.evaluate_admission(value, expected)
        self.assertEqual(decision.action, HostSafetyAction.CONTINUE)
        self.assertEqual(decision.reasons, ())

    def test_admission_rejects_each_frozen_condition(self) -> None:
        cases = {
            HostSafetyReason.AC_POWER_REQUIRED: {"on_ac_power": False},
            HostSafetyReason.LOW_POWER_MODE_ENABLED: {"low_power_mode_enabled": True},
            HostSafetyReason.VM_PRESSURE: {"vm_pressure_level": 2},
            HostSafetyReason.FREE_MEMORY: {"free_memory_percent": 24},
            HostSafetyReason.DISK_SPACE: {"disk_free_bytes": (32 << 30) - 1},
            HostSafetyReason.THERMAL_WARNING: {"thermal_warning": True},
            HostSafetyReason.PERFORMANCE_WARNING: {"performance_warning": True},
            HostSafetyReason.RESIDENT_MODELS: {"resident_models": ()},
            HostSafetyReason.RUNNING_CONTAINERS: {"running_container_ids": ()},
        }
        for reason, changes in cases.items():
            with self.subTest(reason=reason):
                decision = self.policy.evaluate_admission(sample(**changes), resources())
                self.assertEqual(decision.action, HostSafetyAction.STOP)
                self.assertIn(reason, decision.reasons)

    def test_running_uses_growth_not_absolute_swap_and_allows_equal_caps(self) -> None:
        phase = sample(swap_used_bytes=9 << 30)
        block = sample(swap_used_bytes=(9 << 30) + (512 << 20))
        current = sample(
            swap_used_bytes=(9 << 30) + (1 << 30),
            captured_monotonic_ns=85_000_000_000,
        )
        decision = self.policy.evaluate_running(
            current,
            phase_baseline=phase,
            block_baseline=block,
            expected=resources(),
        )
        self.assertEqual(decision.action, HostSafetyAction.CONTINUE)

        too_much = dataclasses.replace(current, swap_used_bytes=current.swap_used_bytes + 1)
        decision = self.policy.evaluate_running(
            too_much,
            phase_baseline=phase,
            block_baseline=block,
            expected=resources(),
        )
        self.assertEqual(decision.action, HostSafetyAction.STOP)
        self.assertIn(HostSafetyReason.PHASE_SWAP_GROWTH, decision.reasons)

    def test_running_guards_power_resources_and_reboot(self) -> None:
        baseline = sample()
        decision = self.policy.evaluate_running(
            sample(on_ac_power=False, low_power_mode_enabled=True),
            phase_baseline=baseline,
            block_baseline=baseline,
            expected=resources(),
        )
        self.assertEqual(decision.action, HostSafetyAction.STOP)
        self.assertIn(HostSafetyReason.AC_POWER_REQUIRED, decision.reasons)
        self.assertIn(HostSafetyReason.LOW_POWER_MODE_ENABLED, decision.reasons)

        rebooted = sample(boot_time_unix_microseconds=baseline.boot_time_unix_microseconds + 1)
        decision = self.policy.evaluate_running(
            rebooted,
            phase_baseline=baseline,
            block_baseline=baseline,
            expected=resources(),
        )
        self.assertEqual(decision.action, HostSafetyAction.RECOVER)
        self.assertEqual(decision.reasons, (HostSafetyReason.BOOT_IDENTITY,))

    def test_cooldown_requires_two_good_samples_at_least_30_seconds_apart(self) -> None:
        start = 55_000_000_000
        first = sample(
            captured_monotonic_ns=start + 30_000_000_000,
            free_memory_percent=20,
            swap_used_bytes=10 << 30,
            resident_models=(),
            running_container_ids=(),
        )
        second = dataclasses.replace(
            first,
            captured_monotonic_ns=first.captured_monotonic_ns + 30_000_000_000,
            swap_used_bytes=first.swap_used_bytes + (64 << 20) - 1,
        )
        expected = ExpectedHostResources()
        decision = self.policy.evaluate_cooldown_pair(
            first,
            second,
            cooldown_started_monotonic_ns=start,
            admission_boot_time_unix_microseconds=first.boot_time_unix_microseconds,
            expected=expected,
        )
        self.assertEqual(decision.action, HostSafetyAction.CONTINUE)

        too_soon = dataclasses.replace(
            second, captured_monotonic_ns=first.captured_monotonic_ns + 29_999_999_999
        )
        decision = self.policy.evaluate_cooldown_pair(
            first,
            too_soon,
            cooldown_started_monotonic_ns=start,
            admission_boot_time_unix_microseconds=first.boot_time_unix_microseconds,
            expected=expected,
        )
        self.assertEqual(decision.action, HostSafetyAction.STOP)
        self.assertIn(HostSafetyReason.SAMPLE_INTERVAL, decision.reasons)

    def test_cooldown_swap_limit_is_strict_and_timeout_or_reboot_stops(self) -> None:
        start = 55_000_000_000
        first = sample(
            captured_monotonic_ns=start,
            swap_used_bytes=10 << 30,
            resident_models=(),
            running_container_ids=(),
        )
        second = dataclasses.replace(
            first,
            captured_monotonic_ns=start + 30_000_000_000,
            swap_used_bytes=first.swap_used_bytes + (64 << 20),
        )
        expected = ExpectedHostResources()
        decision = self.policy.evaluate_cooldown_pair(
            first,
            second,
            cooldown_started_monotonic_ns=start,
            admission_boot_time_unix_microseconds=first.boot_time_unix_microseconds,
            expected=expected,
        )
        self.assertEqual(decision.action, HostSafetyAction.STOP)
        self.assertIn(HostSafetyReason.COOLDOWN_SWAP_GROWTH, decision.reasons)

        timed_out = dataclasses.replace(
            second,
            captured_monotonic_ns=start + 601_000_000_000,
            swap_used_bytes=first.swap_used_bytes,
        )
        decision = self.policy.evaluate_cooldown_pair(
            first,
            timed_out,
            cooldown_started_monotonic_ns=start,
            admission_boot_time_unix_microseconds=first.boot_time_unix_microseconds,
            expected=expected,
        )
        self.assertEqual(decision.action, HostSafetyAction.STOP)
        self.assertIn(HostSafetyReason.COOLDOWN_TIMEOUT, decision.reasons)

        rebooted = dataclasses.replace(
            second,
            boot_time_unix_microseconds=first.boot_time_unix_microseconds + 1,
            swap_used_bytes=first.swap_used_bytes,
        )
        decision = self.policy.evaluate_cooldown_pair(
            first,
            rebooted,
            cooldown_started_monotonic_ns=start,
            admission_boot_time_unix_microseconds=first.boot_time_unix_microseconds,
            expected=expected,
        )
        self.assertEqual(decision.action, HostSafetyAction.RECOVER)
        self.assertEqual(decision.reasons, (HostSafetyReason.BOOT_IDENTITY,))


if __name__ == "__main__":
    unittest.main()
