"""Bounded, fail-closed execution of prepared Docker agent actions.

Only :class:`~edgeloopbench.docker_cli.DockerCli` may prepare an action or
remove a container.  This module supplies the missing streaming boundary: it
executes the already-separated argv, caps both byte streams, enforces one
absolute monotonic deadline, and proves that the pinned idle process tree is
unchanged after the action.  It never invokes a host shell.

Raw action bytes are retained in the private result for later hashing and
debugging, but are excluded from ``repr`` and from the journal-safe event.
Infrastructure-invalid results deliberately have no agent-visible observation.
"""

from __future__ import annotations

import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import InitVar, dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Protocol, cast
from urllib.parse import urlsplit

from .docker_cli import (
    ACTION_START_MARKER,
    ACTION_WRAPPER_ARG0,
    ACTION_WRAPPER_SCRIPT,
    CONTAINER_USER,
    DockerContainer,
    PreparedDockerExec,
)


_AUDIT_STREAM_LIMIT_BYTES = 64 * 1024
_MAX_PRIVATE_STREAM_LIMIT_BYTES = 16 * 1024 * 1024
_MAX_OBSERVATION_LIMIT_BYTES = 1024 * 1024
_MAX_READ_CHUNK_BYTES = 64 * 1024
_MAX_IO_QUEUE_CHUNKS = 64
_MAX_DEADLINE_SECONDS = 3600.0
_MAX_WRITABLE_LAYER_SAMPLE_INTERVAL_SECONDS = 5.0
_MAX_WRITABLE_LAYER_PROBE_TIMEOUT_SECONDS = 5.0
_PROCESS_WAIT_POLL_SECONDS = 0.05
_PROCESS_LINE = re.compile(
    rb"([1-9][0-9]*)[ \t]+([0-9]+)[ \t]+([A-Z][A-Za-z0-9+<]*)"
    rb"[ \t]+([A-Za-z0-9_.-]+)"
)
_HEALTHY_IDLE_STATE = re.compile(r"^S[<NLsl+]*$")
_BOUNDARY_IDENTITY_SEAL = object()


class ActionDisposition(str, Enum):
    """Outcome class used for benchmark accounting."""

    EXECUTED = "executed"
    POLICY_FAILURE = "policy_failure"
    INFRASTRUCTURE_INVALID = "infrastructure_invalid"


class InfrastructureFailure(str, Enum):
    """Sanitized reason an action cannot enter effectiveness accounting."""

    BOUNDARY_REJECTED = "boundary_rejected"
    PROCESS_AUDIT = "process_audit"
    SPAWN_ERROR = "spawn_error"
    ACTION_NOT_STARTED = "action_not_started"
    EXECUTOR_EXCEPTION = "executor_exception"
    OBSERVATION_LEAK = "observation_leak"
    CLEANUP_AMBIGUOUS = "cleanup_ambiguous"
    WRITABLE_LAYER_WATCHDOG = "writable_layer_watchdog"


class ActionPolicyFailure(str, Enum):
    """Model-caused policy outcomes that remain in effectiveness denominators."""

    TIMEOUT = "timeout"
    OUTPUT_OVERFLOW = "output_overflow"
    INVALID_TEXT = "invalid_text"
    RESIDUAL_PROCESS = "residual_process"
    CONTAINER_TERMINATED = "container_terminated"
    WRITABLE_LAYER_OVERFLOW = "writable_layer_overflow"


class _CaptureFailure(str, Enum):
    TIMEOUT = "timeout"
    OUTPUT_OVERFLOW = "output_overflow"
    SPAWN_ERROR = "spawn_error"
    EXECUTOR_EXCEPTION = "executor_exception"
    WRITABLE_LAYER_OVERFLOW = "writable_layer_overflow"
    WRITABLE_LAYER_WATCHDOG = "writable_layer_watchdog"


_POLICY_OBSERVATIONS = {
    ActionPolicyFailure.TIMEOUT: "Command timed out.",
    ActionPolicyFailure.OUTPUT_OVERFLOW: "Command output exceeded the safety limit.",
    ActionPolicyFailure.INVALID_TEXT: "Command output violated text policy.",
    ActionPolicyFailure.RESIDUAL_PROCESS: "Command left a residual process.",
    ActionPolicyFailure.CONTAINER_TERMINATED: (
        "Command terminated the task container."
    ),
    ActionPolicyFailure.WRITABLE_LAYER_OVERFLOW: (
        "Command exceeded the sampled writable-layer safety limit."
    ),
}


class CleanupOutcome(str, Enum):
    """Whether exact run-scoped force cleanup was required and proven."""

    NOT_REQUIRED = "not_required"
    REMOVED = "removed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class DockerActionLimits:
    """Frozen per-action host limits, independent of container limits."""

    deadline_seconds: float
    private_stream_limit_bytes: int
    observation_limit_bytes: int
    read_chunk_bytes: int = 4096
    io_queue_chunks: int = 8
    writable_layer_sample_interval_seconds: float = 0.25
    writable_layer_probe_timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        _require_positive_finite(
            self.deadline_seconds,
            "deadline_seconds",
            maximum=_MAX_DEADLINE_SECONDS,
        )
        _require_positive_integer(
            self.private_stream_limit_bytes,
            "private_stream_limit_bytes",
            maximum=_MAX_PRIVATE_STREAM_LIMIT_BYTES,
        )
        _require_positive_integer(
            self.observation_limit_bytes,
            "observation_limit_bytes",
            maximum=_MAX_OBSERVATION_LIMIT_BYTES,
        )
        _require_positive_integer(
            self.read_chunk_bytes,
            "read_chunk_bytes",
            maximum=_MAX_READ_CHUNK_BYTES,
        )
        _require_positive_integer(
            self.io_queue_chunks,
            "io_queue_chunks",
            maximum=_MAX_IO_QUEUE_CHUNKS,
        )
        _require_positive_finite(
            self.writable_layer_sample_interval_seconds,
            "writable_layer_sample_interval_seconds",
            maximum=_MAX_WRITABLE_LAYER_SAMPLE_INTERVAL_SECONDS,
        )
        _require_positive_finite(
            self.writable_layer_probe_timeout_seconds,
            "writable_layer_probe_timeout_seconds",
            maximum=_MAX_WRITABLE_LAYER_PROBE_TIMEOUT_SECONDS,
        )


