"""Fail-closed planning and execution for the four InterCode agent images."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Protocol, Sequence

from .intercode_gate_manifest import HostSafetyPins
from .intercode_host_safety import (
    DockerTelemetryPins,
    HostSafetyPolicy,
    HostTelemetryError,
    HostTelemetryCollector,
)


PLATFORM = "linux/arm64"
DOCKERFILE_AGENT_SHA256 = (
    "sha256:6c2b440dc7ebe277355fb21664de2a94eb0644f86698c92bcb836b88667a214f"
)
DOCKERIGNORE_SHA256 = (
    "sha256:41f598c8c3bb3868c615a3e59c23b215a4ce3754c2127538427f43b5a3653983"
)
_REVISION = "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8"
_TAGGED_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_SCHEMA = "edgeloopbench.intercode-image-build-manifest.v2"
_MAX_MANIFEST_BYTES = 1 << 20
_MAX_DOCKER_STDOUT_BYTES = 8 << 20
_MAX_DOCKER_STDERR_BYTES = 1 << 20
_DOCKER_BUILD_TIMEOUT_SECONDS = 7_200.0
_DOCKER_READ_TIMEOUT_SECONDS = 30.0
_COLLECTOR_LABELS = {
    "org.edgeloopbench.state-collector.sha256": (
        "sha256:513a0261fad1e52ce77479afd1c3196921ce558cc80e83632b68795e5639bba0"
    ),
    "org.edgeloopbench.state-collector.argv": (
        "/usr/bin/python3 -I -S -B /opt/edgeloop/state_collector.py --profile fsN"
    ),
    "org.edgeloopbench.state-collector.policy-sha256": (
        "sha256:70eeeda4091cb2da38aa8024af7c52dbacb464cf5b20a9f6bfdac5d66ecb67a9"
    ),
    "org.edgeloopbench.state-collector.root-baseline-sha256": (
        "sha256:06dcf54e33c9412b1c0bb2cf7ddab33848169e640012209b9d05c81ee1da457f"
    ),
    "org.edgeloopbench.state-collector.profile-set-sha256": (
        "sha256:1c515db46e794a58c457ac5d906ad80cae2ecb696ce2f07932733087368b1990"
    ),
}
_CONTEXT_ASSETS = {
    ".dockerignore": DOCKERIGNORE_SHA256,
    "docker/intercode/Dockerfile.agent": DOCKERFILE_AGENT_SHA256,
    "docker/intercode/Dockerfile.evaluator": (
        "sha256:318fc5e51345036ada580f2552ae8fed61d37d31c9853eddcd3a893fd9c22ffa"
    ),
    "docker/intercode/evaluator_placeholder.py": (
        "sha256:de4642dd71f18a3b5f1bfcb7a73f99292129aa9e73a25034a49d76269cd32cad"
    ),
    "docker/intercode/setup/setup_nl2b_fs_1.sh": (
        "sha256:3fe38c065ceb7d82a0105c413128d47788f4fd731f30ccc8a4a4d58200663c58"
    ),
    "docker/intercode/setup/setup_nl2b_fs_2.sh": (
        "sha256:29381bf8d1fade3ca86561f3e6bd129a9bbdddcf00f5e5236cc6358dd91d839f"
    ),
    "docker/intercode/setup/setup_nl2b_fs_3.sh": (
        "sha256:7d55db5d64d14ea8b4b72d86fa0fa68e7ed9fdeaa461fcfe8b80ff1f011d7026"
    ),
    "docker/intercode/setup/setup_nl2b_fs_4.sh": (
        "sha256:e155eece189f409162571aa0f300a1a7f57ea216adbe8dec36e6b73affd94858"
    ),
    "docker/intercode/state_collector.py": (
        "sha256:513a0261fad1e52ce77479afd1c3196921ce558cc80e83632b68795e5639bba0"
    ),
    f"vendor/intercode/{_REVISION}/docker/docker.gitignore": (
        "sha256:5479a1cafa260c77e836e8601ba9a345d39df777dc9cb07d6a93f0ac29b69166"
    ),
}


class InterCodeImageBuildError(RuntimeError):
    """The build plan, admission evidence, or Docker result is unsafe."""


class DockerBuildRunner(Protocol):
    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class InterCodeImageBuildRequest:
    repo_root: Path
    docker_binary: Path
    docker_pins: DockerTelemetryPins

    def __post_init__(self) -> None:
        _require_canonical_path(self.repo_root, directory=True, label="repository root")
        _require_canonical_path(self.docker_binary, directory=False, label="Docker binary")
        if self.repo_root.resolve(strict=True) != self.repo_root:
            raise InterCodeImageBuildError("repository root contains a symlink component")
        if self.docker_binary.resolve(strict=True) != self.docker_binary:
            raise InterCodeImageBuildError("Docker binary is not its real path")
        if not isinstance(self.docker_pins, DockerTelemetryPins):
            raise InterCodeImageBuildError("Docker pins must be typed")


@dataclass(frozen=True, slots=True)
class InterCodeImageBuildEntry:
    filesystem_version: int
    profile: str


@dataclass(frozen=True, slots=True)
class InterCodeImageBuildResult:
    plan_sha256: str
    manifest_sha256: str
    resumed_profiles: tuple[str, ...]
    built_profiles: tuple[str, ...]
    image_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ManifestImageRecord:
    sequence: int
    profile: str
    filesystem_version: int
    image_id: str
    inspection_sha256: str
    event_sha256: str


@dataclass(frozen=True, slots=True)
class InterCodeImageBuildPlan:
    platform: str
    docker_binary_sha256: str
    docker_endpoint_sha256: str
    docker_client_version: str
    docker_server_version: str
    dockerfile_sha256: str
    dockerignore_sha256: str
    context_sha256: str
    entries: tuple[InterCodeImageBuildEntry, ...]
    plan_sha256: str
    _repo_root: Path = field(repr=False, compare=False)
    _docker_binary: Path = field(repr=False, compare=False)
    _docker_endpoint: str = field(repr=False, compare=False)

    def _core_record(self) -> dict[str, object]:
        return {
            "schema": "edgeloopbench.intercode-image-build-plan.v2",
            "platform": self.platform,
            "docker": {
                "binary_sha256": self.docker_binary_sha256,
                "endpoint_sha256": self.docker_endpoint_sha256,
                "client_version": self.docker_client_version,
                "server_version": self.docker_server_version,
            },
            "context": {
                "context_sha256": self.context_sha256,
                "dockerfile_sha256": self.dockerfile_sha256,
                "dockerignore_sha256": self.dockerignore_sha256,
            },
            "entries": [
                {
                    "filesystem_version": entry.filesystem_version,
                    "profile": entry.profile,
                }
                for entry in self.entries
            ],
        }

    def canonical_record(self) -> dict[str, object]:
        record = self._core_record()
        if _digest(record) != self.plan_sha256:
            raise InterCodeImageBuildError("image-build plan digest is invalid")
        record["plan_sha256"] = self.plan_sha256
        return record

    def build_argv(
        self,
        entry: InterCodeImageBuildEntry,
        *,
        iidfile: Path,
    ) -> tuple[str, ...]:
        if entry not in self.entries:
            raise InterCodeImageBuildError("build entry is outside this plan")
        _require_canonical_path(iidfile, directory=False, label="Docker iidfile")
        labels = {
            "org.edgeloopbench.build.context-sha256": self.context_sha256,
            "org.edgeloopbench.build.dockerfile-sha256": self.dockerfile_sha256,
            "org.edgeloopbench.build.plan-sha256": self.plan_sha256,
        }
        argv = [
            os.fspath(self._docker_binary),
            "--host",
            self._docker_endpoint,
            "image",
            "build",
            "--quiet",
            "--pull=false",
            "--platform",
            self.platform,
            "--file",
            os.fspath(self._repo_root / "docker/intercode/Dockerfile.agent"),
            "--iidfile",
            os.fspath(iidfile),
            "--build-arg",
            f"FILE_SYSTEM_VERSION={entry.filesystem_version}",
        ]
        for key, value in sorted(labels.items()):
            argv.extend(("--label", f"{key}={value}"))
        argv.append(os.fspath(self._repo_root))
        return tuple(argv)


def create_intercode_image_build_plan(
    request: InterCodeImageBuildRequest,
) -> InterCodeImageBuildPlan:
    if type(request) is not InterCodeImageBuildRequest:
        raise InterCodeImageBuildError("image-build request must be typed")
    binary_sha256 = _hash_regular_file(
        request.docker_binary,
        executable=True,
        maximum_bytes=256 << 20,
        label="Docker binary",
    )
    if binary_sha256 != request.docker_pins.binary_sha256:
        raise InterCodeImageBuildError("Docker binary differs from its content pin")
    assets: dict[str, str] = {}
    for relative, expected in sorted(_CONTEXT_ASSETS.items()):
        asset_path = _require_context_asset_path(request.repo_root, relative)
        observed = _hash_regular_file(
            asset_path,
            executable=False,
            maximum_bytes=8 << 20,
            label=f"build context asset {relative}",
        )
        if observed != expected:
            raise InterCodeImageBuildError(f"reviewed build context asset drifted: {relative}")
        assets[relative] = observed
    setup = _require_context_directory_path(
        request.repo_root,
        "docker/intercode/setup",
    )
    setup_names = tuple(sorted(path.name for path in setup.iterdir()))
    if setup_names != tuple(f"setup_nl2b_fs_{value}.sh" for value in range(1, 5)):
        raise InterCodeImageBuildError("InterCode setup directory inventory drifted")
    context_sha256 = _digest({"assets": assets})
    entries = tuple(
        InterCodeImageBuildEntry(
            filesystem_version=value,
            profile=f"fs{value}",
        )
        for value in range(1, 5)
    )
    values = {
        "platform": PLATFORM,
        "docker_binary_sha256": binary_sha256,
        "docker_endpoint_sha256": request.docker_pins.endpoint_sha256,
        "docker_client_version": request.docker_pins.client_version,
        "docker_server_version": request.docker_pins.server_version,
        "dockerfile_sha256": DOCKERFILE_AGENT_SHA256,
        "dockerignore_sha256": DOCKERIGNORE_SHA256,
        "context_sha256": context_sha256,
        "entries": entries,
    }
    provisional = InterCodeImageBuildPlan(
        **values,
        plan_sha256="sha256:" + "0" * 64,
        _repo_root=request.repo_root,
        _docker_binary=request.docker_binary,
        _docker_endpoint=request.docker_pins.endpoint,
    )
    return InterCodeImageBuildPlan(
        **values,
        plan_sha256=_digest(provisional._core_record()),
        _repo_root=request.repo_root,
        _docker_binary=request.docker_binary,
        _docker_endpoint=request.docker_pins.endpoint,
    )


class _ManifestJournal:
    """One locked append-only manifest descriptor."""

    def __init__(self, path: Path, *, create: bool) -> None:
        self.path = path
        self.descriptor = -1
        self.parent_descriptor = -1
        self._closed = False
        parent_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        parent_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.parent_descriptor = os.open(path.parent, parent_flags)
            self._parent_metadata = os.fstat(self.parent_descriptor)
            self._validate_parent_metadata(self._parent_metadata, initial=True)
            flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= os.O_CREAT | os.O_EXCL if create else 0
            self.descriptor = os.open(
                path.name,
                flags,
                0o600,
                dir_fd=self.parent_descriptor,
            )
            self._file_metadata = os.fstat(self.descriptor)
            self._validate_file_metadata(self._file_metadata, initial=True)
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.revalidate()
        except OSError as error:
            self.close()
            raise InterCodeImageBuildError(
                "private image manifest could not be opened or locked"
            ) from error
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.descriptor >= 0:
                os.close(self.descriptor)
        finally:
            if self.parent_descriptor >= 0:
                os.close(self.parent_descriptor)

    def read(self) -> bytes:
        self.revalidate()
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(self.descriptor, min(65_536, _MAX_MANIFEST_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_MANIFEST_BYTES:
                raise InterCodeImageBuildError("private image manifest exceeds its bound")
        payload = b"".join(chunks)
        self.revalidate()
        return payload

    def append(self, record: Mapping[str, object]) -> None:
        self.revalidate()
        payload = _canonical_json(record) + b"\n"
        if len(payload) > 65_536:
            raise InterCodeImageBuildError("private image manifest event exceeds its bound")
        view = memoryview(payload)
        while view:
            written = os.write(self.descriptor, view)
            if written <= 0:
                raise InterCodeImageBuildError("private image manifest append failed")
            view = view[written:]
        os.fsync(self.descriptor)
        self.revalidate()

    def revalidate(self) -> None:
        """Require retained file, parent, and pathname identities to be exact."""

        if self._closed or self.descriptor < 0 or self.parent_descriptor < 0:
            raise InterCodeImageBuildError("private image manifest journal is closed")
        try:
            parent = os.fstat(self.parent_descriptor)
            file_metadata = os.fstat(self.descriptor)
            parent_link = os.stat(self.path.parent, follow_symlinks=False)
            file_link = os.stat(
                self.path.name,
                dir_fd=self.parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise InterCodeImageBuildError(
                "private image manifest identity could not be revalidated"
            ) from error
        self._validate_parent_metadata(parent, initial=False)
        self._validate_file_metadata(file_metadata, initial=False)
        if (parent_link.st_dev, parent_link.st_ino) != (
            parent.st_dev,
            parent.st_ino,
        ):
            raise InterCodeImageBuildError("private image manifest parent path changed")
        if (file_link.st_dev, file_link.st_ino) != (
            file_metadata.st_dev,
            file_metadata.st_ino,
        ):
            raise InterCodeImageBuildError("private image manifest path changed")

    def _validate_parent_metadata(
        self,
        metadata: os.stat_result,
        *,
        initial: bool,
    ) -> None:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise InterCodeImageBuildError(
                "private image manifest parent identity is unsafe"
            )
        if not initial and (metadata.st_dev, metadata.st_ino) != (
            self._parent_metadata.st_dev,
            self._parent_metadata.st_ino,
        ):
            raise InterCodeImageBuildError("private image manifest parent changed")

    def _validate_file_metadata(
        self,
        metadata: os.stat_result,
        *,
        initial: bool,
    ) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _MAX_MANIFEST_BYTES
        ):
            raise InterCodeImageBuildError("private image manifest identity is unsafe")
        if not initial and (metadata.st_dev, metadata.st_ino) != (
            self._file_metadata.st_dev,
            self._file_metadata.st_ino,
        ):
            raise InterCodeImageBuildError("private image manifest inode changed")


class _RepositoryBuildLock:
    """Cooperative cross-manifest lock for one repository build context."""

    def __init__(self, repo_root: Path) -> None:
        self.descriptor = -1
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.descriptor = os.open(repo_root, flags)
            metadata = os.fstat(self.descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise InterCodeImageBuildError("repository build lock is not a directory")
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            link = os.stat(repo_root, follow_symlinks=False)
            if (link.st_dev, link.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise InterCodeImageBuildError("repository build lock path changed")
        except OSError as error:
            if self.descriptor >= 0:
                os.close(self.descriptor)
            raise InterCodeImageBuildError(
                "repository execution lock is unavailable or already held"
            ) from error
        except BaseException:
            if self.descriptor >= 0:
                os.close(self.descriptor)
            raise

    def close(self) -> None:
        os.close(self.descriptor)


class _IidFile:
    """Precreated inode retained while Docker writes one exact image ID."""

    def __init__(self, path: Path) -> None:
        self.path = path
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self.descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise InterCodeImageBuildError("Docker iidfile is present or unsafe") from error
        self.metadata = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(self.metadata.st_mode)
            or self.metadata.st_nlink != 1
            or stat.S_IMODE(self.metadata.st_mode) != 0o600
        ):
            os.close(self.descriptor)
            raise InterCodeImageBuildError("Docker iidfile identity is unsafe")
        self._closed = False

    def read_image_id(self) -> str:
        self._revalidate()
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        payload = os.read(self.descriptor, 74)
        if len(payload) > 72 or os.read(self.descriptor, 1):
            raise InterCodeImageBuildError("Docker iidfile exceeds its bound")
        try:
            value = payload.decode("ascii")
        except UnicodeDecodeError as error:
            raise InterCodeImageBuildError("Docker iidfile is not ASCII") from error
        if value.endswith("\n"):
            value = value[:-1]
        if _IMAGE_ID.fullmatch(value) is None:
            raise InterCodeImageBuildError("Docker iidfile does not contain one full image ID")
        self._revalidate()
        return value

    def remove_after_success(self) -> None:
        self._revalidate()
        os.unlink(self.path)
        self.close()

    def close(self) -> None:
        if not self._closed:
            os.close(self.descriptor)
            self._closed = True

    def _revalidate(self) -> None:
        after = os.fstat(self.descriptor)
        link = os.stat(self.path, follow_symlinks=False)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or stat.S_IMODE(after.st_mode) != 0o600
            or (link.st_dev, link.st_ino) != (after.st_dev, after.st_ino)
            or (self.metadata.st_dev, self.metadata.st_ino)
            != (after.st_dev, after.st_ino)
        ):
            raise InterCodeImageBuildError("Docker iidfile identity changed")


def _manifest_header(plan: InterCodeImageBuildPlan) -> dict[str, object]:
    core: dict[str, object] = {
        "schema": _MANIFEST_SCHEMA,
        "sequence": 0,
        "kind": "plan",
        "previous_event_sha256": None,
        "plan": plan.canonical_record(),
    }
    return {**core, "event_sha256": _digest(core)}


def _manifest_image_event(
    plan: InterCodeImageBuildPlan,
    entry: InterCodeImageBuildEntry,
    *,
    sequence: int,
    previous_event_sha256: str,
    image_id: str,
    inspection_sha256: str,
) -> dict[str, object]:
    core: dict[str, object] = {
        "schema": _MANIFEST_SCHEMA,
        "sequence": sequence,
        "kind": "image",
        "previous_event_sha256": previous_event_sha256,
        "plan_sha256": plan.plan_sha256,
        "profile": entry.profile,
        "filesystem_version": entry.filesystem_version,
        "image_id": image_id,
        "inspection_sha256": inspection_sha256,
    }
    return {**core, "event_sha256": _digest(core)}


def _parse_manifest(
    payload: bytes,
    plan: InterCodeImageBuildPlan,
) -> tuple[dict[str, object], tuple[_ManifestImageRecord, ...]]:
    if not payload or len(payload) > _MAX_MANIFEST_BYTES or not payload.endswith(b"\n"):
        raise InterCodeImageBuildError("private image manifest is empty, torn, or oversized")
    values: list[dict[str, object]] = []
    for line in payload.splitlines():
        if not line or len(line) > 65_536:
            raise InterCodeImageBuildError("private image manifest contains an invalid line")
        values.append(_parse_json_object(line, "private image manifest event"))
    if not values or len(values) > 5:
        raise InterCodeImageBuildError("private image manifest event count is invalid")
    header = values[0]
    expected_header = _manifest_header(plan)
    if header != expected_header:
        raise InterCodeImageBuildError("private image manifest plan header is invalid")
    previous = expected_header["event_sha256"]
    assert isinstance(previous, str)
    records: list[_ManifestImageRecord] = []
    for sequence, raw in enumerate(values[1:], start=1):
        if sequence > len(plan.entries):
            raise InterCodeImageBuildError("private image manifest has too many strata")
        entry = plan.entries[sequence - 1]
        required = {
            "schema",
            "sequence",
            "kind",
            "previous_event_sha256",
            "plan_sha256",
            "profile",
            "filesystem_version",
            "image_id",
            "inspection_sha256",
            "event_sha256",
        }
        if set(raw) != required:
            raise InterCodeImageBuildError("private image manifest image fields are invalid")
        core = {key: value for key, value in raw.items() if key != "event_sha256"}
        event_sha256 = raw.get("event_sha256")
        image_id = raw.get("image_id")
        inspection_sha256 = raw.get("inspection_sha256")
        if (
            raw.get("schema") != _MANIFEST_SCHEMA
            or raw.get("sequence") != sequence
            or raw.get("kind") != "image"
            or raw.get("previous_event_sha256") != previous
            or raw.get("plan_sha256") != plan.plan_sha256
            or raw.get("profile") != entry.profile
            or raw.get("filesystem_version") != entry.filesystem_version
            or not isinstance(image_id, str)
            or _IMAGE_ID.fullmatch(image_id) is None
            or not isinstance(inspection_sha256, str)
            or _TAGGED_DIGEST.fullmatch(inspection_sha256) is None
            or event_sha256 != _digest(core)
        ):
            raise InterCodeImageBuildError("private image manifest hash chain is invalid")
        assert isinstance(event_sha256, str)
        records.append(
            _ManifestImageRecord(
                sequence=sequence,
                profile=entry.profile,
                filesystem_version=entry.filesystem_version,
                image_id=image_id,
                inspection_sha256=inspection_sha256,
                event_sha256=event_sha256,
            )
        )
        previous = event_sha256
    return header, tuple(records)


def execute_intercode_image_build(
    plan: InterCodeImageBuildPlan,
    *,
    manifest_path: Path,
    collector: HostTelemetryCollector,
    policy: HostSafetyPolicy,
    runner: DockerBuildRunner = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> InterCodeImageBuildResult:
    """Execute or resume one exact plan; no image-deletion command exists."""

    if type(plan) is not InterCodeImageBuildPlan:
        raise InterCodeImageBuildError("image-build plan must be typed")
    plan.canonical_record()
    lock = _RepositoryBuildLock(plan._repo_root)
    try:
        return _execute_intercode_image_build_locked(
            plan,
            manifest_path=manifest_path,
            collector=collector,
            policy=policy,
            runner=runner,
            environment=environment,
        )
    finally:
        lock.close()


def _execute_intercode_image_build_locked(
    plan: InterCodeImageBuildPlan,
    *,
    manifest_path: Path,
    collector: HostTelemetryCollector,
    policy: HostSafetyPolicy,
    runner: DockerBuildRunner,
    environment: Mapping[str, str] | None,
) -> InterCodeImageBuildResult:
    """Implementation entered only while the repository execution lock is held."""

    if type(plan) is not InterCodeImageBuildPlan:
        raise InterCodeImageBuildError("image-build plan must be typed")
    plan.canonical_record()
    if type(collector) is not HostTelemetryCollector:
        raise InterCodeImageBuildError("production HostTelemetryCollector is required")
    if type(policy) is not HostSafetyPolicy:
        raise InterCodeImageBuildError("production HostSafetyPolicy is required")
    _require_manifest_location(manifest_path)
    inherited = dict(os.environ if environment is None else environment)
    if "DOCKER_HOST" in inherited or "DOCKER_CONTEXT" in inherited:
        raise InterCodeImageBuildError(
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
        raise InterCodeImageBuildError("Docker subprocess environment is invalid")

    existing_payload: bytes | None = None
    if manifest_path.exists() or manifest_path.is_symlink():
        _require_private_parent(manifest_path.parent, create=False)
        journal = _ManifestJournal(manifest_path, create=False)
        try:
            existing_payload = journal.read()
            _parse_manifest(existing_payload, plan)
        finally:
            journal.close()
    _assert_plan_inputs_unchanged(plan)

    if existing_payload is None:
        _admit_quiescent(plan, collector, policy)
        _require_private_parent(manifest_path.parent, create=True)
        journal = _ManifestJournal(manifest_path, create=True)
        try:
            journal.append(_manifest_header(plan))
        except BaseException:
            journal.close()
            raise
        records: tuple[_ManifestImageRecord, ...] = ()
    else:
        journal = _ManifestJournal(manifest_path, create=False)
        try:
            current = journal.read()
            if current != existing_payload:
                raise InterCodeImageBuildError(
                    "private image manifest changed before lock"
                )
            _header, records = _parse_manifest(current, plan)
            _admit_quiescent(plan, collector, policy)
        except BaseException:
            journal.close()
            raise

    try:
        for record, entry in zip(records, plan.entries[: len(records)], strict=True):
            evidence = _inspect_exact_image(
                plan,
                entry,
                image_id=record.image_id,
                runner=runner,
                environment=inherited,
            )
            if _digest(evidence) != record.inspection_sha256:
                raise InterCodeImageBuildError(
                    "recorded image inspection differs from its manifest evidence"
                )
        built_profiles: list[str] = []
        previous = (
            records[-1].event_sha256
            if records
            else str(_manifest_header(plan)["event_sha256"])
        )
        for entry in plan.entries[len(records) :]:
            iid_path = manifest_path.parent / (
                f".{manifest_path.name}.{entry.profile}."
                f"{plan.plan_sha256.removeprefix('sha256:')}.iid"
            )
            iid = _IidFile(iid_path)
            try:
                result = _invoke_docker(
                    plan,
                    plan.build_argv(entry, iidfile=iid_path),
                    runner=runner,
                    environment=inherited,
                    pre_invocation_admission=lambda: _admit_quiescent(
                        plan,
                        collector,
                        policy,
                    ),
                )
                _admit_quiescent(plan, collector, policy)
                _assert_plan_inputs_unchanged(plan)
                image_id = iid.read_image_id()
                if result.stdout not in ("", image_id, image_id + "\n"):
                    raise InterCodeImageBuildError(
                        "Docker build stdout contradicts the retained iidfile"
                    )
                evidence = _inspect_exact_image(
                    plan,
                    entry,
                    image_id=image_id,
                    runner=runner,
                    environment=inherited,
                )
                inspection_sha256 = _digest(evidence)
                _admit_quiescent(plan, collector, policy)
                _assert_plan_inputs_unchanged(plan)
                iid.remove_after_success()
                event = _manifest_image_event(
                    plan,
                    entry,
                    sequence=entry.filesystem_version,
                    previous_event_sha256=previous,
                    image_id=image_id,
                    inspection_sha256=inspection_sha256,
                )
                journal.append(event)
                previous = str(event["event_sha256"])
                built_profiles.append(entry.profile)
            finally:
                iid.close()
        _admit_quiescent(plan, collector, policy)
        _assert_plan_inputs_unchanged(plan)
        final_payload = journal.read()
        _header, final_records = _parse_manifest(final_payload, plan)
        journal.revalidate()
        return InterCodeImageBuildResult(
            plan_sha256=plan.plan_sha256,
            manifest_sha256="sha256:" + hashlib.sha256(final_payload).hexdigest(),
            resumed_profiles=tuple(record.profile for record in records),
            built_profiles=tuple(built_profiles),
            image_ids=tuple(record.image_id for record in final_records),
        )
    finally:
        journal.close()


def _admit_quiescent(
    plan: InterCodeImageBuildPlan,
    collector: HostTelemetryCollector,
    policy: HostSafetyPolicy,
) -> None:
    try:
        sample = collector.collect()
        decision = policy.evaluate_admission(sample)
    except (OSError, RuntimeError, ValueError) as error:
        raise InterCodeImageBuildError("quiescent host telemetry failed") from error
    daemon = sample.docker_daemon
    if (
        not decision.allowed
        or sample.vm_pressure_level != 1
        or sample.resident_models
        or sample.running_container_ids
        or daemon.binary_sha256 != plan.docker_binary_sha256
        or daemon.endpoint_sha256 != plan.docker_endpoint_sha256
        or daemon.client_version != plan.docker_client_version
        or daemon.server_version != plan.docker_server_version
    ):
        raise InterCodeImageBuildError("quiescent host-safety admission refused execution")


def _assert_plan_inputs_unchanged(plan: InterCodeImageBuildPlan) -> None:
    pins = DockerTelemetryPins(
        endpoint=plan._docker_endpoint,
        client_version=plan.docker_client_version,
        server_version=plan.docker_server_version,
        binary_sha256=plan.docker_binary_sha256,
    )
    observed = create_intercode_image_build_plan(
        InterCodeImageBuildRequest(
            repo_root=plan._repo_root,
            docker_binary=plan._docker_binary,
            docker_pins=pins,
        )
    )
    if observed.canonical_record() != plan.canonical_record():
        raise InterCodeImageBuildError("image-build plan inputs changed")


def _inspect_exact_image(
    plan: InterCodeImageBuildPlan,
    entry: InterCodeImageBuildEntry,
    *,
    image_id: str,
    runner: DockerBuildRunner,
    environment: Mapping[str, str],
) -> dict[str, object]:
    if _IMAGE_ID.fullmatch(image_id) is None:
        raise InterCodeImageBuildError("image ID is invalid")
    argv = (
        os.fspath(plan._docker_binary),
        "--host",
        plan._docker_endpoint,
        "image",
        "inspect",
        "--format",
        "{{json .}}",
        "--",
        image_id,
    )
    result = _invoke_docker(plan, argv, runner=runner, environment=environment)
    item = _parse_json_object(
        result.stdout.encode("utf-8"),
        "Docker image inspection",
    )
    config = item.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    expected_labels = {
        "org.opencontainers.image.source": "https://github.com/princeton-nlp/intercode",
        "org.opencontainers.image.revision": _REVISION,
        "org.edgeloopbench.role": "agent",
        "org.edgeloopbench.runtime-network": "none-required",
        "org.edgeloopbench.filesystem-version": str(entry.filesystem_version),
        "org.edgeloopbench.state-collector.profile": entry.profile,
        "org.edgeloopbench.build.context-sha256": plan.context_sha256,
        "org.edgeloopbench.build.dockerfile-sha256": plan.dockerfile_sha256,
        "org.edgeloopbench.build.plan-sha256": plan.plan_sha256,
        **_COLLECTOR_LABELS,
    }
    if (
        item.get("Id") != image_id
        or item.get("Os") != "linux"
        or item.get("Architecture") != "arm64"
        or labels != expected_labels
    ):
        raise InterCodeImageBuildError("Docker image inspection differs from the frozen profile")
    return {
        "image_id": image_id,
        "platform": PLATFORM,
        "labels": expected_labels,
    }


def _invoke_docker(
    plan: InterCodeImageBuildPlan,
    argv: Sequence[str],
    *,
    runner: DockerBuildRunner,
    environment: Mapping[str, str],
    pre_invocation_admission: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    expected_prefix = (
        os.fspath(plan._docker_binary),
        "--host",
        plan._docker_endpoint,
    )
    if tuple(argv[:3]) != expected_prefix or any(
        not isinstance(value, str) or "\x00" in value for value in argv
    ):
        raise InterCodeImageBuildError("Docker argv is outside the fixed endpoint boundary")
    if pre_invocation_admission is not None and not callable(
        pre_invocation_admission
    ):
        raise InterCodeImageBuildError("Docker pre-invocation admission is invalid")
    _assert_plan_inputs_unchanged(plan)
    if pre_invocation_admission is not None:
        pre_invocation_admission()
    try:
        timeout = (
            _DOCKER_BUILD_TIMEOUT_SECONDS
            if tuple(argv[3:5]) == ("image", "build")
            else _DOCKER_READ_TIMEOUT_SECONDS
        )
        completed = runner(
            list(argv),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=dict(environment),
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise InterCodeImageBuildError("Docker command failed safely") from error
    if (
        type(completed.returncode) is not int
        or not isinstance(completed.stdout, str)
        or not isinstance(completed.stderr, str)
        or len(completed.stdout.encode("utf-8")) > _MAX_DOCKER_STDOUT_BYTES
        or len(completed.stderr.encode("utf-8")) > _MAX_DOCKER_STDERR_BYTES
        or completed.returncode != 0
        or completed.stderr
    ):
        raise InterCodeImageBuildError("Docker command returned invalid bounded output")
    _assert_plan_inputs_unchanged(plan)
    return completed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m edgeloopbench.intercode_image_build",
        allow_abbrev=False,
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--docker-binary", required=True)
    parser.add_argument("--docker-binary-sha256", required=True)
    parser.add_argument("--docker-endpoint", required=True)
    parser.add_argument("--docker-client-version", required=True)
    parser.add_argument("--docker-server-version", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--docker-data-path")
    parser.add_argument("--host-safety-pins")
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        pins = DockerTelemetryPins(
            endpoint=arguments.docker_endpoint,
            client_version=arguments.docker_client_version,
            server_version=arguments.docker_server_version,
            binary_sha256=arguments.docker_binary_sha256,
        )
        request = InterCodeImageBuildRequest(
            repo_root=Path(arguments.repo_root),
            docker_binary=Path(arguments.docker_binary),
            docker_pins=pins,
        )
        plan = create_intercode_image_build_plan(request)
        if arguments.execute:
            if not arguments.host_safety_pins:
                raise InterCodeImageBuildError(
                    "--host-safety-pins is required with --execute"
                )
            if not arguments.manifest:
                raise InterCodeImageBuildError("--manifest is required with --execute")
            if not arguments.docker_data_path:
                raise InterCodeImageBuildError(
                    "--docker-data-path is required with --execute"
                )
            host_pins = _load_host_safety_pins(Path(arguments.host_safety_pins))
            if (
                host_pins.docker_binary_sha256 != plan.docker_binary_sha256
                or host_pins.docker_endpoint_sha256 != plan.docker_endpoint_sha256
                or host_pins.docker_client_version != plan.docker_client_version
                or host_pins.docker_server_version != plan.docker_server_version
            ):
                raise InterCodeImageBuildError(
                    "host-safety Docker pins differ from the image-build plan"
                )
            docker_data_path = _require_canonical_path(
                Path(arguments.docker_data_path),
                directory=True,
                label="Docker data path",
            )
            collector = HostTelemetryCollector(
                docker_binary=request.docker_binary,
                docker_pins=request.docker_pins,
                docker_data_path=docker_data_path,
            )
            result = execute_intercode_image_build(
                plan,
                manifest_path=Path(arguments.manifest),
                collector=collector,
                policy=HostSafetyPolicy(host_pins),
            )
            print(
                json.dumps(
                    {"mode": "execute", "result": asdict(result)},
                    sort_keys=True,
                )
            )
            return 0
        print(json.dumps({"mode": "plan", "plan": plan.canonical_record()}, sort_keys=True))
        return 0
    except (ValueError, OSError, HostTelemetryError, InterCodeImageBuildError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _require_canonical_path(path: object, *, directory: bool, label: str) -> Path:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or Path(os.path.normpath(os.fspath(path))) != path
        or path.is_symlink()
    ):
        raise InterCodeImageBuildError(f"{label} must be a canonical absolute non-symlink path")
    if directory and not path.is_dir():
        raise InterCodeImageBuildError(f"{label} must be a directory")
    return path


def _require_manifest_location(path: object) -> Path:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or Path(os.path.normpath(os.fspath(path))) != path
        or "\x00" in os.fspath(path)
        or path.name in ("", ".", "..")
    ):
        raise InterCodeImageBuildError("private image manifest path is invalid")
    if path.parent.is_symlink():
        raise InterCodeImageBuildError("private image manifest parent is a symlink")
    return path


def _require_context_directory_path(repo_root: Path, relative: str) -> Path:
    current = repo_root
    for component in Path(relative).parts:
        current = current / component
        try:
            metadata = os.stat(current, follow_symlinks=False)
        except OSError as error:
            raise InterCodeImageBuildError(
                f"build context directory is unavailable: {relative}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise InterCodeImageBuildError(
                f"build context directory is symlinked or invalid: {relative}"
            )
    return current


def _require_context_asset_path(repo_root: Path, relative: str) -> Path:
    parent = os.fspath(Path(relative).parent)
    if parent == ".":
        directory = repo_root
    else:
        directory = _require_context_directory_path(repo_root, parent)
    path = directory / Path(relative).name
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise InterCodeImageBuildError(
            f"build context asset is unavailable: {relative}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InterCodeImageBuildError(
            f"build context asset is symlinked or invalid: {relative}"
        )
    return path


def _require_private_parent(path: Path, *, create: bool) -> None:
    if not path.exists():
        if not create:
            raise InterCodeImageBuildError("private image manifest parent is absent")
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise InterCodeImageBuildError("private image manifest ancestor is unsafe")
        try:
            os.mkdir(path, 0o700)
        except OSError as error:
            raise InterCodeImageBuildError("private image manifest parent creation failed") from error
    if path.is_symlink() or not path.is_dir():
        raise InterCodeImageBuildError("private image manifest parent is unsafe")
    metadata = os.stat(path, follow_symlinks=False)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise InterCodeImageBuildError("private image manifest parent must be owner mode 0700")


def _parse_json_object(payload: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise InterCodeImageBuildError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise InterCodeImageBuildError(f"{label} must be a JSON object")
    return value


def _load_host_safety_pins(path: Path) -> HostSafetyPins:
    _require_canonical_path(path, directory=False, label="host-safety pins")
    payload = _read_small_control_file(path, maximum_bytes=65_536, label="host-safety pins")
    value = _parse_json_object(payload, "host-safety pins")
    expected_fields = {item.name for item in fields(HostSafetyPins)}
    if set(value) != expected_fields:
        raise InterCodeImageBuildError("host-safety pins fields are incomplete or extra")
    try:
        return HostSafetyPins(**value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise InterCodeImageBuildError("host-safety pins are invalid") from error


def _read_small_control_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InterCodeImageBuildError(f"{label} is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
            or before.st_mode & stat.S_IWOTH
        ):
            raise InterCodeImageBuildError(f"{label} file identity is unsafe")
        payload = os.read(descriptor, maximum_bytes + 1)
        if len(payload) != before.st_size or os.read(descriptor, 1):
            raise InterCodeImageBuildError(f"{label} changed while reading")
        after = os.fstat(descriptor)
        fields_to_check = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
        )
        if any(
            getattr(before, item) != getattr(after, item)
            for item in fields_to_check
        ):
            raise InterCodeImageBuildError(f"{label} changed while reading")
        link = os.stat(path, follow_symlinks=False)
        if (link.st_dev, link.st_ino) != (after.st_dev, after.st_ino):
            raise InterCodeImageBuildError(f"{label} path identity changed")
        return payload
    finally:
        os.close(descriptor)


def _hash_regular_file(
    path: Path, *, executable: bool, maximum_bytes: int, label: str
) -> str:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise InterCodeImageBuildError(f"{label} path is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InterCodeImageBuildError(f"{label} is unavailable") from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
            or before.st_mode & stat.S_IWOTH
            or (executable and not before.st_mode & stat.S_IXUSR)
        ):
            raise InterCodeImageBuildError(f"{label} file identity is unsafe")
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
        if any(getattr(before, name) != getattr(after, name) for name in fields):
            raise InterCodeImageBuildError(f"{label} changed while hashing")
        link = os.stat(path, follow_symlinks=False)
        if (link.st_dev, link.st_ino) != (after.st_dev, after.st_ino):
            raise InterCodeImageBuildError(f"{label} path identity changed")
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
