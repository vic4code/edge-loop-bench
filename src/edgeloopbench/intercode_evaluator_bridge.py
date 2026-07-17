"""Private bridge from the pinned state collector to evaluator primitives.

This module is intentionally split from both Docker execution and scoring.
It verifies the exact canonical collector document, converts its bounded state
surface to the evaluator's independent representation, and retains the
private checkpoint material needed by a later isolated evaluator.  No value
stored in :class:`PrivateCheckpointMaterialRegistry` is controller safe.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import posixpath
import re
import threading
import unicodedata
from dataclasses import dataclass

from .docker_cli import DockerTrustedState
from .interactive_environment import EnvironmentCheckpoint
from .intercode_evaluator import (
    MAX_NORMALIZED_OUTPUT_BYTES,
    MAX_STATE_ENTRIES,
    CanonicalStateSnapshot,
    EvaluatorInputError,
    StateEntry,
    hardlink_group_sha256,
)


COLLECTOR_STATE_ADAPTER_REVISION = "intercode-collector-state-adapter-v1"
_COLLECTOR_SCHEMA = "edgeloopbench.filesystem-state.v1"
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_CANONICAL_JSON_BYTES = 4 * 1024 * 1024
_MAX_PRIVATE_STREAM_BYTES = 16 * 1024 * 1024
_MAX_CWD_BYTES = 4096
_ENTRY_KEYS = frozenset(
    {
        "content_sha256",
        "gid",
        "hardlink_group_sha256",
        "mode",
        "path",
        "path_bytes_b64",
        "size_bytes",
        "symlink_target",
        "symlink_target_bytes_b64",
        "type",
        "uid",
    }
)
_DOCUMENT_KEYS = frozenset(
    {
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
)


class CollectorStateBridgeError(ValueError):
    """Private collector material is contradictory or not representable."""


@dataclass(frozen=True, slots=True, repr=False)
class AdaptedCollectorState:
    """Validated evaluator state plus the exact collector/adapter binding."""

    snapshot: CanonicalStateSnapshot
    collector_state_sha256: str
    adapter_revision: str
    binding_sha256: str

    def __repr__(self) -> str:
        return "<AdaptedCollectorState redacted>"


class _PrivateValue:
    __slots__ = ("_locked",)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_locked", False):
            raise AttributeError("private evaluator material is immutable")
        object.__setattr__(self, name, value)

    def __copy__(self) -> object:
        raise TypeError("private evaluator material cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("private evaluator material cannot be copied")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("private evaluator material cannot be serialized")


class PrivateCheckpointMaterial(_PrivateValue):
    """Evaluator-only material retained behind a public checkpoint digest."""

    __slots__ = (
        "scope_sha256",
        "checkpoint",
        "snapshot_image_id",
        "state",
        "collector_state_sha256",
        "adapter_revision",
        "adapter_binding_sha256",
        "raw_stdout",
        "raw_stderr",
        "normalized_output",
        "stdout_sha256",
        "stderr_sha256",
        "normalized_output_sha256",
        "cwd",
        "cwd_sha256",
        "runtime_sha256",
        "profile",
        "profile_sha256",
        "collector_source_sha256",
        "binding_sha256",
    )

    def __init__(self, **values: object) -> None:
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_locked", True)

    def __repr__(self) -> str:
        return "<PrivateCheckpointMaterial redacted>"


class PrivateCheckpointMaterialRegistry(_PrivateValue):
    """Episode-local, in-memory registry never exposed to the controller."""

    __slots__ = (
        "_scope_sha256",
        "_by_reference",
        "_snapshot_ids",
        "_closed",
        "_lock",
    )

    def __init__(self, *, scope_sha256: str) -> None:
        _require_digest(scope_sha256, "scope_sha256")
        object.__setattr__(self, "_scope_sha256", scope_sha256)
        object.__setattr__(self, "_by_reference", {})
        object.__setattr__(self, "_snapshot_ids", set())
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_locked", True)

    def __repr__(self) -> str:
        return "<PrivateCheckpointMaterialRegistry redacted>"

    def register_checkpoint(
        self,
        *,
        checkpoint: EnvironmentCheckpoint,
        snapshot_image_id: str,
        trusted_state: DockerTrustedState,
        raw_stdout: bytes,
        raw_stderr: bytes,
        normalized_output: str,
        cwd: str,
        runtime_sha256: str,
    ) -> None:
        """Validate and retain one verified checkpoint's private material."""

        if type(checkpoint) is not EnvironmentCheckpoint:
            raise CollectorStateBridgeError("checkpoint capability is invalid")
        _require_digest(snapshot_image_id, "snapshot_image_id")
        _require_digest(runtime_sha256, "runtime_sha256")
        _require_cwd(cwd)
        if not isinstance(raw_stdout, bytes) or not isinstance(raw_stderr, bytes):
            raise CollectorStateBridgeError("private streams must be exact bytes")
        if (
            len(raw_stdout) > _MAX_PRIVATE_STREAM_BYTES
            or len(raw_stderr) > _MAX_PRIVATE_STREAM_BYTES
        ):
            raise CollectorStateBridgeError("private stream exceeds its safety limit")
        if not isinstance(normalized_output, str):
            raise CollectorStateBridgeError("normalized output must be text")
        try:
            normalized_bytes = normalized_output.encode("utf-8", errors="strict")
        except UnicodeError:
            raise CollectorStateBridgeError("normalized output must be UTF-8") from None
        if len(normalized_bytes) > MAX_NORMALIZED_OUTPUT_BYTES:
            raise CollectorStateBridgeError("normalized output exceeds its safety limit")
        if normalized_output != _normalize_streams(raw_stdout, raw_stderr):
            raise CollectorStateBridgeError(
                "normalized output contradicts the exact private streams"
            )

        adapted = adapt_collector_state(trusted_state)
        if checkpoint.state_sha256 != adapted.collector_state_sha256:
            raise CollectorStateBridgeError(
                "checkpoint state differs from the trusted collector state"
            )
        stdout_sha256 = _bytes_digest(raw_stdout)
        stderr_sha256 = _bytes_digest(raw_stderr)
        normalized_output_sha256 = _bytes_digest(normalized_bytes)
        cwd_sha256 = _bytes_digest(cwd.encode("utf-8"))
        binding_sha256 = _canonical_digest(
            {
                "adapter_binding_sha256": adapted.binding_sha256,
                "checkpoint_reference_sha256": checkpoint.reference_sha256,
                "collector_source_sha256": trusted_state.collector_source_sha256,
                "cwd_sha256": cwd_sha256,
                "normalized_output_sha256": normalized_output_sha256,
                "profile": trusted_state.profile,
                "profile_sha256": trusted_state.profile_sha256,
                "runtime_sha256": runtime_sha256,
                "schema": "edgeloopbench.private-checkpoint-material.v1",
                "scope_sha256": self._scope_sha256,
                "snapshot_image_id": snapshot_image_id,
                "stderr_sha256": stderr_sha256,
                "stdout_sha256": stdout_sha256,
            }
        )
        material = PrivateCheckpointMaterial(
            scope_sha256=self._scope_sha256,
            checkpoint=checkpoint,
            snapshot_image_id=snapshot_image_id,
            state=adapted.snapshot,
            collector_state_sha256=adapted.collector_state_sha256,
            adapter_revision=adapted.adapter_revision,
            adapter_binding_sha256=adapted.binding_sha256,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            normalized_output=normalized_output,
            stdout_sha256=stdout_sha256,
            stderr_sha256=stderr_sha256,
            normalized_output_sha256=normalized_output_sha256,
            cwd=cwd,
            cwd_sha256=cwd_sha256,
            runtime_sha256=runtime_sha256,
            profile=trusted_state.profile,
            profile_sha256=trusted_state.profile_sha256,
            collector_source_sha256=trusted_state.collector_source_sha256,
            binding_sha256=binding_sha256,
        )
        with self._lock:
            if self._closed:
                raise CollectorStateBridgeError("private material registry is closed")
            if checkpoint.reference_sha256 in self._by_reference:
                raise CollectorStateBridgeError(
                    "checkpoint private material is already registered"
                )
            if snapshot_image_id in self._snapshot_ids:
                raise CollectorStateBridgeError(
                    "snapshot private material identity is not unique"
                )
            self._by_reference[checkpoint.reference_sha256] = material
            self._snapshot_ids.add(snapshot_image_id)

    def material_for_evaluation(
        self,
        checkpoint: EnvironmentCheckpoint,
    ) -> PrivateCheckpointMaterial:
        """Resolve material only for a trusted evaluator-side caller."""

        if type(checkpoint) is not EnvironmentCheckpoint:
            raise CollectorStateBridgeError("checkpoint capability is invalid")
        with self._lock:
            if self._closed:
                raise CollectorStateBridgeError("private material registry is closed")
            material = self._by_reference.get(checkpoint.reference_sha256)
            if material is None or material.checkpoint != checkpoint:
                raise CollectorStateBridgeError(
                    "checkpoint private material is unknown or contradictory"
                )
            return material

    def close(self) -> None:
        """Drop all in-memory references after terminal evaluation/finalization."""

        with self._lock:
            self._by_reference.clear()
            self._snapshot_ids.clear()
            object.__setattr__(self, "_closed", True)