@dataclass(frozen=True, slots=True, repr=False)
class DockerActionExecutorBoundaryIdentity:
    """Read-only binding of one executor to its exact Docker CLI object."""

    boundary: object = field(repr=False, compare=False)
    expected_docker_binary: str
    expected_endpoint: str
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _BOUNDARY_IDENTITY_SEAL:
            raise ValueError(
                "Docker action boundary identities must be executor-issued"
            )
        if not callable(getattr(self.boundary, "prepare_exec_action", None)):
            raise ValueError("Docker action boundary identity is invalid")
        if (
            not isinstance(self.expected_docker_binary, str)
            or not os.path.isabs(self.expected_docker_binary)
            or os.path.normpath(self.expected_docker_binary)
            != self.expected_docker_binary
            or "\x00" in self.expected_docker_binary
        ):
            raise ValueError("Docker action boundary binary path is invalid")
        _require_local_endpoint(self.expected_endpoint)

    def __repr__(self) -> str:
        return "<DockerActionExecutorBoundaryIdentity>"


@dataclass(frozen=True)
class DockerActionResult:
    """Private execution evidence plus a separately bounded observation."""

    disposition: ActionDisposition
    infrastructure_failure: InfrastructureFailure | None
    policy_failure: ActionPolicyFailure | None
    failure_stage: str | None
    cleanup_outcome: CleanupOutcome
    exit_code: int | None
    action_started: bool
    observation: str = field(repr=False)
    private_stdout: bytes = field(repr=False)
    private_stderr: bytes = field(repr=False)
    stdout_bytes_observed: int
    stderr_bytes_observed: int
    observation_truncated: bool
    elapsed_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, ActionDisposition):
            raise ValueError("action disposition must be typed")
        if self.infrastructure_failure is not None and not isinstance(
            self.infrastructure_failure, InfrastructureFailure
        ):
            raise ValueError("infrastructure failure must be typed")
        if self.policy_failure is not None and not isinstance(
            self.policy_failure, ActionPolicyFailure
        ):
            raise ValueError("policy failure must be typed")
        if not isinstance(self.cleanup_outcome, CleanupOutcome):
            raise ValueError("cleanup outcome must be typed")
        if self.failure_stage not in {
            None,
            "prepare",
            "pre_storage",
            "pre_audit",
            "action",
            "post_storage",
            "post_audit",
        }:
            raise ValueError("failure stage is not a frozen public value")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise ValueError("exit code must be an integer or null")
        if not isinstance(self.action_started, bool):
            raise ValueError("action_started must be boolean")
        if not self.action_started and self.exit_code is not None:
            raise ValueError("an unstarted action cannot have an exit code")
        if not isinstance(self.observation, str):
            raise ValueError("observation must be text")
        try:
            normalized_observation = _normalize_output_text(
                self.observation.encode("utf-8")
            )
        except UnicodeError as error:
            raise ValueError("observation must satisfy frozen text policy") from error
        if normalized_observation != self.observation:
            raise ValueError("observation must already be normalized")
        if not isinstance(self.private_stdout, bytes) or not isinstance(
            self.private_stderr, bytes
        ):
            raise ValueError("private action streams must be immutable bytes")
        for observed, captured in (
            (self.stdout_bytes_observed, self.private_stdout),
            (self.stderr_bytes_observed, self.private_stderr),
        ):
            if (
                isinstance(observed, bool)
                or not isinstance(observed, int)
                or observed < len(captured)
            ):
                raise ValueError("observed stream bytes cannot undercount capture")
        if not isinstance(self.observation_truncated, bool):
            raise ValueError("observation_truncated must be boolean")
        if (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, (int, float))
            or not math.isfinite(float(self.elapsed_seconds))
            or self.elapsed_seconds < 0
        ):
            raise ValueError("elapsed_seconds must be finite and non-negative")

        if self.disposition is ActionDisposition.EXECUTED:
            if (
                self.infrastructure_failure is not None
                or self.policy_failure is not None
                or self.failure_stage is not None
                or self.cleanup_outcome is not CleanupOutcome.NOT_REQUIRED
                or self.exit_code is None
                or not self.action_started
            ):
                raise ValueError("executed action result has contradictory accounting")
        elif self.disposition is ActionDisposition.POLICY_FAILURE:
            if (
                self.infrastructure_failure is not None
                or self.policy_failure is None
                or self.failure_stage not in {"action", "post_storage", "post_audit"}
                or self.cleanup_outcome is not CleanupOutcome.REMOVED
                or not self.action_started
                or self.observation != _POLICY_OBSERVATIONS[self.policy_failure]
                or self.observation_truncated
            ):
                raise ValueError("policy-failure result has contradictory accounting")
        elif (
            self.infrastructure_failure is None
            or self.failure_stage is None
            or self.cleanup_outcome is CleanupOutcome.NOT_REQUIRED
            or self.observation
            or self.observation_truncated
            or (
                self.policy_failure is not None
                and self.infrastructure_failure
                is not InfrastructureFailure.CLEANUP_AMBIGUOUS
            )
        ):
            raise ValueError("infrastructure-invalid result has contradictory accounting")
        if self.disposition is ActionDisposition.INFRASTRUCTURE_INVALID:
            if (
                self.cleanup_outcome is CleanupOutcome.AMBIGUOUS
                and self.infrastructure_failure
                is not InfrastructureFailure.CLEANUP_AMBIGUOUS
            ) or (
                self.cleanup_outcome is CleanupOutcome.REMOVED
                and self.infrastructure_failure
                is InfrastructureFailure.CLEANUP_AMBIGUOUS
            ):
                raise ValueError("cleanup outcome and infrastructure reason disagree")
            if self.failure_stage in {"prepare", "pre_storage", "pre_audit"} and self.action_started:
                raise ValueError("action cannot predate preparation or pre-audit")
            if self.failure_stage == "post_audit" and not self.action_started:
                raise ValueError("post-audit failure requires a started action")
            if (
                self.infrastructure_failure
                in {
                    InfrastructureFailure.ACTION_NOT_STARTED,
                    InfrastructureFailure.SPAWN_ERROR,
                }
                and self.action_started
            ):
                raise ValueError("unstarted-action reason contradicts start accounting")
            if (
                self.infrastructure_failure is InfrastructureFailure.OBSERVATION_LEAK
                and not self.action_started
            ):
                raise ValueError("observation leak requires a started action")

    @property
    def admissible(self) -> bool:
        """Whether the executed state may be checkpointed and evaluated."""

        return self.disposition in {
            ActionDisposition.EXECUTED,
        }

    def journal_event(self) -> dict[str, object]:
        """Return scalar-only evidence suitable for ``append_journal_event``.

        Neither the model action, container identity, raw output, observation,
        host path, nor exception detail crosses into the event journal.
        """

        return {
            "type": "docker_action_completed",
            "disposition": self.disposition.value,
            "infrastructure_failure": (
                None
                if self.infrastructure_failure is None
                else self.infrastructure_failure.value
            ),
            "policy_failure": (
                None if self.policy_failure is None else self.policy_failure.value
            ),
            "failure_stage": self.failure_stage,
            "cleanup_outcome": self.cleanup_outcome.value,
            "exit_code": self.exit_code,
            "action_started": self.action_started,
            "stdout_bytes_observed": self.stdout_bytes_observed,
            "stderr_bytes_observed": self.stderr_bytes_observed,
            "captured_stdout_sha256": "sha256:"
            + sha256(self.private_stdout).hexdigest(),
            "captured_stderr_sha256": "sha256:"
            + sha256(self.private_stderr).hexdigest(),
            "observation_utf8_bytes": len(self.observation.encode("utf-8")),
            "observation_truncated": self.observation_truncated,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
        }


