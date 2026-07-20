from __future__ import annotations

import dataclasses
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edgeloopbench.intercode_host_safety import (
    DockerDaemonIdentity,
    DockerTelemetryPins,
    ExpectedHostResources,
    HostSafetySample,
    HostTelemetryCollector,
    ResidentModel,
    parse_host_safety_sample,
)
from edgeloopbench.intercode_local_model import (
    OLLAMA_GENERATION_ENDPOINT_SHA256,
    OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
)
from edgeloopbench.intercode_v07_host_policy import (
    V07EpisodeHostAdmission,
    V07HostSafetyAction,
    V07HostSafetyDenied,
    V07HostSafetyError,
    V07HostSafetyPolicy,
    V07HostSafetyReason,
    V07_MODEL_PRELOAD_ADMISSION_JOURNAL_REVISION,
    open_v07_preload_stabilized_host_safety_session,
    open_v07_host_safety_session,
)
from edgeloopbench.intercode_v07_manifest import (
    V07HostIdentityPins,
    V07HostSafetyPins,
    _HOST_SAFETY_SEAL,
    _HOST_SAFETY_SOURCES,
)
from edgeloopbench.model_adapter import QWEN35_RAW_PROFILE


SHA = "sha256:" + "a" * 64
OTHER_SHA = "sha256:" + "b" * 64
MODEL_DIGEST = QWEN35_RAW_PROFILE.model_manifest_sha256.removeprefix("sha256:")
CONTAINER_ID = "d" * 64
DOCKER_ENDPOINT = "unix:///tmp/edgeloop-v07-docker.sock"
BOOT_MICROSECONDS = 1_784_098_183_710_968


def host_identity() -> V07HostIdentityPins:
    docker = DockerTelemetryPins(
        endpoint=DOCKER_ENDPOINT,
        client_version="27.3.1",
        server_version="27.3.1",
        binary_sha256=SHA,
    )
    return V07HostIdentityPins(
        docker_binary_sha256=docker.binary_sha256,
        docker_endpoint_sha256=docker.endpoint_sha256,
        docker_client_version=docker.client_version,
        docker_server_version=docker.server_version,
        ollama_runtime_binary_sha256=OTHER_SHA,
        ollama_server_version="0.31.1",
        ollama_launch_environment_sha256=OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
        ollama_generation_endpoint_sha256=OLLAMA_GENERATION_ENDPOINT_SHA256,
    )


def pins() -> V07HostSafetyPins:
    return V07HostSafetyPins(
        host_identity=host_identity(),
        policy_source_sha256=SHA,
        telemetry_collector_source_sha256=OTHER_SHA,
        _construction_seal=_HOST_SAFETY_SEAL,
    )


def expected_resources() -> ExpectedHostResources:
    return ExpectedHostResources(
        resident_models=(ResidentModel("qwen3.5:4b", MODEL_DIGEST),),
        running_container_ids=(),
    )


def daemon_identity() -> DockerDaemonIdentity:
    identity = host_identity()
    return DockerDaemonIdentity(
        binary_sha256=identity.docker_binary_sha256,
        endpoint_sha256=identity.docker_endpoint_sha256,
        client_version=identity.docker_client_version,
        server_version=identity.docker_server_version,
    )


def sample(**changes: object) -> HostSafetySample:
    values: dict[str, object] = {
        "captured_unix_ns": 1_800_000_000_000_000_000,
        "captured_monotonic_ns": 10_000_000_000,
        "boot_time_unix_microseconds": BOOT_MICROSECONDS,
        "on_ac_power": True,
        "low_power_mode_enabled": False,
        "vm_pressure_level": 1,
        "free_memory_percent": 47,
        "swap_used_bytes": 4_096 << 20,
        "thermal_warning": False,
        "performance_warning": False,
        "disk_free_bytes": 64 << 30,
        "resident_models": expected_resources().resident_models,
        "running_container_ids": (),
        "docker_daemon": daemon_identity(),
    }
    values.update(changes)
    return HostSafetySample(**values)  # type: ignore[arg-type]