def adapt_collector_state(trusted_state: DockerTrustedState) -> AdaptedCollectorState:
    """Verify exact collector JSON and convert it to evaluator state entries."""

    if type(trusted_state) is not DockerTrustedState:
        raise CollectorStateBridgeError("trusted state capability is invalid")
    for field, value in (
        ("state_sha256", trusted_state.state_sha256),
        ("profile_sha256", trusted_state.profile_sha256),
        ("policy_sha256", trusted_state.policy_sha256),
        ("root_baseline_sha256", trusted_state.root_baseline_sha256),
        (
            "writable_surface_audit_sha256",
            trusted_state.writable_surface_audit_sha256,
        ),
        ("collector_source_sha256", trusted_state.collector_source_sha256),
    ):
        _require_digest(value, field)
    if not isinstance(trusted_state.canonical_json, str):
        raise CollectorStateBridgeError("collector JSON must be text")
    try:
        raw = trusted_state.canonical_json.encode("utf-8", errors="strict")
    except UnicodeError:
        raise CollectorStateBridgeError("collector JSON must be UTF-8") from None
    if not raw or len(raw) > _MAX_CANONICAL_JSON_BYTES:
        raise CollectorStateBridgeError("collector JSON exceeds its safety limit")
    try:
        document = json.loads(
            trusted_state.canonical_json,
            object_pairs_hook=_unique_object,
        )
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise CollectorStateBridgeError("collector JSON is invalid") from None
    if not isinstance(document, dict) or _canonical_json(document) != trusted_state.canonical_json:
        raise CollectorStateBridgeError("collector JSON is not byte-for-byte canonical")
    if set(document) != _DOCUMENT_KEYS:
        raise CollectorStateBridgeError("collector document fields are not frozen")
    if document["schema"] != _COLLECTOR_SCHEMA:
        raise CollectorStateBridgeError("collector schema is not pinned")
    if document["dynamic_root_policy"] != "non_baseline_top_level":
        raise CollectorStateBridgeError("collector dynamic-root policy is not pinned")
    if document["profile"] != trusted_state.profile:
        raise CollectorStateBridgeError("collector profile is contradictory")
    for key, expected in (
        ("state_sha256", trusted_state.state_sha256),
        ("profile_sha256", trusted_state.profile_sha256),
        ("policy_sha256", trusted_state.policy_sha256),
        ("root_baseline_sha256", trusted_state.root_baseline_sha256),
        (
            "writable_surface_audit_sha256",
            trusted_state.writable_surface_audit_sha256,
        ),
    ):
        if document[key] != expected:
            raise CollectorStateBridgeError(f"collector {key} is contradictory")
    _validate_string_list(document["common_roots"], "common roots")
    _validate_string_list(document["task_roots"], "task roots")
    strict_surface = document["strict_surface"]
    if not isinstance(strict_surface, dict) or set(strict_surface) != {
        "failures",
        "status",
    }:
        raise CollectorStateBridgeError("collector strict surface is invalid")
    failures = strict_surface["failures"]
    if not isinstance(failures, list) or any(not isinstance(item, str) for item in failures):
        raise CollectorStateBridgeError("collector strict failures are invalid")
    expected_status = "representable" if trusted_state.strict_representable else "unrepresentable"
    if (
        strict_surface["status"] != expected_status
        or tuple(failures) != trusted_state.strict_failures
        or not trusted_state.strict_representable
        or failures
    ):
        raise CollectorStateBridgeError(
            "collector state is not representable by the strict evaluator"
        )

    raw_entries = document["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_STATE_ENTRIES:
        raise CollectorStateBridgeError("collector entries exceed their safety limit")
    if document["entry_count"] != len(raw_entries):
        raise CollectorStateBridgeError("collector entry count is contradictory")
    if (
        isinstance(document["total_file_bytes"], bool)
        or not isinstance(document["total_file_bytes"], int)
        or document["total_file_bytes"] < 0
    ):
        raise CollectorStateBridgeError("collector file byte count is invalid")

    parsed: list[dict[str, object]] = []
    total_file_bytes = 0
    paths: list[str] = []
    for raw_entry in raw_entries:
        normalized = _validate_entry(raw_entry)
        parsed.append(normalized)
        paths.append(normalized["path"])  # type: ignore[arg-type]
        if normalized["kind"] == "file":
            total_file_bytes += int(normalized["size_bytes"])
    if total_file_bytes != document["total_file_bytes"]:
        raise CollectorStateBridgeError("collector total file bytes are contradictory")
    encoded_paths = [path.encode("utf-8") for path in paths]
    if len(set(paths)) != len(paths) or encoded_paths != sorted(encoded_paths):
        raise CollectorStateBridgeError("collector state paths are not unique and sorted")

    evaluator_hardlinks = _validate_and_rebind_hardlinks(parsed)
    entries: list[StateEntry] = []
    try:
        for normalized in parsed:
            path = normalized["path"]
            collector_group = normalized["hardlink_group_sha256"]
            entries.append(
                StateEntry(
                    path=path,  # type: ignore[arg-type]
                    kind=normalized["kind"],  # type: ignore[arg-type]
                    mode=normalized["mode"],  # type: ignore[arg-type]
                    uid=normalized["uid"],  # type: ignore[arg-type]
                    gid=normalized["gid"],  # type: ignore[arg-type]
                    content_sha256=normalized["content_sha256"],  # type: ignore[arg-type]
                    symlink_target=normalized["symlink_target"],  # type: ignore[arg-type]
                    hardlink_group_sha256=(
                        None
                        if collector_group is None
                        else evaluator_hardlinks[collector_group]  # type: ignore[index]
                    ),
                )
            )
        snapshot = CanonicalStateSnapshot(tuple(entries))
    except (EvaluatorInputError, KeyError, TypeError):
        raise CollectorStateBridgeError(
            "collector state is not representable by the evaluator"
        ) from None

    state_payload = {
        "entries": raw_entries,
        "profile_sha256": document["profile_sha256"],
        "schema": document["schema"],
        "writable_surface_audit_sha256": document[
            "writable_surface_audit_sha256"
        ],
    }
    if _canonical_digest(state_payload) != trusted_state.state_sha256:
        raise CollectorStateBridgeError("collector state digest does not verify")
    binding_sha256 = _canonical_digest(
        {
            "adapter_revision": COLLECTOR_STATE_ADAPTER_REVISION,
            "collector_source_sha256": trusted_state.collector_source_sha256,
            "collector_state_sha256": trusted_state.state_sha256,
            "profile_sha256": trusted_state.profile_sha256,
            "schema": "edgeloopbench.collector-evaluator-binding.v1",
        }
    )
    return AdaptedCollectorState(
        snapshot=snapshot,
        collector_state_sha256=trusted_state.state_sha256,
        adapter_revision=COLLECTOR_STATE_ADAPTER_REVISION,
        binding_sha256=binding_sha256,
    )


def _validate_entry(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _ENTRY_KEYS:
        raise CollectorStateBridgeError("collector entry fields are not frozen")
    path = value["path"]
    if not isinstance(path, str):
        raise CollectorStateBridgeError("collector path is not strict UTF-8 text")
    _validate_encoded_text(value["path_bytes_b64"], path, "path")
    kind = value["type"]
    if kind not in {"file", "directory", "symlink", "missing"}:
        raise CollectorStateBridgeError("collector entry type is unsupported")
    hardlink = value["hardlink_group_sha256"]
    if hardlink is not None:
        _require_digest(hardlink, "hardlink_group_sha256")

    normalized: dict[str, object] = {
        "path": path,
        "kind": "absent" if kind == "missing" else kind,
        "mode": value["mode"],
        "uid": value["uid"],
        "gid": value["gid"],
        "content_sha256": value["content_sha256"],
        "symlink_target": value["symlink_target"],
        "hardlink_group_sha256": hardlink,
        "size_bytes": value["size_bytes"],
    }
    if kind == "missing":
        if any(
            value[field] is not None
            for field in (
                "mode",
                "uid",
                "gid",
                "content_sha256",
                "hardlink_group_sha256",
                "size_bytes",
                "symlink_target",
                "symlink_target_bytes_b64",
            )
        ):
            raise CollectorStateBridgeError("missing entry carries live metadata")
        return normalized

    for field in ("mode", "uid", "gid"):
        field_value = value[field]
        maximum = 0o7777 if field == "mode" else (1 << 32) - 2
        if (
            isinstance(field_value, bool)
            or not isinstance(field_value, int)
            or not 0 <= field_value <= maximum
        ):
            raise CollectorStateBridgeError(f"collector {field} is invalid")
    if kind == "file":
        _require_digest(value["content_sha256"], "content_sha256")
        size = value["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise CollectorStateBridgeError("collector file size is invalid")
        if value["symlink_target"] is not None or value["symlink_target_bytes_b64"] is not None:
            raise CollectorStateBridgeError("collector file carries symlink data")
    elif kind == "directory":
        if any(
            value[field] is not None
            for field in (
                "content_sha256",
                "hardlink_group_sha256",
                "size_bytes",
                "symlink_target",
                "symlink_target_bytes_b64",
            )
        ):
            raise CollectorStateBridgeError("collector directory carries file metadata")
    else:
        if any(
            value[field] is not None
            for field in ("content_sha256", "size_bytes")
        ):
            raise CollectorStateBridgeError("collector symlink carries file metadata")
        target = value["symlink_target"]
        if not isinstance(target, str):
            raise CollectorStateBridgeError("collector symlink is not strict UTF-8")
        _validate_encoded_text(
            value["symlink_target_bytes_b64"],
            target,
            "symlink target",
        )
    return normalized


def _validate_and_rebind_hardlinks(
    entries: list[dict[str, object]],
) -> dict[str, str]:
    groups: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        group = entry["hardlink_group_sha256"]
        if group is not None:
            groups.setdefault(group, []).append(entry)  # type: ignore[arg-type]
    rebound: dict[str, str] = {}
    for collector_digest, members in groups.items():
        if len(members) < 2 or any(member["kind"] != "file" for member in members):
            raise CollectorStateBridgeError(
                "collector hardlinks require at least two regular files"
            )
        paths = tuple(member["path"] for member in members)
        if len(set(paths)) != len(paths):
            raise CollectorStateBridgeError("collector hardlink paths are not unique")
        signatures = {
            (
                member["mode"],
                member["uid"],
                member["gid"],
                member["content_sha256"],
                member["symlink_target"],
                member["size_bytes"],
            )
            for member in members
        }
        if len(signatures) != 1:
            raise CollectorStateBridgeError(
                "collector hardlink metadata or content is contradictory"
            )
        if collector_digest != _collector_hardlink_digest(paths):  # type: ignore[arg-type]
            raise CollectorStateBridgeError("collector hardlink digest does not verify")
        try:
            rebound[collector_digest] = hardlink_group_sha256(paths)  # type: ignore[arg-type]
        except EvaluatorInputError:
            raise CollectorStateBridgeError(
                "collector hardlink group is not evaluator representable"
            ) from None
    return rebound


def _collector_hardlink_digest(paths: tuple[str, ...]) -> str:
    accumulator = hashlib.sha256(b"edgeloopbench-hardlink-group-v1\0")
    for encoded in sorted(path.encode("utf-8") for path in paths):
        accumulator.update(len(encoded).to_bytes(4, "big"))
        accumulator.update(encoded)
    return "sha256:" + accumulator.hexdigest()


def _validate_encoded_text(encoded: object, text: str, field: str) -> None:
    if not isinstance(encoded, str):
        raise CollectorStateBridgeError(f"collector {field} bytes are invalid")
    try:
        decoded = base64.b64decode(encoded, validate=True)
        expected = text.encode("utf-8", errors="strict")
    except (binascii.Error, UnicodeError, ValueError):
        raise CollectorStateBridgeError(f"collector {field} bytes are invalid") from None
    if decoded != expected or base64.b64encode(decoded).decode("ascii") != encoded:
        raise CollectorStateBridgeError(f"collector {field} bytes are contradictory")


def _validate_string_list(value: object, field: str) -> None:
    if (
        not isinstance(value, list)
        or len(value) > MAX_STATE_ENTRIES
        or any(not isinstance(item, str) for item in value)
    ):
        raise CollectorStateBridgeError(f"collector {field} are invalid")


def _normalize_streams(stdout: bytes, stderr: bytes) -> str:
    def normalize(value: bytes) -> str:
        try:
            text = value.decode("utf-8", errors="strict")
        except UnicodeError:
            raise CollectorStateBridgeError("private output must be valid UTF-8") from None
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        for character in text:
            if character in {"\n", "\t"}:
                continue
            category = unicodedata.category(character)
            if category.startswith("C") or category in {"Zl", "Zp"}:
                raise CollectorStateBridgeError(
                    "private output violates the frozen text policy"
                )
        return text

    normalized_stdout = normalize(stdout)
    normalized_stderr = normalize(stderr)
    if normalized_stdout and normalized_stderr:
        return f"{normalized_stdout}\n[stderr]\n{normalized_stderr}"
    return normalized_stdout or normalized_stderr


def _require_cwd(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise CollectorStateBridgeError("cwd must be a canonical absolute POSIX path")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise CollectorStateBridgeError("cwd must be valid UTF-8") from None
    if not encoded or len(encoded) > _MAX_CWD_BYTES:
        raise CollectorStateBridgeError("cwd exceeds its safety limit")
    if posixpath.normpath(value) != value or (value != "/" and value.endswith("/")):
        raise CollectorStateBridgeError("cwd is not lexically canonical")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise CollectorStateBridgeError("cwd contains forbidden text")
    return value


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise CollectorStateBridgeError(
            f"{field} must be a lowercase SHA-256 reference"
        )
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise CollectorStateBridgeError("collector JSON is not canonicalizable") from None


def _canonical_digest(value: object) -> str:
    return _bytes_digest(_canonical_json(value).encode("ascii"))


def _bytes_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
