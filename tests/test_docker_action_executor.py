from __future__ import annotations

import io
import math
import signal
import subprocess
import threading
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from edgeloopbench.docker_action_executor import (
    ActionDisposition,
    CleanupOutcome,
    DockerActionExecutor,
    DockerActionLimits,
    InfrastructureFailure,
    ActionPolicyFailure,
)
from edgeloopbench.docker_cli import (
    ACTION_WRAPPER_ARG0,
    ACTION_WRAPPER_SCRIPT,
    CONTAINER_USER,
    DockerCleanupRefused,
    DockerContainer,
    DockerContainerSpec,
    DockerLimits,
    PreparedDockerExec,
)


CONTAINER_ID = "c" * 64
RUN_ID = "v06-calibration-001"
NAME = "elb-v06-calibration-001-agent-1234567890abcdef"
IMAGE_ID = "sha256:" + "b" * 64
IMAGE = "local/intercode-bash@sha256:" + "a" * 64
IDLE_TOP = b"PID PPID STAT COMMAND\n101 1 Ss tail\n"
ACTION_STARTED_MARKER = b"\x1eELB_ACTION_STARTED_V1\x1f\n"
DOCKER_BINARY = "/usr/local/bin/docker"
DOCKER_ENDPOINT = "unix:///Users/test/.docker/run/docker.sock"


def trusted_container() -> DockerContainer:
    spec = DockerContainerSpec(
        run_id=RUN_ID,
        role="agent",
        image=IMAGE,
        image_id=IMAGE_ID,
        limits=DockerLimits(
            memory_bytes=536_870_912,
            memory_swap_bytes=536_870_912,
            writable_layer_watchdog_bytes=268_435_456,
            nano_cpus=1_000_000_000,
            pids_limit=64,
            nofile_soft=1024,
            nofile_hard=1024,
            nproc_soft=64,
            nproc_hard=64,
        ),
    )
    labels = (
        ("org.edgeloopbench.instance", NAME),
        ("org.edgeloopbench.managed", "v0.6"),
        ("org.edgeloopbench.role", "agent"),
        ("org.edgeloopbench.run", RUN_ID),
    )
    return DockerContainer(CONTAINER_ID, NAME, IMAGE_ID, labels, spec)


class FakeBoundary:
    def __init__(
        self,
        *,
        cleanup_error: Exception | None = None,
        running_state: bool | Exception = True,
        writable_layer_sizes: Sequence[int | Exception] = (),
    ) -> None:
        self.cleanup_error = cleanup_error
        self.running_state = running_state
        self.prepare_calls: list[tuple[DockerContainer, str, str]] = []
        self.cleanup_calls: list[tuple[str, tuple[str, ...]]] = []
        self.state_calls: list[DockerContainer] = []
        self.writable_layer_sizes = list(writable_layer_sizes)
        self.writable_layer_calls: list[tuple[DockerContainer, float]] = []

    def prepare_exec_action(
        self, *, container: DockerContainer, action: str, cwd: str
    ) -> PreparedDockerExec:
        self.prepare_calls.append((container, action, cwd))
        argv = (
            DOCKER_BINARY,
            "--host",
            DOCKER_ENDPOINT,
            "container",
            "exec",
            "--workdir",
            cwd,
            "--user",
            CONTAINER_USER,
            container.identifier,
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            ACTION_WRAPPER_SCRIPT,
            ACTION_WRAPPER_ARG0,
            action,
        )
        return PreparedDockerExec(argv, container.identifier, cwd)

    def inspect_container_running(self, *, container: DockerContainer) -> bool:
        self.state_calls.append(container)
        if isinstance(self.running_state, Exception):
            raise self.running_state
        return self.running_state

    def inspect_container_writable_layer_bytes(
        self, *, container: DockerContainer, timeout_seconds: float
    ) -> int:
        self.writable_layer_calls.append((container, timeout_seconds))
        value = self.writable_layer_sizes.pop(0) if self.writable_layer_sizes else 0
        if isinstance(value, Exception):
            raise value
        return value

    def remove_run_containers(
        self, run_id: str, identifiers: Sequence[str]
    ) -> tuple[str, ...]:
        frozen = tuple(identifiers)
        self.cleanup_calls.append((run_id, frozen))
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return frozen


@dataclass(frozen=True)
class ProcessResponse:
    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int = 0
    hang: bool = False
    read_error: Exception | None = None
    advance_clock_to: float | None = None
    pid: int | None = None
    emit_start_attestation: bool = True
    wait_for_kill: bool = False