class DockerActionBoundary(Protocol):
    """The exact public subset supplied by ``DockerCli``."""

    def prepare_exec_action(
        self, *, container: DockerContainer, action: str, cwd: str
    ) -> PreparedDockerExec: ...

    def remove_run_containers(
        self, run_id: str, identifiers: Sequence[str]
    ) -> tuple[str, ...]: ...

    def inspect_container_running(self, *, container: DockerContainer) -> bool: ...

    def inspect_container_writable_layer_bytes(
        self, *, container: DockerContainer, timeout_seconds: float
    ) -> int: ...


class PopenProcess(Protocol):
    stdout: object
    stderr: object
    returncode: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class PopenFactory(Protocol):
    def __call__(self, argv: Sequence[str], **kwargs: object) -> PopenProcess: ...


class ThreadFactory(Protocol):
    def __call__(self, **kwargs: object) -> threading.Thread: ...


@dataclass(frozen=True)
class _StreamItem:
    channel: str
    chunk: bytes | None = None
    failed: bool = False


class _WritableLayerSignal(str, Enum):
    OVERFLOW = "overflow"
    PROBE_FAILURE = "probe_failure"


@dataclass(frozen=True)
class _CommandCapture:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_bytes_observed: int
    stderr_bytes_observed: int
    failure: _CaptureFailure | None
    spawned: bool

    def __post_init__(self) -> None:
        if not isinstance(self.spawned, bool):
            raise ValueError("spawned marker must be boolean")
        if not self.spawned and (
            self.returncode is not None
            or self.stdout
            or self.stderr
            or self.stdout_bytes_observed
            or self.stderr_bytes_observed
        ):
            raise ValueError("an unspawned command cannot have process evidence")


@dataclass(frozen=True, order=True)
class _ProcessEntry:
    pid: int
    ppid: int
    state: str
    command: str


