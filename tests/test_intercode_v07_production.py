from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import edgeloopbench.intercode_v07_production as production_module
from edgeloopbench.intercode_gate_manifest import HostSafetyPins
from edgeloopbench.intercode_host_safety import (
    DockerDaemonIdentity,
    DockerTelemetryPins,
    HostSafetySample,
    parse_host_safety_sample,
)
from edgeloopbench.intercode_v07_production import (
    V07ProductionConfig,
    V07ProductionError,
    V07ProductionPreflight,
    execute_v07_production,
    inspect_v07_production_preflight,
)
from edgeloopbench.journal import inspect_journal


class _Disk:
    f_frsize = 4096
    f_bavail = 9_000_000


SHA = "sha256:" + "a" * 64
ENDPOINT_SHA = "sha256:" + "b" * 64
CONTAINER_ID = "c" * 64
SECOND_CONTAINER_ID = "d" * 64
STEWARDED_CONTAINER_IDS = (CONTAINER_ID, SECOND_CONTAINER_ID)


class _Samples:
    def __init__(self, samples: list[HostSafetySample]) -> None:
        self.samples = iter(samples)
        self.calls = 0

    def collect(self) -> HostSafetySample:
        self.calls += 1
        return next(self.samples)


def _sample(
    seconds: int,
    *,
    running: tuple[str, ...] = (),
    disk_free_bytes: int = 40 << 30,
    vm_pressure_level: int = 1,
) -> HostSafetySample:
    return HostSafetySample(
        captured_unix_ns=1_800_000_000_000_000_000 + seconds * 1_000_000_000,
        captured_monotonic_ns=seconds * 1_000_000_000,
        boot_time_unix_microseconds=1_780_000_000_000_000,
        on_ac_power=True,
        low_power_mode_enabled=False,
        vm_pressure_level=vm_pressure_level,
        free_memory_percent=50,
        swap_used_bytes=1 << 30,
        thermal_warning=False,
        performance_warning=False,
        disk_free_bytes=disk_free_bytes,
        resident_models=(),
        running_container_ids=running,
        docker_daemon=DockerDaemonIdentity(
            binary_sha256=SHA,
            endpoint_sha256=ENDPOINT_SHA,
            client_version="27.3.1",
            server_version="27.3.1",
        ),
    )


def _pins() -> HostSafetyPins:
    return HostSafetyPins(
        policy_sha256=SHA,
        telemetry_collector_sha256=SHA,
        docker_binary_sha256=SHA,
        docker_endpoint_sha256=ENDPOINT_SHA,
        docker_client_version="27.3.1",
        docker_server_version="27.3.1",
    )