class ErrorStream:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def read(self, _size: int = -1) -> bytes:
        raise self._error

    def close(self) -> None:
        return None


class FakeProcess:
    def __init__(
        self,
        response: ProcessResponse,
        clock: ManualClock | None = None,
    ) -> None:
        self.response = response
        self.clock = clock
        self.stdout = (
            ErrorStream(response.read_error)
            if response.read_error is not None
            else io.BytesIO(response.stdout)
        )
        self.stderr = io.BytesIO(response.stderr)
        self.returncode: int | None = None if response.hang else response.returncode
        self.pid = response.pid
        self.killed = False
        self.terminated = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            if self.response.wait_for_kill and self.terminated.wait(timeout):
                assert self.returncode is not None
                return self.returncode
            if self.response.advance_clock_to is not None:
                assert self.clock is not None
                self.clock.value = self.response.advance_clock_to
            elif self.clock is not None and timeout is not None:
                self.clock.value += timeout
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.terminated.set()


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakePopenFactory:
    def __init__(
        self,
        responses: Sequence[ProcessResponse | Exception],
        *,
        clock: ManualClock | None = None,
    ) -> None:
        self.responses = list(responses)
        self.clock = clock
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, argv: Sequence[str], **kwargs: object) -> FakeProcess:
        self.calls.append((tuple(argv), dict(kwargs)))
        if not self.responses:
            raise AssertionError(f"unexpected Popen call: {argv!r}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if "exec" in argv and response.emit_start_attestation:
            response = replace(
                response,
                stdout=ACTION_STARTED_MARKER + response.stdout,
                stderr=ACTION_STARTED_MARKER + response.stderr,
            )
        process = FakeProcess(response, self.clock)
        self.processes.append(process)
        return process


class ExhaustAfterPreAuditExecutor(DockerActionExecutor):
    def __init__(self, *, test_clock: ManualClock, **kwargs: object) -> None:
        self._test_clock = test_clock
        self._capture_count = 0
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def _run_streaming(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def,override]
        capture = super()._run_streaming(*args, **kwargs)  # type: ignore[arg-type]
        self._capture_count += 1
        if self._capture_count == 1:
            self._test_clock.value = float(kwargs["deadline"])
        return capture


class FailingThread:
    def __init__(self, *, fail_start: bool) -> None:
        self.fail_start = fail_start
        self.started = False
        self.joined = False

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("thread start failed with host detail")
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self.joined = True


class SecondStartFails:
    def __init__(self) -> None:
        self.threads: list[FailingThread] = []

    def __call__(self, **_kwargs: object) -> FailingThread:
        thread = FailingThread(fail_start=len(self.threads) == 1)
        self.threads.append(thread)
        return thread


class SecondConstructionFails:
    def __init__(self) -> None:
        self.calls = 0
        self.threads: list[FailingThread] = []

    def __call__(self, **_kwargs: object) -> FailingThread:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("thread construction failed with host detail")
        thread = FailingThread(fail_start=False)
        self.threads.append(thread)
        return thread


class KillProcessGroupRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pid: int, requested_signal: int) -> None:
        self.calls.append((pid, requested_signal))


class FailingKillProcessGroup(KillProcessGroupRecorder):
    def __call__(self, pid: int, requested_signal: int) -> None:
        super().__call__(pid, requested_signal)
        raise RuntimeError("private process-group failure")