class DockerActionExecutor:
    """Execute one validated agent action and audit its process lifecycle."""

    def __init__(
        self,
        *,
        boundary: DockerActionBoundary,
        expected_docker_binary: str,
        expected_endpoint: str,
        popen_factory: PopenFactory = subprocess.Popen,
        thread_factory: ThreadFactory = threading.Thread,
        watchdog_thread_factory: ThreadFactory = threading.Thread,
        monotonic: Callable[[], float] = time.monotonic,
        kill_process_group: Callable[[int, int], None] = os.killpg,
    ) -> None:
        if not callable(getattr(boundary, "prepare_exec_action", None)):
            raise ValueError("boundary must prepare Docker actions")
        if not callable(getattr(boundary, "remove_run_containers", None)):
            raise ValueError("boundary must verify exact cleanup ownership")
        if not callable(getattr(boundary, "inspect_container_running", None)):
            raise ValueError("boundary must re-attest exact container state")
        if not callable(
            getattr(boundary, "inspect_container_writable_layer_bytes", None)
        ):
            raise ValueError("boundary must sample exact writable-layer state")
        if (
            not callable(popen_factory)
            or not callable(thread_factory)
            or not callable(watchdog_thread_factory)
            or not callable(monotonic)
            or not callable(kill_process_group)
        ):
            raise ValueError("executor process and clock dependencies must be callable")
        if (
            not isinstance(expected_docker_binary, str)
            or not os.path.isabs(expected_docker_binary)
            or os.path.normpath(expected_docker_binary) != expected_docker_binary
            or "\x00" in expected_docker_binary
        ):
            raise ValueError("expected Docker binary must be a canonical absolute path")
        _require_local_endpoint(expected_endpoint)
        self._boundary = boundary
        self._popen_factory = popen_factory
        self._thread_factory = thread_factory
        self._watchdog_thread_factory = watchdog_thread_factory
        self._monotonic = monotonic
        self._kill_process_group = kill_process_group
        self._expected_docker_binary = expected_docker_binary
        self._expected_endpoint = expected_endpoint
        self._env = {"LANG": "C", "LC_ALL": "C"}

    @property
    def boundary_identity(self) -> DockerActionExecutorBoundaryIdentity:
        """Return the immutable executor-to-Docker boundary binding."""

        return DockerActionExecutorBoundaryIdentity(
            boundary=self._boundary,
            expected_docker_binary=self._expected_docker_binary,
            expected_endpoint=self._expected_endpoint,
            _seal=_BOUNDARY_IDENTITY_SEAL,
        )

    def execute(
        self,
        *,
        container: DockerContainer,
        action: str,
        cwd: str,
        limits: DockerActionLimits,
    ) -> DockerActionResult:
        """Execute one action without retries under one absolute deadline."""

        if not isinstance(container, DockerContainer):
            raise ValueError("container must be a validated DockerContainer")
        if not isinstance(limits, DockerActionLimits):
            raise ValueError("limits must be DockerActionLimits")
        start = self._read_clock()
        deadline = start + limits.deadline_seconds
        empty = _CommandCapture(None, b"", b"", 0, 0, None, False)

        try:
            prepared = self._boundary.prepare_exec_action(
                container=container,
                action=action,
                cwd=cwd,
            )
            top_argv = _derive_top_argv(
                prepared,
                container,
                action=action,
                cwd=cwd,
                expected_docker_binary=self._expected_docker_binary,
                expected_endpoint=self._expected_endpoint,
            )
        except Exception:
            return self._invalid_result(
                container=container,
                capture=empty,
                failure=InfrastructureFailure.BOUNDARY_REJECTED,
                stage="prepare",
                start=start,
            )

        pre_storage_bytes = self._sample_writable_layer(container, limits)
        if (
            pre_storage_bytes is None
            or pre_storage_bytes
            > container.spec.limits.writable_layer_watchdog_bytes
        ):
            return self._invalid_result(
                container=container,
                capture=empty,
                failure=InfrastructureFailure.WRITABLE_LAYER_WATCHDOG,
                stage="pre_storage",
                start=start,
            )

        pre_audit = self._run_streaming(
            top_argv,
            deadline=deadline,
            stream_limit=_AUDIT_STREAM_LIMIT_BYTES,
            limits=limits,
        )
        if pre_audit.failure is not None:
            return self._invalid_result(
                container=container,
                capture=empty,
                failure=_capture_as_infrastructure(pre_audit.failure),
                stage="pre_audit",
                start=start,
            )
        if pre_audit.returncode != 0:
            return self._invalid_result(
                container=container,
                capture=empty,
                failure=InfrastructureFailure.PROCESS_AUDIT,
                stage="pre_audit",
                start=start,
            )
        try:
            baseline = _parse_pinned_idle_baseline(pre_audit.stdout)
            if pre_audit.stderr:
                raise ValueError("Docker process audit wrote stderr")
        except (UnicodeError, ValueError):
            return self._invalid_result(
                container=container,
                capture=empty,
                failure=InfrastructureFailure.PROCESS_AUDIT,
                stage="pre_audit",
                start=start,
            )

        action_capture = self._run_streaming(
            prepared.argv,
            deadline=deadline,
            stream_limit=limits.private_stream_limit_bytes + len(ACTION_START_MARKER),
            limits=limits,
            writable_layer_container=container,
        )
        if action_capture.failure is not None and not action_capture.spawned:
            failure = (
                InfrastructureFailure.ACTION_NOT_STARTED
                if action_capture.failure is _CaptureFailure.TIMEOUT
                else _capture_as_infrastructure(action_capture.failure)
            )
            return self._invalid_result(
                container=container,
                capture=action_capture,
                failure=failure,
                stage="action",
                start=start,
            )

        action_capture, action_started = _consume_action_start_attestation(
            action_capture
        )
        if not action_started:
            failure = (
                _capture_as_infrastructure(action_capture.failure)
                if action_capture.failure
                in {
                    _CaptureFailure.EXECUTOR_EXCEPTION,
                    _CaptureFailure.WRITABLE_LAYER_OVERFLOW,
                    _CaptureFailure.WRITABLE_LAYER_WATCHDOG,
                }
                else InfrastructureFailure.ACTION_NOT_STARTED
            )
            return self._invalid_result(
                container=container,
                capture=action_capture,
                failure=failure,
                stage="action",
                start=start,
            )

        if action_capture.failure is not None:
            if not action_capture.spawned:  # pragma: no cover - attestation invariant
                failure = (
                    InfrastructureFailure.ACTION_NOT_STARTED
                    if action_capture.failure is _CaptureFailure.TIMEOUT
                    else _capture_as_infrastructure(action_capture.failure)
                )
                return self._invalid_result(
                    container=container,
                    capture=action_capture,
                    failure=failure,
                    stage="action",
                    start=start,
                )
            policy = _capture_as_policy(action_capture.failure)
            if policy is not None:
                return self._policy_failure_result(
                    container=container,
                    capture=action_capture,
                    failure=policy,
                    stage="action",
                    start=start,
                )
            return self._invalid_result(
                container=container,
                capture=action_capture,
                failure=_capture_as_infrastructure(action_capture.failure),
                stage="action",
                start=start,
                action_started=True,
            )
        try:
            stdout_text = _normalize_output_text(action_capture.stdout)
            stderr_text = _normalize_output_text(action_capture.stderr)
        except UnicodeError:
            return self._policy_failure_result(
                container=container,
                capture=action_capture,
                failure=ActionPolicyFailure.INVALID_TEXT,
                stage="action",
                start=start,
            )
        if _contains_protected_observation(
            stdout_text,
            stderr_text,
            container=container,
            expected_docker_binary=self._expected_docker_binary,
            expected_endpoint=self._expected_endpoint,
        ):
            return self._invalid_result(
                container=container,
                capture=action_capture,
                failure=InfrastructureFailure.OBSERVATION_LEAK,
                stage="action",
                start=start,
                action_started=True,
            )

        post_audit = self._run_streaming(
            top_argv,
            deadline=deadline,
            stream_limit=_AUDIT_STREAM_LIMIT_BYTES,
            limits=limits,
        )
        if post_audit.failure is not None:
            return self._failed_post_audit_result(
                container=container,
                capture=action_capture,
                start=start,
            )
        if post_audit.returncode != 0:
            return self._failed_post_audit_result(
                container=container,
                capture=action_capture,
                start=start,
            )
        try:
            after = _parse_pinned_idle_baseline(post_audit.stdout)
            if post_audit.stderr:
                raise UnicodeError("Docker process audit wrote stderr")
        except UnicodeError:
            return self._failed_post_audit_result(
                container=container,
                capture=action_capture,
                start=start,
            )
        except ValueError:
            running = self._attested_running_state(container)
            if running is None:
                return self._invalid_result(
                    container=container,
                    capture=action_capture,
                    failure=InfrastructureFailure.PROCESS_AUDIT,
                    stage="post_audit",
                    start=start,
                    action_started=True,
                )
            if not running:
                return self._policy_failure_result(
                    container=container,
                    capture=action_capture,
                    failure=ActionPolicyFailure.CONTAINER_TERMINATED,
                    stage="post_audit",
                    start=start,
                )
            return self._policy_failure_result(
                container=container,
                capture=action_capture,
                failure=ActionPolicyFailure.RESIDUAL_PROCESS,
                stage="post_audit",
                start=start,
            )
        if after != baseline:
            return self._policy_failure_result(
                container=container,
                capture=action_capture,
                failure=ActionPolicyFailure.RESIDUAL_PROCESS,
                stage="post_audit",
                start=start,
            )

        post_storage_bytes = self._sample_writable_layer(container, limits)
        if post_storage_bytes is None:
            return self._invalid_result(
                container=container,
                capture=action_capture,
                failure=InfrastructureFailure.WRITABLE_LAYER_WATCHDOG,
                stage="post_storage",
                start=start,
                action_started=True,
            )
        if (
            post_storage_bytes
            > container.spec.limits.writable_layer_watchdog_bytes
        ):
            return self._policy_failure_result(
                container=container,
                capture=action_capture,
                failure=ActionPolicyFailure.WRITABLE_LAYER_OVERFLOW,
                stage="post_storage",
                start=start,
            )

        observation, truncated = _bounded_observation(
            stdout_text,
            stderr_text,
            limit=limits.observation_limit_bytes,
        )
        returncode = action_capture.returncode
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            return self._invalid_result(
                container=container,
                capture=action_capture,
                failure=InfrastructureFailure.EXECUTOR_EXCEPTION,
                stage="action",
                start=start,
                action_started=True,
            )
        return DockerActionResult(
            disposition=ActionDisposition.EXECUTED,
            infrastructure_failure=None,
            policy_failure=None,
            failure_stage=None,
            cleanup_outcome=CleanupOutcome.NOT_REQUIRED,
            exit_code=returncode,
            action_started=True,
            observation=observation,
            private_stdout=action_capture.stdout,
            private_stderr=action_capture.stderr,
            stdout_bytes_observed=action_capture.stdout_bytes_observed,
            stderr_bytes_observed=action_capture.stderr_bytes_observed,
            observation_truncated=truncated,
            elapsed_seconds=self._elapsed(start),
        )

    def _failed_post_audit_result(
        self,
        *,
        container: DockerContainer,
        capture: _CommandCapture,
        start: float,
    ) -> DockerActionResult:
        running = self._attested_running_state(container)
        if running is False:
            return self._policy_failure_result(
                container=container,
                capture=capture,
                failure=ActionPolicyFailure.CONTAINER_TERMINATED,
                stage="post_audit",
                start=start,
            )
        return self._invalid_result(
            container=container,
            capture=capture,
            failure=InfrastructureFailure.PROCESS_AUDIT,
            stage="post_audit",
            start=start,
            action_started=True,
        )

    def _attested_running_state(
        self,
        container: DockerContainer,
    ) -> bool | None:
        try:
            running = self._boundary.inspect_container_running(container=container)
        except Exception:
            return None
        return running if isinstance(running, bool) else None

    def _sample_writable_layer(
        self,
        container: DockerContainer,
        limits: DockerActionLimits,
    ) -> int | None:
        try:
            value = self._boundary.inspect_container_writable_layer_bytes(
                container=container,
                timeout_seconds=limits.writable_layer_probe_timeout_seconds,
            )
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _run_streaming(
        self,
        argv: Sequence[str],
        *,
        deadline: float,
        stream_limit: int,
        limits: DockerActionLimits,
        writable_layer_container: DockerContainer | None = None,
    ) -> _CommandCapture:
        """Drain stdout/stderr concurrently through a bounded chunk queue."""

        frozen_argv = _validate_argv(argv)
        try:
            before_spawn = self._read_clock()
        except Exception:
            return _CommandCapture(
                None, b"", b"", 0, 0, _CaptureFailure.EXECUTOR_EXCEPTION, False
            )
        if before_spawn >= deadline:
            return _CommandCapture(
                None, b"", b"", 0, 0, _CaptureFailure.TIMEOUT, False
            )
        try:
            process = self._popen_factory(
                frozen_argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                close_fds=True,
                start_new_session=True,
                env=dict(self._env),
            )
        except (OSError, subprocess.SubprocessError):
            return _CommandCapture(
                None, b"", b"", 0, 0, _CaptureFailure.SPAWN_ERROR, False
            )
        except Exception:
            return _CommandCapture(
                None, b"", b"", 0, 0, _CaptureFailure.EXECUTOR_EXCEPTION, False
            )

        stdout = getattr(process, "stdout", None)
        stderr = getattr(process, "stderr", None)
        if stdout is None or stderr is None:
            _abort_process(
                process,
                kill_process_group=self._kill_process_group,
            )
            return _CommandCapture(
                None, b"", b"", 0, 0, _CaptureFailure.EXECUTOR_EXCEPTION, True
            )

        messages: queue.Queue[_StreamItem] = queue.Queue(
            maxsize=limits.io_queue_chunks
        )
        stop = threading.Event()
        readers: list[threading.Thread] = []
        started_readers: list[threading.Thread] = []
        try:
            readers.append(
                self._thread_factory(
                    target=_read_stream,
                    args=("stdout", stdout, limits.read_chunk_bytes, messages, stop),
                    daemon=True,
                    name="edgeloop-docker-stdout",
                )
            )
            readers.append(
                self._thread_factory(
                    target=_read_stream,
                    args=("stderr", stderr, limits.read_chunk_bytes, messages, stop),
                    daemon=True,
                    name="edgeloop-docker-stderr",
                )
            )
            for reader in readers:
                reader.start()
                started_readers.append(reader)
        except Exception:
            stop.set()
            _abort_process(
                process,
                kill_process_group=self._kill_process_group,
            )
            _close_stream(stdout)
            _close_stream(stderr)
            for reader in started_readers:
                try:
                    reader.join(timeout=0.25)
                except Exception:
                    pass
            return _CommandCapture(
                _safe_poll(process),
                b"",
                b"",
                0,
                0,
                _CaptureFailure.EXECUTOR_EXCEPTION,
                True,
            )

        captured = {"stdout": bytearray(), "stderr": bytearray()}
        observed = {"stdout": 0, "stderr": 0}
        completed: set[str] = set()
        failure: _CaptureFailure | None = None
        watchdog_stop = threading.Event()
        watchdog_first_sample = threading.Event()
        watchdog_done = threading.Event()
        watchdog_signals: queue.Queue[_WritableLayerSignal] = queue.Queue(maxsize=1)
        watchdog_thread: threading.Thread | None = None
        if writable_layer_container is not None:
            try:
                watchdog_thread = self._watchdog_thread_factory(
                    target=_watch_writable_layer,
                    args=(
                        self._boundary,
                        writable_layer_container,
                        writable_layer_container.spec.limits.writable_layer_watchdog_bytes,
                        limits.writable_layer_sample_interval_seconds,
                        limits.writable_layer_probe_timeout_seconds,
                        watchdog_stop,
                        watchdog_first_sample,
                        watchdog_done,
                        watchdog_signals,
                    ),
                    daemon=True,
                    name="edgeloop-docker-size-rw-watchdog",
                )
                watchdog_thread.start()
                while not watchdog_first_sample.wait(timeout=0.01):
                    if self._read_clock() >= deadline:
                        failure = _CaptureFailure.TIMEOUT
                        break
                signal = _take_writable_layer_signal(watchdog_signals)
                if failure is None and signal is not None:
                    failure = _capture_failure_from_writable_layer_signal(signal)
            except Exception:
                failure = _CaptureFailure.EXECUTOR_EXCEPTION
        try:
            while failure is None and len(completed) < 2:
                signal = _take_writable_layer_signal(watchdog_signals)
                if signal is not None:
                    failure = _capture_failure_from_writable_layer_signal(signal)
                    break
                remaining = deadline - self._read_clock()
                if remaining <= 0:
                    failure = _CaptureFailure.TIMEOUT
                    break
                try:
                    item = messages.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    continue
                if item.failed:
                    failure = _CaptureFailure.EXECUTOR_EXCEPTION
                    break
                if item.chunk is None:
                    completed.add(item.channel)
                    continue
                observed[item.channel] += len(item.chunk)
                room = max(0, stream_limit - len(captured[item.channel]))
                captured[item.channel].extend(item.chunk[:room])
                if observed[item.channel] > stream_limit:
                    failure = _CaptureFailure.OUTPUT_OVERFLOW
                    break
        except Exception:
            failure = _CaptureFailure.EXECUTOR_EXCEPTION

        if failure is not None:
            stop.set()
            _abort_process(
                process,
                kill_process_group=self._kill_process_group,
            )
        else:
            try:
                if self._read_clock() >= deadline:
                    failure = _CaptureFailure.TIMEOUT
                returncode = process.poll()
                while failure is None and returncode is None:
                    signal = _take_writable_layer_signal(watchdog_signals)
                    if signal is not None:
                        failure = _capture_failure_from_writable_layer_signal(signal)
                        break
                    remaining = deadline - self._read_clock()
                    if remaining <= 0:
                        failure = _CaptureFailure.TIMEOUT
                        break
                    try:
                        returncode = process.wait(
                            timeout=min(_PROCESS_WAIT_POLL_SECONDS, remaining)
                        )
                    except subprocess.TimeoutExpired:
                        continue
                if failure is None and self._read_clock() >= deadline:
                    failure = _CaptureFailure.TIMEOUT
            except Exception:
                failure = _CaptureFailure.EXECUTOR_EXCEPTION
                returncode = None
            if failure is not None:
                _abort_process(
                    process,
                    kill_process_group=self._kill_process_group,
                )
        stop.set()
        watchdog_stop.set()
        _close_stream(stdout)
        _close_stream(stderr)
        for reader in started_readers:
            reader.join(timeout=0.05)
        if watchdog_thread is not None:
            watchdog_thread.join(
                timeout=limits.writable_layer_probe_timeout_seconds + 0.25
            )
            if not watchdog_done.is_set():
                failure = _CaptureFailure.EXECUTOR_EXCEPTION
            elif failure is None:
                signal = _take_writable_layer_signal(watchdog_signals)
                if signal is not None:
                    failure = _capture_failure_from_writable_layer_signal(signal)

        if failure is not None:
            returncode = _safe_poll(process)
        return _CommandCapture(
            returncode=returncode,
            stdout=bytes(captured["stdout"]),
            stderr=bytes(captured["stderr"]),
            stdout_bytes_observed=observed["stdout"],
            stderr_bytes_observed=observed["stderr"],
            failure=failure,
            spawned=True,
        )

    def _invalid_result(
        self,
        *,
        container: DockerContainer,
        capture: _CommandCapture,
        failure: InfrastructureFailure,
        stage: str,
        start: float,
        action_started: bool = False,
    ) -> DockerActionResult:
        cleanup = self._exact_cleanup(container)
        reported_failure = (
            InfrastructureFailure.CLEANUP_AMBIGUOUS
            if cleanup is CleanupOutcome.AMBIGUOUS
            else failure
        )
        return DockerActionResult(
            disposition=ActionDisposition.INFRASTRUCTURE_INVALID,
            infrastructure_failure=reported_failure,
            policy_failure=None,
            failure_stage=stage,
            cleanup_outcome=cleanup,
            exit_code=capture.returncode if action_started else None,
            action_started=action_started,
            observation="",
            private_stdout=capture.stdout,
            private_stderr=capture.stderr,
            stdout_bytes_observed=capture.stdout_bytes_observed,
            stderr_bytes_observed=capture.stderr_bytes_observed,
            observation_truncated=False,
            elapsed_seconds=self._elapsed(start),
        )

    def _policy_failure_result(
        self,
        *,
        container: DockerContainer,
        capture: _CommandCapture,
        failure: ActionPolicyFailure,
        stage: str,
        start: float,
    ) -> DockerActionResult:
        """Destroy contaminated state but retain model-caused failures in-sample."""

        cleanup = self._exact_cleanup(container)
        model_exit_code = (
            None
            if failure
            in {
                ActionPolicyFailure.TIMEOUT,
                ActionPolicyFailure.OUTPUT_OVERFLOW,
            }
            or (
                failure is ActionPolicyFailure.WRITABLE_LAYER_OVERFLOW
                and stage == "action"
            )
            else capture.returncode
        )
        if cleanup is CleanupOutcome.AMBIGUOUS:
            return DockerActionResult(
                disposition=ActionDisposition.INFRASTRUCTURE_INVALID,
                infrastructure_failure=InfrastructureFailure.CLEANUP_AMBIGUOUS,
                policy_failure=failure,
                failure_stage=stage,
                cleanup_outcome=cleanup,
                exit_code=model_exit_code,
                action_started=True,
                observation="",
                private_stdout=capture.stdout,
                private_stderr=capture.stderr,
                stdout_bytes_observed=capture.stdout_bytes_observed,
                stderr_bytes_observed=capture.stderr_bytes_observed,
                observation_truncated=False,
                elapsed_seconds=self._elapsed(start),
            )
        return DockerActionResult(
            disposition=ActionDisposition.POLICY_FAILURE,
            infrastructure_failure=None,
            policy_failure=failure,
            failure_stage=stage,
            cleanup_outcome=cleanup,
            exit_code=model_exit_code,
            action_started=True,
            observation=_POLICY_OBSERVATIONS[failure],
            private_stdout=capture.stdout,
            private_stderr=capture.stderr,
            stdout_bytes_observed=capture.stdout_bytes_observed,
            stderr_bytes_observed=capture.stderr_bytes_observed,
            observation_truncated=False,
            elapsed_seconds=self._elapsed(start),
        )

    def _exact_cleanup(self, container: DockerContainer) -> CleanupOutcome:
        """Delegate ownership proof and force removal to the Docker boundary."""

        try:
            removed = self._boundary.remove_run_containers(
                container.spec.run_id,
                (container.identifier,),
            )
        except Exception:
            return CleanupOutcome.AMBIGUOUS
        if removed != (container.identifier,):
            return CleanupOutcome.AMBIGUOUS
        return CleanupOutcome.REMOVED

    def _read_clock(self) -> float:
        value = self._monotonic()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("monotonic clock returned an invalid value")
        rendered = float(value)
        if not math.isfinite(rendered):
            raise RuntimeError("monotonic clock returned an invalid value")
        return rendered

    def _elapsed(self, start: float) -> float:
        try:
            return max(0.0, self._read_clock() - start)
        except Exception:
            return 0.0