class V07HostSafetyPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = V07HostSafetyPolicy(pins())

    def test_manifest_policy_role_hash_covers_the_executable_v07_policy(self) -> None:
        self.assertIn(
            "src/edgeloopbench/intercode_v07_host_policy.py",
            _HOST_SAFETY_SOURCES,
        )

    def test_admission_accepts_exact_boundaries_and_manifest_resources(self) -> None:
        value = sample(
            free_memory_percent=25,
            disk_free_bytes=32 << 30,
        )

        decision = self.policy.evaluate_admission(value, expected_resources())

        self.assertEqual(decision.action, V07HostSafetyAction.CONTINUE)
        self.assertEqual(decision.reasons, ())

    def test_admission_rejects_threshold_resource_and_manifest_daemon_drift(self) -> None:
        cases = {
            V07HostSafetyReason.AC_POWER_REQUIRED: {"on_ac_power": False},
            V07HostSafetyReason.LOW_POWER_MODE_ENABLED: {
                "low_power_mode_enabled": True
            },
            V07HostSafetyReason.VM_PRESSURE: {"vm_pressure_level": 2},
            V07HostSafetyReason.FREE_MEMORY: {"free_memory_percent": 24},
            V07HostSafetyReason.DISK_SPACE: {
                "disk_free_bytes": (32 << 30) - 1
            },
            V07HostSafetyReason.THERMAL_WARNING: {"thermal_warning": True},
            V07HostSafetyReason.PERFORMANCE_WARNING: {
                "performance_warning": True
            },
            V07HostSafetyReason.RESIDENT_MODELS: {"resident_models": ()},
            V07HostSafetyReason.RUNNING_CONTAINERS: {
                "running_container_ids": (CONTAINER_ID,)
            },
            V07HostSafetyReason.DOCKER_IDENTITY: {
                "docker_daemon": dataclasses.replace(
                    daemon_identity(),
                    server_version="27.3.2",
                )
            },
        }
        for reason, changes in cases.items():
            with self.subTest(reason=reason):
                decision = self.policy.evaluate_admission(
                    sample(**changes),
                    expected_resources(),
                )
                self.assertEqual(decision.action, V07HostSafetyAction.STOP)
                self.assertIn(reason, decision.reasons)

    def test_running_enforces_phase_and_episode_swap_growth_with_exact_caps(self) -> None:
        phase = sample(swap_used_bytes=4_096 << 20)
        episode = sample(
            captured_monotonic_ns=20_000_000_000,
            swap_used_bytes=(4_096 + 512) << 20,
        )
        at_caps = sample(
            captured_monotonic_ns=30_000_000_000,
            swap_used_bytes=(4_096 + 1_024) << 20,
        )

        accepted = self.policy.evaluate_running(
            at_caps,
            phase_baseline=phase,
            episode_baseline=episode,
            expected=expected_resources(),
        )
        self.assertEqual(accepted.action, V07HostSafetyAction.CONTINUE)

        phase_over = dataclasses.replace(at_caps, swap_used_bytes=at_caps.swap_used_bytes + 1)
        phase_decision = self.policy.evaluate_running(
            phase_over,
            phase_baseline=phase,
            episode_baseline=episode,
            expected=expected_resources(),
        )
        self.assertIn(V07HostSafetyReason.PHASE_SWAP_GROWTH, phase_decision.reasons)

        episode_over = dataclasses.replace(
            at_caps,
            swap_used_bytes=episode.swap_used_bytes + (512 << 20) + 1,
        )
        episode_decision = self.policy.evaluate_running(
            episode_over,
            phase_baseline=phase,
            episode_baseline=episode,
            expected=expected_resources(),
        )
        self.assertIn(
            V07HostSafetyReason.EPISODE_SWAP_GROWTH,
            episode_decision.reasons,
        )

    def test_running_rejects_boot_and_sample_order_before_other_checks(self) -> None:
        phase = sample(captured_monotonic_ns=10)
        episode = sample(captured_monotonic_ns=20)

        rebooted = sample(
            captured_monotonic_ns=30,
            boot_time_unix_microseconds=BOOT_MICROSECONDS + 1,
        )
        reboot = self.policy.evaluate_running(
            rebooted,
            phase_baseline=phase,
            episode_baseline=episode,
            expected=expected_resources(),
        )
        self.assertEqual(reboot.action, V07HostSafetyAction.RECOVER)
        self.assertEqual(reboot.reasons, (V07HostSafetyReason.BOOT_IDENTITY,))

        out_of_order = sample(captured_monotonic_ns=19)
        order = self.policy.evaluate_running(
            out_of_order,
            phase_baseline=phase,
            episode_baseline=episode,
            expected=expected_resources(),
        )
        self.assertEqual(order.action, V07HostSafetyAction.STOP)
        self.assertIn(V07HostSafetyReason.SAMPLE_ORDER, order.reasons)

    def test_cooldown_requires_exact_two_sample_stability_window(self) -> None:
        start = 100_000_000_000
        first = sample(
            captured_monotonic_ns=start + 10_000_000_000,
            free_memory_percent=20,
            swap_used_bytes=6_000 << 20,
        )
        second = dataclasses.replace(
            first,
            captured_monotonic_ns=first.captured_monotonic_ns + 30_000_000_000,
            swap_used_bytes=first.swap_used_bytes + (64 << 20),
        )

        accepted = self.policy.evaluate_cooldown_pair(
            first,
            second,
            cooldown_started_monotonic_ns=start,
            admission_boot_time_unix_microseconds=BOOT_MICROSECONDS,
            expected=expected_resources(),
        )
        self.assertEqual(accepted.action, V07HostSafetyAction.CONTINUE)

        cases = {
            V07HostSafetyReason.SAMPLE_INTERVAL: dataclasses.replace(
                second,
                captured_monotonic_ns=(
                    first.captured_monotonic_ns + 30_000_000_000 - 1
                ),
            ),
            V07HostSafetyReason.COOLDOWN_TIMEOUT: dataclasses.replace(
                second,
                captured_monotonic_ns=start + 600_000_000_000 + 1,
            ),
            V07HostSafetyReason.FREE_MEMORY: dataclasses.replace(
                second,
                free_memory_percent=19,
            ),
            V07HostSafetyReason.COOLDOWN_SWAP_GROWTH: dataclasses.replace(
                second,
                swap_used_bytes=first.swap_used_bytes + (64 << 20) + 1,
            ),
            V07HostSafetyReason.DOCKER_IDENTITY: dataclasses.replace(
                second,
                docker_daemon=dataclasses.replace(
                    daemon_identity(),
                    client_version="27.3.2",
                ),
            ),
            V07HostSafetyReason.RESIDENT_MODELS: dataclasses.replace(
                second,
                resident_models=(),
            ),
        }
        for reason, changed_second in cases.items():
            with self.subTest(reason=reason):
                decision = self.policy.evaluate_cooldown_pair(
                    first,
                    changed_second,
                    cooldown_started_monotonic_ns=start,
                    admission_boot_time_unix_microseconds=BOOT_MICROSECONDS,
                    expected=expected_resources(),
                )
                self.assertEqual(decision.action, V07HostSafetyAction.STOP)
                self.assertIn(reason, decision.reasons)

        rebooted = dataclasses.replace(
            second,
            boot_time_unix_microseconds=BOOT_MICROSECONDS + 1,
        )
        reboot = self.policy.evaluate_cooldown_pair(
            first,
            rebooted,
            cooldown_started_monotonic_ns=start,
            admission_boot_time_unix_microseconds=BOOT_MICROSECONDS,
            expected=expected_resources(),
        )
        self.assertEqual(reboot.action, V07HostSafetyAction.RECOVER)
        self.assertEqual(reboot.reasons, (V07HostSafetyReason.BOOT_IDENTITY,))