class V07ImageAdmissionStabilizationTests(unittest.TestCase):
    def test_records_denial_then_requires_two_interval_separated_allowed_samples(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "image-build-admission.jsonl"
            collector = _Samples(
                [
                    _sample(0, running=(CONTAINER_ID,)),
                    _sample(30),
                    _sample(60),
                ]
            )
            sleeps: list[float] = []

            accepted = production_module._await_image_build_admission(
                collector,
                _pins(),
                journal,
                stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                require_live_runtime=lambda: None,
                sleep=sleeps.append,
            )

            records = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
            self.assertNotIn(directory, json.dumps(records, sort_keys=True))

        self.assertEqual(accepted.sha256, _sample(60).sha256)
        self.assertEqual(collector.calls, 3)
        self.assertEqual(sleeps, [30.0, 30.0])
        self.assertEqual(
            [record["type"] for record in records],
            [
                "image_build_admission_declared",
                "image_build_admission_sample",
                "image_build_admission_sample",
                "image_build_admission_sample",
                "image_build_admission_completed",
                "journal_sealed",
            ],
        )
        self.assertEqual(records[1]["admission_reasons"], ["running_containers"])
        self.assertEqual(records[0]["retryable_vm_pressure_levels"], [2])
        self.assertEqual(records[3]["pair_action"], "continue")
        self.assertEqual(records[4]["accepted_sample_sha256"], accepted.sha256)
        self.assertEqual(parse_host_safety_sample(records[3]["sample"]), accepted)

    def test_pressure_denials_reset_the_candidate_and_retry_until_two_clean_samples(
        self,
    ) -> None:
        cases = (
            (
                _sample(30, vm_pressure_level=2),
                ["vm_pressure"],
            ),
            (
                _sample(
                    30,
                    running=(CONTAINER_ID,),
                    vm_pressure_level=2,
                ),
                ["vm_pressure", "running_containers"],
            ),
        )
        for denial, expected_reasons in cases:
            with (
                self.subTest(reasons=expected_reasons),
                tempfile.TemporaryDirectory() as directory,
            ):
                collector = _Samples(
                    [
                        _sample(0),
                        denial,
                        _sample(60),
                        _sample(90),
                    ]
                )
                sleeps: list[float] = []
                journal = Path(directory) / "image-build-admission.jsonl"

                accepted = production_module._await_image_build_admission(
                    collector,
                    _pins(),
                    journal,
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                    require_live_runtime=lambda: None,
                    sleep=sleeps.append,
                )

                records = [
                    json.loads(line)
                    for line in journal.read_text(encoding="utf-8").splitlines()
                ]
                denial_record = records[2]

                self.assertEqual(accepted.sha256, _sample(90).sha256)
                self.assertEqual(collector.calls, 4)
                self.assertEqual(sleeps, [30.0, 30.0, 30.0])
                self.assertIs(denial_record["retryable_denial"], True)
                self.assertEqual(
                    denial_record["admission_reasons"],
                    expected_reasons,
                )
                self.assertEqual(denial_record["allowed_streak"], 0)
                self.assertEqual(
                    records[3].get("candidate_sample_sha256"),
                    None,
                )

    def test_only_warning_pressure_level_is_retryable_with_or_without_known_containers(
        self,
    ) -> None:
        for pressure_level in (0, 2, 3, 4):
            for running in ((), (CONTAINER_ID,)):
                expected_retryable = pressure_level == 2
                with (
                    self.subTest(
                        pressure_level=pressure_level,
                        running=bool(running),
                    ),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    collector = _Samples(
                        [
                            _sample(
                                0,
                                running=running,
                                vm_pressure_level=pressure_level,
                            ),
                            _sample(30),
                            _sample(60),
                        ]
                    )
                    sleeps: list[float] = []
                    journal = Path(directory) / "image-build-admission.jsonl"

                    if expected_retryable:
                        accepted = production_module._await_image_build_admission(
                            collector,
                            _pins(),
                            journal,
                            stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                            require_live_runtime=lambda: None,
                            sleep=sleeps.append,
                        )
                        self.assertEqual(accepted.sha256, _sample(60).sha256)
                        self.assertEqual(collector.calls, 3)
                        self.assertEqual(sleeps, [30.0, 30.0])
                    else:
                        with self.assertRaises(V07ProductionError):
                            production_module._await_image_build_admission(
                                collector,
                                _pins(),
                                journal,
                                stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                                require_live_runtime=lambda: None,
                                sleep=sleeps.append,
                            )
                        self.assertEqual(collector.calls, 1)
                        self.assertEqual(sleeps, [])

                    records = [
                        json.loads(line)
                        for line in journal.read_text(encoding="utf-8").splitlines()
                    ]
                    self.assertIs(
                        records[1]["retryable_denial"],
                        expected_retryable,
                    )
                    if not expected_retryable:
                        self.assertEqual(records[-2]["stop_reason"], "hard_denial")

    def test_hard_denial_and_timeout_seal_without_returning_a_baseline(self) -> None:
        cases = (
            ([_sample(0, disk_free_bytes=1)], "hard_denial", 1),
            (
                [
                    _sample(0, running=(CONTAINER_ID,)),
                    _sample(601, running=(CONTAINER_ID,)),
                ],
                "timeout",
                2,
            ),
        )
        for samples, expected_stop, expected_calls in cases:
            with (
                self.subTest(stop=expected_stop),
                tempfile.TemporaryDirectory() as directory,
            ):
                journal = Path(directory) / "image-build-admission.jsonl"
                collector = _Samples(samples)
                sleeps: list[float] = []
                with self.assertRaises(V07ProductionError):
                    production_module._await_image_build_admission(
                        collector,
                        _pins(),
                        journal,
                        stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                        require_live_runtime=lambda: None,
                        sleep=sleeps.append,
                    )
                records = [
                    json.loads(line)
                    for line in journal.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(collector.calls, expected_calls)
                self.assertEqual(records[-2]["type"], "image_build_admission_stopped")
                self.assertEqual(records[-2]["stop_reason"], expected_stop)
                self.assertEqual(records[-1]["type"], "journal_sealed")

    def test_preexisting_journal_is_rejected_before_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "image-build-admission.jsonl"
            journal.write_bytes(b"")
            journal.chmod(0o600)
            collector = _Samples([_sample(0)])

            with self.assertRaises(V07ProductionError):
                production_module._await_image_build_admission(
                    collector,
                    _pins(),
                    journal,
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                    require_live_runtime=lambda: None,
                    sleep=lambda _seconds: None,
                )

        self.assertEqual(collector.calls, 0)

    def test_unknown_mixed_or_second_reason_is_never_retryable(self) -> None:
        unknown = "e" * 64
        cases = (
            _sample(0, running=(unknown,)),
            _sample(0, running=(CONTAINER_ID, unknown)),
            _sample(
                0,
                running=(unknown,),
                vm_pressure_level=2,
            ),
            _sample(0, vm_pressure_level=2, disk_free_bytes=1),
        )
        for first in cases:
            with (
                self.subTest(reasons=first.sha256),
                tempfile.TemporaryDirectory() as directory,
            ):
                collector = _Samples([first])
                sleeps: list[float] = []
                journal = Path(directory) / "image-build-admission.jsonl"

                with self.assertRaises(V07ProductionError):
                    production_module._await_image_build_admission(
                        collector,
                        _pins(),
                        journal,
                        stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                        require_live_runtime=lambda: None,
                        sleep=sleeps.append,
                    )

                records = [
                    json.loads(line)
                    for line in journal.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(sleeps, [])
                self.assertIs(records[1]["retryable_denial"], False)
                self.assertEqual(records[-2]["stop_reason"], "hard_denial")

    def test_container_restart_resets_the_clean_sample_streak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            collector = _Samples(
                [
                    _sample(0),
                    _sample(30, running=(CONTAINER_ID,)),
                    _sample(60),
                    _sample(90),
                ]
            )
            sleeps: list[float] = []

            accepted = production_module._await_image_build_admission(
                collector,
                _pins(),
                Path(directory) / "image-build-admission.jsonl",
                stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                require_live_runtime=lambda: None,
                sleep=sleeps.append,
            )

        self.assertEqual(accepted.sha256, _sample(90).sha256)
        self.assertEqual(collector.calls, 4)
        self.assertEqual(sleeps, [30.0, 30.0, 30.0])

    def test_timeout_collects_no_sample_after_the_exact_boundary(self) -> None:
        samples = [
            _sample(seconds, running=(CONTAINER_ID,))
            for seconds in range(0, 631, 30)
        ]
        with tempfile.TemporaryDirectory() as directory:
            collector = _Samples(samples)
            sleeps: list[float] = []

            with self.assertRaises(V07ProductionError):
                production_module._await_image_build_admission(
                    collector,
                    _pins(),
                    Path(directory) / "image-build-admission.jsonl",
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                    require_live_runtime=lambda: None,
                    sleep=sleeps.append,
                )

        self.assertEqual(collector.calls, 21)
        self.assertEqual(len(sleeps), 20)

    def test_persistent_warning_pressure_times_out_at_the_exact_boundary(
        self,
    ) -> None:
        samples = [
            _sample(seconds, vm_pressure_level=2)
            for seconds in range(0, 631, 30)
        ]
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "image-build-admission.jsonl"
            collector = _Samples(samples)
            sleeps: list[float] = []

            with self.assertRaises(V07ProductionError):
                production_module._await_image_build_admission(
                    collector,
                    _pins(),
                    journal,
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                    require_live_runtime=lambda: None,
                    sleep=sleeps.append,
                )
            records = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]

        sample_records = records[1:-2]
        self.assertEqual(collector.calls, 21)
        self.assertEqual(len(sleeps), 20)
        self.assertEqual(len(sample_records), 21)
        self.assertTrue(
            all(record["retryable_denial"] for record in sample_records)
        )
        self.assertEqual(records[-2]["stop_reason"], "timeout")

    def test_path_replacement_fails_closed_before_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "image-build-admission.jsonl"
            collector = _Samples([_sample(0), _sample(30)])

            def replace_journal(_seconds: float) -> None:
                journal.unlink()
                journal.write_bytes(b"")
                journal.chmod(0o600)

            with self.assertRaises(V07ProductionError):
                production_module._await_image_build_admission(
                    collector,
                    _pins(),
                    journal,
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                    require_live_runtime=lambda: None,
                    sleep=replace_journal,
                )

        self.assertEqual(collector.calls, 2)

    def test_runtime_liveness_failure_seals_without_collecting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "image-build-admission.jsonl"
            collector = _Samples([_sample(0)])

            def unavailable() -> None:
                raise RuntimeError("gone")

            with self.assertRaises(V07ProductionError):
                production_module._await_image_build_admission(
                    collector,
                    _pins(),
                    journal,
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                    require_live_runtime=unavailable,
                    sleep=lambda _seconds: None,
                )
            records = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(collector.calls, 0)
        self.assertEqual(records[-2]["stop_reason"], "runtime_liveness_error")
        self.assertEqual(records[-1]["type"], "journal_sealed")

    def test_runtime_liveness_is_rechecked_after_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "image-build-admission.jsonl"
            collector = _Samples([_sample(0)])
            liveness_calls = 0

            def fail_after_collect() -> None:
                nonlocal liveness_calls
                liveness_calls += 1
                if liveness_calls == 2:
                    raise RuntimeError("gone after collect")

            with self.assertRaises(V07ProductionError):
                production_module._await_image_build_admission(
                    collector,
                    _pins(),
                    journal,
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                    require_live_runtime=fail_after_collect,
                    sleep=lambda _seconds: None,
                )
            records = [
                json.loads(line)
                for line in journal.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(liveness_calls, 2)
        self.assertEqual(collector.calls, 1)
        self.assertEqual(
            [record["type"] for record in records],
            [
                "image_build_admission_declared",
                "image_build_admission_stopped",
                "journal_sealed",
            ],
        )
        self.assertEqual(records[-2]["sample_count"], 0)

    def test_sealed_verifier_rejects_unknown_domain_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "image-build-admission.jsonl"
            collector = _Samples([_sample(0), _sample(30)])
            original = production_module._read_private_admission_records

            def inject_unknown(path, identity):  # type: ignore[no-untyped-def]
                records, inspection = original(path, identity)
                records[0]["undeclared_extra"] = True
                return records, inspection

            with (
                mock.patch.object(
                    production_module,
                    "_read_private_admission_records",
                    side_effect=inject_unknown,
                ),
                self.assertRaises(V07ProductionError),
            ):
                production_module._await_image_build_admission(
                    collector,
                    _pins(),
                    journal,
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                    require_live_runtime=lambda: None,
                    sleep=lambda _seconds: None,
                )

        self.assertEqual(collector.calls, 2)

    def test_sealed_verifier_requires_exact_retryable_pressure_declaration(
        self,
    ) -> None:
        for replacement in ("missing", [2, 3]):
            with (
                self.subTest(replacement=replacement),
                tempfile.TemporaryDirectory() as directory,
            ):
                journal = Path(directory) / "image-build-admission.jsonl"
                collector = _Samples([_sample(0), _sample(30)])
                original = production_module._read_private_admission_records

                def tamper(path, identity):  # type: ignore[no-untyped-def]
                    records, inspection = original(path, identity)
                    if replacement == "missing":
                        records[0].pop("retryable_vm_pressure_levels", None)
                    else:
                        records[0]["retryable_vm_pressure_levels"] = replacement
                    return records, inspection

                with (
                    mock.patch.object(
                        production_module,
                        "_read_private_admission_records",
                        side_effect=tamper,
                    ),
                    self.assertRaises(V07ProductionError),
                ):
                    production_module._await_image_build_admission(
                        collector,
                        _pins(),
                        journal,
                        stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                        require_live_runtime=lambda: None,
                        sleep=lambda _seconds: None,
                    )

                self.assertEqual(collector.calls, 2)

    def test_sealed_verifier_rejects_same_inode_tail_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "image-build-admission.jsonl"
            collector = _Samples([_sample(0), _sample(30)])
            original = production_module._read_bounded_admission_snapshot
            reads = 0

            def mutate_after_second_read(descriptor):  # type: ignore[no-untyped-def]
                nonlocal reads
                payload = original(descriptor)
                reads += 1
                if reads == 2:
                    with journal.open("ab") as stream:
                        stream.write(b"corrupt-tail")
                        stream.flush()
                return payload

            with (
                mock.patch.object(
                    production_module,
                    "_read_bounded_admission_snapshot",
                    side_effect=mutate_after_second_read,
                ),
                self.assertRaises(V07ProductionError),
            ):
                production_module._await_image_build_admission(
                    collector,
                    _pins(),
                    journal,
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                    require_live_runtime=lambda: None,
                    sleep=lambda _seconds: None,
                )

        self.assertEqual(reads, 3)
        self.assertEqual(collector.calls, 2)

    def test_sealed_verifier_rejects_continuation_after_pair_denial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "image-build-admission.jsonl"
            identity = production_module._declare_private_journal(journal)
            pins = _pins()
            production_module._append_admission_event(
                journal,
                {
                    "type": "image_build_admission_declared",
                    "expected_resources": {
                        "resident_models": [],
                        "running_container_ids": [],
                    },
                    "journal_revision": (
                        production_module.V07_IMAGE_ADMISSION_JOURNAL_REVISION
                    ),
                    "journal_instance_id": "0" * 32,
                    "policy_sha256": pins.policy_sha256,
                    "runner_revision": (
                        production_module.V07_PRODUCTION_RUNNER_REVISION
                    ),
                    "sample_interval_seconds": pins.sample_interval_seconds,
                    "required_consecutive_samples": (
                        pins.cooldown_consecutive_samples
                    ),
                    "retryable_vm_pressure_levels": [2],
                    "stewarded_container_ids": list(STEWARDED_CONTAINER_IDS),
                    "telemetry_collector_sha256": (
                        pins.telemetry_collector_sha256
                    ),
                    "timeout_seconds": pins.cooldown_timeout_seconds,
                },
                identity,
            )
            policy = production_module.HostSafetyPolicy(pins)
            expected = production_module.ExpectedHostResources()
            samples = (_sample(0), _sample(10), _sample(40))
            candidate = None
            for sample in samples:
                admission = policy.evaluate_admission(sample, expected)
                pair = None
                if candidate is not None:
                    pair = policy.evaluate_cooldown_pair(
                        candidate,
                        sample,
                        cooldown_started_monotonic_ns=0,
                        admission_boot_time_unix_microseconds=(
                            sample.boot_time_unix_microseconds
                        ),
                        expected=expected,
                    )
                event = {
                    "type": "image_build_admission_sample",
                    "sample": sample.to_record(),
                    "admission_action": admission.action.value,
                    "admission_reasons": [],
                    "retryable_denial": False,
                    "allowed_streak": (
                        2 if pair is not None and pair.allowed else 1
                    ),
                }
                if candidate is not None:
                    event["candidate_sample_sha256"] = candidate.sha256
                if pair is not None:
                    event["pair_action"] = pair.action.value
                    event["pair_reasons"] = [
                        reason.value for reason in pair.reasons
                    ]
                production_module._append_admission_event(
                    journal,
                    event,
                    identity,
                )
                candidate = sample
            production_module._append_admission_event(
                journal,
                {
                    "type": "image_build_admission_completed",
                    "accepted_sample_sha256": samples[-1].sha256,
                    "sample_count": 3,
                },
                identity,
            )
            production_module._seal_admission_journal(journal, identity)

            with self.assertRaises(V07ProductionError):
                production_module._verify_completed_admission_journal(
                    journal,
                    identity,
                    pins=pins,
                    stewarded_container_ids=STEWARDED_CONTAINER_IDS,
                )


class V07ProductionPreflightTests(unittest.TestCase):
    def _production_fixture(
        self,
        root: Path,
        *,
        stewarded_container_ids: tuple[str, ...] = (),
    ) -> V07ProductionConfig:
        executables = []
        for name in ("docker", "ollama", "llama-tokenize"):
            path = root / name
            path.write_bytes(name.encode("ascii"))
            path.chmod(0o755)
            executables.append(path)
        (root / "llama-tokenize.provenance.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        models = root / "models"
        models.mkdir()
        return V07ProductionConfig(
            repository_root=root,
            artifact_root=root / "run",
            docker_binary=executables[0],
            docker_endpoint="unix:///tmp/docker.sock",
            docker_data_path=root,
            ollama_binary=executables[1],
            ollama_models_root=models,
            tokenizer_helper=executables[2],
            stewarded_container_ids=stewarded_container_ids,
        )

    def test_cli_canonically_binds_the_exact_stewarded_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = self._production_fixture(root)
            observed: list[V07ProductionConfig] = []

            def inspect(candidate):  # type: ignore[no-untyped-def]
                observed.append(candidate)
                return V07ProductionPreflight(1, 50, 40 << 30, ())

            argv = [
                "--repository-root",
                str(config.repository_root),
                "--artifact-root",
                str(config.artifact_root),
                "--docker-binary",
                str(config.docker_binary),
                "--docker-endpoint",
                config.docker_endpoint,
                "--docker-data-path",
                str(config.docker_data_path),
                "--ollama-binary",
                str(config.ollama_binary),
                "--ollama-models-root",
                str(config.ollama_models_root),
                "--tokenizer-helper",
                str(config.tokenizer_helper),
                "--stewarded-container-id",
                SECOND_CONTAINER_ID,
                "--stewarded-container-id",
                CONTAINER_ID,
            ]
            with (
                mock.patch.object(
                    production_module,
                    "inspect_v07_production_preflight",
                    side_effect=inspect,
                ),
                mock.patch("builtins.print"),
            ):
                status = production_module.main(argv)

        self.assertEqual(status, 0)
        self.assertEqual(len(observed), 1)
        self.assertEqual(
            observed[0].stewarded_container_ids,
            STEWARDED_CONTAINER_IDS,
        )

    def test_production_seals_admission_before_creating_image_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = self._production_fixture(root)
            inventory = mock.Mock()
            inventory.canonical_record.return_value = {}
            managed = mock.Mock()
            plan = object()
            plan_calls = 0

            def create_plan(_request):  # type: ignore[no-untyped-def]
                nonlocal plan_calls
                plan_calls += 1
                journal = (
                    config.artifact_root
                    / "records"
                    / "image-build-admission.jsonl"
                )
                inspection = inspect_journal(journal, require_sealed=True)
                self.assertTrue(inspection.sealed)
                return plan

            docker_pins = DockerTelemetryPins(
                endpoint="unix:///tmp/docker.sock",
                client_version="27.3.1",
                server_version="27.3.1",
                binary_sha256=SHA,
            )
            with (
                mock.patch.object(
                    production_module,
                    "inspect_v07_production_preflight",
                    return_value=V07ProductionPreflight(1, 50, 40 << 30, ()),
                ),
                mock.patch.object(
                    production_module,
                    "build_verified_source_inventory",
                    return_value=inventory,
                ),
                mock.patch.object(
                    production_module,
                    "launch_managed_v07_ollama",
                    return_value=managed,
                ),
                mock.patch.object(
                    production_module,
                    "_sha256_executable",
                    return_value=SHA,
                ),
                mock.patch.object(
                    production_module,
                    "_inspect_docker_pins",
                    return_value=docker_pins,
                ),
                mock.patch.object(
                    production_module,
                    "HostTelemetryCollector",
                    return_value=_Samples([_sample(0), _sample(30)]),
                ),
                mock.patch.object(
                    production_module,
                    "_image_build_safety_pins",
                    return_value=_pins(),
                ),
                mock.patch.object(
                    production_module,
                    "require_live_managed_ollama_receipt",
                ),
                mock.patch.object(production_module.time, "sleep"),
                mock.patch.object(
                    production_module,
                    "create_intercode_image_build_plan",
                    side_effect=create_plan,
                ),
                mock.patch.object(
                    production_module,
                    "execute_intercode_image_build",
                    side_effect=RuntimeError("test boundary"),
                ),
            ):
                with self.assertRaises(V07ProductionError):
                    execute_v07_production(config)

            self.assertEqual(plan_calls, 1)
            managed.close.assert_called_once_with()

    def test_denied_production_seals_evidence_and_never_plans_an_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = self._production_fixture(root)
            inventory = mock.Mock()
            inventory.canonical_record.return_value = {}
            managed = mock.Mock()
            docker_pins = DockerTelemetryPins(
                endpoint="unix:///tmp/docker.sock",
                client_version="27.3.1",
                server_version="27.3.1",
                binary_sha256=SHA,
            )
            with (
                mock.patch.object(
                    production_module,
                    "inspect_v07_production_preflight",
                    return_value=V07ProductionPreflight(1, 50, 40 << 30, ()),
                ),
                mock.patch.object(
                    production_module,
                    "build_verified_source_inventory",
                    return_value=inventory,
                ),
                mock.patch.object(
                    production_module,
                    "launch_managed_v07_ollama",
                    return_value=managed,
                ),
                mock.patch.object(
                    production_module,
                    "_sha256_executable",
                    return_value=SHA,
                ),
                mock.patch.object(
                    production_module,
                    "_inspect_docker_pins",
                    return_value=docker_pins,
                ),
                mock.patch.object(
                    production_module,
                    "HostTelemetryCollector",
                    return_value=_Samples(
                        [_sample(0, running=(CONTAINER_ID,))]
                    ),
                ),
                mock.patch.object(
                    production_module,
                    "_image_build_safety_pins",
                    return_value=_pins(),
                ),
                mock.patch.object(
                    production_module,
                    "require_live_managed_ollama_receipt",
                ),
                mock.patch.object(production_module.time, "sleep"),
                mock.patch.object(
                    production_module,
                    "create_intercode_image_build_plan",
                ) as create_plan,
            ):
                with self.assertRaises(V07ProductionError):
                    execute_v07_production(config)

            journal = config.artifact_root / "records" / "image-build-admission.jsonl"
            self.assertTrue(inspect_journal(journal, require_sealed=True).sealed)
            create_plan.assert_not_called()
            managed.close.assert_called_once_with()

    def test_execute_requires_tokenizer_before_creating_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executables = []
            for name in ("docker", "ollama"):
                path = root / name
                path.write_bytes(name.encode("ascii"))
                path.chmod(0o755)
                executables.append(path)
            models = root / "models"
            models.mkdir()
            config = V07ProductionConfig(
                repository_root=root,
                artifact_root=root / "run",
                docker_binary=executables[0],
                docker_endpoint="unix:///tmp/docker.sock",
                docker_data_path=root,
                ollama_binary=executables[1],
                ollama_models_root=models,
                tokenizer_helper=root / "llama-tokenize",
            )
            allowed = V07ProductionPreflight(
                vm_pressure_level=1,
                free_memory_percent=41,
                disk_free_bytes=40 << 30,
                reasons=(),
            )

            with mock.patch(
                "edgeloopbench.intercode_v07_production."
                "inspect_v07_production_preflight",
                return_value=allowed,
            ):
                with self.assertRaises(V07ProductionError):
                    execute_v07_production(config)

            self.assertFalse(config.artifact_root.exists())

    def test_preflight_does_not_require_tokenizer_provisioning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executables = []
            for name in ("docker", "ollama"):
                path = root / name
                path.write_bytes(name.encode("ascii"))
                path.chmod(0o755)
                executables.append(path)
            models = root / "models"
            models.mkdir()
            tokenizer = root / "build" / "artifacts" / "llama-tokenize"
            config = V07ProductionConfig(
                repository_root=root,
                artifact_root=root / "run",
                docker_binary=executables[0],
                docker_endpoint="unix:///tmp/docker.sock",
                docker_data_path=root,
                ollama_binary=executables[1],
                ollama_models_root=models,
                tokenizer_helper=tokenizer,
            )

            def runner(argv, **_kwargs):  # type: ignore[no-untyped-def]
                output = (
                    b"1\n"
                    if argv[-1] == "kern.memorystatus_vm_pressure_level"
                    else b"System-wide memory free percentage: 41%\n"
                )
                return subprocess.CompletedProcess(argv, 0, output, b"")

            snapshot = inspect_v07_production_preflight(
                config,
                runner=runner,
                statvfs=lambda _path: _Disk(),
            )

            self.assertTrue(snapshot.allowed)
            self.assertFalse(tokenizer.exists())
            self.assertFalse(config.artifact_root.exists())

    def test_preflight_is_read_only_and_reports_the_exact_frozen_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = []
            for name in ("docker", "ollama", "llama-tokenize"):
                path = root / name
                path.write_bytes(name.encode("ascii"))
                path.chmod(0o755)
                inputs.append(path)
            models = root / "models"
            models.mkdir()
            config = V07ProductionConfig(
                repository_root=root,
                artifact_root=root / "run",
                docker_binary=inputs[0],
                docker_endpoint="unix:///tmp/docker.sock",
                docker_data_path=root,
                ollama_binary=inputs[1],
                ollama_models_root=models,
                tokenizer_helper=inputs[2],
            )
            calls: list[tuple[str, ...]] = []

            def runner(argv, **_kwargs):  # type: ignore[no-untyped-def]
                calls.append(tuple(argv))
                if argv[-1] == "kern.memorystatus_vm_pressure_level":
                    output = b"1\n"
                else:
                    output = (
                        b"The system has 17179869184 (1048576 pages with a "
                        b"page size of 16384).\n"
                        b"System-wide memory free percentage: 41%\n"
                    )
                return subprocess.CompletedProcess(argv, 0, output, b"")

            snapshot = inspect_v07_production_preflight(
                config,
                runner=runner,
                statvfs=lambda _path: _Disk(),
            )

            def invalid_geometry_runner(
                argv, **_kwargs
            ):  # type: ignore[no-untyped-def]
                output = (
                    b"1\n"
                    if argv[-1] == "kern.memorystatus_vm_pressure_level"
                    else (
                        b"The system has 17179869185 (1048576 pages with a "
                        b"page size of 16384).\n"
                        b"System-wide memory free percentage: 41%\n"
                    )
                )
                return subprocess.CompletedProcess(argv, 0, output, b"")

            with self.assertRaisesRegex(V07ProductionError, "geometry"):
                inspect_v07_production_preflight(
                    config,
                    runner=invalid_geometry_runner,
                    statvfs=lambda _path: _Disk(),
                )

        self.assertTrue(snapshot.allowed)
        self.assertEqual(snapshot.vm_pressure_level, 1)
        self.assertEqual(snapshot.free_memory_percent, 41)
        self.assertGreaterEqual(snapshot.disk_free_bytes, 32 << 30)
        self.assertEqual(len(calls), 2)
        self.assertFalse(config.artifact_root.exists())

    def test_warning_pressure_is_a_hard_stop_even_with_disk_and_free_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = []
            for name in ("docker", "ollama", "llama-tokenize"):
                path = root / name
                path.write_bytes(name.encode("ascii"))
                path.chmod(0o755)
                inputs.append(path)
            models = root / "models"
            models.mkdir()
            config = V07ProductionConfig(
                repository_root=root,
                artifact_root=root / "run",
                docker_binary=inputs[0],
                docker_endpoint="unix:///tmp/docker.sock",
                docker_data_path=root,
                ollama_binary=inputs[1],
                ollama_models_root=models,
                tokenizer_helper=inputs[2],
            )

            def runner(argv, **_kwargs):  # type: ignore[no-untyped-def]
                output = (
                    b"2\n"
                    if argv[-1] == "kern.memorystatus_vm_pressure_level"
                    else b"System-wide memory free percentage: 50%\n"
                )
                return subprocess.CompletedProcess(argv, 0, output, b"")

            snapshot = inspect_v07_production_preflight(
                config,
                runner=runner,
                statvfs=lambda _path: _Disk(),
            )

        self.assertFalse(snapshot.allowed)
        self.assertEqual(snapshot.reasons, ("vm_pressure",))


if __name__ == "__main__":
    unittest.main()