def _derive_top_argv(
    prepared: PreparedDockerExec,
    container: DockerContainer,
    *,
    action: str,
    cwd: str,
    expected_docker_binary: str,
    expected_endpoint: str,
) -> tuple[str, ...]:
    if not isinstance(prepared, PreparedDockerExec):
        raise ValueError("Docker boundary returned an invalid prepared action")
    if prepared.container_id != container.identifier:
        raise ValueError("prepared action target differs from validated container")
    if prepared.cwd != cwd:
        raise ValueError("prepared action cwd differs from requested cwd")
    argv = _validate_argv(prepared.argv)
    expected = (
        expected_docker_binary,
        "--host",
        expected_endpoint,
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
    if argv != expected:
        raise ValueError("prepared Docker argv differs from the frozen exec shape")
    return (
        expected_docker_binary,
        "--host",
        expected_endpoint,
        "container",
        "top",
        container.identifier,
        "-eo",
        "pid=PID,ppid=PPID,stat=STAT,comm=COMMAND",
    )


def _parse_pinned_idle_baseline(data: bytes) -> tuple[_ProcessEntry, ...]:
    """Require the one stable ``tail`` process installed as container PID 1."""

    lines = data.splitlines()
    if not lines or lines[0].split() != [b"PID", b"PPID", b"STAT", b"COMMAND"]:
        raise ValueError("Docker process audit header is malformed")
    entries: list[_ProcessEntry] = []
    for line in lines[1:]:
        match = _PROCESS_LINE.fullmatch(line.strip())
        if match is None:
            raise ValueError("Docker process audit output is malformed")
        pid = int(match.group(1))
        ppid = int(match.group(2))
        state = match.group(3).decode("ascii")
        command = match.group(4).decode("ascii")
        entries.append(_ProcessEntry(pid, ppid, state, command))
    if len(entries) != 1:
        raise ValueError("Docker process audit differs from pinned idle cardinality")
    idle = entries[0]
    if idle.command != "tail":
        raise ValueError("Docker process audit differs from pinned idle command")
    if _HEALTHY_IDLE_STATE.fullmatch(idle.state) is None:
        raise ValueError("Docker process audit reports an unhealthy idle state")
    if idle.ppid == idle.pid:
        raise ValueError("Docker process audit has an invalid idle parent")
    return (idle,)


def _consume_action_start_attestation(
    capture: _CommandCapture,
) -> tuple[_CommandCapture, bool]:
    """Strip the wrapper marker only when both streams prove action start."""

    marker_size = len(ACTION_START_MARKER)
    stdout_marked = capture.stdout.startswith(ACTION_START_MARKER)
    stderr_marked = capture.stderr.startswith(ACTION_START_MARKER)
    both_marked = stdout_marked and stderr_marked
    overflow_after_marker = (
        capture.failure is _CaptureFailure.OUTPUT_OVERFLOW
        and (
            (stdout_marked and capture.stdout_bytes_observed > marker_size)
            or (stderr_marked and capture.stderr_bytes_observed > marker_size)
        )
    )
    if not (both_marked or overflow_after_marker):
        return capture, False
    stdout_offset = marker_size if stdout_marked else 0
    stderr_offset = marker_size if stderr_marked else 0
    return (
        _CommandCapture(
            returncode=capture.returncode,
            stdout=capture.stdout[stdout_offset:],
            stderr=capture.stderr[stderr_offset:],
            stdout_bytes_observed=capture.stdout_bytes_observed - stdout_offset,
            stderr_bytes_observed=capture.stderr_bytes_observed - stderr_offset,
            failure=capture.failure,
            spawned=capture.spawned,
        ),
        True,
    )


def _read_stream(
    channel: str,
    stream: object,
    chunk_size: int,
    messages: queue.Queue[_StreamItem],
    stop: threading.Event,
) -> None:
    try:
        read = getattr(stream, "read")
        while not stop.is_set():
            chunk = read(chunk_size)
            if not isinstance(chunk, bytes):
                raise TypeError("subprocess stream did not return bytes")
            if not chunk:
                _put_stream_item(messages, _StreamItem(channel), stop)
                return
            if len(chunk) > chunk_size:
                raise ValueError("subprocess stream exceeded requested chunk size")
            if not _put_stream_item(messages, _StreamItem(channel, chunk), stop):
                return
    except Exception:
        _put_stream_item(messages, _StreamItem(channel, failed=True), stop)


def _watch_writable_layer(
    boundary: DockerActionBoundary,
    container: DockerContainer,
    threshold_bytes: int,
    sample_interval_seconds: float,
    probe_timeout_seconds: float,
    stop: threading.Event,
    first_sample: threading.Event,
    done: threading.Event,
    signals: queue.Queue[_WritableLayerSignal],
) -> None:
    """Sample Docker ``SizeRw`` until stopped or one terminal signal occurs."""

    try:
        while not stop.is_set():
            try:
                value = boundary.inspect_container_writable_layer_bytes(
                    container=container,
                    timeout_seconds=probe_timeout_seconds,
                )
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                ):
                    raise ValueError("invalid writable-layer sample")
            except Exception:
                try:
                    running = boundary.inspect_container_running(container=container)
                except Exception:
                    running = None
                if running is False:
                    first_sample.set()
                    return
                _offer_writable_layer_signal(
                    signals, _WritableLayerSignal.PROBE_FAILURE
                )
                first_sample.set()
                return
            first_sample.set()
            if value > threshold_bytes:
                _offer_writable_layer_signal(signals, _WritableLayerSignal.OVERFLOW)
                return
            if stop.wait(sample_interval_seconds):
                return
    finally:
        first_sample.set()
        done.set()


