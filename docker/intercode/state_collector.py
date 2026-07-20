"""Canonical, gold-free filesystem state collection for InterCode-Bash.

The helper is copied into the agent image as a root-owned, read-only program.
Its command-line surface is intentionally one fixed option, and it uses only
descriptor-relative standard-library filesystem operations.  It does not run
commands, inspect Git metadata as metadata, or follow symbolic links.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


SCHEMA = "edgeloopbench.filesystem-state.v1"
ERROR_SCHEMA = "edgeloopbench.filesystem-state-error.v1"
AUDIT_SCHEMA = "edgeloopbench.writable-surface-audit.v1"
AUDIT_RELATIVE_PATH = "opt/edgeloop/writable_surface_audit.json"
AGENT_UID = 65532
AGENT_GID = 65532
ROOT_OWNER_UID = 0
ROOT_MODE = 0o1777
PSEUDO_MOUNT_ROOTS = ("dev", "proc", "sys")
RUNTIME_INJECTED_ROOT_NAMES = (".dockerenv",)
VAR_LOCK_PATH = "var/lock"
VAR_LOCK_RESOLVED_TARGET = "run/lock"
_MAX_AUDIT_SCAN_ENTRIES = 250_000
_MAX_AUDIT_DEPTH = 128
_MAX_AUDIT_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_BYTES = 1024 * 1024
_POSIX_ACL_NAMES = frozenset(
    {"system.posix_acl_access", "system.posix_acl_default"}
)

# These roots are derived only from the public fixture setup and task surfaces.
# Other agent-created top-level names are discovered by the frozen root policy.
PROFILE_TASK_ROOTS = MappingProxyType(
    {
        "fs1": ("testbed",),
        "fs2": ("system",),
        "fs3": ("workspace", "backup"),
        "fs4": (),
    }
)
COMMON_WRITABLE_ROOTS = (
    "home/agent",
    "usr/workspace",
    "tmp",
    "var/tmp",
    "run/lock",
)
EPHEMERAL_EMPTY_ROOTS = ("dev/shm", "dev/mqueue")
SYSVIPC_HEADERS = MappingProxyType(
    {
        "proc/sysvipc/shm": (
            "key",
            "shmid",
            "perms",
            "size",
            "cpid",
            "lpid",
            "nattch",
            "uid",
            "gid",
            "cuid",
            "cgid",
            "atime",
            "dtime",
            "ctime",
            "rss",
            "swap",
        ),
        "proc/sysvipc/sem": (
            "key",
            "semid",
            "perms",
            "nsems",
            "uid",
            "gid",
            "cuid",
            "cgid",
            "otime",
            "ctime",
        ),
        "proc/sysvipc/msg": (
            "key",
            "msqid",
            "perms",
            "cbytes",
            "qnum",
            "lspid",
            "lrpid",
            "uid",
            "gid",
            "cuid",
            "cgid",
            "stime",
            "rtime",
            "ctime",
        ),
    }
)
_MAX_EPHEMERAL_TABLE_BYTES = 64 * 1024

# Names supplied by the pinned base image or by the runtime boundary are not
# task state.  They are never traversed through the dynamic-root rule.  Narrow
# writable descendants and task roots are collected separately above.
IMMUTABLE_ROOT_NAMES = (
    ".dockerenv",
    ".git",
    ".gitignore",
    "bin",
    "boot",
    "dev",
    "etc",
    "home",
    "lib",
    "media",
    "mnt",
    "opt",
    "proc",
    "root",
    "run",
    "sbin",
    "srv",
    "sys",
    "tmp",
    "usr",
    "var",
)


@dataclass(frozen=True, slots=True)
class CollectionLimits:
    max_entries: int
    max_depth: int
    max_file_bytes: int
    max_total_file_bytes: int
    max_output_bytes: int
    max_path_bytes: int
    max_symlink_target_bytes: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_entries",
            "max_depth",
            "max_file_bytes",
            "max_total_file_bytes",
            "max_output_bytes",
            "max_path_bytes",
            "max_symlink_target_bytes",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("collection limits must be positive integers")
        if self.max_total_file_bytes < self.max_file_bytes:
            raise ValueError("total byte limit cannot be smaller than file byte limit")

    def to_record(self) -> dict[str, int]:
        return {
            "max_depth": self.max_depth,
            "max_entries": self.max_entries,
            "max_file_bytes": self.max_file_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_path_bytes": self.max_path_bytes,
            "max_symlink_target_bytes": self.max_symlink_target_bytes,
            "max_total_file_bytes": self.max_total_file_bytes,
        }


DEFAULT_LIMITS = CollectionLimits(
    max_entries=4096,
    max_depth=32,
    max_file_bytes=16 * 1024 * 1024,
    max_total_file_bytes=64 * 1024 * 1024,
    max_output_bytes=4 * 1024 * 1024,
    max_path_bytes=4096,
    max_symlink_target_bytes=4096,
)


class CollectionFailure(str, Enum):
    INVALID_INVOCATION = "invalid_invocation"
    IO_FAILURE = "io_failure"
    ROOT_BOUNDARY = "root_boundary"
    RACE_DETECTED = "race_detected"
    ENTRY_LIMIT = "entry_limit"
    DEPTH_LIMIT = "depth_limit"
    FILE_BYTES_LIMIT = "file_bytes_limit"
    TOTAL_BYTES_LIMIT = "total_bytes_limit"
    OUTPUT_LIMIT = "output_limit"
    PATH_LIMIT = "path_limit"
    SYMLINK_TARGET_LIMIT = "symlink_target_limit"
    SPECIAL_FILE = "special_file"
    INCOMPLETE_HARDLINK = "incomplete_hardlink"
    EPHEMERAL_STATE = "ephemeral_state"
    ACL_UNVERIFIED = "acl_unverified"
    ACL_PRESENT = "acl_present"
    XATTR_PRESENT = "xattr_present"
    WRITABLE_SURFACE = "writable_surface"
    AUDIT_INVALID = "audit_invalid"
    INTERNAL_ERROR = "internal_error"


class StrictSurfaceFailure(str, Enum):
    INVALID_UTF8_PATH = "invalid_utf8_path"
    INVALID_UTF8_SYMLINK_TARGET = "invalid_utf8_symlink_target"


class StateCollectionError(RuntimeError):
    """A typed, path-free collection failure safe for orchestration."""

    __slots__ = ("kind",)

    def __init__(self, kind: CollectionFailure) -> None:
        self.kind = kind
        super().__init__(kind.value)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _sha256_record(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


ROOT_BASELINE_SHA256 = _sha256_record(
    {"immutable_root_names": list(IMMUTABLE_ROOT_NAMES)}
)


def _policy_record(limits: CollectionLimits) -> dict[str, object]:
    return {
        "algorithm": "writable-surface-v1",
        "agent_gid": AGENT_GID,
        "agent_uid": AGENT_UID,
        "audit": {
            "acl_policy": "reject_posix_acl",
            "captured_xattr_policy": "reject_all",
            "max_depth": _MAX_AUDIT_DEPTH,
            "max_scan_entries": _MAX_AUDIT_SCAN_ENTRIES,
            "pseudo_mount_roots": list(PSEUDO_MOUNT_ROOTS),
            "relative_path": AUDIT_RELATIVE_PATH,
            "runtime_injected_root_names": list(RUNTIME_INJECTED_ROOT_NAMES),
            "schema": AUDIT_SCHEMA,
            "var_lock_resolved_target": VAR_LOCK_RESOLVED_TARGET,
        },
        "runtime_xattr_policy": "reject_all",
        "common_roots": list(COMMON_WRITABLE_ROOTS),
        "dynamic_root_policy": "non_baseline_top_level",
        "ephemeral_empty_roots": list(EPHEMERAL_EMPTY_ROOTS),
        "ephemeral_header_only_files": {
            path: list(fields) for path, fields in SYSVIPC_HEADERS.items()
        },
        "limits": limits.to_record(),
        "profiles": {
            profile: list(roots) for profile, roots in PROFILE_TASK_ROOTS.items()
        },
        "root": {"mode": ROOT_MODE, "uid": ROOT_OWNER_UID},
        "root_baseline_sha256": ROOT_BASELINE_SHA256,
        "schema": SCHEMA,
    }


def _policy_sha256(limits: CollectionLimits) -> str:
    return _sha256_record(_policy_record(limits))


def _profile_sha256(profile: str, policy_sha256: str) -> str:
    return _sha256_record(
        {
            "policy_sha256": policy_sha256,
            "profile": profile,
            "task_roots": list(PROFILE_TASK_ROOTS[profile]),
        }
    )


POLICY_SHA256 = _policy_sha256(DEFAULT_LIMITS)
PROFILE_SHA256 = MappingProxyType(
    {
        profile: _profile_sha256(profile, POLICY_SHA256)
        for profile in PROFILE_TASK_ROOTS
    }
)
PROFILE_SET_SHA256 = _sha256_record(dict(PROFILE_SHA256))


@dataclass(slots=True)
class _CollectedEntry:
    path_bytes: bytes
    record: dict[str, object]
    hardlink_identity: tuple[int, int] | None = None
    link_count: int = 0


_OPEN_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_OPEN_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
if hasattr(os, "O_CLOEXEC"):
    _OPEN_DIRECTORY_FLAGS |= os.O_CLOEXEC
    _OPEN_FILE_FLAGS |= os.O_CLOEXEC


class _WritableSurfaceAuditor:
    def __init__(
        self,
        profile: str,
        root_prefix: bytes,
        acl_probe: object,
    ) -> None:
        self.profile = profile
        self.root_prefix = root_prefix
        self.acl_probe = acl_probe
        self.scanned_entries = 0
        self.acl_probed_entries = 0
        self.controlled_records: list[dict[str, object]] = []
        self.tree_digest = hashlib.sha256(
            b"edgeloopbench-writable-surface-tree-v1\0"
        )
        self.root_device: int | None = None

    def audit(self) -> dict[str, object]:
        try:
            root_descriptor = os.open(self.root_prefix, _OPEN_DIRECTORY_FLAGS)
        except OSError as error:
            raise StateCollectionError(CollectionFailure.IO_FAILURE) from error
        try:
            root_metadata = os.fstat(root_descriptor)
            _validate_audit_root(root_metadata)
            self.root_device = root_metadata.st_dev
            self._reject_build_xattrs(root_descriptor, b".")
            self._observe(b".", root_metadata, "directory")
            children = self._directory_names(root_descriptor)
            child_names = {raw_name for raw_name, _name in children}
            required_names = {
                name.encode("ascii")
                for name in IMMUTABLE_ROOT_NAMES
                if name not in RUNTIME_INJECTED_ROOT_NAMES
            }
            if not required_names.issubset(child_names):
                raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
            for raw_name, name in children:
                if raw_name in {value.encode("ascii") for value in PSEUDO_MOUNT_ROOTS}:
                    continue
                self._audit_named(
                    root_descriptor,
                    name,
                    raw_name,
                    depth=1,
                )
            after = os.fstat(root_descriptor)
            if _stability_tuple(root_metadata) != _stability_tuple(after):
                raise StateCollectionError(CollectionFailure.RACE_DETECTED)
            var_lock_target = self._validate_var_lock(root_descriptor)
        finally:
            os.close(root_descriptor)

        policy_sha256 = _policy_sha256(DEFAULT_LIMITS)
        profile_sha256 = _profile_sha256(self.profile, policy_sha256)
        content: dict[str, object] = {
            "acl_policy": "reject_posix_acl",
            "acl_probe": "os.listxattr",
            "acl_probed_entry_count": self.acl_probed_entries,
            "collector_source_sha256": _collector_source_sha256(),
            "captured_xattr_policy": "reject_all",
            "controlled_records": self.controlled_records,
            "policy_sha256": policy_sha256,
            "profile": self.profile,
            "profile_set_sha256": PROFILE_SET_SHA256,
            "profile_sha256": profile_sha256,
            "pseudo_mount_roots": list(PSEUDO_MOUNT_ROOTS),
            "root_baseline_sha256": ROOT_BASELINE_SHA256,
            "runtime_injected_root_names": list(RUNTIME_INJECTED_ROOT_NAMES),
            "scanned_entry_count": self.scanned_entries,
            "scanned_tree_sha256": "sha256:" + self.tree_digest.hexdigest(),
            "schema": AUDIT_SCHEMA,
            "var_lock_resolved_target": VAR_LOCK_RESOLVED_TARGET,
            "var_lock_symlink_target": var_lock_target,
        }
        content["audit_sha256"] = _sha256_record(content)
        if len(_canonical_json(content)) > _MAX_AUDIT_BYTES:
            raise StateCollectionError(CollectionFailure.OUTPUT_LIMIT)
        return content

    def _audit_named(
        self,
        parent_descriptor: int,
        name: str,
        path_bytes: bytes,
        *,
        depth: int,
    ) -> None:
        if depth > _MAX_AUDIT_DEPTH:
            raise StateCollectionError(CollectionFailure.DEPTH_LIMIT)
        if len(path_bytes) > DEFAULT_LIMITS.max_path_bytes:
            raise StateCollectionError(CollectionFailure.PATH_LIMIT)
        try:
            metadata = os.lstat(name, dir_fd=parent_descriptor)
        except OSError as error:
            raise StateCollectionError(CollectionFailure.RACE_DETECTED) from error
        kind = _audit_kind(metadata.st_mode)
        if kind == "symlink":
            self._observe(path_bytes, metadata, kind)
            return
        if kind not in {"directory", "file"}:
            raise StateCollectionError(CollectionFailure.ACL_UNVERIFIED)
        if kind == "file":
            descriptor = _open_verified_file_at(parent_descriptor, name, metadata)
            try:
                self._reject_build_xattrs(descriptor, path_bytes)
            finally:
                os.close(descriptor)
            self._observe(path_bytes, metadata, kind)
            return
        if metadata.st_dev != self.root_device:
            raise StateCollectionError(CollectionFailure.WRITABLE_SURFACE)
        descriptor = _open_verified_directory_at(parent_descriptor, name, metadata)
        try:
            self._reject_build_xattrs(descriptor, path_bytes)
            self._observe(path_bytes, metadata, kind)
            for raw_name, child_name in self._directory_names(descriptor):
                self._audit_named(
                    descriptor,
                    child_name,
                    path_bytes + b"/" + raw_name,
                    depth=depth + 1,
                )
            after = os.fstat(descriptor)
            if _stability_tuple(metadata) != _stability_tuple(after):
                raise StateCollectionError(CollectionFailure.RACE_DETECTED)
        finally:
            os.close(descriptor)

    def _directory_names(self, descriptor: int) -> list[tuple[bytes, str]]:
        names: list[tuple[bytes, str]] = []
        try:
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    names.append((os.fsencode(entry.name), entry.name))
                    if self.scanned_entries + len(names) > _MAX_AUDIT_SCAN_ENTRIES:
                        raise StateCollectionError(CollectionFailure.ENTRY_LIMIT)
        except StateCollectionError:
            raise
        except OSError as error:
            raise StateCollectionError(CollectionFailure.IO_FAILURE) from error
        names.sort(key=lambda item: item[0])
        return names

    def _observe(
        self,
        path_bytes: bytes,
        metadata: os.stat_result,
        kind: str,
    ) -> None:
        self.scanned_entries += 1
        if self.scanned_entries > _MAX_AUDIT_SCAN_ENTRIES:
            raise StateCollectionError(CollectionFailure.ENTRY_LIMIT)
        try:
            path = path_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY) from error
        record = {
            "gid": metadata.st_gid,
            "mode": stat.S_IMODE(metadata.st_mode),
            "path": path,
            "type": kind,
            "uid": metadata.st_uid,
        }
        encoded = _canonical_json(record)
        self.tree_digest.update(len(encoded).to_bytes(4, "big"))
        self.tree_digest.update(encoded)
        if not _agent_controls(metadata, kind):
            return
        if not self._captured_by_runtime(path_bytes):
            raise StateCollectionError(CollectionFailure.WRITABLE_SURFACE)
        self.controlled_records.append(record)

    def _reject_build_xattrs(self, descriptor: int, path_bytes: bytes) -> None:
        try:
            names = _normalize_xattr_names(self.acl_probe(descriptor))  # type: ignore[operator]
        except StateCollectionError:
            raise
        except Exception as error:
            raise StateCollectionError(CollectionFailure.ACL_UNVERIFIED) from error
        self.acl_probed_entries += 1
        if names.intersection(_POSIX_ACL_NAMES):
            raise StateCollectionError(CollectionFailure.ACL_PRESENT)
        if names and self._captured_by_runtime(path_bytes):
            raise StateCollectionError(CollectionFailure.XATTR_PRESENT)

    def _captured_by_runtime(self, path_bytes: bytes) -> bool:
        if path_bytes == b".":
            return True
        top_level = path_bytes.split(b"/", 1)[0]
        immutable = {name.encode("ascii") for name in IMMUTABLE_ROOT_NAMES}
        if top_level not in immutable:
            return True
        roots = (
            *PROFILE_TASK_ROOTS[self.profile],
            *COMMON_WRITABLE_ROOTS,
        )
        return any(
            path_bytes == root.encode("ascii")
            or path_bytes.startswith(root.encode("ascii") + b"/")
            for root in roots
        )

    def _validate_var_lock(self, root_descriptor: int) -> str:
        try:
            var_metadata = os.lstat("var", dir_fd=root_descriptor)
            if not stat.S_ISDIR(var_metadata.st_mode):
                raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
            var_descriptor = _open_verified_directory_at(
                root_descriptor, "var", var_metadata
            )
        except StateCollectionError:
            raise
        except OSError as error:
            raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY) from error
        try:
            metadata = os.lstat("lock", dir_fd=var_descriptor)
            if not stat.S_ISLNK(metadata.st_mode):
                raise StateCollectionError(CollectionFailure.WRITABLE_SURFACE)
            target = os.readlink("lock", dir_fd=var_descriptor)
            after = os.lstat("lock", dir_fd=var_descriptor)
            if _stability_tuple(metadata) != _stability_tuple(after):
                raise StateCollectionError(CollectionFailure.RACE_DETECTED)
        except StateCollectionError:
            raise
        except OSError as error:
            raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY) from error
        finally:
            os.close(var_descriptor)
        target_bytes = os.fsencode(target)
        if target_bytes == b"/run/lock":
            resolved = b"run/lock"
        elif target_bytes == b"../run/lock":
            resolved = b"run/lock"
        else:
            raise StateCollectionError(CollectionFailure.WRITABLE_SURFACE)
        if resolved != VAR_LOCK_RESOLVED_TARGET.encode("ascii"):
            raise StateCollectionError(CollectionFailure.WRITABLE_SURFACE)
        try:
            return target_bytes.decode("ascii", errors="strict")
        except UnicodeDecodeError as error:
            raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY) from error


class _StateCollector:
    def __init__(
        self,
        profile: str,
        root_prefix: bytes,
        limits: CollectionLimits,
        acl_probe: object = None,
    ) -> None:
        self.profile = profile
        self.root_prefix = root_prefix
        self.limits = limits
        self.acl_probe = _default_acl_probe if acl_probe is None else acl_probe
        if not callable(self.acl_probe):
            raise TypeError("acl_probe must be callable")
        self.entries: list[_CollectedEntry] = []
        self.total_file_bytes = 0
        self.strict_failures: set[StrictSurfaceFailure] = set()
        self.audit_sha256 = ""

    def collect(self) -> dict[str, object]:
        try:
            root_descriptor = os.open(self.root_prefix, _OPEN_DIRECTORY_FLAGS)
        except OSError as error:
            raise StateCollectionError(CollectionFailure.IO_FAILURE) from error

        try:
            root_before = os.fstat(root_descriptor)
            self._reject_runtime_xattrs(root_descriptor)
            self.audit_sha256 = self._load_writable_surface_audit(root_descriptor)
            self._inspect_ephemeral_roots(root_descriptor)
            self._inspect_sysvipc_tables(root_descriptor)

            fixed_roots = (
                *PROFILE_TASK_ROOTS[self.profile],
                *COMMON_WRITABLE_ROOTS,
            )
            for relative in fixed_roots:
                self._collect_fixed_root(root_descriptor, relative)

            reserved_top_level = {
                os.fsencode(relative.split("/", 1)[0]) for relative in fixed_roots
            }
            immutable = {name.encode("ascii") for name in IMMUTABLE_ROOT_NAMES}
            for raw_name, name in self._dynamic_root_names(
                root_descriptor,
                reserved_top_level=reserved_top_level,
                immutable=immutable,
            ):
                self._walk_named(
                    root_descriptor,
                    name,
                    raw_name,
                    depth=1,
                    missing_is_state=False,
                )

            root_after = os.fstat(root_descriptor)
            if _stability_tuple(root_before) != _stability_tuple(root_after):
                raise StateCollectionError(CollectionFailure.RACE_DETECTED)
        except StateCollectionError:
            raise
        except OSError as error:
            raise StateCollectionError(CollectionFailure.IO_FAILURE) from error
        finally:
            os.close(root_descriptor)

        self._bind_hardlink_groups()
        self.entries.sort(key=lambda entry: entry.path_bytes)
        records = [entry.record for entry in self.entries]
        policy_sha256 = _policy_sha256(self.limits)
        profile_sha256 = _profile_sha256(self.profile, policy_sha256)
        state_sha256 = _sha256_record(
            {
                "entries": records,
                "profile_sha256": profile_sha256,
                "schema": SCHEMA,
                "writable_surface_audit_sha256": self.audit_sha256,
            }
        )
        failures = [
            failure.value
            for failure in (
                StrictSurfaceFailure.INVALID_UTF8_PATH,
                StrictSurfaceFailure.INVALID_UTF8_SYMLINK_TARGET,
            )
            if failure in self.strict_failures
        ]
        payload: dict[str, object] = {
            "common_roots": list(COMMON_WRITABLE_ROOTS),
            "dynamic_root_policy": "non_baseline_top_level",
            "entries": records,
            "entry_count": len(records),
            "policy_sha256": policy_sha256,
            "profile": self.profile,
            "profile_sha256": profile_sha256,
            "root_baseline_sha256": ROOT_BASELINE_SHA256,
            "schema": SCHEMA,
            "state_sha256": state_sha256,
            "strict_surface": {
                "failures": failures,
                "status": "unrepresentable" if failures else "representable",
            },
            "task_roots": list(PROFILE_TASK_ROOTS[self.profile]),
            "total_file_bytes": self.total_file_bytes,
            "writable_surface_audit_sha256": self.audit_sha256,
        }
        if len(_canonical_json(payload)) > self.limits.max_output_bytes:
            raise StateCollectionError(CollectionFailure.OUTPUT_LIMIT)
        return payload

    def _load_writable_surface_audit(self, root_descriptor: int) -> str:
        resolved = self._resolve_parent(root_descriptor, AUDIT_RELATIVE_PATH)
        if resolved is None:
            raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
        parent_descriptor, name = resolved
        try:
            try:
                metadata = os.lstat(name, dir_fd=parent_descriptor)
            except OSError as error:
                raise StateCollectionError(CollectionFailure.AUDIT_INVALID) from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_AUDIT_BYTES
                or _agent_controls(metadata, "file")
            ):
                raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
            try:
                descriptor = os.open(
                    name, _OPEN_FILE_FLAGS, dir_fd=parent_descriptor
                )
            except OSError as error:
                raise StateCollectionError(CollectionFailure.AUDIT_INVALID) from error
            try:
                opened = os.fstat(descriptor)
                if (
                    not _same_object(metadata, opened)
                    or _stability_tuple(metadata) != _stability_tuple(opened)
                ):
                    raise StateCollectionError(CollectionFailure.RACE_DETECTED)
                self._reject_runtime_xattrs(descriptor)
                chunks: list[bytes] = []
                observed = 0
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    observed += len(chunk)
                    if observed > _MAX_AUDIT_BYTES:
                        raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
                    chunks.append(chunk)
                after = os.fstat(descriptor)
                if (
                    observed != metadata.st_size
                    or _stability_tuple(opened) != _stability_tuple(after)
                ):
                    raise StateCollectionError(CollectionFailure.RACE_DETECTED)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_descriptor)

        raw = b"".join(chunks)
        try:
            decoded = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise StateCollectionError(CollectionFailure.AUDIT_INVALID) from error
        if not isinstance(decoded, dict) or _canonical_json(decoded) != raw:
            raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
        expected_keys = {
            "acl_policy",
            "acl_probe",
            "acl_probed_entry_count",
            "audit_sha256",
            "collector_source_sha256",
            "controlled_records",
            "captured_xattr_policy",
            "policy_sha256",
            "profile",
            "profile_set_sha256",
            "profile_sha256",
            "pseudo_mount_roots",
            "root_baseline_sha256",
            "runtime_injected_root_names",
            "scanned_entry_count",
            "scanned_tree_sha256",
            "schema",
            "var_lock_resolved_target",
            "var_lock_symlink_target",
        }
        if set(decoded) != expected_keys:
            raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
        claimed_sha256 = decoded.get("audit_sha256")
        content = dict(decoded)
        content.pop("audit_sha256", None)
        expected_values = {
            "acl_policy": "reject_posix_acl",
            "acl_probe": "os.listxattr",
            "captured_xattr_policy": "reject_all",
            "collector_source_sha256": _collector_source_sha256(),
            "policy_sha256": POLICY_SHA256,
            "profile": self.profile,
            "profile_set_sha256": PROFILE_SET_SHA256,
            "profile_sha256": PROFILE_SHA256[self.profile],
            "pseudo_mount_roots": list(PSEUDO_MOUNT_ROOTS),
            "root_baseline_sha256": ROOT_BASELINE_SHA256,
            "runtime_injected_root_names": list(RUNTIME_INJECTED_ROOT_NAMES),
            "schema": AUDIT_SCHEMA,
            "var_lock_resolved_target": VAR_LOCK_RESOLVED_TARGET,
        }
        if any(decoded.get(key) != value for key, value in expected_values.items()):
            raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
        if claimed_sha256 != _sha256_record(content):
            raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
        for count_name in ("acl_probed_entry_count", "scanned_entry_count"):
            count = decoded.get(count_name)
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
        if not isinstance(decoded.get("controlled_records"), list):
            raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
        for digest_name in ("scanned_tree_sha256", "audit_sha256"):
            digest = decoded.get(digest_name)
            if (
                not isinstance(digest, str)
                or len(digest) != 71
                or not digest.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in digest[7:])
            ):
                raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
        assert isinstance(claimed_sha256, str)
        return claimed_sha256

    def _inspect_ephemeral_roots(self, root_descriptor: int) -> None:
        for relative in EPHEMERAL_EMPTY_ROOTS:
            resolved = self._resolve_parent(root_descriptor, relative)
            if resolved is None:
                raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
            parent_descriptor, name = resolved
            try:
                try:
                    metadata = os.lstat(name, dir_fd=parent_descriptor)
                except FileNotFoundError as error:
                    raise StateCollectionError(
                        CollectionFailure.ROOT_BOUNDARY
                    ) from error
                if not stat.S_ISDIR(metadata.st_mode):
                    raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
                descriptor = self._open_verified_directory(
                    parent_descriptor, name, metadata
                )
                try:
                    opened = os.fstat(descriptor)
                    self._reject_runtime_xattrs(descriptor)
                    with os.scandir(descriptor) as iterator:
                        try:
                            next(iterator)
                        except StopIteration:
                            pass
                        else:
                            raise StateCollectionError(
                                CollectionFailure.EPHEMERAL_STATE
                            )
                    after = os.fstat(descriptor)
                    if _stability_tuple(opened) != _stability_tuple(after):
                        raise StateCollectionError(CollectionFailure.RACE_DETECTED)
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent_descriptor)

    def _inspect_sysvipc_tables(self, root_descriptor: int) -> None:
        for relative, expected_header in SYSVIPC_HEADERS.items():
            resolved = self._resolve_parent(root_descriptor, relative)
            if resolved is None:
                raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
            parent_descriptor, name = resolved
            try:
                try:
                    metadata = os.lstat(name, dir_fd=parent_descriptor)
                except FileNotFoundError as error:
                    raise StateCollectionError(
                        CollectionFailure.ROOT_BOUNDARY
                    ) from error
                if not stat.S_ISREG(metadata.st_mode):
                    raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
                try:
                    descriptor = os.open(
                        name, _OPEN_FILE_FLAGS, dir_fd=parent_descriptor
                    )
                except OSError as error:
                    if error.errno in {errno.ELOOP, errno.ENOENT}:
                        raise StateCollectionError(
                            CollectionFailure.RACE_DETECTED
                        ) from error
                    raise StateCollectionError(CollectionFailure.IO_FAILURE) from error
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not _same_object(metadata, opened)
                        or not stat.S_ISREG(opened.st_mode)
                        or _stability_tuple(metadata) != _stability_tuple(opened)
                    ):
                        raise StateCollectionError(CollectionFailure.RACE_DETECTED)
                    chunks: list[bytes] = []
                    observed = 0
                    while True:
                        chunk = os.read(descriptor, 4096)
                        if not chunk:
                            break
                        observed += len(chunk)
                        if observed > _MAX_EPHEMERAL_TABLE_BYTES:
                            raise StateCollectionError(
                                CollectionFailure.EPHEMERAL_STATE
                            )
                        chunks.append(chunk)
                    after = os.fstat(descriptor)
                    if _stability_tuple(opened) != _stability_tuple(after):
                        raise StateCollectionError(
                            CollectionFailure.RACE_DETECTED
                        )
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent_descriptor)

            try:
                lines = b"".join(chunks).decode("ascii", errors="strict").splitlines()
            except UnicodeDecodeError as error:
                raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY) from error
            if not lines or tuple(lines[0].split()) != expected_header:
                raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
            if any(line.strip() for line in lines[1:]):
                raise StateCollectionError(CollectionFailure.EPHEMERAL_STATE)

    def _collect_fixed_root(self, root_descriptor: int, relative: str) -> None:
        relative_bytes = relative.encode("ascii")
        self._validate_path(relative_bytes)
        resolved = self._resolve_parent(root_descriptor, relative)
        if resolved is None:
            self._append_missing(relative_bytes)
            return
        parent_descriptor, name = resolved
        try:
            self._walk_named(
                parent_descriptor,
                name,
                relative_bytes,
                depth=len(relative_bytes.split(b"/")),
                missing_is_state=True,
            )
        finally:
            os.close(parent_descriptor)

    def _resolve_parent(
        self, root_descriptor: int, relative: str
    ) -> tuple[int, str] | None:
        components = relative.split("/")
        descriptor = os.dup(root_descriptor)
        try:
            for component in components[:-1]:
                try:
                    metadata = os.lstat(component, dir_fd=descriptor)
                except FileNotFoundError:
                    os.close(descriptor)
                    return None
                if not stat.S_ISDIR(metadata.st_mode):
                    raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
                child = self._open_verified_directory(
                    descriptor, component, metadata
                )
                os.close(descriptor)
                descriptor = child
            return descriptor, components[-1]
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    def _dynamic_root_names(
        self,
        root_descriptor: int,
        *,
        reserved_top_level: set[bytes],
        immutable: set[bytes],
    ) -> list[tuple[bytes, str]]:
        selected: list[tuple[bytes, str]] = []
        seen_immutable: set[bytes] = set()
        with os.scandir(root_descriptor) as iterator:
            for directory_entry in iterator:
                raw_name = os.fsencode(directory_entry.name)
                if raw_name in immutable:
                    seen_immutable.add(raw_name)
                    continue
                if raw_name in reserved_top_level:
                    continue
                selected.append((raw_name, directory_entry.name))
                if len(selected) + len(self.entries) > self.limits.max_entries:
                    raise StateCollectionError(CollectionFailure.ENTRY_LIMIT)
        if seen_immutable != immutable:
            raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
        selected.sort(key=lambda item: item[0])
        return selected

    def _walk_named(
        self,
        parent_descriptor: int,
        name: str,
        path_bytes: bytes,
        *,
        depth: int,
        missing_is_state: bool,
    ) -> None:
        self._validate_path(path_bytes)
        if depth > self.limits.max_depth:
            raise StateCollectionError(CollectionFailure.DEPTH_LIMIT)
        try:
            metadata = os.lstat(name, dir_fd=parent_descriptor)
        except FileNotFoundError as error:
            if missing_is_state:
                self._append_missing(path_bytes)
                return
            raise StateCollectionError(CollectionFailure.RACE_DETECTED) from error

        kind = _classify_mode(metadata.st_mode)
        if kind == "file":
            self._collect_file(parent_descriptor, name, path_bytes, metadata)
        elif kind == "symlink":
            self._collect_symlink(parent_descriptor, name, path_bytes, metadata)
        else:
            self._collect_directory(
                parent_descriptor,
                name,
                path_bytes,
                metadata,
                depth=depth,
            )

    def _collect_file(
        self,
        parent_descriptor: int,
        name: str,
        path_bytes: bytes,
        metadata: os.stat_result,
    ) -> None:
        if metadata.st_size < 0 or metadata.st_size > self.limits.max_file_bytes:
            raise StateCollectionError(CollectionFailure.FILE_BYTES_LIMIT)
        if (
            self.total_file_bytes + metadata.st_size
            > self.limits.max_total_file_bytes
        ):
            raise StateCollectionError(CollectionFailure.TOTAL_BYTES_LIMIT)
        try:
            descriptor = os.open(name, _OPEN_FILE_FLAGS, dir_fd=parent_descriptor)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOENT}:
                raise StateCollectionError(CollectionFailure.RACE_DETECTED) from error
            raise StateCollectionError(CollectionFailure.IO_FAILURE) from error
        try:
            opened = os.fstat(descriptor)
            if (
                not _same_object(metadata, opened)
                or not stat.S_ISREG(opened.st_mode)
                or _stability_tuple(metadata) != _stability_tuple(opened)
            ):
                raise StateCollectionError(CollectionFailure.RACE_DETECTED)
            self._reject_runtime_xattrs(descriptor)
            digest = hashlib.sha256()
            observed = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > self.limits.max_file_bytes:
                    raise StateCollectionError(CollectionFailure.FILE_BYTES_LIMIT)
                if (
                    self.total_file_bytes + observed
                    > self.limits.max_total_file_bytes
                ):
                    raise StateCollectionError(CollectionFailure.TOTAL_BYTES_LIMIT)
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (
                observed != metadata.st_size
                or _stability_tuple(opened) != _stability_tuple(after)
            ):
                raise StateCollectionError(CollectionFailure.RACE_DETECTED)
        finally:
            os.close(descriptor)

        self.total_file_bytes += observed
        entry = _CollectedEntry(
            path_bytes=path_bytes,
            record=self._base_record(
                path_bytes,
                kind="file",
                metadata=metadata,
                content_sha256="sha256:" + digest.hexdigest(),
                size_bytes=observed,
            ),
            hardlink_identity=(metadata.st_dev, metadata.st_ino),
            link_count=metadata.st_nlink,
        )
        self._append_entry(entry)

    def _collect_symlink(
        self,
        parent_descriptor: int,
        name: str,
        path_bytes: bytes,
        metadata: os.stat_result,
    ) -> None:
        try:
            target = os.readlink(name, dir_fd=parent_descriptor)
            after = os.lstat(name, dir_fd=parent_descriptor)
        except OSError as error:
            raise StateCollectionError(CollectionFailure.RACE_DETECTED) from error
        if _stability_tuple(metadata) != _stability_tuple(after):
            raise StateCollectionError(CollectionFailure.RACE_DETECTED)
        target_bytes = os.fsencode(target)
        if len(target_bytes) > self.limits.max_symlink_target_bytes:
            raise StateCollectionError(CollectionFailure.SYMLINK_TARGET_LIMIT)
        target_text = self._decode_text(
            target_bytes, StrictSurfaceFailure.INVALID_UTF8_SYMLINK_TARGET
        )
        record = self._base_record(
            path_bytes,
            kind="symlink",
            metadata=metadata,
            symlink_target=target_text,
            symlink_target_bytes_b64=_base64(target_bytes),
        )
        self._append_entry(
            _CollectedEntry(
                path_bytes=path_bytes,
                record=record,
                hardlink_identity=(metadata.st_dev, metadata.st_ino),
                link_count=metadata.st_nlink,
            )
        )

    def _collect_directory(
        self,
        parent_descriptor: int,
        name: str,
        path_bytes: bytes,
        metadata: os.stat_result,
        *,
        depth: int,
    ) -> None:
        self._append_entry(
            _CollectedEntry(
                path_bytes=path_bytes,
                record=self._base_record(
                    path_bytes,
                    kind="directory",
                    metadata=metadata,
                ),
            )
        )
        descriptor = self._open_verified_directory(parent_descriptor, name, metadata)
        try:
            self._reject_runtime_xattrs(descriptor)
            children: list[tuple[bytes, str]] = []
            with os.scandir(descriptor) as iterator:
                for directory_entry in iterator:
                    raw_name = os.fsencode(directory_entry.name)
                    children.append((raw_name, directory_entry.name))
                    if len(children) + len(self.entries) > self.limits.max_entries:
                        raise StateCollectionError(CollectionFailure.ENTRY_LIMIT)
            children.sort(key=lambda item: item[0])
            for raw_name, child_name in children:
                child_path = path_bytes + b"/" + raw_name
                self._walk_named(
                    descriptor,
                    child_name,
                    child_path,
                    depth=depth + 1,
                    missing_is_state=False,
                )
            after = os.fstat(descriptor)
            if _stability_tuple(metadata) != _stability_tuple(after):
                raise StateCollectionError(CollectionFailure.RACE_DETECTED)
        finally:
            os.close(descriptor)

    def _open_verified_directory(
        self,
        parent_descriptor: int,
        name: str,
        metadata: os.stat_result,
    ) -> int:
        try:
            descriptor = os.open(
                name,
                _OPEN_DIRECTORY_FLAGS,
                dir_fd=parent_descriptor,
            )
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
                raise StateCollectionError(CollectionFailure.RACE_DETECTED) from error
            raise StateCollectionError(CollectionFailure.IO_FAILURE) from error
        try:
            opened = os.fstat(descriptor)
            if (
                not _same_object(metadata, opened)
                or not stat.S_ISDIR(opened.st_mode)
                or _stability_tuple(metadata) != _stability_tuple(opened)
            ):
                raise StateCollectionError(CollectionFailure.RACE_DETECTED)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _append_missing(self, path_bytes: bytes) -> None:
        self._append_entry(
            _CollectedEntry(
                path_bytes=path_bytes,
                record=self._base_record(path_bytes, kind="missing", metadata=None),
            )
        )

    def _append_entry(self, entry: _CollectedEntry) -> None:
        if len(self.entries) >= self.limits.max_entries:
            raise StateCollectionError(CollectionFailure.ENTRY_LIMIT)
        self.entries.append(entry)

    def _base_record(
        self,
        path_bytes: bytes,
        *,
        kind: str,
        metadata: os.stat_result | None,
        content_sha256: str | None = None,
        size_bytes: int | None = None,
        symlink_target: str | None = None,
        symlink_target_bytes_b64: str | None = None,
    ) -> dict[str, object]:
        path_text = self._decode_text(
            path_bytes, StrictSurfaceFailure.INVALID_UTF8_PATH
        )
        return {
            "content_sha256": content_sha256,
            "gid": metadata.st_gid if metadata is not None else None,
            "hardlink_group_sha256": None,
            "mode": stat.S_IMODE(metadata.st_mode) if metadata is not None else None,
            "path": path_text,
            "path_bytes_b64": _base64(path_bytes),
            "size_bytes": size_bytes,
            "symlink_target": symlink_target,
            "symlink_target_bytes_b64": symlink_target_bytes_b64,
            "type": kind,
            "uid": metadata.st_uid if metadata is not None else None,
        }

    def _decode_text(
        self, value: bytes, failure: StrictSurfaceFailure
    ) -> str | None:
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            self.strict_failures.add(failure)
            return None

    def _reject_runtime_xattrs(self, descriptor: int) -> None:
        try:
            names = _normalize_xattr_names(self.acl_probe(descriptor))  # type: ignore[operator]
        except StateCollectionError:
            raise
        except Exception as error:
            raise StateCollectionError(CollectionFailure.ACL_UNVERIFIED) from error
        if names:
            raise StateCollectionError(CollectionFailure.XATTR_PRESENT)

    def _validate_path(self, path_bytes: bytes) -> None:
        if (
            not path_bytes
            or path_bytes.startswith(b"/")
            or path_bytes.endswith(b"/")
            or b"//" in path_bytes
            or any(
                component in {b"", b".", b".."}
                for component in path_bytes.split(b"/")
            )
        ):
            raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
        if len(path_bytes) > self.limits.max_path_bytes:
            raise StateCollectionError(CollectionFailure.PATH_LIMIT)

    def _bind_hardlink_groups(self) -> None:
        groups: dict[tuple[int, int], list[_CollectedEntry]] = {}
        for entry in self.entries:
            if entry.hardlink_identity is not None:
                groups.setdefault(entry.hardlink_identity, []).append(entry)
        for members in groups.values():
            expected = members[0].link_count
            if expected < 1 or any(member.link_count != expected for member in members):
                raise StateCollectionError(CollectionFailure.RACE_DETECTED)
            if expected != len(members):
                raise StateCollectionError(CollectionFailure.INCOMPLETE_HARDLINK)
            if expected == 1:
                continue
            sorted_paths = sorted(member.path_bytes for member in members)
            digest = hashlib.sha256(b"edgeloopbench-hardlink-group-v1\0")
            for path in sorted_paths:
                digest.update(len(path).to_bytes(4, "big"))
                digest.update(path)
            group_sha256 = "sha256:" + digest.hexdigest()
            for member in members:
                member.record["hardlink_group_sha256"] = group_sha256


def _audit_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "character_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "unknown"


def _validate_audit_root(metadata: os.stat_result) -> None:
    if (
        metadata.st_uid != ROOT_OWNER_UID
        or stat.S_IMODE(metadata.st_mode) != ROOT_MODE
    ):
        raise StateCollectionError(CollectionFailure.WRITABLE_SURFACE)


def _agent_controls(metadata: os.stat_result, kind: str) -> bool:
    # Symlink permission bits are not access controls; replacement is governed
    # by the containing directory and sticky-owner rules.  Ownership therefore
    # still matters even though the symlink's displayed 0777 mode does not.
    if kind == "symlink":
        return metadata.st_uid == AGENT_UID
    if metadata.st_uid == AGENT_UID:
        return True
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_gid == AGENT_GID:
        return bool(mode & stat.S_IWGRP)
    return bool(mode & stat.S_IWOTH)


def _default_acl_probe(path: bytes | int) -> object:
    probe = getattr(os, "listxattr", None)
    if probe is None:
        raise StateCollectionError(CollectionFailure.ACL_UNVERIFIED)
    try:
        if isinstance(path, int):
            return probe(path)
        return probe(path, follow_symlinks=False)
    except (NotImplementedError, TypeError) as error:
        raise StateCollectionError(CollectionFailure.ACL_UNVERIFIED) from error
    except OSError as error:
        raise StateCollectionError(CollectionFailure.ACL_UNVERIFIED) from error


def _normalize_xattr_names(names: object) -> set[str]:
    if not isinstance(names, (list, tuple)):
        raise StateCollectionError(CollectionFailure.ACL_UNVERIFIED)
    normalized: set[str] = set()
    for name in names:
        if isinstance(name, bytes):
            try:
                normalized.add(name.decode("ascii", errors="strict"))
            except UnicodeDecodeError as error:
                raise StateCollectionError(
                    CollectionFailure.ACL_UNVERIFIED
                ) from error
        elif isinstance(name, str):
            normalized.add(name)
        else:
            raise StateCollectionError(CollectionFailure.ACL_UNVERIFIED)
    return normalized


def _open_verified_directory_at(
    parent_descriptor: int,
    name: str,
    metadata: os.stat_result,
) -> int:
    try:
        descriptor = os.open(name, _OPEN_DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise StateCollectionError(CollectionFailure.RACE_DETECTED) from error
        raise StateCollectionError(CollectionFailure.IO_FAILURE) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not _same_object(metadata, opened)
            or not stat.S_ISDIR(opened.st_mode)
            or _stability_tuple(metadata) != _stability_tuple(opened)
        ):
            raise StateCollectionError(CollectionFailure.RACE_DETECTED)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_verified_file_at(
    parent_descriptor: int,
    name: str,
    metadata: os.stat_result,
) -> int:
    try:
        descriptor = os.open(name, _OPEN_FILE_FLAGS, dir_fd=parent_descriptor)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise StateCollectionError(CollectionFailure.RACE_DETECTED) from error
        raise StateCollectionError(CollectionFailure.IO_FAILURE) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not _same_object(metadata, opened)
            or not stat.S_ISREG(opened.st_mode)
            or _stability_tuple(metadata) != _stability_tuple(opened)
        ):
            raise StateCollectionError(CollectionFailure.RACE_DETECTED)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _collector_source_sha256() -> str:
    path = os.fsencode(__file__)
    try:
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SOURCE_BYTES:
            raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
        descriptor = os.open(path, _OPEN_FILE_FLAGS)
    except StateCollectionError:
        raise
    except OSError as error:
        raise StateCollectionError(CollectionFailure.AUDIT_INVALID) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not _same_object(metadata, opened)
            or _stability_tuple(metadata) != _stability_tuple(opened)
        ):
            raise StateCollectionError(CollectionFailure.RACE_DETECTED)
        digest = hashlib.sha256()
        observed = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if observed > _MAX_SOURCE_BYTES:
                raise StateCollectionError(CollectionFailure.AUDIT_INVALID)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            observed != metadata.st_size
            or _stability_tuple(opened) != _stability_tuple(after)
        ):
            raise StateCollectionError(CollectionFailure.RACE_DETECTED)
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


def build_writable_surface_audit_bytes(
    profile: str,
    *,
    _root_prefix: str | bytes | os.PathLike[str] | os.PathLike[bytes] = b"/",
    _acl_probe: object | None = None,
) -> bytes:
    if profile not in PROFILE_TASK_ROOTS:
        raise StateCollectionError(CollectionFailure.INVALID_INVOCATION)
    root_prefix = os.fsencode(os.fspath(_root_prefix))
    if not root_prefix or b"\0" in root_prefix or not os.path.isabs(root_prefix):
        raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
    probe = _default_acl_probe if _acl_probe is None else _acl_probe
    if not callable(probe):
        raise TypeError("_acl_probe must be callable")
    payload = _WritableSurfaceAuditor(profile, root_prefix, probe).audit()
    encoded = _canonical_json(payload)
    if len(encoded) > _MAX_AUDIT_BYTES:
        raise StateCollectionError(CollectionFailure.OUTPUT_LIMIT)
    return encoded


def write_build_writable_surface_audit(
    profile: str,
    *,
    _root_prefix: str | bytes | os.PathLike[str] | os.PathLike[bytes] = b"/",
    _acl_probe: object | None = None,
) -> bytes:
    root_prefix = os.fsencode(os.fspath(_root_prefix))
    payload = build_writable_surface_audit_bytes(
        profile,
        _root_prefix=root_prefix,
        _acl_probe=_acl_probe,
    )
    descriptors: list[int] = []
    try:
        root_descriptor = os.open(root_prefix, _OPEN_DIRECTORY_FLAGS)
        descriptors.append(root_descriptor)
        opt_metadata = os.lstat("opt", dir_fd=root_descriptor)
        opt_descriptor = _open_verified_directory_at(
            root_descriptor, "opt", opt_metadata
        )
        descriptors.append(opt_descriptor)
        parent_metadata = os.lstat("edgeloop", dir_fd=opt_descriptor)
        parent_descriptor = _open_verified_directory_at(
            opt_descriptor, "edgeloop", parent_metadata
        )
        descriptors.append(parent_descriptor)
        descriptor = os.open(
            "writable_surface_audit.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o444,
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        for opened_descriptor in reversed(descriptors):
            os.close(opened_descriptor)
        raise StateCollectionError(CollectionFailure.AUDIT_INVALID) from error
    except BaseException:
        for opened_descriptor in reversed(descriptors):
            os.close(opened_descriptor)
        raise
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise StateCollectionError(CollectionFailure.IO_FAILURE)
            view = view[written:]
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        for opened_descriptor in reversed(descriptors):
            os.close(opened_descriptor)
    return payload


def _classify_mode(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    raise StateCollectionError(CollectionFailure.SPECIAL_FILE)


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        stat.S_IFMT(first.st_mode),
    ) == (
        second.st_dev,
        second.st_ino,
        stat.S_IFMT(second.st_mode),
    )


def _stability_tuple(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def collect_canonical_bytes(
    profile: str,
    *,
    _root_prefix: str | bytes | os.PathLike[str] | os.PathLike[bytes] = b"/",
    _limits: CollectionLimits | None = None,
    _acl_probe: object | None = None,
) -> bytes:
    """Collect one profile and return bounded canonical JSON bytes.

    The private override parameters exist for adversarial unit tests.  The CLI
    exposes neither: measured invocation always collects the actual root with
    the frozen default limits.
    """

    if profile not in PROFILE_TASK_ROOTS:
        raise StateCollectionError(CollectionFailure.INVALID_INVOCATION)
    limits = DEFAULT_LIMITS if _limits is None else _limits
    if not isinstance(limits, CollectionLimits):
        raise TypeError("_limits must be CollectionLimits")
    root_prefix = os.fsencode(os.fspath(_root_prefix))
    if not root_prefix or b"\0" in root_prefix or not os.path.isabs(root_prefix):
        raise StateCollectionError(CollectionFailure.ROOT_BOUNDARY)
    collector = _StateCollector(
        profile,
        root_prefix,
        limits,
        _default_acl_probe if _acl_probe is None else _acl_probe,
    )
    payload = collector.collect()
    encoded = _canonical_json(payload)
    if len(encoded) > limits.max_output_bytes:
        raise StateCollectionError(CollectionFailure.OUTPUT_LIMIT)
    return encoded


def _error_bytes(kind: CollectionFailure) -> bytes:
    return _canonical_json({"error": {"kind": kind.value}, "schema": ERROR_SCHEMA})


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if (
        len(arguments) == 2
        and arguments[0] == "--build-audit"
        and arguments[1] in PROFILE_TASK_ROOTS
    ):
        try:
            audit = write_build_writable_surface_audit(arguments[1])
            decoded = json.loads(audit)
            summary = _canonical_json(
                {
                    "audit_sha256": decoded["audit_sha256"],
                    "schema": AUDIT_SCHEMA,
                }
            )
        except StateCollectionError as error:
            sys.stdout.buffer.write(_error_bytes(error.kind) + b"\n")
            return 74
        except Exception:
            sys.stdout.buffer.write(
                _error_bytes(CollectionFailure.INTERNAL_ERROR) + b"\n"
            )
            return 70
        sys.stdout.buffer.write(summary + b"\n")
        return 0
    if (
        len(arguments) != 2
        or arguments[0] != "--profile"
        or arguments[1] not in PROFILE_TASK_ROOTS
    ):
        sys.stdout.buffer.write(
            _error_bytes(CollectionFailure.INVALID_INVOCATION) + b"\n"
        )
        return 64
    try:
        payload = collect_canonical_bytes(arguments[1])
    except StateCollectionError as error:
        sys.stdout.buffer.write(_error_bytes(error.kind) + b"\n")
        return 74
    except Exception:
        sys.stdout.buffer.write(_error_bytes(CollectionFailure.INTERNAL_ERROR) + b"\n")
        return 70
    sys.stdout.buffer.write(payload + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
