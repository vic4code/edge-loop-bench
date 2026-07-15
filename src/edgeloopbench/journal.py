"""Crash-tolerant, hash-chained JSONL event journal.

The journal owns framing, sequence numbers, and integrity metadata.  It never
adds wall-clock timestamps or filesystem paths.  Recovery is deliberately a
separate, explicit operation: normal inspection and append calls never discard
bytes after a crashed write.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import NoReturn


GENESIS_EVENT_SHA256 = "0" * 64
SEALED_EVENT_TYPE = "journal_sealed"
MAX_JOURNAL_EVENT_BYTES = 256 * 1024
_RESERVED_FIELDS = frozenset(
    {"sequence", "previous_event_sha256", "event_sha256", "sealed_event_count"}
)
_HEX_DIGITS = frozenset("0123456789abcdef")


class JournalError(ValueError):
    """Base class for journal validation and state errors."""


class JournalIntegrityError(JournalError):
    """Raised when complete journal records fail integrity validation."""


class JournalPartialTailError(JournalIntegrityError):
    """Raised when an operation requires an explicitly recovered tail."""


class JournalSealedError(JournalError):
    """Raised when a caller tries to append to a sealed journal."""


class JournalSecurityError(JournalError):
    """Raised when the journal target is not a securely opened regular file."""


class _DuplicateJSONKey(ValueError):
    pass


@dataclass(frozen=True)
class PartialTail:
    """A final non-newline byte range left outside the complete JSONL prefix."""

    byte_offset: int
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class JournalInspection:
    """Validated journal state without embedding a host filesystem path."""

    record_count: int
    next_sequence: int
    last_event_sha256: str
    sealed: bool
    complete_byte_length: int
    file_byte_length: int
    partial_tail: PartialTail | None


def canonical_event_bytes(event: Mapping[str, object]) -> bytes:
    """Return the canonical hash input, excluding ``event_sha256`` itself."""

    if not isinstance(event, Mapping):
        raise ValueError("journal event must be a mapping")
    content = dict(event)
    content.pop("event_sha256", None)
    _validate_json_value(content)
    try:
        serialized = json.dumps(
            content,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"journal event is not valid JSON: {error}") from error
    return serialized.encode("utf-8")


def append_journal_event(
    path: str | Path, event: Mapping[str, object]
) -> dict[str, object]:
    """Append one event with the next sequence number and chain digest."""

    payload = _prepare_payload(event)
    if payload.get("type") == SEALED_EVENT_TYPE:
        raise ValueError("journal seal records may only be created by seal_journal")
    return _append_record(path, payload, seal=False)


def seal_journal(path: str | Path) -> dict[str, object]:
    """Append the one terminal seal record to an otherwise valid journal."""

    return _append_record(path, {}, seal=True)


def inspect_journal(
    path: str | Path, *, require_sealed: bool = False
) -> JournalInspection:
    """Validate complete records and report, but never alter, a partial tail."""

    descriptor = _secure_open(path, os.O_RDONLY, allow_missing=True)
    if descriptor is None:
        inspection = _empty_inspection()
    else:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            inspection = _inspect_bytes(_read_all(descriptor))
        finally:
            os.close(descriptor)
    _enforce_requested_state(inspection, require_sealed=require_sealed)
    return inspection


def recover_partial_tail(path: str | Path) -> JournalInspection:
    """Explicitly truncate one final non-newline tail after validating its prefix.

    Newline-terminated corruption is never eligible for this recovery path.
    The file is re-read and validated while exclusively locked, so the decision
    and truncation operate on the same byte snapshot.
    """

    descriptor = _secure_open(path, os.O_RDWR, allow_missing=False)
    assert descriptor is not None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        data = _read_all(descriptor)
        inspection = _inspect_bytes(data)
        if inspection.partial_tail is None:
            raise JournalPartialTailError("journal has no partial tail to recover")
        os.ftruncate(descriptor, inspection.complete_byte_length)
        os.fsync(descriptor)
        recovered = _inspect_bytes(data[: inspection.complete_byte_length])
        if recovered.partial_tail is not None:  # pragma: no cover - defensive invariant
            raise JournalIntegrityError("journal recovery did not end on a record boundary")
        return recovered
    finally:
        os.close(descriptor)


def _prepare_payload(event: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(event, Mapping):
        raise ValueError("journal event must be a mapping")
    payload = dict(event)
    if any(not isinstance(key, str) for key in payload):
        raise ValueError("journal object keys must be strings")
    reserved = _RESERVED_FIELDS.intersection(payload)
    if reserved:
        raise ValueError(f"journal event uses reserved field: {sorted(reserved)[0]}")
    event_type = payload.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("journal event type must be a non-empty string")
    canonical_event_bytes(payload)
    return payload


def _append_record(
    path: str | Path, payload: Mapping[str, object], *, seal: bool
) -> dict[str, object]:
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise JournalSecurityError("event journal parent could not be prepared") from error

    descriptor = _secure_open(
        target,
        os.O_RDWR | os.O_APPEND | os.O_CREAT,
        mode=0o600,
        allow_missing=False,
    )
    assert descriptor is not None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        inspection = _inspect_bytes(_read_all(descriptor))
        if inspection.partial_tail is not None:
            raise JournalPartialTailError(
                "journal has a partial tail; inspect and explicitly recover it before appending"
            )
        if inspection.sealed:
            raise JournalSealedError("journal is already sealed")

        event_payload = (
            {"type": SEALED_EVENT_TYPE, "sealed_event_count": inspection.record_count}
            if seal
            else dict(payload)
        )
        record: dict[str, object] = {
            **event_payload,
            "sequence": inspection.next_sequence,
            "previous_event_sha256": inspection.last_event_sha256,
        }
        record["event_sha256"] = sha256(canonical_event_bytes(record)).hexdigest()
        serialized = _canonical_record_bytes(record) + b"\n"
        if len(serialized) > MAX_JOURNAL_EVENT_BYTES:
            raise ValueError("journal event exceeds safety limit")
        _write_all(descriptor, serialized)
        os.fsync(descriptor)
        return record
    finally:
        os.close(descriptor)


def _inspect_bytes(data: bytes) -> JournalInspection:
    complete_length = len(data)
    partial_tail: PartialTail | None = None
    if data and not data.endswith(b"\n"):
        last_newline = data.rfind(b"\n")
        complete_length = last_newline + 1
        tail = data[complete_length:]
        if len(tail) > MAX_JOURNAL_EVENT_BYTES:
            raise JournalIntegrityError("journal partial tail exceeds safety limit")
        partial_tail = PartialTail(
            byte_offset=complete_length,
            byte_length=len(tail),
            sha256=sha256(tail).hexdigest(),
        )

    prefix = data[:complete_length]
    expected_sequence = 1
    previous_digest = GENESIS_EVENT_SHA256
    sealed = False
    for line in prefix.splitlines(keepends=True):
        if len(line) > MAX_JOURNAL_EVENT_BYTES:
            raise JournalIntegrityError("journal record exceeds safety limit")
        if not line.endswith(b"\n"):
            raise JournalIntegrityError("journal complete prefix is not newline terminated")
        body = line[:-1]
        record = _parse_record(body)
        try:
            canonical_record = _canonical_record_bytes(record)
        except ValueError as error:
            raise JournalIntegrityError("journal record is not canonical JSON") from error
        if canonical_record != body:
            raise JournalIntegrityError("journal record is not canonical JSON")

        event_type = record.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise JournalIntegrityError("journal event type must be a non-empty string")
        sequence = record.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise JournalIntegrityError("journal sequence must be an integer")
        if sequence != expected_sequence:
            raise JournalIntegrityError("journal sequence is not monotonic and contiguous")
        if record.get("previous_event_sha256") != previous_digest:
            raise JournalIntegrityError("journal previous-event hash chain is broken")

        event_digest = record.get("event_sha256")
        if not _is_sha256(event_digest):
            raise JournalIntegrityError("journal event hash is not lowercase SHA-256")
        try:
            expected_digest = sha256(canonical_event_bytes(record)).hexdigest()
        except ValueError as error:
            raise JournalIntegrityError("journal event content is not valid JSON") from error
        if event_digest != expected_digest:
            raise JournalIntegrityError("journal event hash does not match its content")

        if sealed:
            raise JournalIntegrityError("journal seal is not the terminal record")
        if event_type == SEALED_EVENT_TYPE:
            sealed_count = record.get("sealed_event_count")
            if (
                isinstance(sealed_count, bool)
                or not isinstance(sealed_count, int)
                or sealed_count != sequence - 1
            ):
                raise JournalIntegrityError("journal seal event count is invalid")
            sealed = True
        elif "sealed_event_count" in record:
            raise JournalIntegrityError("non-seal journal record has seal metadata")

        previous_digest = event_digest
        expected_sequence += 1

    return JournalInspection(
        record_count=expected_sequence - 1,
        next_sequence=expected_sequence,
        last_event_sha256=previous_digest,
        sealed=sealed,
        complete_byte_length=complete_length,
        file_byte_length=len(data),
        partial_tail=partial_tail,
    )


def _parse_record(body: bytes) -> dict[str, object]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise JournalIntegrityError("journal record is not valid UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except _DuplicateJSONKey as error:
        raise JournalIntegrityError(str(error)) from error
    except (json.JSONDecodeError, RecursionError) as error:
        raise JournalIntegrityError("journal record is not valid JSON") from error
    if not isinstance(value, dict):
        raise JournalIntegrityError("journal record must be a JSON object")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(f"journal record has duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_record_bytes(record: Mapping[str, object]) -> bytes:
    _validate_json_value(record)
    try:
        return json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"journal record is not valid JSON: {error}") from error


def _validate_json_value(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("journal object keys must be strings")
            _validate_json_value(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_json_value(child)
        return
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    raise ValueError(f"journal event contains unsupported JSON value: {type(value).__name__}")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _empty_inspection() -> JournalInspection:
    return JournalInspection(
        record_count=0,
        next_sequence=1,
        last_event_sha256=GENESIS_EVENT_SHA256,
        sealed=False,
        complete_byte_length=0,
        file_byte_length=0,
        partial_tail=None,
    )


def _enforce_requested_state(
    inspection: JournalInspection, *, require_sealed: bool
) -> None:
    if inspection.partial_tail is not None and require_sealed:
        raise JournalPartialTailError("journal has an unrecovered partial tail")
    if require_sealed and not inspection.sealed:
        raise JournalIntegrityError("journal is not sealed")


def _secure_open(
    path: str | Path,
    flags: int,
    *,
    mode: int = 0o600,
    allow_missing: bool,
) -> int | None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise JournalSecurityError("platform does not support no-follow file opens")
    flags |= nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.fspath(path), flags, mode)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise JournalSecurityError("event journal does not exist") from None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            raise JournalSecurityError("event journal target is not a regular file") from error
        raise JournalSecurityError("event journal could not be securely opened") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise JournalSecurityError("event journal target is not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_all(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(descriptor, 64 * 1024)
        except InterruptedError:
            continue
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(descriptor, remaining)
        except InterruptedError:
            continue
        if written <= 0 or written > len(remaining):
            _raise_short_write()
        remaining = remaining[written:]


def _raise_short_write() -> NoReturn:
    raise OSError("event journal write made no forward progress")
