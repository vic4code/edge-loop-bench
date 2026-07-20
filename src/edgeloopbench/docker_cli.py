"""Small, auditable Docker CLI boundary for interactive task containers.

The module deliberately does not provide a generic Docker wrapper.  It emits a
fixed container profile, validates the profile returned by ``docker inspect``,
and keeps every host-side invocation in an argv sequence with ``shell=False``.
Streaming, lifecycle orchestration, and checkpointing belong to later slices.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Never, Protocol, cast
from urllib.parse import urlsplit


MANAGED_LABEL = "org.edgeloopbench.managed"
RUN_LABEL = "org.edgeloopbench.run"
ROLE_LABEL = "org.edgeloopbench.role"
INSTANCE_LABEL = "org.edgeloopbench.instance"
RUNTIME_NETWORK_LABEL = "org.edgeloopbench.runtime-network"
FILESYSTEM_VERSION_LABEL = "org.edgeloopbench.filesystem-version"
STATE_COLLECTOR_ARGV_LABEL = "org.edgeloopbench.state-collector.argv"
STATE_COLLECTOR_POLICY_LABEL = "org.edgeloopbench.state-collector.policy-sha256"
STATE_COLLECTOR_PROFILE_LABEL = "org.edgeloopbench.state-collector.profile"
STATE_COLLECTOR_PROFILE_SET_LABEL = (
    "org.edgeloopbench.state-collector.profile-set-sha256"
)
STATE_COLLECTOR_ROOT_LABEL = (
    "org.edgeloopbench.state-collector.root-baseline-sha256"
)
STATE_COLLECTOR_SOURCE_LABEL = "org.edgeloopbench.state-collector.sha256"
MANAGED_VALUE = "v0.6"
_RUNTIME_LABEL_KEYS = frozenset(
    {MANAGED_LABEL, RUN_LABEL, ROLE_LABEL, INSTANCE_LABEL}
)
_IMAGE_METADATA_LABEL_KEYS = frozenset(
    {
        FILESYSTEM_VERSION_LABEL,
        RUNTIME_NETWORK_LABEL,
        STATE_COLLECTOR_ARGV_LABEL,
        STATE_COLLECTOR_POLICY_LABEL,
        STATE_COLLECTOR_PROFILE_LABEL,
        STATE_COLLECTOR_PROFILE_SET_LABEL,
        STATE_COLLECTOR_ROOT_LABEL,
        STATE_COLLECTOR_SOURCE_LABEL,
    }
)
_ALLOWED_PROJECT_LABEL_KEYS = _RUNTIME_LABEL_KEYS | _IMAGE_METADATA_LABEL_KEYS

CONTAINER_HOSTNAME = "edgeloop-agent"
CONTAINER_USER = "65532:65532"
CONTAINER_WORKDIR = "/"
CONTAINER_OS = "linux"
CONTAINER_ARCHITECTURE = "arm64"
RUNTIME_ROOT_MUTATION_ARGV = ("/bin/chmod", "1777", "/")
RUNTIME_ROOT_ATTESTATION_ARGV = (
    "/usr/bin/stat",
    "--format=%u:%g:%a",
    "--",
    "/",
)
RUNTIME_ROOT_ATTESTATION_STDOUT = "0:0:1777\n"
ACTION_START_MARKER = b"\x1eELB_ACTION_STARTED_V1\x1f\n"
ACTION_WRAPPER_ARG0 = "edgeloop-action-v1"
ACTION_WRAPPER_SCRIPT = (
    "printf '\\036ELB_ACTION_STARTED_V1\\037\\n'\n"
    "printf '\\036ELB_ACTION_STARTED_V1\\037\\n' >&2\n"
    'exec /bin/bash --noprofile --norc -c "$1"'
)
IDLE_ENTRYPOINT = "/bin/bash"
IDLE_COMMAND = (
    "--noprofile",
    "--norc",
    "-c",
    "exec /usr/bin/tail -f /dev/null",
)
SECURITY_OPTIONS = frozenset(
    {"no-new-privileges=true", "seccomp=builtin"}
)

_IMMUTABLE_IMAGE_PATTERN = re.compile(
    r"^(?:[a-z0-9]+(?:[._:/-][a-z0-9]+)*@)?sha256:[0-9a-f]{64}$"
)
_CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")
_ROLE_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,10}[a-z0-9])?$")
_CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_NONCE_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_STATE_PROFILE_PATTERN = re.compile(r"^fs[1-4]$")
_MAX_ACTION_BYTES = 8 * 1024
_MAX_CWD_BYTES = 4 * 1024
_MAX_TRUSTED_STATE_BYTES = 4 * 1024 * 1024
WRITABLE_LAYER_STORAGE_MODE = "sampled-size-rw-no-hard-quota-v1"
DEFAULT_FSIZE_BYTES = 16 * 1024 * 1024
_STATE_COLLECTOR_ARGV = (
    "/usr/bin/python3 -I -S -B /opt/edgeloop/state_collector.py --profile fsN"
)
_BOUNDARY_IDENTITY_SEAL = object()


class SubprocessRunner(Protocol):
    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class DockerCommandResult:
    """Complete private result of one non-shell Docker CLI invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class DockerCliError(RuntimeError):
    """Base class for failures at the Docker trust boundary."""


class DockerCommandError(DockerCliError):
    def __init__(self, message: str, result: DockerCommandResult) -> None:
        super().__init__(message)
        self.result = result


class DockerCommandTimeout(DockerCliError):
    """A Docker CLI invocation exceeded the fixed host-side deadline."""


class DockerAdmissionError(DockerCliError):
    def __init__(
        self,
        message: str,
        *,
        running_container_ids: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.running_container_ids = tuple(running_container_ids)


class DockerSecurityError(DockerCliError):
    """The daemon's inspected container profile differs from the frozen one."""


class DockerCleanupRefused(DockerCliError):
    """A requested cleanup target is not owned by the exact run identity."""


class DockerOrphanedResourceError(DockerCliError):
    """Creation may have succeeded but exact-label cleanup could not be proven."""

    def __init__(self, message: str, *, run_id: str, name: str) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.name = name


@dataclass(frozen=True)
class DockerLimits:
    memory_bytes: int
    memory_swap_bytes: int
    writable_layer_watchdog_bytes: int
    nano_cpus: int
    pids_limit: int
    nofile_soft: int
    nofile_hard: int
    nproc_soft: int
    nproc_hard: int
    fsize_soft: int = DEFAULT_FSIZE_BYTES
    fsize_hard: int = DEFAULT_FSIZE_BYTES
    storage_enforcement_mode: str = WRITABLE_LAYER_STORAGE_MODE

    def __post_init__(self) -> None:
        for field, value in self.__dict__.items():
            if field == "storage_enforcement_mode":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"Docker limit {field} must be a positive integer")
        if self.storage_enforcement_mode != WRITABLE_LAYER_STORAGE_MODE:
            raise ValueError("Docker storage enforcement mode differs from the frozen mode")
        if self.memory_swap_bytes != self.memory_bytes:
            raise ValueError(
                "memory_swap_bytes must equal memory_bytes to prevent container swap"
            )
        if self.nofile_soft > self.nofile_hard:
            raise ValueError("nofile_soft must not exceed nofile_hard")
        if self.nproc_soft > self.nproc_hard:
            raise ValueError("nproc_soft must not exceed nproc_hard")
        if self.fsize_soft > self.fsize_hard:
            raise ValueError("fsize_soft must not exceed fsize_hard")
        if self.nano_cpus > 64_000_000_000:
            raise ValueError("nano_cpus exceeds the supported safety ceiling")
        if self.pids_limit > 4096:
            raise ValueError("pids_limit exceeds the supported safety ceiling")