def limits(**overrides: object) -> DockerActionLimits:
    values: dict[str, object] = {
        "deadline_seconds": 10.0,
        "private_stream_limit_bytes": 64,
        "observation_limit_bytes": 32,
        "read_chunk_bytes": 16,
        "io_queue_chunks": 4,
        "writable_layer_sample_interval_seconds": 0.01,
        "writable_layer_probe_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return DockerActionLimits(**values)  # type: ignore[arg-type]


def executor(
    responses: Sequence[ProcessResponse | Exception],
    *,
    boundary: FakeBoundary | None = None,
    clock: ManualClock | None = None,
    thread_factory: object = threading.Thread,
    kill_process_group: object | None = None,
) -> tuple[DockerActionExecutor, FakeBoundary, FakePopenFactory]:
    fake_boundary = FakeBoundary() if boundary is None else boundary
    fake_clock = ManualClock() if clock is None else clock
    popen = FakePopenFactory(responses, clock=fake_clock)
    executor_kwargs: dict[str, object] = {}
    if kill_process_group is not None:
        executor_kwargs["kill_process_group"] = kill_process_group
    return (
        DockerActionExecutor(
            boundary=fake_boundary,
            popen_factory=popen,
            thread_factory=thread_factory,  # type: ignore[arg-type]
            monotonic=fake_clock,
            expected_docker_binary=DOCKER_BINARY,
            expected_endpoint=DOCKER_ENDPOINT,
            **executor_kwargs,
        ),
        fake_boundary,
        popen,
    )


class DockerActionExecutorTests(unittest.TestCase):
    def test_writable_layer_watchdog_interrupts_process_after_streams_close(self) -> None:
        threshold = trusted_container().spec.limits.writable_layer_watchdog_bytes
        boundary = FakeBoundary(writable_layer_sizes=(0, 0, threshold + 1))
        subject, checked, popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(hang=True, wait_for_kill=True),
            ],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="close streams and keep writing",
            cwd="/testbed",
            limits=limits(deadline_seconds=1.0),
        )

        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertEqual(
            result.policy_failure,
            ActionPolicyFailure.WRITABLE_LAYER_OVERFLOW,
        )
        self.assertEqual(result.failure_stage, "action")
        self.assertIsNone(result.exit_code)
        self.assertTrue(popen.processes[-1].killed)
        self.assertEqual(checked.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_writable_layer_watchdog_samples_pre_during_and_post_action(self) -> None:
        boundary = FakeBoundary(writable_layer_sizes=(10, 20, 30))
        subject, checked, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"ok\n"),
                ProcessResponse(stdout=IDLE_TOP),
            ],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.EXECUTED)
        self.assertGreaterEqual(len(checked.writable_layer_calls), 3)
        self.assertTrue(
            all(timeout == 1.0 for _container, timeout in checked.writable_layer_calls)
        )

    def test_early_writable_layer_overflow_kills_before_start_marker_is_drained(self) -> None:
        threshold = trusted_container().spec.limits.writable_layer_watchdog_bytes
        boundary = FakeBoundary(writable_layer_sizes=(0, threshold + 1))
        subject, checked, popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(hang=True),
            ],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="fill writable layer",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.WRITABLE_LAYER_WATCHDOG,
        )
        self.assertIsNone(result.policy_failure)
        self.assertFalse(result.action_started)
        self.assertEqual(result.failure_stage, "action")
        self.assertTrue(popen.processes[-1].killed)
        self.assertEqual(checked.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_writable_layer_overflow_after_post_audit_fails_closed(self) -> None:
        threshold = trusted_container().spec.limits.writable_layer_watchdog_bytes
        boundary = FakeBoundary(writable_layer_sizes=(0, 0, threshold + 1))
        subject, checked, popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"ok\n"),
                ProcessResponse(stdout=IDLE_TOP),
            ],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="fill at exit",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertEqual(
            result.policy_failure,
            ActionPolicyFailure.WRITABLE_LAYER_OVERFLOW,
        )
        self.assertEqual(result.failure_stage, "post_storage")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(popen.calls), 3)
        self.assertEqual(checked.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_writable_layer_probe_failure_during_action_kills_and_cleans(self) -> None:
        boundary = FakeBoundary(
            writable_layer_sizes=(0, RuntimeError("private daemon detail"))
        )
        subject, checked, popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(hang=True),
            ],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="keep writing",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.WRITABLE_LAYER_WATCHDOG,
        )
        self.assertEqual(result.failure_stage, "action")
        self.assertFalse(result.action_started)
        self.assertTrue(popen.processes[-1].killed)
        self.assertNotIn("private daemon detail", repr(result))
        self.assertEqual(checked.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_writable_probe_defers_attested_container_exit_to_post_audit(self) -> None:
        boundary = FakeBoundary(
            running_state=False,
            writable_layer_sizes=(0, RuntimeError("container is not running")),
        )
        subject, checked, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(returncode=137),
                ProcessResponse(returncode=1),
            ],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="kill 1",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertEqual(
            result.policy_failure,
            ActionPolicyFailure.CONTAINER_TERMINATED,
        )
        self.assertEqual(result.failure_stage, "post_audit")
        self.assertEqual(result.exit_code, 137)
        self.assertGreaterEqual(len(checked.state_calls), 2)
        self.assertEqual(checked.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_writable_layer_probe_failure_is_infrastructure_invalid_and_cleans(self) -> None:
        boundary = FakeBoundary(
            writable_layer_sizes=(RuntimeError("private daemon detail"),)
        )
        subject, checked, popen = executor([], boundary=boundary)

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.WRITABLE_LAYER_WATCHDOG,
        )
        self.assertEqual(result.failure_stage, "pre_storage")
        self.assertEqual(popen.calls, [])
        self.assertNotIn("private daemon detail", repr(result))
        self.assertEqual(checked.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_clean_docker_exec_failure_without_start_attestation_is_not_an_attempt(self) -> None:
        subject, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(
                    stderr=b"daemon rejected exec\n",
                    returncode=126,
                    emit_start_attestation=False,
                ),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.ACTION_NOT_STARTED,
        )
        self.assertFalse(result.action_started)
        self.assertIsNone(result.exit_code)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_stopped_idle_process_cannot_pass_the_post_action_audit(self) -> None:
        subject, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(),
                ProcessResponse(stdout=b"101 1 Ts tail\n"),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="kill -STOP 1",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertEqual(result.policy_failure, ActionPolicyFailure.RESIDUAL_PROCESS)
        self.assertFalse(result.admissible)
        self.assertTrue(result.action_started)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_preexisting_stopped_idle_process_blocks_before_the_action(self) -> None:
        subject, boundary, popen = executor(
            [ProcessResponse(stdout=b"101 1 Ts tail\n")]
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.PROCESS_AUDIT,
        )
        self.assertFalse(result.action_started)
        self.assertEqual(len(popen.calls), 1)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_one_sided_marker_does_not_attest_a_clean_action_start(self) -> None:
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                subject, boundary, _popen = executor(
                    [
                        ProcessResponse(stdout=IDLE_TOP),
                        ProcessResponse(
                            **{stream: ACTION_STARTED_MARKER},
                            emit_start_attestation=False,
                        ),
                    ]
                )

                result = subject.execute(
                    container=trusted_container(),
                    action="true",
                    cwd="/testbed",
                    limits=limits(),
                )

                self.assertEqual(
                    result.infrastructure_failure,
                    InfrastructureFailure.ACTION_NOT_STARTED,
                )
                self.assertFalse(result.action_started)
                self.assertIsNone(result.exit_code)
                self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_model_emitted_reserved_marker_remains_and_fails_text_policy(self) -> None:
        subject, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=ACTION_STARTED_MARKER),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="printf reserved-marker",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertEqual(result.policy_failure, ActionPolicyFailure.INVALID_TEXT)
        self.assertEqual(result.private_stdout, ACTION_STARTED_MARKER)
        self.assertTrue(result.action_started)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_stream_limit_applies_after_attestation_bytes_are_stripped(self) -> None:
        limit = 32
        exact, _boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"x" * limit, stderr=b"y" * limit),
                ProcessResponse(stdout=IDLE_TOP),
            ]
        )
        exact_result = exact.execute(
            container=trusted_container(),
            action="emit exact limits",
            cwd="/testbed",
            limits=limits(
                private_stream_limit_bytes=limit,
                observation_limit_bytes=128,
            ),
        )
        self.assertEqual(exact_result.disposition, ActionDisposition.EXECUTED)
        self.assertEqual(exact_result.stdout_bytes_observed, limit)
        self.assertEqual(exact_result.stderr_bytes_observed, limit)

        overflow, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"x" * (limit + 1)),
            ]
        )
        overflow_result = overflow.execute(
            container=trusted_container(),
            action="emit one excess byte",
            cwd="/testbed",
            limits=limits(private_stream_limit_bytes=limit),
        )
        self.assertEqual(
            overflow_result.policy_failure,
            ActionPolicyFailure.OUTPUT_OVERFLOW,
        )
        self.assertIsNone(overflow_result.exit_code)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_bash_syntax_error_after_markers_is_a_started_nonzero_action(self) -> None:
        subject, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stderr=b"syntax error\n", returncode=2),
                ProcessResponse(stdout=IDLE_TOP),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="if then",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.EXECUTED)
        self.assertTrue(result.action_started)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.private_stderr, b"syntax error\n")
        self.assertEqual(boundary.cleanup_calls, [])

    def test_success_streams_private_bytes_and_returns_bounded_observation(self) -> None:
        action_stdout = "αβγδεζηθικλμνξοπρστυφχψω\n".encode()
        subject, boundary, popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=action_stdout, stderr=b"warning\n"),
                ProcessResponse(stdout=IDLE_TOP),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="printf unicode",
            cwd="/testbed/dir1",
            limits=limits(private_stream_limit_bytes=128),
        )

        self.assertEqual(result.disposition, ActionDisposition.EXECUTED)
        self.assertTrue(result.admissible)
        self.assertIsNone(result.infrastructure_failure)
        self.assertEqual(result.cleanup_outcome, CleanupOutcome.NOT_REQUIRED)
        self.assertTrue(result.action_started)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.private_stdout, action_stdout)
        self.assertEqual(result.private_stderr, b"warning\n")
        self.assertLessEqual(len(result.observation.encode("utf-8")), 32)
        self.assertTrue(result.observation_truncated)
        self.assertEqual(boundary.cleanup_calls, [])
        self.assertEqual(len(popen.calls), 3)

        event = result.journal_event()
        self.assertEqual(event["type"], "docker_action_completed")
        self.assertEqual(event["disposition"], "executed")
        self.assertIs(event["action_started"], True)
        self.assertNotIn("observation", event)
        self.assertNotIn(action_stdout, event.values())
        self.assertNotIn(CONTAINER_ID, event.values())
        self.assertTrue(all(_journal_safe(value) for value in event.values()))

    def test_clean_nonzero_exit_is_executed_and_left_for_the_evaluator(self) -> None:
        subject, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stderr=b"missing file\n", returncode=7),
                ProcessResponse(stdout=IDLE_TOP),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="test -f expected",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.EXECUTED)
        self.assertTrue(result.admissible)
        self.assertEqual(result.exit_code, 7)
        self.assertIsNone(result.infrastructure_failure)
        self.assertEqual(result.cleanup_outcome, CleanupOutcome.NOT_REQUIRED)
        self.assertEqual(boundary.cleanup_calls, [])

    def test_action_that_terminates_its_container_remains_a_model_failure(self) -> None:
        boundary = FakeBoundary(running_state=False)
        subject, _boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(returncode=137),
                ProcessResponse(returncode=1, stderr=b"container is not running\n"),
            ],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="kill -KILL 1",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertEqual(
            result.policy_failure,
            ActionPolicyFailure.CONTAINER_TERMINATED,
        )
        self.assertIsNone(result.infrastructure_failure)
        self.assertEqual(result.observation, "Command terminated the task container.")
        self.assertTrue(result.action_started)
        self.assertEqual(result.exit_code, 137)
        self.assertEqual(boundary.state_calls, [trusted_container()])
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_failed_post_audit_with_unprovable_state_is_infrastructure_invalid(self) -> None:
        boundary = FakeBoundary(running_state=RuntimeError("private daemon detail"))
        subject, _boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(returncode=137),
                ProcessResponse(returncode=1, stderr=b"daemon error\n"),
            ],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="kill -KILL 1",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.PROCESS_AUDIT,
        )
        self.assertIsNone(result.policy_failure)
        self.assertNotIn("private daemon detail", repr(result))
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_timeout_is_a_valid_model_policy_failure_and_cleans_exact_identity(self) -> None:
        clock = ManualClock()
        subject, boundary, popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(hang=True, advance_clock_to=11.0),
            ],
            clock=clock,
        )

        result = subject.execute(
            container=trusted_container(),
            action="sleep 99999",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertEqual(result.policy_failure, ActionPolicyFailure.TIMEOUT)
        self.assertIsNone(result.infrastructure_failure)
        self.assertFalse(result.admissible)
        self.assertEqual(result.observation, "Command timed out.")
        self.assertEqual(result.cleanup_outcome, CleanupOutcome.REMOVED)
        self.assertTrue(result.action_started)
        self.assertIsNone(result.exit_code)
        self.assertTrue(popen.processes[-1].killed)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_stdout_and_stderr_overflow_are_bounded_and_fail_closed(self) -> None:
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                payload = b"x" * 200
                response = ProcessResponse(**{stream: payload}, hang=True)
                subject, boundary, popen = executor(
                    [ProcessResponse(stdout=IDLE_TOP), response]
                )

                result = subject.execute(
                    container=trusted_container(),
                    action="produce output",
                    cwd="/testbed",
                    limits=limits(private_stream_limit_bytes=32),
                )

                self.assertEqual(
                    result.policy_failure,
                    ActionPolicyFailure.OUTPUT_OVERFLOW,
                )
                self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
                self.assertIsNone(result.infrastructure_failure)
                self.assertLessEqual(len(result.private_stdout), 32)
                self.assertLessEqual(len(result.private_stderr), 32)
                self.assertTrue(popen.processes[-1].killed)
                self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_action_spawn_error_is_sanitized_and_fails_closed(self) -> None:
        subject, boundary, _popen = executor(
            [ProcessResponse(stdout=IDLE_TOP), OSError("secret host detail")]
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.infrastructure_failure, InfrastructureFailure.SPAWN_ERROR)
        self.assertIsNone(result.policy_failure)
        self.assertFalse(result.action_started)
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.observation, "")
        self.assertNotIn("secret", repr(result))
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_deadline_exhausted_before_action_spawn_is_infrastructure_invalid(self) -> None:
        boundary = FakeBoundary()
        clock = ManualClock()
        popen = FakePopenFactory([ProcessResponse(stdout=IDLE_TOP)], clock=clock)
        subject = ExhaustAfterPreAuditExecutor(
            test_clock=clock,
            boundary=boundary,
            popen_factory=popen,
            monotonic=clock,
            expected_docker_binary=DOCKER_BINARY,
            expected_endpoint=DOCKER_ENDPOINT,
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.ACTION_NOT_STARTED,
        )
        self.assertFalse(result.action_started)
        self.assertIsNone(result.exit_code)
        self.assertEqual(len(popen.calls), 1)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_prepare_rejection_has_no_synthetic_exit_code_or_spawn(self) -> None:
        boundary = FakeBoundary()

        def reject_prepare(**_kwargs: object) -> PreparedDockerExec:
            raise RuntimeError("private boundary detail")

        boundary.prepare_exec_action = reject_prepare  # type: ignore[method-assign]
        subject, checked, popen = executor([], boundary=boundary)

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.BOUNDARY_REJECTED,
        )
        self.assertFalse(result.action_started)
        self.assertIsNone(result.exit_code)
        self.assertEqual(popen.calls, [])
        self.assertEqual(checked.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_ambiguous_cleanup_dominates_an_infrastructure_trigger(self) -> None:
        boundary = FakeBoundary(cleanup_error=RuntimeError("private cleanup detail"))
        subject, _boundary, _popen = executor(
            [ProcessResponse(stdout=IDLE_TOP), OSError("private spawn detail")],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.CLEANUP_AMBIGUOUS,
        )
        self.assertEqual(result.cleanup_outcome, CleanupOutcome.AMBIGUOUS)
        self.assertFalse(result.action_started)

    def test_cleanup_ambiguity_is_reported_without_an_unscoped_fallback(self) -> None:
        boundary = FakeBoundary(
            cleanup_error=DockerCleanupRefused("identity labels changed")
        )
        subject, _boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"x" * 200),
            ],
            boundary=boundary,
        )

        result = subject.execute(
            container=trusted_container(),
            action="produce output",
            cwd="/testbed",
            limits=limits(private_stream_limit_bytes=32),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(result.infrastructure_failure, InfrastructureFailure.CLEANUP_AMBIGUOUS)
        self.assertEqual(result.policy_failure, ActionPolicyFailure.OUTPUT_OVERFLOW)
        self.assertEqual(result.cleanup_outcome, CleanupOutcome.AMBIGUOUS)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_residual_background_process_after_action_invalidates_attempt(self) -> None:
        contaminated = IDLE_TOP + b"310 101 S python3\n"
        subject, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"started\n"),
                ProcessResponse(stdout=contaminated),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="python3 worker.py &",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(
            result.policy_failure,
            ActionPolicyFailure.RESIDUAL_PROCESS,
        )
        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertIsNone(result.infrastructure_failure)
        self.assertEqual(result.observation, "Command left a residual process.")
        self.assertEqual(result.cleanup_outcome, CleanupOutcome.REMOVED)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_idle_pid_identity_change_is_model_contamination(self) -> None:
        replaced_idle = b"999 1 Ss tail\n"
        subject, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"done\n"),
                ProcessResponse(stdout=replaced_idle),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertEqual(result.policy_failure, ActionPolicyFailure.RESIDUAL_PROCESS)
        self.assertFalse(result.admissible)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_invalid_utf8_returns_only_a_frozen_text_policy_observation(self) -> None:
        subject, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"valid-prefix\xffsecret"),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="emit binary",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(
            result.policy_failure,
            ActionPolicyFailure.INVALID_TEXT,
        )
        self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
        self.assertIsNone(result.infrastructure_failure)
        self.assertEqual(result.observation, "Command output violated text policy.")
        self.assertEqual(result.private_stdout, b"valid-prefix\xffsecret")
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_control_and_bidi_output_are_model_policy_failures(self) -> None:
        for hostile in (b"prefix\x1b[31mred", "prefix\u2066bidi".encode("utf-8")):
            with self.subTest(hostile=hostile):
                subject, boundary, _popen = executor(
                    [
                        ProcessResponse(stdout=IDLE_TOP),
                        ProcessResponse(stdout=hostile),
                    ]
                )

                result = subject.execute(
                    container=trusted_container(),
                    action="emit hostile text",
                    cwd="/testbed",
                    limits=limits(),
                )

                self.assertEqual(result.disposition, ActionDisposition.POLICY_FAILURE)
                self.assertEqual(result.policy_failure, ActionPolicyFailure.INVALID_TEXT)
                self.assertEqual(result.observation, "Command output violated text policy.")
                self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_observation_canaries_never_cross_the_agent_boundary(self) -> None:
        canaries = (
            (CONTAINER_ID, "stdout"),
            (NAME, "stderr"),
            (DOCKER_ENDPOINT, "stderr"),
            (DOCKER_BINARY, "stderr"),
            ("/Users/test/private/host-evidence.txt", "stderr"),
        )
        for canary, stream in canaries:
            with self.subTest(canary=canary, stream=stream):
                leaked = f"prefix {canary} suffix\n".encode("utf-8")
                subject, boundary, _popen = executor(
                    [
                        ProcessResponse(stdout=IDLE_TOP),
                        ProcessResponse(**{stream: leaked}),
                        ProcessResponse(stdout=IDLE_TOP),
                    ]
                )

                result = subject.execute(
                    container=trusted_container(),
                    action="emit protected host detail",
                    cwd="/testbed",
                    limits=limits(private_stream_limit_bytes=256),
                )

                self.assertEqual(
                    result.disposition,
                    ActionDisposition.INFRASTRUCTURE_INVALID,
                )
                self.assertEqual(
                    result.infrastructure_failure,
                    InfrastructureFailure.OBSERVATION_LEAK,
                )
                self.assertEqual(result.observation, "")
                self.assertTrue(result.action_started)
                self.assertEqual(getattr(result, f"private_{stream}"), leaked)
                self.assertNotIn(canary, repr(result))
                self.assertEqual(
                    boundary.cleanup_calls,
                    [(RUN_ID, (CONTAINER_ID,))],
                )

    def test_reader_exception_is_executor_invalid_and_never_exposes_details(self) -> None:
        subject, boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(read_error=RuntimeError("private host path")),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.EXECUTOR_EXCEPTION,
        )
        self.assertNotIn("private host path", repr(result))
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_reader_thread_start_failure_aborts_and_exactly_cleans(self) -> None:
        threads = SecondStartFails()
        subject, boundary, popen = executor(
            [ProcessResponse(stdout=IDLE_TOP, hang=True)],
            thread_factory=threads,
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.EXECUTOR_EXCEPTION,
        )
        self.assertTrue(popen.processes[0].killed)
        self.assertTrue(threads.threads[0].joined)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_reader_thread_constructor_failure_kills_group_after_parent_exit(self) -> None:
        threads = SecondConstructionFails()
        kill_process_group = KillProcessGroupRecorder()
        subject, boundary, popen = executor(
            [ProcessResponse(stdout=IDLE_TOP, returncode=0, pid=4321)],
            thread_factory=threads,
            kill_process_group=kill_process_group,
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(
            result.infrastructure_failure,
            InfrastructureFailure.EXECUTOR_EXCEPTION,
        )
        self.assertEqual(kill_process_group.calls, [(4321, signal.SIGKILL)])
        self.assertFalse(popen.processes[0].killed)
        self.assertFalse(threads.threads[0].started)
        self.assertFalse(threads.threads[0].joined)
        self.assertTrue(popen.processes[0].stdout.closed)
        self.assertTrue(popen.processes[0].stderr.closed)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_failed_group_kill_falls_back_to_a_live_parent_process(self) -> None:
        threads = SecondConstructionFails()
        kill_process_group = FailingKillProcessGroup()
        subject, boundary, popen = executor(
            [ProcessResponse(stdout=IDLE_TOP, hang=True, pid=4322)],
            thread_factory=threads,
            kill_process_group=kill_process_group,
        )

        result = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
        self.assertEqual(kill_process_group.calls, [(4322, signal.SIGKILL)])
        self.assertTrue(popen.processes[0].killed)
        self.assertEqual(boundary.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_model_action_remains_one_argv_value_and_popen_never_uses_a_shell(self) -> None:
        hostile = "printf x; touch /host-marker; echo $(id); echo `uname`"
        subject, _boundary, popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"ok\n"),
                ProcessResponse(stdout=IDLE_TOP),
            ]
        )

        result = subject.execute(
            container=trusted_container(),
            action=hostile,
            cwd="/testbed",
            limits=limits(),
        )

        self.assertEqual(result.disposition, ActionDisposition.EXECUTED)
        action_argv = popen.calls[1][0]
        self.assertEqual(action_argv.count(hostile), 1)
        self.assertEqual(action_argv[-4], "-c")
        self.assertIn("exec /bin/bash --noprofile --norc", action_argv[-3])
        self.assertEqual(action_argv[-2:], ("edgeloop-action-v1", hostile))
        for argv, kwargs in popen.calls:
            self.assertIs(kwargs["shell"], False)
            self.assertIs(kwargs["text"], False)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["env"], {"LANG": "C", "LC_ALL": "C"})
            self.assertEqual(argv.count(hostile), 1 if argv == action_argv else 0)
        self.assertEqual(
            popen.calls[0][0][-5:],
            (
                "container",
                "top",
                CONTAINER_ID,
                "-eo",
                "pid=PID,ppid=PPID,stat=STAT,comm=COMMAND",
            ),
        )

    def test_defective_boundary_cannot_change_host_binary_endpoint_or_exec_shape(self) -> None:
        valid = FakeBoundary()
        prepared = valid.prepare_exec_action(
            container=trusted_container(), action="true", cwd="/testbed"
        )
        mutations = (
            ("host shell", ("/bin/sh", "-c", *prepared.argv)),
            (
                "endpoint",
                (
                    prepared.argv[0],
                    "--host",
                    "unix:///tmp/other.sock",
                    *prepared.argv[3:],
                ),
            ),
            (
                "target",
                (*prepared.argv[:9], "d" * 64, *prepared.argv[10:]),
            ),
            (
                "cwd",
                (*prepared.argv[:6], "/", *prepared.argv[7:]),
            ),
            (
                "action",
                (*prepared.argv[:-1], "touch /host-marker"),
            ),
        )

        for label, hostile_argv in mutations:
            with self.subTest(label=label):
                boundary = FakeBoundary()

                def bad_prepare(**_kwargs: object) -> PreparedDockerExec:
                    return PreparedDockerExec(
                        tuple(hostile_argv),
                        CONTAINER_ID,
                        "/testbed",
                    )

                boundary.prepare_exec_action = bad_prepare  # type: ignore[method-assign]
                subject, checked, popen = executor([], boundary=boundary)

                result = subject.execute(
                    container=trusted_container(),
                    action="true",
                    cwd="/testbed",
                    limits=limits(),
                )

                self.assertEqual(result.disposition, ActionDisposition.INFRASTRUCTURE_INVALID)
                self.assertEqual(
                    result.infrastructure_failure,
                    InfrastructureFailure.BOUNDARY_REJECTED,
                )
                self.assertEqual(popen.calls, [])
                self.assertEqual(checked.cleanup_calls, [(RUN_ID, (CONTAINER_ID,))])

    def test_limits_reject_unbounded_or_ambiguous_values(self) -> None:
        invalid = (
            {"deadline_seconds": 0},
            {"deadline_seconds": math.inf},
            {"deadline_seconds": True},
            {"private_stream_limit_bytes": 0},
            {"observation_limit_bytes": 0},
            {"read_chunk_bytes": 0},
            {"io_queue_chunks": 0},
            {"writable_layer_sample_interval_seconds": 0},
            {"writable_layer_probe_timeout_seconds": 0},
        )
        for override in invalid:
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    limits(**override)

    def test_result_rejects_contradictory_or_malformed_accounting_state(self) -> None:
        subject, _boundary, _popen = executor(
            [
                ProcessResponse(stdout=IDLE_TOP),
                ProcessResponse(stdout=b"ok\n"),
                ProcessResponse(stdout=IDLE_TOP),
            ]
        )
        valid = subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )
        mutations = (
            {"infrastructure_failure": InfrastructureFailure.PROCESS_AUDIT},
            {"policy_failure": ActionPolicyFailure.TIMEOUT},
            {"cleanup_outcome": CleanupOutcome.REMOVED},
            {"exit_code": True},
            {"stdout_bytes_observed": -1},
            {"stdout_bytes_observed": 0, "private_stdout": b"not-observed"},
            {"elapsed_seconds": math.nan},
            {"failure_stage": "private/host/path"},
            {"action_started": False},
            {"observation": "unsafe\x1b[31mtext"},
            {"observation": "line\rrewrite"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    replace(valid, **mutation)

        invalid_subject, _invalid_boundary, _invalid_popen = executor(
            [ProcessResponse(stdout=IDLE_TOP), OSError("private spawn detail")]
        )
        invalid = invalid_subject.execute(
            container=trusted_container(),
            action="true",
            cwd="/testbed",
            limits=limits(),
        )
        with self.assertRaises(ValueError):
            replace(
                invalid,
                infrastructure_failure=InfrastructureFailure.CLEANUP_AMBIGUOUS,
            )
        with self.assertRaises(ValueError):
            replace(invalid, action_started=True)


def _journal_safe(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


if __name__ == "__main__":
    unittest.main()
