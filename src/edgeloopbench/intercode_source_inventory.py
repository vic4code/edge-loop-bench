"""Trusted, path-free source inventory for v0.7 production authorization.

The public evidence contains no checkout or tracked-file names.  A trusted
builder nevertheless verifies every tracked byte against the clean committed
Git ``HEAD`` and retains only domain-separated path keys for later component
hash derivation.  Revalidation repeats the entire proof before a new intent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import InitVar, dataclass
from pathlib import Path


SOURCE_INVENTORY_SCHEMA_REVISION = "edgeloopbench.source-inventory.v1"
SOURCE_SUBSET_SCHEMA_REVISION = "edgeloopbench.source-subset.v1"

_INVENTORY_SEAL = object()
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_ALLOWED_OBJECT_FORMATS = {"sha1": 40, "sha256": 64}
_REGULAR_GIT_MODES = {"100644", "100755"}
_PATH_KEY_DOMAIN = b"edgeloopbench.v0.7.source-path-key.v1\0"
_MAX_GIT_OUTPUT_BYTES = 32 << 20
_MAX_TRACKED_FILES = 100_000
_MAX_TRACKED_FILE_BYTES = 1 << 30
_MAX_TRACKED_TOTAL_BYTES = 8 << 30
_GIT_TIMEOUT_SECONDS = 30.0


class SourceInventoryError(ValueError):
    """The checkout cannot authorize exact committed source bytes."""


@dataclass(frozen=True, slots=True, repr=False)
class _SourceFileIdentity:
    path_key_sha256: str
    git_mode: str
    size_bytes: int
    content_sha256: str

    def canonical_record(self) -> dict[str, object]:
        return {
            "content_sha256": self.content_sha256,
            "git_mode": self.git_mode,
            "path_key_sha256": self.path_key_sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedSourceInventory:
    """Builder-sealed proof of one clean committed, regular-file checkout."""

    git_object_format: str
    head_commit: str
    head_tree: str
    tracked_file_count: int
    tracked_byte_count: int
    inventory_sha256: str
    _files: tuple[_SourceFileIdentity, ...] = ()
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _INVENTORY_SEAL:
            raise SourceInventoryError("source inventories are builder-sealed")
        _validate_inventory(self)

    @property
    def source_inventory_root_sha256(self) -> str:
        return self.inventory_sha256

    def canonical_record(self) -> dict[str, object]:
        _validate_inventory(self)
        return {
            "git_object_format": self.git_object_format,
            "head_commit": self.head_commit,
            "head_tree": self.head_tree,
            "inventory_sha256": self.inventory_sha256,
            "schema": SOURCE_INVENTORY_SCHEMA_REVISION,
            "tracked_byte_count": self.tracked_byte_count,
            "tracked_file_count": self.tracked_file_count,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_record()) + b"\n"

    def __repr__(self) -> str:
        return (
            "<VerifiedSourceInventory "
            f"head={self.head_commit} files={self.tracked_file_count} "
            f"root={self.inventory_sha256}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _GitEntry:
    name: bytes
    mode: str
    oid: str


@dataclass(frozen=True, slots=True, repr=False)
class _RepositorySnapshot:
    object_format: str
    head_commit: str
    head_tree: str
    entries: tuple[_GitEntry, ...]


def build_verified_source_inventory(repository_root: Path) -> VerifiedSourceInventory:
    """Verify and seal every tracked byte of a clean committed Git checkout."""

    root = _require_repository_root(repository_root)
    try:
        first = _snapshot_repository(root)
    except SourceInventoryError:
        raise
    except Exception as error:  # pragma: no cover - fail-closed boundary
        raise SourceInventoryError("source inventory could not inspect Git repository") from error

    root_descriptor = _open_repository_root(root)
    files: list[_SourceFileIdentity] = []
    total_bytes = 0
    try:
        for entry in first.entries:
            identity = _read_tracked_identity(
                root_descriptor,
                entry,
                object_format=first.object_format,
            )
            files.append(identity)
            total_bytes += identity.size_bytes
            if total_bytes > _MAX_TRACKED_TOTAL_BYTES:
                raise SourceInventoryError("tracked source byte inventory exceeds safety bound")
    finally:
        os.close(root_descriptor)

    second = _snapshot_repository(root)
    if second != first:
        raise SourceInventoryError("source inventory changed while it was being verified")

    frozen_files = tuple(files)
    core = _inventory_core(
        object_format=first.object_format,
        head_commit=first.head_commit,
        head_tree=first.head_tree,
        files=frozen_files,
    )
    return VerifiedSourceInventory(
        git_object_format=first.object_format,
        head_commit=first.head_commit,
        head_tree=first.head_tree,
        tracked_file_count=len(frozen_files),
        tracked_byte_count=total_bytes,
        inventory_sha256=_digest_record(core),
        _files=frozen_files,
        _construction_seal=_INVENTORY_SEAL,
    )


def revalidate_source_inventory(
    inventory: VerifiedSourceInventory,
    repository_root: Path,
) -> VerifiedSourceInventory:
    """Repeat the clean-byte proof and require exact original provenance."""

    if type(inventory) is not VerifiedSourceInventory:
        raise SourceInventoryError("source inventory revalidation requires verified evidence")
    _validate_inventory(inventory)
    try:
        observed = build_verified_source_inventory(repository_root)
    except (OSError, SourceInventoryError):
        raise SourceInventoryError("source inventory revalidation failed") from None
    if observed != inventory:
        raise SourceInventoryError("source inventory revalidation differs from authorization")
    return inventory


def derive_source_subset_sha256(
    inventory: VerifiedSourceInventory,
    tracked_files: Sequence[str],
) -> str:
    """Derive a path-free ordered component root from verified tracked files."""

    if type(inventory) is not VerifiedSourceInventory:
        raise SourceInventoryError("source subset requires verified inventory")
    _validate_inventory(inventory)
    if isinstance(tracked_files, (str, bytes)) or not isinstance(tracked_files, Sequence):
        raise SourceInventoryError("source subset must be an ordered file sequence")
    names = tuple(tracked_files)
    if not names:
        raise SourceInventoryError("source subset must contain at least one tracked file")
    keys = tuple(_path_key(_validated_relative_name(name)) for name in names)
    if len(set(keys)) != len(keys):
        raise SourceInventoryError("source subset contains a duplicate tracked file")
    identities = {item.path_key_sha256: item for item in inventory._files}
    try:
        selected = tuple(identities[key] for key in keys)
    except KeyError:
        raise SourceInventoryError("source subset names an unverified tracked file") from None
    return _digest_record(
        {
            "files": [item.canonical_record() for item in selected],
            "inventory_sha256": inventory.inventory_sha256,
            "schema": SOURCE_SUBSET_SCHEMA_REVISION,
        }
    )


def _snapshot_repository(root: Path) -> _RepositorySnapshot:
    try:
        top_level = _run_git(root, "rev-parse", "--show-toplevel").rstrip(b"\n")
    except SourceInventoryError:
        raise SourceInventoryError("repository root is not a Git repository") from None
    try:
        decoded_top_level = Path(top_level.decode("utf-8", "strict"))
        if not os.path.samefile(root, decoded_top_level):
            raise SourceInventoryError("repository root must be the Git worktree root")
    except (OSError, UnicodeError):
        raise SourceInventoryError("repository root is not a canonical Git worktree") from None

    object_format = _single_line(
        _run_git(root, "rev-parse", "--show-object-format"),
        "Git object format",
    )
    expected_oid_length = _ALLOWED_OBJECT_FORMATS.get(object_format)
    if expected_oid_length is None:
        raise SourceInventoryError("Git object format is not supported")
    head_commit = _single_line(
        _run_git(root, "rev-parse", "--verify", "HEAD^{commit}"),
        "Git HEAD commit",
    )
    head_tree = _single_line(
        _run_git(root, "rev-parse", "--verify", "HEAD^{tree}"),
        "Git HEAD tree",
    )
    _require_git_oid(head_commit, expected_oid_length, "Git HEAD commit")
    _require_git_oid(head_tree, expected_oid_length, "Git HEAD tree")

    head_entries = _parse_tree_entries(
        _run_git(root, "ls-tree", "-r", "-z", "--full-tree", "HEAD"),
        expected_oid_length,
    )
    index_entries = _parse_index_entries(
        _run_git(root, "ls-files", "--stage", "-z"),
        expected_oid_length,
    )
    if head_entries != index_entries:
        raise SourceInventoryError("source checkout must have a clean committed HEAD")
    if _run_git(root, "ls-files", "--others", "--exclude-standard", "-z"):
        raise SourceInventoryError("source checkout must have a clean committed HEAD")
    if len(head_entries) > _MAX_TRACKED_FILES:
        raise SourceInventoryError("tracked source inventory exceeds file-count safety bound")
    for entry in head_entries:
        if entry.mode not in _REGULAR_GIT_MODES:
            raise SourceInventoryError("tracked entries must be regular non-symlink files")
    return _RepositorySnapshot(
        object_format=object_format,
        head_commit=head_commit,
        head_tree=head_tree,
        entries=head_entries,
    )


def _parse_tree_entries(payload: bytes, oid_length: int) -> tuple[_GitEntry, ...]:
    entries: list[_GitEntry] = []
    for record in _nul_records(payload, "Git HEAD tree"):
        try:
            header, name = record.split(b"\t", 1)
            mode_bytes, object_type, oid_bytes = header.split(b" ", 2)
            mode = mode_bytes.decode("ascii")
            oid = oid_bytes.decode("ascii")
        except (UnicodeError, ValueError):
            raise SourceInventoryError("Git HEAD tree is malformed") from None
        if object_type != b"blob":
            raise SourceInventoryError("tracked entries must be regular non-symlink files")
        _validate_git_name(name)
        _require_git_oid(oid, oid_length, "tracked Git object")
        entries.append(_GitEntry(name, mode, oid))
    return _ordered_unique_entries(entries, "Git HEAD tree")


def _parse_index_entries(payload: bytes, oid_length: int) -> tuple[_GitEntry, ...]:
    entries: list[_GitEntry] = []
    for record in _nul_records(payload, "Git index"):
        try:
            header, name = record.split(b"\t", 1)
            mode_bytes, oid_bytes, stage = header.split(b" ", 2)
            mode = mode_bytes.decode("ascii")
            oid = oid_bytes.decode("ascii")
        except (UnicodeError, ValueError):
            raise SourceInventoryError("Git index is malformed") from None
        if stage != b"0":
            raise SourceInventoryError("source checkout must have a clean committed HEAD")
        _validate_git_name(name)
        _require_git_oid(oid, oid_length, "indexed Git object")
        entries.append(_GitEntry(name, mode, oid))
    return _ordered_unique_entries(entries, "Git index")


def _ordered_unique_entries(
    entries: list[_GitEntry], label: str
) -> tuple[_GitEntry, ...]:
    ordered = tuple(sorted(entries, key=lambda item: item.name))
    if len({item.name for item in ordered}) != len(ordered):
        raise SourceInventoryError(f"{label} repeats a tracked entry")
    return ordered


def _nul_records(payload: bytes, label: str) -> tuple[bytes, ...]:
    if not payload:
        return ()
    if not payload.endswith(b"\0"):
        raise SourceInventoryError(f"{label} lacks NUL framing")
    return tuple(payload[:-1].split(b"\0"))


def _read_tracked_identity(
    root_descriptor: int,
    entry: _GitEntry,
    *,
    object_format: str,
) -> _SourceFileIdentity:
    descriptor = _open_beneath(root_descriptor, entry.name)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceInventoryError("tracked entries must be regular non-symlink files")
        if before.st_size < 0 or before.st_size > _MAX_TRACKED_FILE_BYTES:
            raise SourceInventoryError("tracked source file exceeds safety bound")
        expected_executable = entry.mode == "100755"
        observed_executable = bool(before.st_mode & 0o111)
        if observed_executable != expected_executable:
            raise SourceInventoryError("tracked file mode differs from committed HEAD")

        content_hasher = hashlib.sha256()
        git_hasher = hashlib.new(object_format)
        git_hasher.update(f"blob {before.st_size}\0".encode("ascii"))
        observed_bytes = 0
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            observed_bytes += len(chunk)
            if observed_bytes > _MAX_TRACKED_FILE_BYTES:
                raise SourceInventoryError("tracked source file exceeds safety bound")
            content_hasher.update(chunk)
            git_hasher.update(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if observed_bytes != before.st_size or after_identity != before_identity:
            raise SourceInventoryError("tracked source changed while it was being read")
        if git_hasher.hexdigest() != entry.oid:
            raise SourceInventoryError("source checkout must have a clean committed HEAD")
        return _SourceFileIdentity(
            path_key_sha256=_path_key(entry.name),
            git_mode=entry.mode,
            size_bytes=observed_bytes,
            content_sha256="sha256:" + content_hasher.hexdigest(),
        )
    finally:
        os.close(descriptor)


def _open_repository_root(root: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SourceInventoryError("platform lacks no-follow source verification")
    try:
        return os.open(os.fspath(root), flags | nofollow)
    except OSError:
        raise SourceInventoryError("repository root could not be opened securely") from None


def _open_beneath(root_descriptor: int, name: bytes) -> int:
    parts = name.split(b"/")
    directory = os.dup(root_descriptor)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        for part in parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | nofollow | cloexec, dir_fd=directory)
    except OSError:
        raise SourceInventoryError("tracked entries must be regular non-symlink files") from None
    finally:
        os.close(directory)


def _inventory_core(
    *,
    object_format: str,
    head_commit: str,
    head_tree: str,
    files: tuple[_SourceFileIdentity, ...],
) -> dict[str, object]:
    return {
        "files": [item.canonical_record() for item in files],
        "git_object_format": object_format,
        "head_commit": head_commit,
        "head_tree": head_tree,
        "schema": SOURCE_INVENTORY_SCHEMA_REVISION,
    }


def _validate_inventory(inventory: VerifiedSourceInventory) -> None:
    oid_length = _ALLOWED_OBJECT_FORMATS.get(inventory.git_object_format)
    if oid_length is None:
        raise SourceInventoryError("source inventory object format is invalid")
    _require_git_oid(inventory.head_commit, oid_length, "source inventory commit")
    _require_git_oid(inventory.head_tree, oid_length, "source inventory tree")
    if (
        type(inventory.tracked_file_count) is not int
        or inventory.tracked_file_count < 1
        or type(inventory.tracked_byte_count) is not int
        or inventory.tracked_byte_count < 0
        or type(inventory._files) is not tuple
        or len(inventory._files) != inventory.tracked_file_count
    ):
        raise SourceInventoryError("source inventory aggregate is invalid")
    if any(type(item) is not _SourceFileIdentity for item in inventory._files):
        raise SourceInventoryError("source inventory file identity is invalid")
    if len({item.path_key_sha256 for item in inventory._files}) != len(inventory._files):
        raise SourceInventoryError("source inventory repeats a file identity")
    total = 0
    for item in inventory._files:
        if (
            _SHA256.fullmatch(item.path_key_sha256) is None
            or _SHA256.fullmatch(item.content_sha256) is None
            or item.git_mode not in _REGULAR_GIT_MODES
            or type(item.size_bytes) is not int
            or item.size_bytes < 0
        ):
            raise SourceInventoryError("source inventory file identity is invalid")
        total += item.size_bytes
    if total != inventory.tracked_byte_count:
        raise SourceInventoryError("source inventory byte aggregate is inconsistent")
    expected = _digest_record(
        _inventory_core(
            object_format=inventory.git_object_format,
            head_commit=inventory.head_commit,
            head_tree=inventory.head_tree,
            files=inventory._files,
        )
    )
    if _SHA256.fullmatch(inventory.inventory_sha256) is None or expected != inventory.inventory_sha256:
        raise SourceInventoryError("source inventory root is inconsistent")


def _require_repository_root(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise SourceInventoryError("repository root must be an absolute Path")
    try:
        metadata = value.lstat()
    except OSError:
        raise SourceInventoryError("repository root is not a Git repository") from None
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SourceInventoryError("repository root must be a non-symlink directory")
    return value


def _run_git(root: Path, *arguments: str) -> bytes:
    binary = shutil.which("git")
    if binary is None:
        raise SourceInventoryError("Git executable is unavailable")
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": os.fspath(root),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    try:
        completed = subprocess.run(
            [
                binary,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                os.fspath(root),
                *arguments,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            shell=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        raise SourceInventoryError("Git repository inspection failed") from None
    if completed.returncode != 0:
        raise SourceInventoryError("Git repository inspection failed")
    if (
        len(completed.stdout) > _MAX_GIT_OUTPUT_BYTES
        or len(completed.stderr) > _MAX_GIT_OUTPUT_BYTES
    ):
        raise SourceInventoryError("Git repository inspection exceeded output bound")
    return completed.stdout


def _single_line(payload: bytes, label: str) -> str:
    try:
        value = payload.decode("ascii").rstrip("\n")
    except UnicodeError:
        raise SourceInventoryError(f"{label} is malformed") from None
    if not value or "\n" in value or "\r" in value:
        raise SourceInventoryError(f"{label} is malformed")
    return value


def _require_git_oid(value: str, expected_length: int, label: str) -> None:
    if len(value) != expected_length or _GIT_OID.fullmatch(value) is None:
        raise SourceInventoryError(f"{label} is malformed")


def _validate_git_name(name: bytes) -> None:
    if (
        not name
        or name.startswith(b"/")
        or b"\0" in name
        or any(part in {b"", b".", b".."} for part in name.split(b"/"))
    ):
        raise SourceInventoryError("Git tracked entry name is unsafe")


def _validated_relative_name(value: object) -> bytes:
    if type(value) is not str or not value or value.startswith("/") or "\x00" in value:
        raise SourceInventoryError("source subset tracked file name is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SourceInventoryError("source subset tracked file name is invalid")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        raise SourceInventoryError("source subset tracked file name is invalid") from None
    _validate_git_name(encoded)
    return encoded


def _path_key(name: bytes) -> str:
    return "sha256:" + hashlib.sha256(_PATH_KEY_DOMAIN + name).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest_record(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


__all__ = [
    "SOURCE_INVENTORY_SCHEMA_REVISION",
    "SOURCE_SUBSET_SCHEMA_REVISION",
    "SourceInventoryError",
    "VerifiedSourceInventory",
    "build_verified_source_inventory",
    "derive_source_subset_sha256",
    "revalidate_source_inventory",
]