@dataclasses.dataclass(frozen=True)
class TelemetryState:
    monotonic_ns: int
    swap_mebibytes: int = 4_096
    free_memory_percent: int = 47
    disk_free_bytes: int = 64 << 30
    boot_microseconds: int = BOOT_MICROSECONDS
    vm_pressure_level: int = 1
    thermal_warning: bool = False
    performance_warning: bool = False
    docker_server_version: str = "27.3.1"
    resident_models: tuple[ResidentModel, ...] = (
        ResidentModel("qwen3.5:4b", MODEL_DIGEST),
    )
    running_container_ids: tuple[str, ...] = ()


class TelemetryScenario:
    class Response:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self) -> TelemetryScenario.Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return self._payload[:limit]

        def geturl(self) -> str:
            return "http://127.0.0.1:11434/api/ps"

    @dataclasses.dataclass
    class Stat:
        f_frsize: int
        f_bavail: int

    def __init__(self, states: tuple[TelemetryState, ...]) -> None:
        self.states = states
        self.index = -1

    @property
    def current(self) -> TelemetryState:
        if self.index < 0 or self.index >= len(self.states):
            raise AssertionError("telemetry sample index is invalid")
        return self.states[self.index]

    def runner(
        self,
        argv: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        key = tuple(argv)
        if key == ("/usr/bin/pmset", "-g", "batt"):
            self.index += 1
            output = b"Now drawing from 'AC Power'\n"
        elif key == ("/usr/bin/pmset", "-g", "custom"):
            output = b"AC Power:\n lowpowermode 0\n"
        elif key == (
            "/usr/sbin/sysctl",
            "-n",
            "kern.memorystatus_vm_pressure_level",
        ):
            output = f"{self.current.vm_pressure_level}\n".encode("ascii")
        elif key == ("/usr/sbin/sysctl", "-n", "vm.swapusage"):
            used = self.current.swap_mebibytes
            output = (
                f"total = 8192.00M  used = {used}.00M  free = 1.00M  "
                "(encrypted)\n"
            ).encode("ascii")
        elif key == ("/usr/bin/memory_pressure", "-Q"):
            output = (
                "The system has 17179869184 bytes.\n"
                f"System-wide memory free percentage: "
                f"{self.current.free_memory_percent}%\n"
            ).encode("ascii")
        elif key == ("/usr/bin/pmset", "-g", "therm"):
            thermal = (
                b"Thermal warning level: 1\n"
                if self.current.thermal_warning
                else b"Note: No thermal warning level has been recorded\n"
            )
            performance = (
                b"Performance warning level: 1\n"
                if self.current.performance_warning
                else b"Note: No performance warning level has been recorded\n"
            )
            output = thermal + performance
        elif key == ("/usr/sbin/sysctl", "-n", "kern.boottime"):
            seconds, micros = divmod(self.current.boot_microseconds, 1_000_000)
            output = f"{{ sec = {seconds}, usec = {micros} }}\n".encode("ascii")
        elif key == (
            "/usr/local/bin/docker",
            "--host",
            DOCKER_ENDPOINT,
            "version",
            "--format",
            "{{json .}}",
        ):
            output = json.dumps(
                {
                    "Client": {"Version": "27.3.1"},
                    "Server": {"Version": self.current.docker_server_version},
                },
                separators=(",", ":"),
            ).encode("ascii")
        elif key == (
            "/usr/local/bin/docker",
            "--host",
            DOCKER_ENDPOINT,
            "container",
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            "status=running",
        ):
            output = "".join(
                f"{container_id}\n"
                for container_id in self.current.running_container_ids
            ).encode("ascii")
        else:
            raise AssertionError(f"unexpected telemetry command: {key!r}")
        return subprocess.CompletedProcess(argv, 0, output, b"")

    def urlopen(self, _request: object, _timeout: float) -> TelemetryScenario.Response:
        payload = json.dumps(
            {
                "models": [
                    {"model": model.model, "digest": model.digest}
                    for model in self.current.resident_models
                ]
            }
        ).encode("ascii")
        return self.Response(payload)

    def statvfs(self, _path: object) -> TelemetryScenario.Stat:
        return self.Stat(1, self.current.disk_free_bytes)

    def time_ns(self) -> int:
        return 1_800_000_000_000_000_000 + self.current.monotonic_ns

    def monotonic_ns(self) -> int:
        return self.current.monotonic_ns


def collector_for(states: tuple[TelemetryState, ...]) -> HostTelemetryCollector:
    scenario = TelemetryScenario(states)
    docker = DockerTelemetryPins(
        endpoint=DOCKER_ENDPOINT,
        client_version="27.3.1",
        server_version="27.3.1",
        binary_sha256=SHA,
    )
    return HostTelemetryCollector(
        docker_binary=Path("/usr/local/bin/docker"),
        docker_data_path=Path("/tmp/docker-data"),
        docker_pins=docker,
        environment={},
        docker_binary_sha256=lambda _path: SHA,
        runner=scenario.runner,
        urlopen=scenario.urlopen,
        statvfs=scenario.statvfs,
        time_ns=scenario.time_ns,
        monotonic_ns=scenario.monotonic_ns,
    )


class V07ModelPreloadAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from edgeloopbench import ollama_loopback_http as http_module
        from edgeloopbench.intercode_v07_runtime_factory import (
            issue_v07_managed_residency_boundary,
            transition_v07_model_residency,
        )
        from tests.test_intercode_v07_runtime_factory import FakeResidencyHttp
        from tests.test_intercode_v07_study_binding import V07PreparedStudyTests

        V07PreparedStudyTests.setUpClass()
        cls._study_fixture = V07PreparedStudyTests
        fake_http = FakeResidencyHttp()
        with mock.patch.object(
            http_module._OLLAMA_HTTP_OPENER,
            "open",
            side_effect=fake_http.open,
        ):
            cls._receipt = transition_v07_model_residency(
                previous=None,
                target=V07PreparedStudyTests.qwen,
                boundary=issue_v07_managed_residency_boundary(
                    V07PreparedStudyTests.runtime.receipt
                ),
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._study_fixture.tearDownClass()

    def _journal_path(self, directory: str, name: str = "calibration-01.jsonl") -> Path:
        root = Path(directory).resolve() / "model-preload-admission"
        root.mkdir(mode=0o700, exist_ok=True)
        return root / name

    def _open(
        self,
        directory: str,
        states: tuple[TelemetryState, ...],
        *,
        sleeper=lambda _seconds: None,
        transition=lambda: None,
        transition_result=None,
    ):
        def perform_transition():  # type: ignore[no-untyped-def]
            transition()
            return self._receipt if transition_result is None else transition_result

        return open_v07_preload_stabilized_host_safety_session(
            pins=pins(),
            collector=collector_for(states),
            previous_expected=ExpectedHostResources(),
            expected=expected_resources(),
            require_live_before=lambda: None,
            perform_transition=perform_transition,
            require_live_runtime=lambda: None,
            expected_runtime_receipt_sha256=(
                self._receipt.runtime_receipt_sha256
            ),
            journal_path=self._journal_path(directory),
            phase="calibration",
            transition_index=1,
            monotonic_ns=lambda: 10_000_000_000,
            sleeper=sleeper,
        )

    def test_waits_only_for_warning_pressure_then_reuses_verified_last_sample(
        self,
    ) -> None:
        target = expected_resources().resident_models
        waited: list[float] = []
        transitions: list[str] = []
        states = (
            TelemetryState(monotonic_ns=1, resident_models=()),
            TelemetryState(
                monotonic_ns=10_000_000_000,
                vm_pressure_level=2,
            ),
            TelemetryState(monotonic_ns=40_000_000_000),
            TelemetryState(
                monotonic_ns=70_000_000_000,
                swap_mebibytes=4_096 + 64,
            ),
        )
        self.assertEqual(states[-1].resident_models, target)

        with tempfile.TemporaryDirectory() as directory:
            session = self._open(
                directory,
                states,
                sleeper=waited.append,
                transition=lambda: transitions.append("loaded"),
            )
            journal = self._journal_path(directory)
            records = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
            self.assertNotIn(directory, json.dumps(records, sort_keys=True))

        self.assertEqual(transitions, ["loaded"])
        self.assertEqual(waited, [30.0, 30.0])
        self.assertEqual(session.phase_baseline.captured_monotonic_ns, 70_000_000_000)
        self.assertEqual(
            [record["type"] for record in records],
            [
                "model_preload_admission_declared",
                "model_preload_transition_baseline",
                "model_preload_admission_started",
                "model_preload_admission_sample",
                "model_preload_admission_sample",
                "model_preload_admission_sample",
                "model_preload_admission_completed",
                "journal_sealed",
            ],
        )
        self.assertEqual(
            records[0]["journal_revision"],
            V07_MODEL_PRELOAD_ADMISSION_JOURNAL_REVISION,
        )
        self.assertEqual(records[3]["admission_reasons"], ["vm_pressure"])
        self.assertIs(records[3]["retryable_denial"], True)
        self.assertEqual(records[5]["pair_action"], "continue")
        self.assertEqual(
            records[6]["accepted_sample_sha256"],
            session.phase_baseline.sha256,
        )
        self.assertEqual(
            parse_host_safety_sample(records[5]["sample"]),
            session.phase_baseline,
        )

    def test_unclean_transition_baseline_prevents_load_and_seals_stop(self) -> None:
        transitions: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal_path(directory)
            with self.assertRaises(V07HostSafetyDenied):
                self._open(
                    directory,
                    (
                        TelemetryState(
                            monotonic_ns=1,
                            resident_models=(),
                            free_memory_percent=24,
                        ),
                    ),
                    transition=lambda: transitions.append("loaded"),
                )
            records = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(transitions, [])
        self.assertEqual(records[-2]["type"], "model_preload_admission_stopped")
        self.assertEqual(records[-2]["stop_reason"], "transition_baseline_denied")
        self.assertEqual(records[-1]["type"], "journal_sealed")

    def test_only_raw_warning_pressure_as_the_sole_reason_is_retryable(self) -> None:
        target = expected_resources().resident_models
        cases = (
            TelemetryState(monotonic_ns=10_000_000_000, vm_pressure_level=0),
            TelemetryState(monotonic_ns=10_000_000_000, vm_pressure_level=3),
            TelemetryState(monotonic_ns=10_000_000_000, vm_pressure_level=4),
            TelemetryState(monotonic_ns=10_000_000_000, free_memory_percent=24),
            TelemetryState(
                monotonic_ns=10_000_000_000,
                vm_pressure_level=2,
                free_memory_percent=24,
            ),
            TelemetryState(
                monotonic_ns=10_000_000_000,
                resident_models=(ResidentModel("unknown:4b", "e" * 64),),
            ),
            TelemetryState(
                monotonic_ns=10_000_000_000,
                running_container_ids=(CONTAINER_ID,),
            ),
            TelemetryState(
                monotonic_ns=10_000_000_000,
                thermal_warning=True,
            ),
            TelemetryState(
                monotonic_ns=10_000_000_000,
                boot_microseconds=BOOT_MICROSECONDS + 1,
            ),
        )
        self.assertEqual(cases[0].resident_models, target)
        for denied in cases:
            with (
                self.subTest(denied=denied),
                tempfile.TemporaryDirectory() as directory,
            ):
                waited: list[float] = []
                with self.assertRaises((V07HostSafetyDenied, V07HostSafetyError)):
                    self._open(
                        directory,
                        (
                            TelemetryState(monotonic_ns=1, resident_models=()),
                            denied,
                        ),
                        sleeper=waited.append,
                    )
                records = [
                    json.loads(line)
                    for line in self._journal_path(directory).read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]

            self.assertEqual(waited, [])
            self.assertIs(records[3]["retryable_denial"], False)
            self.assertEqual(records[-2]["stop_reason"], "hard_denial")

    def test_non_builder_transition_receipt_is_rejected_and_sealed(self) -> None:
        forged = object.__new__(type(self._receipt))
        for field in dataclasses.fields(self._receipt):
            object.__setattr__(
                forged,
                field.name,
                getattr(self._receipt, field.name),
            )
        for transition_result in (self._receipt.canonical_record(), forged):
            with (
                self.subTest(result_type=type(transition_result).__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                with self.assertRaises(V07HostSafetyError):
                    self._open(
                        directory,
                        (TelemetryState(monotonic_ns=1, resident_models=()),),
                        transition_result=transition_result,
                    )
                records = [
                    json.loads(line)
                    for line in self._journal_path(directory).read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]

            self.assertEqual(records[-2]["stop_reason"], "transition_failed")
            self.assertEqual(records[-1]["type"], "journal_sealed")

    def test_preexisting_journal_is_rejected_before_telemetry_or_transition(
        self,
    ) -> None:
        transitions: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            journal = self._journal_path(directory)
            journal.write_bytes(b"")
            journal.chmod(0o600)
            with self.assertRaises(V07HostSafetyError):
                self._open(
                    directory,
                    (TelemetryState(monotonic_ns=1, resident_models=()),),
                    transition=lambda: transitions.append("loaded"),
                )

        self.assertEqual(transitions, [])

    def test_swap_caps_are_hard_denials_before_a_session_is_returned(self) -> None:
        cases = (
            (
                TelemetryState(
                    monotonic_ns=10_000_000_000,
                    swap_mebibytes=4_096 + 1_024,
                ),
                TelemetryState(
                    monotonic_ns=40_000_000_000,
                    swap_mebibytes=4_096 + 1_025,
                ),
                "phase_swap_growth",
            ),
            (
                TelemetryState(
                    monotonic_ns=10_000_000_000,
                    swap_mebibytes=4_096 + 900,
                ),
                TelemetryState(
                    monotonic_ns=40_000_000_000,
                    swap_mebibytes=4_096 + 965,
                ),
                "cooldown_swap_growth",
            ),
        )
        for first, second, expected_reason in cases:
            with (
                self.subTest(reason=expected_reason),
                tempfile.TemporaryDirectory() as directory,
            ):
                with self.assertRaises(V07HostSafetyDenied) as raised:
                    self._open(
                        directory,
                        (
                            TelemetryState(monotonic_ns=1, resident_models=()),
                            first,
                            second,
                        ),
                    )
                records = [
                    json.loads(line)
                    for line in self._journal_path(directory).read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]

            self.assertIn(
                expected_reason,
                [reason.value for reason in raised.exception.decision.reasons],
            )
            self.assertEqual(records[-2]["stop_reason"], "hard_denial")

    def test_first_clean_sample_over_transition_swap_cap_is_not_waitable(self) -> None:
        for pressure_level in (1, 2):
            with (
                self.subTest(pressure_level=pressure_level),
                tempfile.TemporaryDirectory() as directory,
            ):
                waited: list[float] = []
                with self.assertRaises(V07HostSafetyDenied) as raised:
                    self._open(
                        directory,
                        (
                            TelemetryState(monotonic_ns=1, resident_models=()),
                            TelemetryState(
                                monotonic_ns=10_000_000_000,
                                vm_pressure_level=pressure_level,
                                swap_mebibytes=4_096 + 1_024 + 1,
                            ),
                        ),
                        sleeper=waited.append,
                    )
                records = [
                    json.loads(line)
                    for line in self._journal_path(directory).read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]

            self.assertEqual(waited, [])
            self.assertEqual(
                raised.exception.decision.reasons,
                (V07HostSafetyReason.PHASE_SWAP_GROWTH,),
            )
            self.assertIs(records[3]["retryable_denial"], False)
            self.assertEqual(records[3]["allowed_streak"], 0)
            self.assertEqual(records[-2]["stop_reason"], "hard_denial")

    def test_collectable_telemetry_failures_stop_and_seal_the_journal(self) -> None:
        cases = (
            ((), "transition_baseline_collection_failed", False),
            (
                (TelemetryState(monotonic_ns=1, resident_models=()),),
                "sample_collection_failed",
                True,
            ),
            (
                (
                    TelemetryState(monotonic_ns=1, resident_models=()),
                    TelemetryState(
                        monotonic_ns=10_000_000_000,
                        docker_server_version="27.3.2",
                    ),
                ),
                "sample_collection_failed",
                True,
            ),
        )
        for states, expected_stop, expected_transition in cases:
            with (
                self.subTest(stop=expected_stop),
                tempfile.TemporaryDirectory() as directory,
            ):
                transitions: list[str] = []
                with self.assertRaises(V07HostSafetyError):
                    self._open(
                        directory,
                        states,
                        transition=lambda: transitions.append("loaded"),
                    )
                records = [
                    json.loads(line)
                    for line in self._journal_path(directory).read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]

            self.assertEqual(transitions, ["loaded"] if expected_transition else [])
            self.assertEqual(records[-2]["stop_reason"], expected_stop)
            self.assertEqual(records[-1]["type"], "journal_sealed")

    def test_persistent_warning_pressure_times_out_after_twenty_waits(self) -> None:
        pressure = tuple(
            TelemetryState(
                monotonic_ns=(10 + seconds) * 1_000_000_000,
                vm_pressure_level=2,
            )
            for seconds in range(0, 601, 30)
        )
        waited: list[float] = []
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(V07HostSafetyError):
                self._open(
                    directory,
                    (
                        TelemetryState(monotonic_ns=1, resident_models=()),
                        *pressure,
                    ),
                    sleeper=waited.append,
                )
            records = [
                json.loads(line)
                for line in self._journal_path(directory).read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertEqual(waited, [30.0] * 20)
        self.assertEqual(records[-2]["stop_reason"], "timeout")
        self.assertEqual(records[-2]["sample_count"], 21)
        self.assertEqual(records[-1]["type"], "journal_sealed")
class V07HostSafetySessionTests(unittest.TestCase):
    def test_phase_requires_one_preloaded_exact_resident_model(self) -> None:
        collector = collector_for((TelemetryState(monotonic_ns=10),))

        with self.assertRaisesRegex(V07HostSafetyError, "preloaded resident model"):
            open_v07_host_safety_session(
                pins=pins(),
                collector=collector,
                expected=ExpectedHostResources(),
                require_live_runtime=lambda: None,
            )

        for expected in (
            ExpectedHostResources(
                resident_models=(ResidentModel("gemma3:4b", "e" * 64),)
            ),
            ExpectedHostResources(
                resident_models=expected_resources().resident_models,
                running_container_ids=(CONTAINER_ID,),
            ),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(
                    V07HostSafetyError,
                    "preloaded resident model|running containers",
                ):
                    open_v07_host_safety_session(
                        pins=pins(),
                        collector=collector,
                        expected=expected,
                        require_live_runtime=lambda: None,
                    )

    def test_session_owns_real_collector_and_issues_before_after_evidence(self) -> None:
        live_checks: list[str] = []
        collector = collector_for(
            (
                TelemetryState(monotonic_ns=10),
                TelemetryState(monotonic_ns=20, swap_mebibytes=4_100),
                TelemetryState(monotonic_ns=30, swap_mebibytes=4_200),
            )
        )
        session = open_v07_host_safety_session(
            pins=pins(),
            collector=collector,
            expected=expected_resources(),
            require_live_runtime=lambda: live_checks.append("checked"),
        )
        initial_baseline = session.phase_baseline
        with self.assertRaisesRegex(V07HostSafetyError, "already set"):
            session._start()  # noqa: SLF001 - regression probe for reseeding
        self.assertIs(session.phase_baseline, initial_baseline)

        admission = session.issue_episode_admission()
        before = admission.before_episode_admission()
        after = admission.after_episode_admission()

        self.assertIs(type(admission), V07EpisodeHostAdmission)
        self.assertEqual(before.captured_monotonic_ns, 20)
        self.assertEqual(after.captured_monotonic_ns, 30)
        self.assertEqual(admission.evidence.before, before)
        self.assertEqual(admission.evidence.after, after)
        self.assertEqual(admission.evidence.phase_baseline_sha256, session.phase_baseline.sha256)
        self.assertEqual(len(live_checks), 6)

    def test_denied_before_sample_prevents_caller_work(self) -> None:
        work_called = False
        session = open_v07_host_safety_session(
            pins=pins(),
            collector=collector_for(
                (
                    TelemetryState(monotonic_ns=10),
                    TelemetryState(monotonic_ns=20, free_memory_percent=24),
                )
            ),
            expected=expected_resources(),
            require_live_runtime=lambda: None,
        )
        admission = session.issue_episode_admission()

        def caller_work() -> None:
            nonlocal work_called
            work_called = True

        with self.assertRaises(V07HostSafetyDenied):
            admission.execute(caller_work)

        self.assertFalse(work_called)

    def test_after_sample_enforces_episode_growth_and_invalidates_session(self) -> None:
        session = open_v07_host_safety_session(
            pins=pins(),
            collector=collector_for(
                (
                    TelemetryState(monotonic_ns=10),
                    TelemetryState(monotonic_ns=20),
                    TelemetryState(
                        monotonic_ns=30,
                        swap_mebibytes=4_096 + 513,
                    ),
                )
            ),
            expected=expected_resources(),
            require_live_runtime=lambda: None,
        )
        admission = session.issue_episode_admission()
        admission.before_episode_admission()

        with self.assertRaises(V07HostSafetyDenied) as raised:
            admission.after_episode_admission()

        self.assertIn(
            V07HostSafetyReason.EPISODE_SWAP_GROWTH,
            raised.exception.decision.reasons,
        )
        with self.assertRaises(V07HostSafetyError):
            session.issue_episode_admission()

    def test_failed_episode_can_collect_exact_runtime_bound_cooldown_evidence(self) -> None:
        live_checks: list[str] = []
        waited: list[float] = []
        session = open_v07_host_safety_session(
            pins=pins(),
            collector=collector_for(
                (
                    TelemetryState(monotonic_ns=10_000_000_000),
                    TelemetryState(monotonic_ns=20_000_000_000),
                    TelemetryState(
                        monotonic_ns=30_000_000_000,
                        swap_mebibytes=4_096 + 513,
                    ),
                    TelemetryState(
                        monotonic_ns=40_000_000_000,
                        swap_mebibytes=5_000,
                        free_memory_percent=20,
                    ),
                    TelemetryState(
                        monotonic_ns=70_000_000_000,
                        swap_mebibytes=5_064,
                        free_memory_percent=20,
                    ),
                )
            ),
            expected=expected_resources(),
            require_live_runtime=lambda: live_checks.append("checked"),
        )
        admission = session.issue_episode_admission()
        admission.before_episode_admission()
        with self.assertRaises(V07HostSafetyDenied):
            admission.after_episode_admission()

        evidence = session.collect_cooldown_evidence(
            sleeper=lambda seconds: waited.append(seconds)
        )

        self.assertEqual(waited, [30])
        self.assertEqual(evidence.first.captured_monotonic_ns, 40_000_000_000)
        self.assertEqual(evidence.second.captured_monotonic_ns, 70_000_000_000)
        self.assertEqual(
            evidence.cooldown_started_monotonic_ns,
            30_000_000_000,
        )
        self.assertEqual(len(live_checks), 10)
        with self.assertRaises(V07HostSafetyError):
            session.issue_episode_admission()

    def test_runtime_failure_and_out_of_order_hook_calls_fail_closed(self) -> None:
        def not_live() -> None:
            raise RuntimeError("private runtime detail")

        with self.assertRaisesRegex(V07HostSafetyError, "runtime liveness"):
            open_v07_host_safety_session(
                pins=pins(),
                collector=collector_for((TelemetryState(monotonic_ns=10),)),
                expected=expected_resources(),
                require_live_runtime=not_live,
            )

        session = open_v07_host_safety_session(
            pins=pins(),
            collector=collector_for(
                (
                    TelemetryState(monotonic_ns=10),
                    TelemetryState(monotonic_ns=20),
                )
            ),
            expected=expected_resources(),
            require_live_runtime=lambda: None,
        )
        admission = session.issue_episode_admission()
        with self.assertRaisesRegex(V07HostSafetyError, "before"):
            admission.after_episode_admission()
        with self.assertRaises(V07HostSafetyError):
            admission.before_episode_admission()


if __name__ == "__main__":
    unittest.main()