@dataclass(frozen=True)
class DockerContainerSpec:
    run_id: str
    role: str
    image: str
    limits: DockerLimits
    image_id: str

    def __post_init__(self) -> None:
        _require_run_id(self.run_id)
        _require_role(self.role)
        if self.role != "agent":
            raise ValueError(
                "this trust-boundary slice admits only the pinned agent role"
            )
        if not _IMMUTABLE_IMAGE_PATTERN.fullmatch(self.image):
            raise ValueError(
                "Docker image must be an immutable sha256:<64 hex> image ID or "
                "lowercase repository@sha256 reference"
            )
        if not isinstance(self.limits, DockerLimits):
            raise ValueError("Docker container limits must be a DockerLimits value")
        if not isinstance(self.image_id, str) or not _IMAGE_ID_PATTERN.fullmatch(
            self.image_id
        ):
            raise ValueError("Docker image_id must be a lowercase SHA-256 image config id")
        if self.image.startswith("sha256:") and self.image != self.image_id:
            raise ValueError("a bare Docker image reference must equal image_id")


@dataclass(frozen=True)
class DockerContainer:
    """Identity returned only after the daemon profile passes inspection."""

    identifier: str
    name: str
    image_id: str
    labels: tuple[tuple[str, str], ...]
    spec: DockerContainerSpec


@dataclass(frozen=True, slots=True)
class DockerTrustedState:
    """Validated canonical output from the image-pinned state collector."""

    canonical_json: str = field(repr=False)
    state_sha256: str
    profile: str
    profile_sha256: str
    policy_sha256: str
    root_baseline_sha256: str
    writable_surface_audit_sha256: str
    collector_source_sha256: str
    strict_representable: bool
    strict_failures: tuple[str, ...]


@dataclass(frozen=True)
class PreparedDockerExec:
    """Validated argv for the later bounded streaming action executor."""

    argv: tuple[str, ...]
    container_id: str
    cwd: str


@dataclass(frozen=True)
class DockerAdmission:
    context: str
    endpoint: str
    running_container_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class DockerCliBoundaryIdentity:
    """Read-only identity of the exact pathful Docker CLI boundary.

    The local binary path remains out of journals and manifests.  Production
    composition may attest that path against the path-free host pin without
    reading ``DockerCli`` implementation attributes.
    """

    expected_context: str
    expected_endpoint: str
    docker_binary: str
    endpoint_sha256: str
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _BOUNDARY_IDENTITY_SEAL:
            raise DockerSecurityError(
                "Docker CLI boundary identities must be DockerCli-issued"
            )
        if not _CONTEXT_PATTERN.fullmatch(self.expected_context):
            raise DockerSecurityError("Docker CLI boundary context is invalid")
        _require_local_endpoint(self.expected_endpoint)
        if (
            not isinstance(self.docker_binary, str)
            or not os.path.isabs(self.docker_binary)
            or os.path.normpath(self.docker_binary) != self.docker_binary
            or "\x00" in self.docker_binary
        ):
            raise DockerSecurityError("Docker CLI boundary binary path is invalid")
        expected_sha256 = "sha256:" + hashlib.sha256(
            self.expected_endpoint.encode("utf-8")
        ).hexdigest()
        if self.endpoint_sha256 != expected_sha256:
            raise DockerSecurityError("Docker CLI boundary endpoint root is invalid")

    def __repr__(self) -> str:
        return (
            "<DockerCliBoundaryIdentity "
            f"context={self.expected_context!r} "
            f"endpoint_sha256={self.endpoint_sha256}>"
        )


