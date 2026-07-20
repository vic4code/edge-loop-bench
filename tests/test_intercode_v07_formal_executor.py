from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edgeloopbench.docker_action_executor import DockerActionExecutor
from edgeloopbench.docker_cli import DockerCli
from edgeloopbench.interactive_controller import InteractiveResult
from edgeloopbench.intercode_campaign_ledger import (
    CampaignAdvance,
    CampaignEpisodeExecution,
    CampaignProgress,
    load_episode_execution_envelope,
)
from edgeloopbench.intercode_host_safety import (
    DockerTelemetryPins,
    ExpectedHostResources,
    HostTelemetryCollector,
    ResidentModel,
)
from edgeloopbench.intercode_source import load_intercode_source
from edgeloopbench.intercode_v07_attempt_factory import V07DockerAttemptFactory
from edgeloopbench.intercode_v07_calibration import (
    V07_CALIBRATION_ARMS,
    V07_CALIBRATION_TASK_IDS,
)
from edgeloopbench.intercode_v07_calibration_executor import (
    V07CalibrationExecutionRow,
    V07CalibrationRuntime,
    v07_calibration_budget,
)
from edgeloopbench.intercode_v07_calibration_runtime import (
    V07CalibrationRuntimeCompositionError,
    build_v07_calibration_runtime_composer,
)
from edgeloopbench.intercode_v07_formal_executor import (
    V07FormalCampaignRun,
    V07FormalExecutorError,
    V07FormalPhaseExecutor,
    advance_v07_formal_phase,
    build_v07_formal_phase_executor,
    run_v07_formal_campaign,
)
from edgeloopbench.intercode_v07_host_policy import (
    V07HostSafetyError,
    open_v07_host_safety_session,
)
from edgeloopbench.intercode_v07_model_phase import (
    V07ModelPhaseError,
    V07ModelPhaseManager,
    build_v07_model_phase_manager,
)
from edgeloopbench.intercode_v07_manifest import build_v07_execution_pins
from edgeloopbench.intercode_v07_runner import V07EpisodeRun
from edgeloopbench.intercode_v07_runtime_factory import (
    issue_v07_managed_residency_boundary,
    transition_v07_model_residency,
)
from edgeloopbench.model_adapter import PHI4_MINI_RAW_PROFILE, QWEN35_RAW_PROFILE
from tests.test_intercode_v07_study_binding import V07PreparedStudyTests
from tests.test_intercode_v07_manifest import source_inventory, source_repository_root


DOCKER_ENDPOINT = "unix:///var/run/docker.sock"
BOOT_MICROSECONDS = 1_700_000_000_000_000


class _Response:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]

    def geturl(self) -> str:
        return "http://127.0.0.1:11434/api/ps"


class _Stat:
    f_frsize = 1
    f_bavail = 64 << 30


class _Telemetry:
    def __init__(
        self,
        model: ResidentModel,
        *,
        docker_binary: Path,
        docker_endpoint: str,
        docker_client_version: str,
        docker_server_version: str,
    ) -> None:
        self.model = model
        self.docker_binary = docker_binary
        self.docker_endpoint = docker_endpoint
        self.docker_client_version = docker_client_version
        self.docker_server_version = docker_server_version
        self.index = -1

    @property
    def monotonic(self) -> int:
        return (self.index + 1) * 10_000_000_000

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
            output = b"1\n"
        elif key == ("/usr/sbin/sysctl", "-n", "vm.swapusage"):
            output = (
                b"total = 8192.00M  used = 4096.00M  "
                b"free = 4096.00M  (encrypted)\n"
            )
        elif key == ("/usr/bin/memory_pressure", "-Q"):
            output = b"System-wide memory free percentage: 50%\n"
        elif key == ("/usr/bin/pmset", "-g", "therm"):
            output = (
                b"Note: No thermal warning level has been recorded\n"
                b"Note: No performance warning level has been recorded\n"
                b"Note: No CPU power status has been recorded\n"
            )
        elif key == ("/usr/sbin/sysctl", "-n", "kern.boottime"):
            seconds, micros = divmod(BOOT_MICROSECONDS, 1_000_000)
            output = f"{{ sec = {seconds}, usec = {micros} }}\n".encode("ascii")
        elif key == (
            os.fspath(self.docker_binary),
            "--host",
            self.docker_endpoint,
            "version",
            "--format",
            "{{json .}}",
        ):
            output = json.dumps(
                {
                    "Client": {"Version": self.docker_client_version},
                    "Server": {"Version": self.docker_server_version},
                },
                separators=(",", ":"),
            ).encode("ascii")
        elif key == (
            os.fspath(self.docker_binary),
            "--host",
            self.docker_endpoint,
            "container",
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            "status=running",
        ):
            output = b""
        else:
            raise AssertionError(f"unexpected telemetry command: {key!r}")
        return subprocess.CompletedProcess(argv, 0, output, b"")

    def urlopen(self, _request: object, _timeout: float) -> _Response:
        return _Response(
            json.dumps(
                {"models": [{"model": self.model.model, "digest": self.model.digest}]}
            ).encode("ascii")
        )

    def statvfs(self, _path: object) -> _Stat:
        return _Stat()

    def time_ns(self) -> int:
        return 1_800_000_000_000_000_000 + self.monotonic

    def monotonic_ns(self) -> int:
        return self.monotonic