def _offer_writable_layer_signal(
    signals: queue.Queue[_WritableLayerSignal],
    signal_value: _WritableLayerSignal,
) -> None:
    try:
        signals.put_nowait(signal_value)
    except queue.Full:
        return


def _take_writable_layer_signal(
    signals: queue.Queue[_WritableLayerSignal],
) -> _WritableLayerSignal | None:
    try:
        return signals.get_nowait()
    except queue.Empty:
        return None


def _capture_failure_from_writable_layer_signal(
    signal_value: _WritableLayerSignal,
) -> _CaptureFailure:
    if signal_value is _WritableLayerSignal.OVERFLOW:
        return _CaptureFailure.WRITABLE_LAYER_OVERFLOW
    return _CaptureFailure.WRITABLE_LAYER_WATCHDOG


def _put_stream_item(
    messages: queue.Queue[_StreamItem],
    item: _StreamItem,
    stop: threading.Event,
) -> bool:
    while not stop.is_set():
        try:
            messages.put(item, timeout=0.01)
            return True
        except queue.Full:
            continue
    return False


def _abort_process(
    process: PopenProcess,
    *,
    kill_process_group: Callable[[int, int], None],
) -> None:
    group_killed = False
    try:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
            try:
                kill_process_group(pid, signal.SIGKILL)
                group_killed = True
            except Exception:
                pass
        if not group_killed and process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
    except Exception:
        pass
    _close_stream(getattr(process, "stdout", None))
    _close_stream(getattr(process, "stderr", None))
    try:
        process.wait(timeout=0.25)
    except Exception:
        pass