class DockerCli:
    """Pinned Docker CLI client with no generic command escape hatch."""

    def __init__(
        self,
        *,
        expected_context: str,
        expected_endpoint: str,
        docker_binary: str,
        env: Mapping[str, str] | None = None,
        runner: SubprocessRunner = subprocess.run,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(8),
        command_timeout_seconds: float = 30.0,
    ) -> None:
        if not _CONTEXT_PATTERN.fullmatch(expected_context):
            raise ValueError("expected Docker context has an invalid name")
        _require_local_endpoint(expected_endpoint)
        if (
            not isinstance(docker_binary, str)
            or not os.path.isabs(docker_binary)
            or os.path.normpath(docker_binary) != docker_binary
            or "\x00" in docker_binary
        ):
            raise ValueError("Docker binary must be a canonical absolute path")
        if (
            isinstance(command_timeout_seconds, bool)
            or not isinstance(command_timeout_seconds, (int, float))
            or not math.isfinite(float(command_timeout_seconds))
            or command_timeout_seconds <= 0
        ):
            raise ValueError("Docker command timeout must be positive")
        self._expected_context = expected_context
        self._expected_endpoint = expected_endpoint
        self._env = dict(os.environ if env is None else env)
        self._runner = runner
        self._docker_binary = docker_binary
        self._nonce_factory = nonce_factory
        self._command_timeout_seconds = float(command_timeout_seconds)

    @property
    def boundary_identity(self) -> DockerCliBoundaryIdentity:
        """Return the immutable context, endpoint, and binary path binding."""

        return DockerCliBoundaryIdentity(
            expected_context=self._expected_context,
            expected_endpoint=self._expected_endpoint,
            docker_binary=self._docker_binary,
            endpoint_sha256="sha256:"
            + hashlib.sha256(self._expected_endpoint.encode("utf-8")).hexdigest(),
            _seal=_BOUNDARY_IDENTITY_SEAL,
        )

    def admit(self, *, require_no_running: bool = True) -> DockerAdmission:
        """Verify the local daemon identity and list running containers.

        Admission is read-only.  In particular, an unrelated running container
        is reported or rejected but is never stopped or removed.
        """

        current_context, endpoint = self._verify_local_daemon()
        running = self.list_running_containers()
        if require_no_running and running:
            raise DockerAdmissionError(
                "Docker admission found a running container; no mutation was attempted",
                running_container_ids=running,
            )
        return DockerAdmission(current_context, endpoint, running)

    def list_running_containers(self) -> tuple[str, ...]:
        """Return full IDs for running containers without changing daemon state."""

        result = self._invoke(
            "container", "ls", "--quiet", "--no-trunc", "--filter", "status=running"
        )
        return _parse_container_ids(result.stdout, "running-container list")

    def create_container(self, spec: DockerContainerSpec) -> DockerContainer:
        """Create one stopped container and require its exact inspected profile."""

        if not isinstance(spec, DockerContainerSpec):
            raise ValueError("spec must be a DockerContainerSpec")
        nonce = self._nonce_factory()
        if not isinstance(nonce, str) or not _NONCE_PATTERN.fullmatch(nonce):
            raise ValueError("container nonce must be exactly 16 lowercase hex characters")
        name = f"elb-{spec.run_id}-{spec.role}-{nonce}"
        if len(name) > 63 or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError("generated Docker container name is unsafe")
        labels = _runtime_labels(spec, name)
        self._verify_local_daemon()
        self._require_no_unrelated_running(spec.run_id)
        self._validate_image(spec)
        argv = self._create_arguments(spec, name, labels)
        result = self._invoke(*argv)
        try:
            identifier = self._single_line(result.stdout, "created container id")
            _require_container_id(identifier)
        except (DockerCliError, ValueError) as error:
            self._cleanup_failed_create(name, spec.run_id, error)
        try:
            inspected = self._inspect_one(identifier)
        except DockerCliError as error:
            self._cleanup_failed_create(name, spec.run_id, error)
        try:
            image_id = self._validate_security_profile(
                inspected,
                identifier=identifier,
                name=name,
                spec=spec,
                labels=labels,
                running=False,
            )
        except DockerSecurityError as validation_error:
            # The just-created object may be removed only after its immutable
            # ID and exact runtime ownership labels are independently present.
            try:
                self._validate_cleanup_ownership(inspected, spec.run_id, identifier)
            except DockerCleanupRefused as cleanup_error:
                raise DockerSecurityError(
                    f"{validation_error}; automatic cleanup refused: {cleanup_error}"
                ) from validation_error
            self._invoke(
                "container", "rm", "--force", "--volumes", "--", identifier
            )
            raise
        return DockerContainer(
            identifier=identifier,
            name=name,
            image_id=image_id,
            labels=tuple(sorted(labels.items())),
            spec=spec,
        )

    def _cleanup_failed_create(
        self, name: str, run_id: str, cause: BaseException
    ) -> Never:
        """Fail closed after a create response/inspection ambiguity."""

        try:
            item = self._inspect_one(name)
            identifier = item.get("Id")
            _require_container_id(cast(str, identifier))
            self._validate_cleanup_ownership(item, run_id, cast(str, identifier))
            self._invoke(
                "container",
                "rm",
                "--force",
                "--volumes",
                "--",
                cast(str, identifier),
            )
        except (DockerCliError, ValueError) as cleanup_error:
            raise DockerOrphanedResourceError(
                "container creation became ambiguous and exact-label cleanup "
                "could not be proven; run-level cleanup is required",
                run_id=run_id,
                name=name,
            ) from cleanup_error
        raise DockerSecurityError(
            "container creation became ambiguous; exact-label resource was removed"
        ) from cause

    def start_container(self, container: DockerContainer) -> DockerContainer:
        """Start one exact container and normalize its exported root mount."""

        self._require_container_handle(container)
        self._verify_local_daemon()
        self._attest_container_lifecycle(container, lifecycle="created")
        result = self._invoke("container", "start", "--", container.identifier)
        self._attest_container_lifecycle(container, lifecycle="running")
        reported = self._single_line(result.stdout, "Docker container start result")
        if reported != container.identifier:
            raise DockerSecurityError(
                "Docker container start did not report the exact full id"
            )
        mutation = self._invoke(
            "container",
            "exec",
            "--user",
            "0:0",
            container.identifier,
            *RUNTIME_ROOT_MUTATION_ARGV,
        )
        if mutation.stdout or mutation.stderr:
            raise DockerSecurityError("runtime root mode mutation produced output")
        attestation = self._invoke(
            "container",
            "exec",
            "--user",
            "0:0",
            container.identifier,
            *RUNTIME_ROOT_ATTESTATION_ARGV,
        )
        if attestation.stderr:
            raise DockerSecurityError("runtime root mount attestation produced stderr")
        if attestation.stdout != RUNTIME_ROOT_ATTESTATION_STDOUT:
            raise DockerSecurityError(
                "runtime root owner or mode differs from UID 0, GID 0, mode 01777"
            )
        self._attest_container_lifecycle(container, lifecycle="running")
        return container

    def collect_trusted_state(
        self,
        container: DockerContainer,
        *,
        profile: str,
    ) -> DockerTrustedState:
        """Run only the image-pinned, root-owned logical-state collector."""

        self._require_container_handle(container)
        _require_state_profile(profile)
        self._verify_local_daemon()
        inspected = self._attest_container_lifecycle(
            container, lifecycle="running"
        )
        collector_labels = self._validate_collector_labels(
            inspected, profile=profile
        )
        result = self._invoke(
            "container",
            "exec",
            "--user",
            "0:0",
            container.identifier,
            "/usr/bin/python3",
            "-I",
            "-S",
            "-B",
            "/opt/edgeloop/state_collector.py",
            "--profile",
            profile,
        )
        # Collection is logically read-only. Re-attest before accepting any
        # evidence so plausible JSON cannot hide lifecycle or identity drift.
        self._attest_container_lifecycle(container, lifecycle="running")
        if result.stderr:
            raise DockerSecurityError("trusted state collector wrote to stderr")
        return _decode_trusted_state(
            result.stdout,
            profile=profile,
            collector_source_sha256=collector_labels[
                STATE_COLLECTOR_SOURCE_LABEL
            ],
            expected_policy_sha256=collector_labels[
                STATE_COLLECTOR_POLICY_LABEL
            ],
            expected_root_sha256=collector_labels[STATE_COLLECTOR_ROOT_LABEL],
        )

    def prepare_exec_action(
        self,
        *,
        container: DockerContainer,
        action: str,
        cwd: str,
    ) -> PreparedDockerExec:
        """Validate a target and prepare one container-side Bash invocation.

        The complete action occupies one argv element after the fixed ``-c``.
        This slice intentionally does not execute it: bounded streaming,
        timeout destruction, and output capture belong to the action-executor
        slice.  Returning argv here prevents an unsafe unbounded fallback.
        """

        if not isinstance(container, DockerContainer):
            raise ValueError("container must be a validated DockerContainer")
        _require_container_id(container.identifier)
        _require_action(action)
        _require_container_cwd(cwd)
        expected_labels = _runtime_labels(container.spec, container.name)
        if container.labels != tuple(sorted(expected_labels.items())):
            raise DockerSecurityError("container identity labels were modified")
        self._verify_local_daemon()
        inspected = self._inspect_one(container.identifier)
        image_id = self._validate_security_profile(
            inspected,
            identifier=container.identifier,
            name=container.name,
            spec=container.spec,
            labels=expected_labels,
            running=True,
        )
        if image_id != container.image_id:
            raise DockerSecurityError("container image identity changed after validation")
        argv = self._build_argv(
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
        """Re-attest one exact action container and return its running state.

        Only the frozen ``running`` and terminal ``exited`` lifecycle states
        are accepted.  A created, paused, restarting, dead, mislabeled, or
        otherwise drifted object is ambiguous and therefore rejected.
        """

        if not isinstance(container, DockerContainer):
            raise ValueError("container must be a validated DockerContainer")
        _require_container_id(container.identifier)
        expected_labels = _runtime_labels(container.spec, container.name)
        if container.labels != tuple(sorted(expected_labels.items())):
            raise DockerSecurityError("container identity labels were modified")
        self._verify_local_daemon()
        inspected = self._inspect_one(container.identifier)
        state = _mapping(inspected.get("State"), "State")
        if state.get("Status") == "running" and state.get("Running") is True:
            running = True
            exited = False
        elif state.get("Status") == "exited" and state.get("Running") is False:
            running = False
            exited = True
        else:
            raise DockerSecurityError(
                "container lifecycle is neither attested running nor exited"
            )
        image_id = self._validate_security_profile(
            inspected,
            identifier=container.identifier,
            name=container.name,
            spec=container.spec,
            labels=expected_labels,
            running=running,
            exited=exited,
        )
        if image_id != container.image_id:
            raise DockerSecurityError("container image identity changed after validation")
        return running

    def inspect_container_writable_layer_bytes(
        self,
        *,
        container: DockerContainer,
        timeout_seconds: float,
    ) -> int:
        """Sample ``SizeRw`` for one exact running container.

        Docker Desktop does not provide the frozen study with a supported hard
        writable-layer quota.  This sampled value is therefore a watchdog
        signal, not a capacity guarantee.
        """

        self._require_container_handle(container)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("writable-layer probe timeout must be positive")
        result = self._invoke(
            "container",
            "inspect",
            "--size",
            "--",
            container.identifier,
            timeout_seconds=float(timeout_seconds),
        )
        inspected = _decode_single_inspection(
            result.stdout, "Docker sized container inspection"
        )
        expected_labels = _runtime_labels(container.spec, container.name)
        image_id = self._validate_security_profile(
            inspected,
            identifier=container.identifier,
            name=container.name,
            spec=container.spec,
            labels=expected_labels,
            running=True,
        )
        if image_id != container.image_id:
            raise DockerSecurityError(
                "container image identity changed during writable-layer probe"
            )
        size = inspected.get("SizeRw")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise DockerSecurityError("container writable layer size is unavailable")
        return size

    def list_run_containers(self, run_id: str) -> tuple[str, ...]:
        """Discover stopped or running resources carrying the exact run labels."""

        _require_run_id(run_id)
        self._verify_local_daemon()
        result = self._invoke(
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            f"label={MANAGED_LABEL}={MANAGED_VALUE}",
            "--filter",
            f"label={RUN_LABEL}={run_id}",
        )
        identifiers = _parse_container_ids(result.stdout, "run-container list")
        inspected = [self._inspect_one(identifier) for identifier in identifiers]
        for identifier, item in zip(identifiers, inspected, strict=True):
            self._validate_cleanup_ownership(item, run_id, identifier)
        return identifiers

    def remove_run_containers(
        self, run_id: str, identifiers: Sequence[str]
    ) -> tuple[str, ...]:
        """Remove only fully inspected containers owned by ``run_id``.

        Every target is inspected before the first removal, so a mixed list
        cannot cause a partial cleanup before an unrelated target is noticed.
        """

        _require_run_id(run_id)
        frozen_ids = tuple(identifiers)
        if len(set(frozen_ids)) != len(frozen_ids):
            raise DockerCleanupRefused("cleanup container identifiers must be unique")
        for identifier in frozen_ids:
            try:
                _require_container_id(identifier)
            except ValueError as error:
                raise DockerCleanupRefused("cleanup requires full container IDs") from error

        self._verify_local_daemon()
        inspected = [self._inspect_one(identifier) for identifier in frozen_ids]
        for identifier, item in zip(frozen_ids, inspected, strict=True):
            self._validate_cleanup_ownership(item, run_id, identifier)

        for identifier in frozen_ids:
            self._invoke(
                "container", "rm", "--force", "--volumes", "--", identifier
            )
        return frozen_ids

    def cleanup_run_containers(self, run_id: str) -> tuple[str, ...]:
        """Discover, validate, and remove only resources with exact run labels."""

        identifiers = self.list_run_containers(run_id)
        return self.remove_run_containers(run_id, identifiers)

    def _create_arguments(
        self,
        spec: DockerContainerSpec,
        name: str,
        labels: Mapping[str, str],
    ) -> tuple[str, ...]:
        limit = spec.limits
        arguments: list[str] = [
            "container",
            "create",
            "--name",
            name,
        ]
        for key in sorted(labels):
            arguments.extend(("--label", f"{key}={labels[key]}"))
        arguments.extend(
            (
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--security-opt",
                "seccomp=builtin",
                "--memory",
                str(limit.memory_bytes),
                "--memory-swap",
                str(limit.memory_swap_bytes),
                "--cpus",
                _render_cpus(limit.nano_cpus),
                "--pids-limit",
                str(limit.pids_limit),
                "--ulimit",
                f"fsize={limit.fsize_soft}:{limit.fsize_hard}",
                "--ulimit",
                f"nofile={limit.nofile_soft}:{limit.nofile_hard}",
                "--ulimit",
                f"nproc={limit.nproc_soft}:{limit.nproc_hard}",
                "--ipc",
                "private",
                "--hostname",
                CONTAINER_HOSTNAME,
                "--user",
                CONTAINER_USER,
                "--workdir",
                CONTAINER_WORKDIR,
                "--restart",
                "no",
                "--pull",
                "never",
                "--entrypoint",
                IDLE_ENTRYPOINT,
                spec.image,
                *IDLE_COMMAND,
            )
        )
        return tuple(arguments)

    def _verify_local_daemon(self) -> tuple[str, str]:
        """Re-check the context endpoint immediately before any mutation."""

        docker_host = self._env.get("DOCKER_HOST", "")
        if docker_host:
            raise DockerAdmissionError(
                "DOCKER_HOST must be unset; daemon overrides are not admitted"
            )
        current_context = self._single_line(
            self._invoke("context", "show", pinned_endpoint=False).stdout,
            "Docker context",
        )
        if current_context != self._expected_context:
            raise DockerAdmissionError(
                f"unexpected Docker context: expected {self._expected_context!r}, "
                f"got {current_context!r}"
            )

        endpoint_result = self._invoke(
            "context",
            "inspect",
            "--format",
            "{{json .Endpoints.docker.Host}}",
            self._expected_context,
            pinned_endpoint=False,
        )
        try:
            endpoint = json.loads(endpoint_result.stdout)
        except json.JSONDecodeError as error:
            raise DockerAdmissionError("Docker context endpoint is not valid JSON") from error
        if not isinstance(endpoint, str):
            raise DockerAdmissionError("Docker context endpoint must be a string")
        try:
            _require_local_endpoint(endpoint)
        except ValueError as error:
            raise DockerAdmissionError(
                "remote or unexpected Docker endpoint is not admitted"
            ) from error
        if endpoint != self._expected_endpoint:
            raise DockerAdmissionError(
                "remote or unexpected Docker endpoint is not admitted: endpoint drift"
            )
        return current_context, endpoint

    def _require_no_unrelated_running(self, run_id: str) -> None:
        running = self.list_running_containers()
        for identifier in running:
            item = self._inspect_one(identifier)
            try:
                self._validate_cleanup_ownership(item, run_id, identifier)
            except DockerCleanupRefused as error:
                raise DockerAdmissionError(
                    "Docker mutation refused because an unrelated container is running",
                    running_container_ids=running,
                ) from error

    def _validate_image(self, spec: DockerContainerSpec) -> None:
        result = self._invoke("image", "inspect", "--", spec.image)
        item = _decode_single_inspection(result.stdout, "Docker image inspection")
        if item.get("Id") != spec.image_id:
            raise DockerSecurityError("resolved image config id differs from the frozen pin")
        if item.get("Os") != CONTAINER_OS:
            raise DockerSecurityError("resolved image platform OS is not linux")
        if item.get("Architecture") != CONTAINER_ARCHITECTURE:
            raise DockerSecurityError("resolved image architecture is not arm64")
        image_labels = _labels(item)
        if image_labels.get(ROLE_LABEL) != spec.role:
            raise DockerSecurityError("resolved image role label differs from agent role")
        if image_labels.get(RUNTIME_NETWORK_LABEL) != "none-required":
            raise DockerSecurityError(
                "resolved image network-policy label is not none-required"
            )
        if "@sha256:" in spec.image:
            repo_digests = item.get("RepoDigests")
            if not isinstance(repo_digests, list) or spec.image not in repo_digests:
                raise DockerSecurityError(
                    "resolved image repository digest differs from the frozen reference"
                )

    def _invoke(
        self,
        *arguments: str,
        pinned_endpoint: bool = True,
        timeout_seconds: float | None = None,
    ) -> DockerCommandResult:
        for argument in arguments:
            if not isinstance(argument, str) or "\x00" in argument:
                raise ValueError("Docker argv entries must be NUL-free strings")
        if timeout_seconds is None:
            timeout = self._command_timeout_seconds
        elif (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("Docker command timeout override must be positive")
        else:
            timeout = float(timeout_seconds)
        argv = list(self._build_argv(*arguments, pinned_endpoint=pinned_endpoint))
        try:
            completed = self._runner(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                env=dict(self._env),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise DockerCommandTimeout("Docker CLI command timed out") from error
        except (OSError, UnicodeError) as error:
            raise DockerCliError("Docker CLI could not be executed safely") from error
        if (
            isinstance(completed.returncode, bool)
            or not isinstance(completed.returncode, int)
            or not isinstance(completed.stdout, str)
            or not isinstance(completed.stderr, str)
        ):
            raise DockerCliError("Docker CLI runner returned an invalid result type")
        result = DockerCommandResult(
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "no diagnostic text"
            raise DockerCommandError(
                f"Docker command failed with exit code {result.returncode}: {detail}",
                result,
            )
        return result

    def _build_argv(
        self, *arguments: str, pinned_endpoint: bool = True
    ) -> tuple[str, ...]:
        for argument in arguments:
            if not isinstance(argument, str) or "\x00" in argument:
                raise ValueError("Docker argv entries must be NUL-free strings")
        argv = [self._docker_binary]
        if pinned_endpoint:
            # Context identity is admitted separately, but every mutation and
            # inspection targets the already-verified local socket directly.
            # This prevents DOCKER_CONFIG/HOME drift between admission and a
            # later streaming subprocess from remapping a context name.
            argv.extend(("--host", self._expected_endpoint))
        argv.extend(arguments)
        return tuple(argv)

    def _inspect_one(self, identifier: str) -> dict[str, object]:
        result = self._invoke("container", "inspect", "--", identifier)
        return _decode_single_inspection(result.stdout, "Docker container inspection")

    def _require_container_handle(self, container: DockerContainer) -> None:
        if not isinstance(container, DockerContainer):
            raise ValueError("container must be a validated DockerContainer")
        _require_container_id(container.identifier)
        expected_labels = _runtime_labels(container.spec, container.name)
        if container.labels != tuple(sorted(expected_labels.items())):
            raise DockerSecurityError("container identity labels were modified")
        if container.image_id != container.spec.image_id:
            raise DockerSecurityError("container image handle contradicts its spec")

    def _attest_container_lifecycle(
        self,
        container: DockerContainer,
        *,
        lifecycle: str,
    ) -> dict[str, object]:
        self._require_container_handle(container)
        expected_labels = _runtime_labels(container.spec, container.name)
        inspected = self._inspect_one(container.identifier)
        if lifecycle == "created":
            running = False
        elif lifecycle == "running":
            running = True
        else:  # pragma: no cover - private call invariant
            raise ValueError("unsupported container lifecycle attestation")
        image_id = self._validate_security_profile(
            inspected,
            identifier=container.identifier,
            name=container.name,
            spec=container.spec,
            labels=expected_labels,
            running=running,
        )
        if image_id != container.image_id:
            raise DockerSecurityError("container image identity changed after validation")
        return inspected

    def _validate_collector_labels(
        self,
        item: Mapping[str, object],
        *,
        profile: str,
    ) -> Mapping[str, str]:
        labels = _labels(item)
        if labels.get(STATE_COLLECTOR_ARGV_LABEL) != _STATE_COLLECTOR_ARGV:
            raise DockerSecurityError("state collector fixed argv label differs")
        if labels.get(STATE_COLLECTOR_PROFILE_LABEL) != profile:
            raise DockerSecurityError("state collector profile label differs")
        if labels.get(FILESYSTEM_VERSION_LABEL) != profile.removeprefix("fs"):
            raise DockerSecurityError("state collector filesystem version differs")
        for key in (
            STATE_COLLECTOR_POLICY_LABEL,
            STATE_COLLECTOR_PROFILE_SET_LABEL,
            STATE_COLLECTOR_ROOT_LABEL,
            STATE_COLLECTOR_SOURCE_LABEL,
        ):
            try:
                _require_image_digest(labels.get(key), key)
            except ValueError as error:
                raise DockerSecurityError(
                    "state collector digest labels are unavailable"
                ) from error
        return labels

    def _validate_security_profile(
        self,
        item: Mapping[str, object],
        *,
        identifier: str,
        name: str,
        spec: DockerContainerSpec,
        labels: Mapping[str, str],
        running: bool,
        exited: bool = False,
    ) -> str:
        self._require_identity(item, identifier, name, labels)
        config = _mapping(item.get("Config"), "Config")
        host = _mapping(item.get("HostConfig"), "HostConfig")

        if config.get("Image") != spec.image:
            raise DockerSecurityError("mutable image or image identity drift detected")
        image_id = item.get("Image")
        if image_id != spec.image_id:
            raise DockerSecurityError("inspected image config id differs from the frozen pin")
        if item.get("Platform") != CONTAINER_OS:
            raise DockerSecurityError("container platform is not the frozen linux platform")
        _validate_container_state(item.get("State"), running=running, exited=exited)
        if config.get("Hostname") != CONTAINER_HOSTNAME:
            raise DockerSecurityError("container hostname drift detected")
        if config.get("User") != CONTAINER_USER:
            raise DockerSecurityError("container user drift detected")
        if config.get("WorkingDir") != CONTAINER_WORKDIR:
            raise DockerSecurityError("container working directory drift detected")
        if config.get("Entrypoint") != [IDLE_ENTRYPOINT]:
            raise DockerSecurityError("container entrypoint drift detected")
        if config.get("Cmd") != list(IDLE_COMMAND):
            raise DockerSecurityError("container idle command drift detected")

        if host.get("NetworkMode") != "none":
            raise DockerSecurityError("container network must be exactly none")
        networks = _mapping(
            _mapping(item.get("NetworkSettings"), "NetworkSettings").get("Networks"),
            "NetworkSettings.Networks",
        )
        if set(networks) != {"none"}:
            raise DockerSecurityError("container network attachment drift detected")

        for field, value in (
            ("mount list", item.get("Mounts")),
            ("mount binds", host.get("Binds")),
            ("mount requests", host.get("Mounts")),
            ("mount tmpfs", host.get("Tmpfs")),
            ("mount volume inheritance", host.get("VolumesFrom")),
            ("mount image volumes", config.get("Volumes")),
        ):
            _require_empty(value, field)
        for field in ("Devices", "DeviceRequests", "DeviceCgroupRules"):
            try:
                _require_empty(host.get(field), f"device field {field}")
            except DockerSecurityError as error:
                raise DockerSecurityError("container device access is not empty") from error
        if host.get("Privileged") is not False:
            raise DockerSecurityError("container privileged mode must be false")
        _require_empty(host.get("CapAdd"), "added capabilities")
        cap_drop = host.get("CapDrop")
        if cap_drop != ["ALL"]:
            raise DockerSecurityError("container capabilities must drop exactly ALL")

        security_options = host.get("SecurityOpt")
        if not isinstance(security_options, list) or not all(
            isinstance(value, str) for value in security_options
        ):
            raise DockerSecurityError("container security options are malformed")
        security_set = set(security_options)
        if "no-new-privileges=true" not in security_set:
            raise DockerSecurityError("container no-new-privileges option is missing")
        if "seccomp=builtin" not in security_set:
            raise DockerSecurityError("container seccomp builtin profile is missing")
        if security_set != SECURITY_OPTIONS or len(security_options) != len(SECURITY_OPTIONS):
            raise DockerSecurityError("container security options contain unexpected values")

        # Docker represents its isolated default PID and UTS namespaces as an
        # empty mode.  The only documented PID overrides are ``host`` and
        # ``container:<id>``; inventing ``--pid private`` would make the argv
        # look explicit while being incompatible with the real CLI.
        if host.get("PidMode") != "":
            raise DockerSecurityError("container pid namespace must be private")
        if host.get("IpcMode") != "private":
            raise DockerSecurityError("container IPC namespace must be private")
        if host.get("UTSMode") != "":
            raise DockerSecurityError("container UTS namespace must be private")

        limits = spec.limits
        _require_exact_integer(host.get("Memory"), limits.memory_bytes, "memory")
        _require_exact_integer(
            host.get("MemorySwap"), limits.memory_swap_bytes, "memory swap"
        )
        _require_empty(host.get("StorageOpt"), "storage options")
        _require_exact_integer(host.get("NanoCpus"), limits.nano_cpus, "CPU")
        _require_exact_integer(host.get("PidsLimit"), limits.pids_limit, "pids")
        expected_ulimits = {
            "fsize": (limits.fsize_soft, limits.fsize_hard),
            "nofile": (limits.nofile_soft, limits.nofile_hard),
            "nproc": (limits.nproc_soft, limits.nproc_hard),
        }
        if _parse_ulimits(host.get("Ulimits")) != expected_ulimits:
            raise DockerSecurityError("container ulimits differ from the frozen profile")

        if host.get("PublishAllPorts") is not False:
            raise DockerSecurityError("container port publishing must be disabled")
        _require_empty(host.get("PortBindings"), "port bindings")
        _require_empty(host.get("Links"), "container links")
        _require_empty(host.get("ExtraHosts"), "extra hosts")
        _require_empty(host.get("Sysctls"), "sysctls")
        restart = _mapping(host.get("RestartPolicy"), "RestartPolicy")
        if restart.get("Name") != "no" or restart.get("MaximumRetryCount") != 0:
            raise DockerSecurityError("container restart policy must be exactly no")
        if host.get("AutoRemove") is not False:
            raise DockerSecurityError("container auto-remove must be disabled")
        if "OomKillDisable" not in host:
            raise DockerSecurityError("container OOM kill must remain enabled")
        oom_kill_disable = host["OomKillDisable"]
        if not (
            oom_kill_disable is False
            or ((running or exited) and oom_kill_disable is None)
        ):
            raise DockerSecurityError("container OOM kill must remain enabled")
        return image_id

    def _require_identity(
        self,
        item: Mapping[str, object],
        identifier: str,
        name: str,
        expected_labels: Mapping[str, str],
    ) -> None:
        if item.get("Id") != identifier:
            raise DockerSecurityError("inspected container id does not match the request")
        if item.get("Name") != f"/{name}":
            raise DockerSecurityError("inspected container name does not match the request")
        labels = _labels(item)
        for key, expected in expected_labels.items():
            if labels.get(key) != expected:
                raise DockerSecurityError(f"container label {key!r} does not match")
        unexpected = {
            key
            for key in labels
            if key.startswith("org.edgeloopbench.")
            and key not in _ALLOWED_PROJECT_LABEL_KEYS
        }
        if unexpected:
            raise DockerSecurityError("container has unexpected EdgeLoopBench labels")
        if labels.get(RUNTIME_NETWORK_LABEL) != "none-required":
            raise DockerSecurityError("container image network-policy label is missing")

    def _validate_cleanup_ownership(
        self,
        item: Mapping[str, object],
        run_id: str,
        identifier: str,
    ) -> None:
        if item.get("Id") != identifier:
            raise DockerCleanupRefused("cleanup inspect id does not match requested id")
        labels = _labels(item, cleanup=True)
        if labels.get(MANAGED_LABEL) != MANAGED_VALUE:
            raise DockerCleanupRefused("cleanup target lacks the exact managed label")
        if labels.get(RUN_LABEL) != run_id:
            raise DockerCleanupRefused("cleanup target lacks the exact run label")
        role = labels.get(ROLE_LABEL)
        if role != "agent":
            raise DockerCleanupRefused("cleanup target lacks the exact agent role label")
        name = item.get("Name")
        if not isinstance(name, str) or not name.startswith("/"):
            raise DockerCleanupRefused("cleanup target has no inspectable name")
        bare_name = name[1:]
        if labels.get(INSTANCE_LABEL) != bare_name:
            raise DockerCleanupRefused("cleanup target instance label does not match its name")
        expected_name = re.compile(
            rf"^elb-{re.escape(run_id)}-{re.escape(role)}-[0-9a-f]{{16}}$"
        )
        if not expected_name.fullmatch(bare_name):
            raise DockerCleanupRefused(
                "cleanup target name is not bound to run, role, and nonce"
            )
        unexpected = {
            key
            for key in labels
            if key.startswith("org.edgeloopbench.")
            and key not in _ALLOWED_PROJECT_LABEL_KEYS
        }
        if unexpected:
            raise DockerCleanupRefused(
                "cleanup target has unexpected EdgeLoopBench labels"
            )

    @staticmethod
    def _single_line(value: str, field: str) -> str:
        lines = value.splitlines()
        if len(lines) != 1 or not lines[0].strip() or lines[0] != lines[0].strip():
            raise DockerCliError(f"{field} must be exactly one non-empty line")
        return lines[0]


def _require_run_id(value: str) -> None:
    if not isinstance(value, str) or not _RUN_ID_PATTERN.fullmatch(value):
        raise ValueError("run_id must be a safe lowercase identifier of at most 24 chars")


def _runtime_labels(spec: DockerContainerSpec, name: str) -> dict[str, str]:
    return {
        MANAGED_LABEL: MANAGED_VALUE,
        RUN_LABEL: spec.run_id,
        ROLE_LABEL: spec.role,
        INSTANCE_LABEL: name,
    }


def _require_role(value: str) -> None:
    if not isinstance(value, str) or not _ROLE_PATTERN.fullmatch(value):
        raise ValueError("role must be a safe lowercase identifier of at most 12 chars")


def _require_container_id(value: str) -> None:
    if not isinstance(value, str) or not _CONTAINER_ID_PATTERN.fullmatch(value):
        raise ValueError("Docker container identifier must be exactly 64 lowercase hex")


def _require_image_digest(value: object, field: str) -> None:
    if not isinstance(value, str) or not _IMAGE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 image identity")


def _require_state_profile(value: object) -> None:
    if not isinstance(value, str) or not _STATE_PROFILE_PATTERN.fullmatch(value):
        raise ValueError("state collector profile must be one of fs1 through fs4")


def _require_local_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str) or not endpoint.startswith("unix://"):
        raise ValueError("Docker endpoint must use a local Unix socket")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("Docker endpoint must be an absolute local Unix socket")
    if "\x00" in parsed.path or any(part == ".." for part in PurePosixPath(parsed.path).parts):
        raise ValueError("Docker endpoint path is unsafe")


def _require_action(action: str) -> None:
    if not isinstance(action, str) or not action or action != action.strip():
        raise ValueError("Docker action must be non-empty without surrounding whitespace")
    try:
        encoded = action.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("Docker action contains invalid Unicode") from error
    if len(encoded) > _MAX_ACTION_BYTES:
        raise ValueError("Docker action exceeds the fixed byte limit")
    if any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in action
    ):
        raise ValueError("Docker action must be one line without control characters")


def _require_container_cwd(cwd: str) -> None:
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
        raise ValueError("container cwd must be an absolute POSIX path")
    if len(cwd.encode("utf-8")) > _MAX_CWD_BYTES:
        raise ValueError("container cwd exceeds the fixed byte limit")
    if any(part == ".." for part in PurePosixPath(cwd).parts):
        raise ValueError("container cwd must not contain parent traversal")
    if str(PurePosixPath(cwd)) != cwd:
        raise ValueError("container cwd must use canonical POSIX spelling")
    if any(unicodedata.category(character).startswith("C") for character in cwd):
        raise ValueError("container cwd contains control characters")


def _render_cpus(nano_cpus: int) -> str:
    rendered = format(Decimal(nano_cpus) / Decimal(1_000_000_000), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _parse_container_ids(stdout: str, field: str) -> tuple[str, ...]:
    if not stdout:
        return ()
    lines = stdout.splitlines()
    identifiers: list[str] = []
    for line in lines:
        if not _CONTAINER_ID_PATTERN.fullmatch(line):
            raise DockerSecurityError(f"{field} contains an invalid full container id")
        identifiers.append(line)
    if len(set(identifiers)) != len(identifiers):
        raise DockerSecurityError(f"{field} contains duplicate container ids")
    return tuple(identifiers)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_single_inspection(stdout: str, field: str) -> dict[str, object]:
    try:
        decoded = json.loads(stdout, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise DockerSecurityError(f"{field} is not unambiguous JSON") from error
    if not isinstance(decoded, list) or len(decoded) != 1:
        raise DockerSecurityError(f"{field} must contain exactly one object")
    item = decoded[0]
    if not isinstance(item, dict):
        raise DockerSecurityError(f"{field} item must be an object")
    return cast(dict[str, object], item)


def _canonical_json_text(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _reject_json_constant(value: str) -> Never:
    raise ValueError(f"invalid JSON constant: {value}")


def _decode_trusted_state(
    stdout: str,
    *,
    profile: str,
    collector_source_sha256: str,
    expected_policy_sha256: str,
    expected_root_sha256: str,
) -> DockerTrustedState:
    try:
        encoded = stdout.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise DockerSecurityError(
            "trusted state collector output is not UTF-8"
        ) from error
    if (
        not encoded.endswith(b"\n")
        or encoded.endswith(b"\n\n")
        or len(encoded) > _MAX_TRUSTED_STATE_BYTES + 1
    ):
        raise DockerSecurityError(
            "trusted state collector output lacks its exact bounded framing"
        )
    canonical = stdout[:-1]
    try:
        decoded = json.loads(
            canonical,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise DockerSecurityError(
            "trusted state collector output is not unambiguous JSON"
        ) from error
    if not isinstance(decoded, dict) or _canonical_json_text(decoded) != canonical:
        raise DockerSecurityError("trusted state collector output is not canonical")
    expected_keys = {
        "common_roots",
        "dynamic_root_policy",
        "entries",
        "entry_count",
        "policy_sha256",
        "profile",
        "profile_sha256",
        "root_baseline_sha256",
        "schema",
        "state_sha256",
        "strict_surface",
        "task_roots",
        "total_file_bytes",
        "writable_surface_audit_sha256",
    }
    if set(decoded) != expected_keys:
        raise DockerSecurityError("trusted state collector schema fields differ")
    if decoded.get("schema") != "edgeloopbench.filesystem-state.v1":
        raise DockerSecurityError("trusted state collector schema differs")
    if decoded.get("profile") != profile:
        raise DockerSecurityError("trusted state collector profile differs")
    if decoded.get("policy_sha256") != expected_policy_sha256:
        raise DockerSecurityError("trusted state collector policy pin differs")
    if decoded.get("root_baseline_sha256") != expected_root_sha256:
        raise DockerSecurityError("trusted state collector root pin differs")
    for key in (
        "state_sha256",
        "profile_sha256",
        "policy_sha256",
        "root_baseline_sha256",
        "writable_surface_audit_sha256",
    ):
        try:
            _require_image_digest(decoded.get(key), key)
        except ValueError as error:
            raise DockerSecurityError(
                "trusted state collector digest field is malformed"
            ) from error
    entries = decoded.get("entries")
    entry_count = decoded.get("entry_count")
    total_file_bytes = decoded.get("total_file_bytes")
    if (
        not isinstance(entries, list)
        or isinstance(entry_count, bool)
        or not isinstance(entry_count, int)
        or entry_count < 0
        or entry_count != len(entries)
        or isinstance(total_file_bytes, bool)
        or not isinstance(total_file_bytes, int)
        or total_file_bytes < 0
    ):
        raise DockerSecurityError("trusted state collector accounting is malformed")
    strict = _mapping(decoded.get("strict_surface"), "strict_surface")
    if set(strict) != {"failures", "status"}:
        raise DockerSecurityError("trusted state collector strict surface is malformed")
    failures = strict.get("failures")
    if (
        not isinstance(failures, list)
        or not all(isinstance(value, str) for value in failures)
        or len(set(failures)) != len(failures)
        or any(
            value not in {"invalid_utf8_path", "invalid_utf8_symlink_target"}
            for value in failures
        )
    ):
        raise DockerSecurityError("trusted state collector strict failures differ")
    representable = not failures
    expected_status = "representable" if representable else "unrepresentable"
    if strict.get("status") != expected_status:
        raise DockerSecurityError("trusted state collector strict status differs")
    return DockerTrustedState(
        canonical_json=canonical,
        state_sha256=cast(str, decoded["state_sha256"]),
        profile=profile,
        profile_sha256=cast(str, decoded["profile_sha256"]),
        policy_sha256=cast(str, decoded["policy_sha256"]),
        root_baseline_sha256=cast(str, decoded["root_baseline_sha256"]),
        writable_surface_audit_sha256=cast(
            str, decoded["writable_surface_audit_sha256"]
        ),
        collector_source_sha256=collector_source_sha256,
        strict_representable=representable,
        strict_failures=tuple(cast(list[str], failures)),
    )


def _validate_container_state(
    value: object,
    *,
    running: bool,
    exited: bool = False,
) -> None:
    state = _mapping(value, "State")
    if running and exited:
        raise DockerSecurityError("container lifecycle expectation is contradictory")
    expected_status = "running" if running else "exited" if exited else "created"
    if state.get("Status") != expected_status or state.get("Running") is not running:
        raise DockerSecurityError(
            f"container lifecycle must be exactly {expected_status}"
        )
    for field in ("Paused", "Restarting", "Dead"):
        if state.get(field) is not False:
            raise DockerSecurityError(f"container lifecycle field {field} must be false")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DockerSecurityError(f"Docker inspection field {field} must be an object")
    return cast(Mapping[str, object], value)


def _labels(
    item: Mapping[str, object], *, cleanup: bool = False
) -> Mapping[str, str]:
    error_type: type[DockerCliError] = DockerCleanupRefused if cleanup else DockerSecurityError
    try:
        config = _mapping(item.get("Config"), "Config")
    except DockerSecurityError as error:
        raise error_type("container configuration is unavailable") from error
    raw = config.get("Labels")
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw.items()
    ):
        raise error_type("container labels are unavailable or malformed")
    return cast(Mapping[str, str], raw)


def _require_empty(value: object, field: str) -> None:
    if value is None or value == [] or value == {}:
        return
    raise DockerSecurityError(f"container {field} must be empty")


def _require_exact_integer(value: object, expected: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise DockerSecurityError(f"container {field} limit differs from the frozen value")


def _parse_ulimits(value: object) -> dict[str, tuple[int, int]]:
    if not isinstance(value, list):
        raise DockerSecurityError("container ulimits must be a list")
    parsed: dict[str, tuple[int, int]] = {}
    for index, raw in enumerate(value):
        item = _mapping(raw, f"Ulimits[{index}]")
        name = item.get("Name")
        soft = item.get("Soft")
        hard = item.get("Hard")
        if (
            not isinstance(name, str)
            or isinstance(soft, bool)
            or not isinstance(soft, int)
            or isinstance(hard, bool)
            or not isinstance(hard, int)
            or name in parsed
        ):
            raise DockerSecurityError("container ulimit entry is malformed")
        parsed[name] = (soft, hard)
    return parsed