def _result() -> InteractiveResult:
    return InteractiveResult(
        run_status="completed",
        official_success=False,
        strict_success=False,
        stop_reason="direct_complete",
        attempts=1,
        model_calls=1,
        logical_prompt_tokens=100,
        logical_completion_tokens=10,
        environment_actions=1,
        replayed_environment_actions=0,
        evaluator_calls=2,
        checkpoint_creates=1,
        checkpoint_restores=0,
        safety_recoveries=0,
        parser_failures=0,
        initial_prompts=1,
        independent_sample_prompts=0,
        feedback_followups=0,
        human_prompts=0,
    )


class V07FormalExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        V07PreparedStudyTests.setUpClass()
        cls.fixture = V07PreparedStudyTests

    @classmethod
    def tearDownClass(cls) -> None:
        V07PreparedStudyTests.tearDownClass()

    def _host_session(  # type: ignore[no-untyped-def]
        self,
        *,
        docker_binary: Path,
        pins=None,
        model_id: str = QWEN35_RAW_PROFILE.model,
    ):
        prepared = self.fixture.prepared
        selected_pins = pins or prepared.execution_pins.host_safety
        runtime = prepared.model_runtime(model_id)
        resident = ResidentModel(
            runtime.model_id,
            runtime.model_identity.generation.profile.model_manifest_sha256
            .removeprefix("sha256:"),
        )
        identity = selected_pins.host_identity
        docker_pins = DockerTelemetryPins(
            endpoint=DOCKER_ENDPOINT,
            client_version=identity.docker_client_version,
            server_version=identity.docker_server_version,
            binary_sha256=identity.docker_binary_sha256,
        )
        scenario = _Telemetry(
            resident,
            docker_binary=docker_binary,
            docker_endpoint=docker_pins.endpoint,
            docker_client_version=docker_pins.client_version,
            docker_server_version=docker_pins.server_version,
        )
        collector = HostTelemetryCollector(
            docker_binary=docker_binary,
            docker_data_path=Path("/tmp/docker-data"),
            docker_pins=docker_pins,
            environment={},
            docker_binary_sha256=lambda _path: identity.docker_binary_sha256,
            runner=scenario.runner,
            urlopen=scenario.urlopen,
            statvfs=scenario.statvfs,
            time_ns=scenario.time_ns,
            monotonic_ns=scenario.monotonic_ns,
        )
        return open_v07_host_safety_session(
            pins=selected_pins,
            collector=collector,
            expected=ExpectedHostResources(resident_models=(resident,)),
            require_live_runtime=runtime.require_live,
        )

    def _docker(
        self,
        *,
        docker_binary: Path,
        context: str = "desktop-linux",
        endpoint: str = DOCKER_ENDPOINT,
        action_boundary: DockerCli | None = None,
    ) -> tuple[DockerCli, DockerActionExecutor]:
        docker = DockerCli(
            expected_context=context,
            expected_endpoint=endpoint,
            docker_binary=os.fspath(docker_binary),
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("unit composition must not invoke Docker")
            ),
        )
        executor = DockerActionExecutor(
            boundary=action_boundary or docker,
            expected_docker_binary=os.fspath(docker_binary),
            expected_endpoint=endpoint,
        )
        return docker, executor

    def _docker_binary(self, directory: str, content: bytes = b"docker") -> Path:
        path = Path(directory).resolve() / "docker"
        path.write_bytes(content)
        path.chmod(0o755)
        return path

    def test_composes_exact_authorities_paths_host_hooks_and_envelope(self) -> None:
        prepared = self.fixture.prepared
        source = load_intercode_source()
        with tempfile.TemporaryDirectory() as directory:
            docker_binary = self._docker_binary(directory)
            docker, action_executor = self._docker(docker_binary=docker_binary)
            root = Path(directory).resolve() / "formal"
            executor = build_v07_formal_phase_executor(
                prepared_study=prepared,
                source=source,
                manifest=self.fixture.manifest,
                phase_model_id=QWEN35_RAW_PROFILE.model,
                host_session=self._host_session(docker_binary=docker_binary),
                docker_cli=docker,
                action_executor=action_executor,
                artifact_root=root,
            )
            episode = prepared.bound_campaign_spec.episodes[0]

            def run(**kwargs: object) -> V07EpisodeRun:
                before = kwargs["before_episode_admission"]()
                after = kwargs["after_episode_admission"]()
                execution = CampaignEpisodeExecution(
                    result=_result(),
                    execution_authority_sha256=prepared.study_binding_sha256,
                    controller_log_sha256="sha256:" + "a" * 64,
                    active_wall_time_ns=1,
                    before_host_sample=before,
                    after_host_sample=after,
                )
                from edgeloopbench.intercode_campaign_ledger import (
                    write_episode_execution_envelope,
                )

                write_episode_execution_envelope(
                    kwargs["execution_envelope"],
                    kwargs["episode"],
                    execution,
                )
                return V07EpisodeRun(execution)

            boundary = object.__new__(V07DockerAttemptFactory)
            with mock.patch(
                "edgeloopbench.intercode_v07_formal_executor."
                "build_v07_formal_docker_attempt_factory",
                return_value=boundary,
            ) as build_attempt, mock.patch(
                "edgeloopbench.intercode_v07_formal_executor.run_v07_episode",
                side_effect=run,
            ) as run_episode:
                observed = executor(episode)

            self.assertIs(type(executor), V07FormalPhaseExecutor)
            self.assertEqual(observed.result, _result())
            attempt_kwargs = build_attempt.call_args.kwargs
            self.assertIs(attempt_kwargs["prepared_study"], prepared)
            self.assertIs(attempt_kwargs["source"], source)
            self.assertEqual(attempt_kwargs["task"].task_id, episode.task_id)
            run_kwargs = run_episode.call_args.kwargs
            self.assertIs(run_kwargs["boundary_factory"], boundary)
            self.assertEqual(
                run_kwargs["execution_authority_sha256"],
                prepared.study_binding_sha256,
            )
            self.assertIs(
                run_kwargs["private_gold"],
                prepared.trusted_gold_for_episode(episode),
            )
            envelope = root / "envelopes/episode-0001.execution.jsonl"
            self.assertEqual(
                load_episode_execution_envelope(envelope, episode),
                observed,
            )
            self.assertEqual(
                oct((root / "controllers").stat().st_mode & 0o777),
                "0o700",
            )
            self.assertEqual(
                oct((root / "envelopes").stat().st_mode & 0o777),
                "0o700",
            )

            phi_episode = prepared.bound_campaign_spec.episodes[120]
            with self.assertRaisesRegex(V07FormalExecutorError, "phase model"):
                executor.require_phase_episode(phi_episode)

    def test_rejects_symlink_artifact_root_before_durable_work(self) -> None:
        prepared = self.fixture.prepared
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            docker_binary = self._docker_binary(directory)
            docker, action_executor = self._docker(docker_binary=docker_binary)
            real = base / "real"
            link = base / "link"
            real.mkdir()
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(V07FormalExecutorError, "artifact"):
                build_v07_formal_phase_executor(
                    prepared_study=prepared,
                    source=load_intercode_source(),
                    manifest=self.fixture.manifest,
                    phase_model_id=QWEN35_RAW_PROFILE.model,
                    host_session=self._host_session(docker_binary=docker_binary),
                    docker_cli=docker,
                    action_executor=action_executor,
                    artifact_root=link,
                )

    def test_advance_composes_phase_guard_binding_revalidation_and_reconciler(
        self,
    ) -> None:
        prepared = self.fixture.prepared
        source = load_intercode_source()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            docker_binary = self._docker_binary(directory)
            docker, action_executor = self._docker(docker_binary=docker_binary)
            executor = build_v07_formal_phase_executor(
                prepared_study=prepared,
                source=source,
                manifest=self.fixture.manifest,
                phase_model_id=QWEN35_RAW_PROFILE.model,
                host_session=self._host_session(docker_binary=docker_binary),
                docker_cli=docker,
                action_executor=action_executor,
                artifact_root=base / "formal",
            )
            expected = CampaignAdvance(
                "episode_completed",
                prepared.bound_campaign_spec.episodes[0],
                CampaignProgress(240, 1, 0, 0, 239, False),
            )
            with mock.patch(
                "edgeloopbench.intercode_v07_formal_executor.advance_campaign",
                return_value=expected,
            ) as advance:
                observed = advance_v07_formal_phase(
                    executor=executor,
                    campaign_journal_path=base / "campaign.jsonl",
                    repository_root=source_repository_root(),
                    intervention_journal_path=self.fixture.intervention_path,
                )

            self.assertIs(observed, expected)
            args = advance.call_args.args
            kwargs = advance.call_args.kwargs
            self.assertEqual(
                args[:2],
                (base / "campaign.jsonl", prepared.bound_campaign_spec),
            )
            self.assertIs(args[2], executor)
            self.assertIs(kwargs["reconcile_pending"].__self__, executor)
            callback = kwargs["before_new_intent"]
            self.assertIsNone(callback(prepared.bound_campaign_spec.episodes[0]))
            with self.assertRaisesRegex(V07FormalExecutorError, "phase model"):
                callback(prepared.bound_campaign_spec.episodes[120])

    def test_rejects_host_session_from_different_policy_pins(self) -> None:
        prepared = self.fixture.prepared
        identity = prepared.execution_pins.host_safety.host_identity
        other_identity = dataclasses.replace(
            identity,
            docker_client_version="28.3.3",
        )
        other_pins = build_v07_execution_pins(
            source_inventory=source_inventory(),
            host_identity=other_identity,
        ).host_safety
        with tempfile.TemporaryDirectory() as directory:
            docker_binary = self._docker_binary(directory)
            docker, action_executor = self._docker(docker_binary=docker_binary)
            with self.assertRaisesRegex(V07FormalExecutorError, "host policy"):
                build_v07_formal_phase_executor(
                    prepared_study=prepared,
                    source=load_intercode_source(),
                    manifest=self.fixture.manifest,
                    phase_model_id=QWEN35_RAW_PROFILE.model,
                    host_session=self._host_session(
                        docker_binary=docker_binary,
                        pins=other_pins,
                    ),
                    docker_cli=docker,
                    action_executor=action_executor,
                    artifact_root=Path(directory).resolve() / "formal",
                )

    def test_rejects_docker_context_endpoint_and_binary_drift(self) -> None:
        prepared = self.fixture.prepared
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            docker_binary = self._docker_binary(directory)
            session = self._host_session(docker_binary=docker_binary)
            cases = (
                ("other-context", DOCKER_ENDPOINT, docker_binary, "context"),
                (
                    "desktop-linux",
                    "unix:///tmp/edge-loop-other.sock",
                    docker_binary,
                    "endpoint",
                ),
                (
                    "desktop-linux",
                    DOCKER_ENDPOINT,
                    self._docker_binary(directory, b"other-docker"),
                    "binary",
                ),
            )
            for index, (context, endpoint, binary, reason) in enumerate(cases):
                with self.subTest(reason=reason):
                    docker, action_executor = self._docker(
                        docker_binary=binary,
                        context=context,
                        endpoint=endpoint,
                    )
                    with self.assertRaisesRegex(V07FormalExecutorError, reason):
                        build_v07_formal_phase_executor(
                            prepared_study=prepared,
                            source=load_intercode_source(),
                            manifest=self.fixture.manifest,
                            phase_model_id=QWEN35_RAW_PROFILE.model,
                            host_session=session,
                            docker_cli=docker,
                            action_executor=action_executor,
                            artifact_root=root / f"formal-{index}",
                        )

    def test_rejects_action_executor_from_a_different_docker_boundary(self) -> None:
        prepared = self.fixture.prepared
        with tempfile.TemporaryDirectory() as directory:
            docker_binary = self._docker_binary(directory)
            first, _unused = self._docker(docker_binary=docker_binary)
            second, action_executor = self._docker(docker_binary=docker_binary)
            self.assertIsNot(first, second)
            with self.assertRaisesRegex(V07FormalExecutorError, "Docker boundary"):
                build_v07_formal_phase_executor(
                    prepared_study=prepared,
                    source=load_intercode_source(),
                    manifest=self.fixture.manifest,
                    phase_model_id=QWEN35_RAW_PROFILE.model,
                    host_session=self._host_session(docker_binary=docker_binary),
                    docker_cli=first,
                    action_executor=action_executor,
                    artifact_root=Path(directory).resolve() / "formal",
                )

    def test_factory_failure_does_not_leave_an_active_host_admission(self) -> None:
        prepared = self.fixture.prepared
        with tempfile.TemporaryDirectory() as directory:
            docker_binary = self._docker_binary(directory)
            docker, action_executor = self._docker(docker_binary=docker_binary)
            session = self._host_session(docker_binary=docker_binary)
            executor = build_v07_formal_phase_executor(
                prepared_study=prepared,
                source=load_intercode_source(),
                manifest=self.fixture.manifest,
                phase_model_id=QWEN35_RAW_PROFILE.model,
                host_session=session,
                docker_cli=docker,
                action_executor=action_executor,
                artifact_root=Path(directory).resolve() / "formal",
            )
            with mock.patch(
                "edgeloopbench.intercode_v07_formal_executor."
                "build_v07_formal_docker_attempt_factory",
                side_effect=RuntimeError("factory failed"),
            ), self.assertRaises(V07FormalExecutorError):
                executor(prepared.bound_campaign_spec.episodes[0])

            self.assertIsNotNone(session.issue_episode_admission())

    def test_stale_artifact_failure_releases_an_unstarted_host_admission(self) -> None:
        prepared = self.fixture.prepared
        with tempfile.TemporaryDirectory() as directory:
            docker_binary = self._docker_binary(directory)
            docker, action_executor = self._docker(docker_binary=docker_binary)
            session = self._host_session(docker_binary=docker_binary)
            executor = build_v07_formal_phase_executor(
                prepared_study=prepared,
                source=load_intercode_source(),
                manifest=self.fixture.manifest,
                phase_model_id=QWEN35_RAW_PROFILE.model,
                host_session=session,
                docker_cli=docker,
                action_executor=action_executor,
                artifact_root=Path(directory).resolve() / "formal",
            )
            episode = prepared.bound_campaign_spec.episodes[0]
            executor.controller_directory.joinpath("episode-0001.jsonl").write_text(
                "stale\n",
                encoding="utf-8",
            )
            boundary = object.__new__(V07DockerAttemptFactory)
            with mock.patch(
                "edgeloopbench.intercode_v07_formal_executor."
                "build_v07_formal_docker_attempt_factory",
                return_value=boundary,
            ), self.assertRaises(V07FormalExecutorError):
                executor(episode)

            self.assertIsNotNone(session.issue_episode_admission())

    def test_interrupt_before_host_hook_releases_unstarted_admission(self) -> None:
        prepared = self.fixture.prepared
        with tempfile.TemporaryDirectory() as directory:
            docker_binary = self._docker_binary(directory)
            docker, action_executor = self._docker(docker_binary=docker_binary)
            session = self._host_session(docker_binary=docker_binary)
            executor = build_v07_formal_phase_executor(
                prepared_study=prepared,
                source=load_intercode_source(),
                manifest=self.fixture.manifest,
                phase_model_id=QWEN35_RAW_PROFILE.model,
                host_session=session,
                docker_cli=docker,
                action_executor=action_executor,
                artifact_root=Path(directory).resolve() / "formal",
            )
            boundary = object.__new__(V07DockerAttemptFactory)
            with mock.patch(
                "edgeloopbench.intercode_v07_formal_executor."
                "build_v07_formal_docker_attempt_factory",
                return_value=boundary,
            ), mock.patch(
                "edgeloopbench.intercode_v07_formal_executor.run_v07_episode",
                side_effect=KeyboardInterrupt,
            ), self.assertRaises(KeyboardInterrupt):
                executor(prepared.bound_campaign_spec.episodes[0])

            self.assertIsNotNone(session.issue_episode_admission())

    def test_failure_after_before_hook_terminally_invalidates_phase(self) -> None:
        prepared = self.fixture.prepared
        with tempfile.TemporaryDirectory() as directory:
            docker_binary = self._docker_binary(directory)
            docker, action_executor = self._docker(docker_binary=docker_binary)
            session = self._host_session(docker_binary=docker_binary)
            executor = build_v07_formal_phase_executor(
                prepared_study=prepared,
                source=load_intercode_source(),
                manifest=self.fixture.manifest,
                phase_model_id=QWEN35_RAW_PROFILE.model,
                host_session=session,
                docker_cli=docker,
                action_executor=action_executor,
                artifact_root=Path(directory).resolve() / "formal",
            )
            boundary = object.__new__(V07DockerAttemptFactory)

            def fail_after_before(**kwargs: object) -> V07EpisodeRun:
                kwargs["before_episode_admission"]()
                raise RuntimeError("runner failed after before-host sample")

            with mock.patch(
                "edgeloopbench.intercode_v07_formal_executor."
                "build_v07_formal_docker_attempt_factory",
                return_value=boundary,
            ), mock.patch(
                "edgeloopbench.intercode_v07_formal_executor.run_v07_episode",
                side_effect=fail_after_before,
            ), self.assertRaises(V07FormalExecutorError):
                executor(prepared.bound_campaign_spec.episodes[0])

            with self.assertRaisesRegex(V07HostSafetyError, "session is invalid"):
                session.issue_episode_admission()

    def test_campaign_driver_runs_exact_model_major_order_and_one_transition(self) -> None:
        spec = self.fixture.prepared.bound_campaign_spec
        qwen = object.__new__(V07FormalPhaseExecutor)
        qwen._phase_model_id = QWEN35_RAW_PROFILE.model  # type: ignore[attr-defined]  # noqa: SLF001
        phi = object.__new__(V07FormalPhaseExecutor)
        phi._phase_model_id = PHI4_MINI_RAW_PROFILE.model  # type: ignore[attr-defined]  # noqa: SLF001
        opened: list[tuple[str | None, str]] = []

        def open_phase(
            previous_model_id: str | None,
            target_model_id: str,
        ) -> V07FormalPhaseExecutor:
            opened.append((previous_model_id, target_model_id))
            return qwen if target_model_id == QWEN35_RAW_PROFILE.model else phi

        advances = tuple(
            CampaignAdvance(
                "episode_completed",
                episode,
                CampaignProgress(
                    240,
                    index,
                    0,
                    0,
                    240 - index,
                    index == 240,
                ),
            )
            for index, episode in enumerate(spec.episodes, 1)
        )

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "edgeloopbench.intercode_v07_formal_executor.inspect_campaign",
            return_value=CampaignProgress(240, 0, 0, 0, 240, False),
        ), mock.patch(
            "edgeloopbench.intercode_v07_formal_executor.advance_v07_formal_phase",
            side_effect=advances,
        ) as advance:
            root = Path(directory).resolve()
            result = run_v07_formal_campaign(
                spec=spec,
                open_phase=open_phase,
                campaign_journal_path=root / "campaign.jsonl",
                repository_root=source_repository_root(),
                intervention_journal_path=self.fixture.intervention_path,
            )

        self.assertIs(type(result), V07FormalCampaignRun)
        self.assertEqual(result.advanced_episode_count, 240)
        self.assertTrue(result.progress.sealed)
        self.assertEqual(
            opened,
            [
                (None, QWEN35_RAW_PROFILE.model),
                (QWEN35_RAW_PROFILE.model, PHI4_MINI_RAW_PROFILE.model),
            ],
        )
        self.assertEqual(advance.call_count, 240)
        self.assertTrue(
            all(
                call.kwargs["executor"] is qwen
                for call in advance.call_args_list[:120]
            )
        )
        self.assertTrue(
            all(
                call.kwargs["executor"] is phi
                for call in advance.call_args_list[120:]
            )
        )

    def test_calibration_runtime_composer_binds_model_phase_host_and_docker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            docker_binary = self._docker_binary(directory)
            docker, action_executor = self._docker(docker_binary=docker_binary)
            session = self._host_session(docker_binary=docker_binary)
            opened = []

            def open_phase(previous, target):  # type: ignore[no-untyped-def]
                opened.append((previous, target))
                return session

            composer = build_v07_calibration_runtime_composer(
                runtime_session=self.fixture.runtime_session,
                source=load_intercode_source(),
                calibration_gold=self.fixture.calibration_gold,
                manifest=self.fixture.manifest,
                open_phase=open_phase,
                docker_cli=docker,
                action_executor=action_executor,
            )
            task = load_intercode_source().calibration_tasks[0]
            episode = self.fixture.spec.episodes[0]
            calibration_episode = dataclasses.replace(
                episode,
                task_id=V07_CALIBRATION_TASK_IDS[0],
                arm=V07_CALIBRATION_ARMS[0],
            )
            row = V07CalibrationExecutionRow(
                episode=calibration_episode,
                task=task,
                request_cap=1,
                budget=v07_calibration_budget(),
            )
            boundary = object.__new__(V07DockerAttemptFactory)
            with mock.patch(
                "edgeloopbench.intercode_v07_calibration_runtime."
                "build_v07_calibration_docker_attempt_factory",
                return_value=boundary,
            ) as build_attempt:
                runtime = composer(row)

            self.assertIs(type(runtime), V07CalibrationRuntime)
            self.assertIs(
                runtime.model,
                self.fixture.runtime_session.model_runtime(
                    QWEN35_RAW_PROFILE.model
                ).model,
            )
            self.assertIs(runtime.boundary_factory, boundary)
            self.assertEqual(len(opened), 1)
            self.assertIsNone(opened[0][0])
            self.assertEqual(opened[0][1].model_id, QWEN35_RAW_PROFILE.model)
            self.assertIs(
                build_attempt.call_args.kwargs["calibration_gold"],
                self.fixture.calibration_gold,
            )
            runtime.abort_episode_admission()
            self.assertIsNotNone(session.issue_episode_admission())

            with self.assertRaises(V07CalibrationRuntimeCompositionError):
                composer(row)

    def test_model_phase_manager_carries_phi_residency_into_formal_qwen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            docker_binary = self._docker_binary(directory)
            identity = self.fixture.execution_pins.host_safety.host_identity
            docker_pins = DockerTelemetryPins(
                endpoint=DOCKER_ENDPOINT,
                client_version=identity.docker_client_version,
                server_version=identity.docker_server_version,
                binary_sha256=identity.docker_binary_sha256,
            )
            qwen = self.fixture.runtime_session.model_runtime(
                QWEN35_RAW_PROFILE.model
            )
            phi = self.fixture.runtime_session.model_runtime(
                PHI4_MINI_RAW_PROFILE.model
            )
            telemetry = _Telemetry(
                qwen.expected_resident_model,
                docker_binary=docker_binary,
                docker_endpoint=docker_pins.endpoint,
                docker_client_version=docker_pins.client_version,
                docker_server_version=docker_pins.server_version,
            )
            collector = HostTelemetryCollector(
                docker_binary=docker_binary,
                docker_data_path=root,
                docker_pins=docker_pins,
                environment={},
                docker_binary_sha256=lambda _path: identity.docker_binary_sha256,
                runner=telemetry.runner,
                urlopen=telemetry.urlopen,
                statvfs=telemetry.statvfs,
                time_ns=telemetry.time_ns,
                monotonic_ns=telemetry.monotonic_ns,
            )
            boundary = issue_v07_managed_residency_boundary(
                self.fixture.runtime.receipt
            )
            intervention_path = root / "interventions.jsonl"
            preload_directory = root / "model-preload-admission"
            preload_directory.mkdir(mode=0o700)
            from edgeloopbench.intercode_v07_interventions import (
                declare_v07_intervention_journal,
            )

            declare_v07_intervention_journal(intervention_path)
            manager = build_v07_model_phase_manager(
                runtime_session=self.fixture.runtime_session,
                execution_pins=self.fixture.execution_pins,
                collector=collector,
                residency_boundary=boundary,
                intervention_journal_path=intervention_path,
                preload_admission_directory=preload_directory,
                monotonic_ns=lambda: 10_000_000_000,
                sleeper=lambda _seconds: None,
            )
            sessions = [
                self._host_session(
                    docker_binary=docker_binary,
                    model_id=QWEN35_RAW_PROFILE.model,
                ),
                self._host_session(
                    docker_binary=docker_binary,
                    model_id=PHI4_MINI_RAW_PROFILE.model,
                ),
                self._host_session(
                    docker_binary=docker_binary,
                    model_id=QWEN35_RAW_PROFILE.model,
                ),
                self._host_session(
                    docker_binary=docker_binary,
                    model_id=PHI4_MINI_RAW_PROFILE.model,
                ),
            ]

            from edgeloopbench import ollama_loopback_http as http_module
            from tests.test_intercode_v07_runtime_factory import FakeResidencyHttp

            fake_http = FakeResidencyHttp()
            with mock.patch.object(
                http_module._OLLAMA_HTTP_OPENER,
                "open",
                side_effect=fake_http.open,
            ):
                receipt_boundary = issue_v07_managed_residency_boundary(
                    self.fixture.runtime.receipt
                )
                receipts = (
                    transition_v07_model_residency(
                        previous=None,
                        target=qwen,
                        boundary=receipt_boundary,
                    ),
                    transition_v07_model_residency(
                        previous=qwen,
                        target=phi,
                        boundary=receipt_boundary,
                    ),
                    transition_v07_model_residency(
                        previous=phi,
                        target=qwen,
                        boundary=receipt_boundary,
                    ),
                    transition_v07_model_residency(
                        previous=qwen,
                        target=phi,
                        boundary=receipt_boundary,
                    ),
                )

            opened_preloads: list[dict[str, object]] = []

            def open_preload(**kwargs):  # type: ignore[no-untyped-def]
                opened_preloads.append(kwargs)
                receipt = kwargs["perform_transition"]()
                self.assertIs(receipt, receipts[len(opened_preloads) - 1])
                return sessions[len(opened_preloads) - 1]

            with mock.patch(
                "edgeloopbench.intercode_v07_model_phase."
                "transition_v07_model_residency",
                side_effect=receipts,
            ) as transition, mock.patch(
                "edgeloopbench.intercode_v07_model_phase."
                "open_v07_preload_stabilized_host_safety_session",
                side_effect=open_preload,
            ) as open_host:
                first = manager.open_calibration_phase(None, qwen)
                second = manager.open_calibration_phase(qwen, phi)
                third = manager.open_formal_phase(None, QWEN35_RAW_PROFILE.model)
                fourth = manager.open_formal_phase(
                    QWEN35_RAW_PROFILE.model,
                    PHI4_MINI_RAW_PROFILE.model,
                )

            self.assertIs(type(manager), V07ModelPhaseManager)
            self.assertIs(first, sessions[0])
            self.assertIs(second, sessions[1])
            self.assertIs(third, sessions[2])
            self.assertIs(fourth, sessions[3])
            self.assertIsNone(transition.call_args_list[0].kwargs["previous"])
            self.assertIs(
                transition.call_args_list[1].kwargs["previous"],
                qwen,
            )
            self.assertIs(
                transition.call_args_list[2].kwargs["previous"],
                phi,
            )
            self.assertIs(
                transition.call_args_list[3].kwargs["previous"],
                qwen,
            )
            self.assertEqual(open_host.call_count, 4)
            self.assertEqual(
                [Path(call["journal_path"]).name for call in opened_preloads],
                [
                    "calibration-01.jsonl",
                    "calibration-02.jsonl",
                    "confirmatory-01.jsonl",
                    "confirmatory-02.jsonl",
                ],
            )
            self.assertEqual(
                [call["expected_runtime_receipt_sha256"] for call in opened_preloads],
                [qwen.runtime_receipt_sha256] * 4,
            )

            failed_intervention = root / "failed-interventions.jsonl"
            failed_preload = root / "failed-model-preload-admission"
            failed_preload.mkdir(mode=0o700)
            declare_v07_intervention_journal(failed_intervention)
            failed_manager = build_v07_model_phase_manager(
                runtime_session=self.fixture.runtime_session,
                execution_pins=self.fixture.execution_pins,
                collector=collector,
                residency_boundary=issue_v07_managed_residency_boundary(
                    self.fixture.runtime.receipt
                ),
                intervention_journal_path=failed_intervention,
                preload_admission_directory=failed_preload,
                monotonic_ns=lambda: 10_000_000_000,
                sleeper=lambda _seconds: None,
            )

            def fail_after_transition(**kwargs):  # type: ignore[no-untyped-def]
                kwargs["perform_transition"]()
                raise V07HostSafetyError("post-load gate denied")

            with mock.patch(
                "edgeloopbench.intercode_v07_model_phase."
                "transition_v07_model_residency",
                return_value=receipts[0],
            ) as failed_transition, mock.patch(
                "edgeloopbench.intercode_v07_model_phase."
                "open_v07_preload_stabilized_host_safety_session",
                side_effect=fail_after_transition,
            ):
                with self.assertRaisesRegex(
                    V07ModelPhaseError,
                    "failed closed",
                ):
                    failed_manager.open_calibration_phase(None, qwen)
                self.assertIsNone(failed_manager.active_model_id)
                with self.assertRaisesRegex(V07ModelPhaseError, "terminally"):
                    failed_manager.open_calibration_phase(None, qwen)

            self.assertEqual(failed_transition.call_count, 1)


if __name__ == "__main__":
    unittest.main()