def _safe_poll(process: PopenProcess) -> int | None:
    try:
        result = process.poll()
    except Exception:
        return None
    if result is None or (
        isinstance(result, int) and not isinstance(result, bool)
    ):
        return result
    return None


def _close_stream(stream: object) -> None:
    try:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def _bounded_observation(stdout: str, stderr: str, *, limit: int) -> tuple[str, bool]:
    if stdout and stderr:
        combined = f"{stdout}\n[stderr]\n{stderr}"
    else:
        combined = stdout or stderr
    encoded = combined.encode("utf-8")
    if len(encoded) <= limit:
        return combined, False
    prefix = encoded[:limit]
    while prefix:
        try:
            return prefix.decode("utf-8"), True
        except UnicodeDecodeError as error:
            prefix = prefix[: error.start]
    return "", True


def _normalize_output_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="strict").replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    for character in text:
        if character in {"\n", "\t"}:
            continue
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            raise UnicodeError("output contains a forbidden control character")
    return text


def _contains_protected_observation(
    stdout: str,
    stderr: str,
    *,
    container: DockerContainer,
    expected_docker_binary: str,
    expected_endpoint: str,
) -> bool:
    """Reject host/controller canaries before text is returned to the model."""

    combined = f"{stdout}\n{stderr}"
    endpoint_path = urlsplit(expected_endpoint).path
    exact_canaries = (
        container.identifier,
        container.name,
        expected_endpoint,
        endpoint_path,
        expected_docker_binary,
    )
    if any(canary and canary in combined for canary in exact_canaries):
        return True

    parts = endpoint_path.split("/")
    if len(parts) >= 3 and parts[1] in {"Users", "home"} and parts[2]:
        host_home = f"/{parts[1]}/{parts[2]}"
        return re.search(re.escape(host_home) + r"(?=/|$)", combined) is not None
    return False


def _capture_as_policy(
    failure: _CaptureFailure,
) -> ActionPolicyFailure | None:
    if failure is _CaptureFailure.TIMEOUT:
        return ActionPolicyFailure.TIMEOUT
    if failure is _CaptureFailure.OUTPUT_OVERFLOW:
        return ActionPolicyFailure.OUTPUT_OVERFLOW
    if failure is _CaptureFailure.WRITABLE_LAYER_OVERFLOW:
        return ActionPolicyFailure.WRITABLE_LAYER_OVERFLOW
    return None


def _capture_as_infrastructure(
    failure: _CaptureFailure,
) -> InfrastructureFailure:
    if failure is _CaptureFailure.SPAWN_ERROR:
        return InfrastructureFailure.SPAWN_ERROR
    if failure is _CaptureFailure.EXECUTOR_EXCEPTION:
        return InfrastructureFailure.EXECUTOR_EXCEPTION
    if failure is _CaptureFailure.WRITABLE_LAYER_WATCHDOG:
        return InfrastructureFailure.WRITABLE_LAYER_WATCHDOG
    if failure is _CaptureFailure.WRITABLE_LAYER_OVERFLOW:
        return InfrastructureFailure.WRITABLE_LAYER_WATCHDOG
    # A timeout/overflow while running the trusted audit command is daemon or
    # host infrastructure failure, never evidence about model effectiveness.
    return InfrastructureFailure.PROCESS_AUDIT


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)):
        raise ValueError("Docker command must be an argv sequence")
    frozen = tuple(argv)
    if not frozen:
        raise ValueError("Docker command argv must not be empty")
    for argument in frozen:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ValueError("Docker argv entries must be NUL-free strings")
    return frozen


def _require_local_endpoint(endpoint: object) -> str:
    if not isinstance(endpoint, str) or "\x00" in endpoint:
        raise ValueError("expected Docker endpoint must be text")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path in {"", "/"}
        or parsed.query
        or parsed.fragment
        or os.path.normpath(parsed.path) != parsed.path
    ):
        raise ValueError("expected Docker endpoint must be a canonical local Unix socket")
    return endpoint


def _require_positive_integer(value: object, field_name: str, *, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValueError(f"{field_name} must be a positive bounded integer")


def _require_positive_finite(
    value: object, field_name: str, *, maximum: float
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a positive finite number")
    rendered = float(cast(float, value))
    if not math.isfinite(rendered) or not 0 < rendered <= maximum:
        raise ValueError(f"{field_name} must be a positive bounded number")
