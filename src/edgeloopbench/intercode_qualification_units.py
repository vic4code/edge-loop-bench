"""Crash-resumable logical-unit evidence for InterCode qualification.

Each ``(task_id, replay_index)`` is isolated in its own exact-mode journal.
This module owns only the durable state machine and private aggregate index;
the InterCode source-capability check remains in :mod:`intercode_qualification`
and the eventual Docker adapter must issue lifecycle receipts from inspected
resources.  The underscored synthetic receipt helpers exist only for unit
tests and cannot construct the sealed aggregate evidence type directly.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import TypeVar

from .journal import (
    MAX_JOURNAL_EVENT_BYTES,
    SEALED_EVENT_TYPE,
    JournalError,
    canonical_event_bytes,
    _inspect_bytes,
)


_TAGGED_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^bash-(fs[1-4])-[0-9]{3}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_MAX_UNIT_BYTES = 2 * 1024 * 1024
_MAX_INDEX_BYTES = 8 * 1024 * 1024
_MAX_RECORDS = 1024
_CHAIN_FIELDS = frozenset(
    {"sequence", "previous_event_sha256", "event_sha256"}
)
_UNIT_AUTHORITY = object()
_AGGREGATE_SEAL = object()
_T = TypeVar("_T")


class QualificationUnitError(ValueError):
    """A unit journal, receipt, or aggregate is unsafe or inconsistent."""


class QualificationIncompleteError(QualificationUnitError):
    """Qualification cannot continue or aggregate without new authority."""


class QualificationUnitStatus(str, Enum):
    STARTED = "started"
    ACQUIRE_PENDING = "acquire_pending"
    ACQUIRED = "acquired"
    RESULT_DURABLE = "result_durable"
    RELEASE_PENDING = "release_pending"
    RELEASED = "released"
    COMPLETED = "completed"
    ABORTED = "aborted"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class QualificationUnitKey:
    task_id: str
    replay_index: int

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or _TASK_ID.fullmatch(self.task_id) is None:
            raise ValueError("qualification unit task ID is not canonical")
        if type(self.replay_index) is not int or self.replay_index not in (1, 2):
            raise ValueError("qualification replay index must be exactly 1 or 2")

    @property
    def sha256(self) -> str:
        return _canonical_sha256(
            {"task_id": self.task_id, "replay_index": self.replay_index}
        )


@dataclass(frozen=True, slots=True)
class QualificationUnitExpectation:
    task_id: str
    stratum: str
    replay_index: int
    source_capability_sha256: str
    suite_name: str
    source_revision: str
    source_population_sha256: str
    source_corpus_sha256: str
    static_exclusion_audit_sha256: str
    image_sha256: str
    evaluator_sha256: str
    state_normalization_sha256: str

    def __post_init__(self) -> None:
        key = QualificationUnitKey(self.task_id, self.replay_index)
        match = _TASK_ID.fullmatch(key.task_id)
        assert match is not None
        if self.stratum != match.group(1):
            raise ValueError("qualification unit stratum differs from task ID")
        if type(self.suite_name) is not str or not self.suite_name:
            raise ValueError("qualification suite name must be non-empty")
        if (
            type(self.source_revision) is not str
            or _REVISION.fullmatch(self.source_revision) is None
        ):
            raise ValueError("qualification source revision must be a full commit")
        for name in (
            "source_capability_sha256",
            "source_population_sha256",
            "source_corpus_sha256",
            "static_exclusion_audit_sha256",
            "image_sha256",
            "evaluator_sha256",
            "state_normalization_sha256",
        ):
            _require_digest(getattr(self, name), name)

    @property
    def key(self) -> QualificationUnitKey:
        return QualificationUnitKey(self.task_id, self.replay_index)

    def binding(self, generation: int) -> QualificationUnitBinding:
        return QualificationUnitBinding(expectation=self, generation=generation)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "stratum": self.stratum,
            "replay_index": self.replay_index,
            "source_capability_sha256": self.source_capability_sha256,
            "suite_name": self.suite_name,
            "source_revision": self.source_revision,
            "source_population_sha256": self.source_population_sha256,
            "source_corpus_sha256": self.source_corpus_sha256,
            "static_exclusion_audit_sha256": self.static_exclusion_audit_sha256,
            "image_sha256": self.image_sha256,
            "evaluator_sha256": self.evaluator_sha256,
            "state_normalization_sha256": self.state_normalization_sha256,
        }


@dataclass(frozen=True, slots=True)
class QualificationUnitBinding:
    expectation: QualificationUnitExpectation
    generation: int

    def __post_init__(self) -> None:
        if type(self.expectation) is not QualificationUnitExpectation:
            raise ValueError("qualification expectation must use the exact type")
        self.expectation.__post_init__()
        if type(self.generation) is not int or self.generation not in (0, 1):
            raise ValueError("qualification generation must be exactly 0 or 1")

    @property
    def key(self) -> QualificationUnitKey:
        return self.expectation.key

    @property
    def image_sha256(self) -> str:
        return self.expectation.image_sha256

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            **self.expectation.to_dict(),
            "generation": self.generation,
            "logical_unit_sha256": self.key.sha256,
        }


@dataclass(frozen=True, slots=True)
class QualificationUnitResult:
    infrastructure_valid: bool
    setup_valid: bool
    network_required: bool
    state_supported: bool
    evaluator_valid: bool
    exit_policy_passed: bool
    official_reward: float
    strict_success: bool
    initial_state_sha256: str
    normalized_output_sha256: str
    observable_state_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "infrastructure_valid",
            "setup_valid",
            "network_required",
            "state_supported",
            "evaluator_valid",
            "exit_policy_passed",
            "strict_success",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if (
            isinstance(self.official_reward, bool)
            or not isinstance(self.official_reward, (int, float))
            or not math.isfinite(float(self.official_reward))
            or not 0.0 <= float(self.official_reward) <= 1.0
        ):
            raise ValueError("official reward must be finite in [0, 1]")
        for name in (
            "initial_state_sha256",
            "normalized_output_sha256",
            "observable_state_sha256",
        ):
            _require_digest(getattr(self, name), name)

    def to_dict(self) -> dict[str, object]:
        return {
            "infrastructure_valid": self.infrastructure_valid,
            "setup_valid": self.setup_valid,
            "network_required": self.network_required,
            "state_supported": self.state_supported,
            "evaluator_valid": self.evaluator_valid,
            "exit_policy_passed": self.exit_policy_passed,
            "official_reward": float(self.official_reward),
            "strict_success": self.strict_success,
            "initial_state_sha256": self.initial_state_sha256,
            "normalized_output_sha256": self.normalized_output_sha256,
            "observable_state_sha256": self.observable_state_sha256,
        }


@dataclass(frozen=True, slots=True)
class QualificationUnitInspection:
    binding: QualificationUnitBinding
    status: QualificationUnitStatus
    generation: int
    sealed: bool
    event_types: tuple[str, ...]
    chain_sha256: str
    terminal_root_sha256: str | None
    result_event_sha256: str | None
    release_completion_event_sha256: str | None
    result: QualificationUnitResult | None
    lifecycle_identity_sha256: str | None
    container_identity_sha256: str | None
    partial_tail_sha256: str | None
    incomplete_reason: str | None


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    owner: int


class _PrivateReceipt:
    __slots__ = ("_journal_identity", "_intent_event_sha256", "_values")

    def __init__(
        self,
        journal_identity: str,
        intent_event_sha256: str,
        values: tuple[object, ...],
    ) -> None:
        object.__setattr__(self, "_journal_identity", journal_identity)
        object.__setattr__(self, "_intent_event_sha256", intent_event_sha256)
        object.__setattr__(self, "_values", values)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("qualification lifecycle receipts are immutable")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} private>"

    def __reduce__(self) -> object:
        raise TypeError("qualification lifecycle receipts cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("qualification lifecycle receipts cannot be serialized")


class _AcquireIntent(_PrivateReceipt):
    pass


class _AcquireReceipt(_PrivateReceipt):
    pass


class _ReleaseIntent(_PrivateReceipt):
    pass


class _CleanupReceipt(_PrivateReceipt):
    pass


class _ProcessGuard:
    __slots__ = ("lock",)

    def __init__(self) -> None:
        self.lock = threading.RLock()


_GUARDS_LOCK = threading.Lock()
_GUARDS: dict[_FileIdentity, _ProcessGuard] = {}


def _guard_for(identity: _FileIdentity) -> _ProcessGuard:
    with _GUARDS_LOCK:
        return _GUARDS.setdefault(identity, _ProcessGuard())


class _ExactJournal:
    __slots__ = (
        "path",
        "file_identity_sha256",
        "parent_identity_sha256",
        "_guard",
    )

    def __init__(
        self,
        path: Path,
        file_identity_sha256: str,
        parent_identity_sha256: str,
        guard: _ProcessGuard,
    ) -> None:
        self.path = path
        self.file_identity_sha256 = file_identity_sha256
        self.parent_identity_sha256 = parent_identity_sha256
        self._guard = guard

    @classmethod
    def create(
        cls,
        path: Path,
        first_event: Callable[[str, str], Mapping[str, object]],
        validator: Callable[[bytes], object],
        *,
        maximum_bytes: int,
    ) -> _ExactJournal:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise QualificationUnitError(
                "qualification journal parent could not be prepared"
            ) from error
        parent = _open_parent(path.parent)
        descriptor: int | None = None
        try:
            parent_meta = _verify_parent(parent)
            descriptor = _create_file(path.name, parent)
            os.fchmod(descriptor, 0o600)
            file_meta = _verify_file(descriptor)
            file_digest = _metadata_digest(file_meta, "file")
            parent_digest = _metadata_digest(parent_meta, "parent")
            identity = _identity(file_meta)
            guard = _guard_for(identity)
            with guard.lock:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                payload = dict(first_event(file_digest, parent_digest))
                _append_locked(
                    descriptor,
                    b"",
                    payload,
                    validator,
                    maximum_bytes=maximum_bytes,
                )
                os.fsync(parent)
        except BaseException:
            if descriptor is not None:
                try:
                    os.unlink(path.name, dir_fd=parent)
                    os.fsync(parent)
                except OSError:
                    pass
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)
        return cls(path, file_digest, parent_digest, guard)

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        maximum_bytes: int,
    ) -> _ExactJournal:
        parent = _open_parent(path.parent)
        try:
            parent_meta = _verify_parent(parent)
            descriptor = _open_file(path.name, parent, writable=False)
            try:
                file_meta = _verify_file(descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                data = _read_descriptor(descriptor, maximum_bytes)
                generic = _generic_inspect(data, maximum_bytes)
                if generic.record_count == 0:
                    raise QualificationUnitError("qualification journal is empty")
                first = json.loads(
                    data[: generic.complete_byte_length].splitlines()[0]
                )
                file_digest = _require_digest(
                    first.get("journal_file_identity_sha256"),
                    "journal file identity",
                )
                parent_digest = _require_digest(
                    first.get("journal_parent_identity_sha256"),
                    "journal parent identity",
                )
                if (
                    _metadata_digest(file_meta, "file") != file_digest
                    or _metadata_digest(parent_meta, "parent") != parent_digest
                ):
                    raise QualificationUnitError(
                        "qualification journal inode or parent identity changed"
                    )
                _verify_link(path.name, parent, file_digest)
                identity = _identity(file_meta)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)
        return cls(path, file_digest, parent_digest, _guard_for(identity))

    def read(self, *, maximum_bytes: int) -> bytes:
        return self._with(
            writable=False,
            exclusive=False,
            maximum_bytes=maximum_bytes,
            operation=lambda descriptor, _parent: _read_descriptor(
                descriptor, maximum_bytes
            ),
        )

    def append(
        self,
        payload: Mapping[str, object],
        validator: Callable[[bytes], object],
        *,
        maximum_bytes: int,
    ) -> dict[str, object]:
        def operation(descriptor: int, _parent: int) -> dict[str, object]:
            data = _read_descriptor(descriptor, maximum_bytes)
            record, _ = _append_locked(
                descriptor,
                data,
                payload,
                validator,
                maximum_bytes=maximum_bytes,
            )
            return record

        return self._with(
            writable=True,
            exclusive=True,
            maximum_bytes=maximum_bytes,
            operation=operation,
        )

    def seal(
        self,
        validator: Callable[[bytes], object],
        *,
        maximum_bytes: int,
    ) -> dict[str, object]:
        def operation(descriptor: int, _parent: int) -> dict[str, object]:
            data = _read_descriptor(descriptor, maximum_bytes)
            generic = _generic_inspect(data, maximum_bytes)
            record, _ = _append_locked(
                descriptor,
                data,
                {
                    "type": SEALED_EVENT_TYPE,
                    "sealed_event_count": generic.record_count,
                },
                validator,
                maximum_bytes=maximum_bytes,
            )
            return record

        return self._with(
            writable=True,
            exclusive=True,
            maximum_bytes=maximum_bytes,
            operation=operation,
        )

    def _with(
        self,
        *,
        writable: bool,
        exclusive: bool,
        maximum_bytes: int,
        operation: Callable[[int, int], _T],
    ) -> _T:
        with self._guard.lock:
            parent = _open_parent(self.path.parent)
            try:
                if (
                    _metadata_digest(_verify_parent(parent), "parent")
                    != self.parent_identity_sha256
                ):
                    raise QualificationUnitError(
                        "qualification journal parent identity changed"
                    )
                descriptor = _open_file(
                    self.path.name, parent, writable=writable
                )
                try:
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                    )
                    self._revalidate(descriptor, parent)
                    result = operation(descriptor, parent)
                    self._revalidate(descriptor, parent)
                    _read_descriptor(descriptor, maximum_bytes)
                    return result
                finally:
                    os.close(descriptor)
            finally:
                os.close(parent)

    def _revalidate(self, descriptor: int, parent: int) -> None:
        if (
            _metadata_digest(_verify_file(descriptor), "file")
            != self.file_identity_sha256
        ):
            raise QualificationUnitError(
                "qualification journal inode identity changed"
            )
        _verify_link(self.path.name, parent, self.file_identity_sha256)


class QualificationUnitAttempt:
    """One exact-inode generation for a deterministic logical unit."""

    __slots__ = ("_journal", "binding")

    def __init__(
        self,
        journal: _ExactJournal,
        binding: QualificationUnitBinding,
        *,
        _authority: object | None = None,
    ) -> None:
        if _authority is not _UNIT_AUTHORITY:
            raise QualificationUnitError(
                "qualification unit attempts are repository-owned"
            )
        self._journal = journal
        self.binding = binding

    @property
    def path(self) -> Path:
        return self._journal.path

    @property
    def _identity_sha256(self) -> str:
        return _canonical_sha256(
            {
                "file": self._journal.file_identity_sha256,
                "binding": self.binding.sha256,
            }
        )

    @classmethod
    def _create(
        cls,
        path: Path,
        binding: QualificationUnitBinding,
    ) -> QualificationUnitAttempt:
        binding.__post_init__()

        def first(file_identity: str, parent_identity: str) -> dict[str, object]:
            return {
                "type": "qualification_unit_started",
                "unit_schema_version": 1,
                "binding": binding.to_dict(),
                "binding_sha256": binding.sha256,
                "journal_file_identity_sha256": file_identity,
                "journal_parent_identity_sha256": parent_identity,
            }

        journal = _ExactJournal.create(
            path,
            first,
            _parse_unit,
            maximum_bytes=_MAX_UNIT_BYTES,
        )
        return cls(journal, binding, _authority=_UNIT_AUTHORITY)

    @classmethod
    def _open(
        cls,
        path: Path,
        expected_binding: QualificationUnitBinding,
    ) -> QualificationUnitAttempt:
        journal = _ExactJournal.open(path, maximum_bytes=_MAX_UNIT_BYTES)
        state = _parse_unit(journal.read(maximum_bytes=_MAX_UNIT_BYTES))
        if state.binding != expected_binding:
            raise QualificationUnitError(
                "qualification unit belongs to a different frozen binding"
            )
        return cls(journal, expected_binding, _authority=_UNIT_AUTHORITY)

    def inspect(
        self,
        *,
        require_sealed: bool = False,
    ) -> QualificationUnitInspection:
        state = self._state()
        inspection = state.inspection
        if inspection.partial_tail_sha256 is not None:
            if require_sealed:
                raise QualificationIncompleteError(
                    "qualification unit has an unquarantined partial tail"
                )
        if require_sealed and not inspection.sealed:
            raise QualificationIncompleteError("qualification unit is not sealed")
        return inspection

    def record_acquire_intent(
        self,
        *,
        lifecycle_identity_sha256: str,
        planned_locator_sha256: str,
    ) -> _AcquireIntent:
        lifecycle = _require_digest(
            lifecycle_identity_sha256, "lifecycle identity"
        )
        locator = _require_digest(planned_locator_sha256, "planned locator")
        state = self._require_status(QualificationUnitStatus.STARTED)
        del state
        record = self._append(
            {
                "type": "container_acquire_intent",
                "binding_sha256": self.binding.sha256,
                "lifecycle_identity_sha256": lifecycle,
                "planned_locator_sha256": locator,
                "image_sha256": self.binding.image_sha256,
            }
        )
        return _AcquireIntent(
            self._identity_sha256,
            _event_tag(record),
            (lifecycle, locator),
        )

    def record_acquire_completion(
        self,
        intent: object,
        receipt: object,
    ) -> None:
        state = self._require_status(QualificationUnitStatus.ACQUIRE_PENDING)
        if (
            type(intent) is not _AcquireIntent
            or intent._journal_identity != self._identity_sha256
            or intent._intent_event_sha256 != state.acquire_intent_event_sha256
            or type(receipt) is not _AcquireReceipt
            or receipt._journal_identity != self._identity_sha256
            or receipt._intent_event_sha256 != state.acquire_intent_event_sha256
        ):
            raise QualificationUnitError(
                "container acquisition receipt belongs to a foreign unit"
            )
        lifecycle, locator = intent._values
        (
            container,
            image,
            profile,
            acquisition_receipt,
        ) = receipt._values
        if lifecycle != state.lifecycle_identity_sha256:
            raise QualificationIncompleteError(
                "container acquisition lifecycle identity mismatch"
            )
        if image != self.binding.image_sha256:
            raise QualificationIncompleteError(
                "container acquisition image identity mismatch"
            )
        self._append(
            {
                "type": "container_acquire_completed",
                "intent_event_sha256": state.acquire_intent_event_sha256,
                "lifecycle_identity_sha256": lifecycle,
                "planned_locator_sha256": locator,
                "container_identity_sha256": container,
                "image_sha256": image,
                "profile_sha256": profile,
                "acquisition_receipt_sha256": acquisition_receipt,
            }
        )

    def record_result(self, result: QualificationUnitResult) -> str:
        self._require_status(QualificationUnitStatus.ACQUIRED)
        if type(result) is not QualificationUnitResult:
            raise QualificationUnitError(
                "qualification result must use the exact typed surface"
            )
        result.__post_init__()
        record = self._append(
            {
                "type": "qualification_unit_result",
                "acquire_completion_event_sha256": self._state().acquire_completion_event_sha256,
                **result.to_dict(),
            }
        )
        return _event_tag(record)

    def record_release_intent(self, *, recovery: bool) -> _ReleaseIntent:
        if type(recovery) is not bool:
            raise QualificationUnitError("release recovery marker must be boolean")
        state = self._state()
        if state.inspection.status not in (
            QualificationUnitStatus.STARTED,
            QualificationUnitStatus.ACQUIRE_PENDING,
            QualificationUnitStatus.ACQUIRED,
            QualificationUnitStatus.RESULT_DURABLE,
        ):
            raise QualificationUnitError(
                "container release intent is not admitted in the current state"
            )
        if state.result_event_sha256 is None and not recovery:
            raise QualificationUnitError(
                "pre-result cleanup must be exact recovery reconciliation"
            )
        record = self._append(
            {
                "type": "container_release_intent",
                "binding_sha256": self.binding.sha256,
                "acquire_intent_event_sha256": state.acquire_intent_event_sha256,
                "acquire_completion_event_sha256": state.acquire_completion_event_sha256,
                "result_event_sha256": state.result_event_sha256,
                "lifecycle_identity_sha256": state.lifecycle_identity_sha256,
                "container_identity_sha256": state.container_identity_sha256,
                "recovery": recovery,
            }
        )
        return _ReleaseIntent(
            self._identity_sha256,
            _event_tag(record),
            (
                state.lifecycle_identity_sha256,
                state.container_identity_sha256,
                recovery,
            ),
        )

    def record_release_completion(
        self,
        intent: object,
        receipt: object,
    ) -> None:
        state = self._require_status(QualificationUnitStatus.RELEASE_PENDING)
        if (
            type(intent) is not _ReleaseIntent
            or intent._journal_identity != self._identity_sha256
            or intent._intent_event_sha256 != state.release_intent_event_sha256
            or type(receipt) is not _CleanupReceipt
            or receipt._journal_identity != self._identity_sha256
            or receipt._intent_event_sha256 != state.release_intent_event_sha256
        ):
            raise QualificationUnitError(
                "container cleanup receipt belongs to a foreign unit"
            )
        (
            lifecycle,
            container,
            present_before,
            absent_after,
            identity_match,
            profile_match,
            ambiguous,
            cleanup_receipt,
        ) = receipt._values
        expected_container = state.container_identity_sha256
        observed_container_is_exact = (
            container == expected_container
            if expected_container is not None
            else (
                (present_before and container is not None)
                or (not present_before and container is None)
            )
        )
        self._append(
            {
                "type": "container_release_completed",
                "intent_event_sha256": state.release_intent_event_sha256,
                "lifecycle_identity_sha256": lifecycle,
                "container_identity_sha256": container,
                "container_present_before": present_before,
                "container_absent_after": absent_after,
                "identity_match": identity_match,
                "profile_match": profile_match,
                "ambiguous": ambiguous,
                "cleanup_receipt_sha256": cleanup_receipt,
            }
        )
        if (
            lifecycle != state.lifecycle_identity_sha256
            or not observed_container_is_exact
            or not absent_after
            or not identity_match
            or not profile_match
            or ambiguous
        ):
            raise QualificationIncompleteError(
                "qualification cleanup reconciliation is ambiguous or mismatched"
            )

    def resume_release_intent(self) -> _ReleaseIntent:
        """Reissue only the typed capability for an already durable intent."""

        state = self._require_status(QualificationUnitStatus.RELEASE_PENDING)
        assert state.release_intent_event_sha256 is not None
        return _ReleaseIntent(
            self._identity_sha256,
            state.release_intent_event_sha256,
            (
                state.lifecycle_identity_sha256,
                state.container_identity_sha256,
                True,
            ),
        )

    def mark_completed(self) -> None:
        state = self._require_status(QualificationUnitStatus.RELEASED)
        if state.result_event_sha256 is None:
            raise QualificationUnitError(
                "completed qualification unit requires a durable result"
            )
        self._append(
            {
                "type": "qualification_unit_completed",
                "binding_sha256": self.binding.sha256,
                "result_event_sha256": state.result_event_sha256,
                "release_completion_event_sha256": state.release_completion_event_sha256,
            }
        )

    def mark_aborted(self) -> None:
        state = self._require_status(QualificationUnitStatus.RELEASED)
        if state.result_event_sha256 is not None:
            raise QualificationUnitError(
                "durable qualification result must be retained, not aborted"
            )
        if self.binding.generation != 0:
            raise QualificationIncompleteError(
                "second pre-result interruption makes qualification incomplete"
            )
        self._append(
            {
                "type": "qualification_unit_aborted",
                "binding_sha256": self.binding.sha256,
                "reason": "pre_result_interruption",
                "release_completion_event_sha256": state.release_completion_event_sha256,
            }
        )

    def seal(self) -> None:
        state = self._state()
        if state.inspection.status not in (
            QualificationUnitStatus.COMPLETED,
            QualificationUnitStatus.ABORTED,
        ):
            raise QualificationUnitError(
                "qualification unit must be terminal before sealing"
            )
        if state.inspection.sealed:
            raise QualificationUnitError("qualification unit is already sealed")
        self._journal.seal(_parse_unit, maximum_bytes=_MAX_UNIT_BYTES)

    def _append(self, payload: Mapping[str, object]) -> dict[str, object]:
        return self._journal.append(
            payload,
            _parse_unit,
            maximum_bytes=_MAX_UNIT_BYTES,
        )

    def _state(self) -> _UnitState:
        state = _parse_unit(self._journal.read(maximum_bytes=_MAX_UNIT_BYTES))
        if state.binding != self.binding:
            raise QualificationUnitError("qualification unit binding changed")
        return state

    def _require_status(self, status: QualificationUnitStatus) -> _UnitState:
        state = self._state()
        if state.inspection.partial_tail_sha256 is not None:
            raise QualificationIncompleteError(
                "qualification unit has an unquarantined partial tail"
            )
        if state.inspection.sealed:
            raise QualificationUnitError("qualification unit is already sealed")
        if state.inspection.status is not status:
            raise QualificationUnitError(
                f"qualification unit must be {status.value}"
            )
        return state


@dataclass(frozen=True, slots=True)
class _UnitState:
    inspection: QualificationUnitInspection
    binding: QualificationUnitBinding
    acquire_intent_event_sha256: str | None
    acquire_completion_event_sha256: str | None
    release_intent_event_sha256: str | None
    release_completion_event_sha256: str | None
    result_event_sha256: str | None
    lifecycle_identity_sha256: str | None
    container_identity_sha256: str | None


def _parse_unit(data: bytes) -> _UnitState:
    generic = _generic_inspect(data, _MAX_UNIT_BYTES)
    records = _records(data, generic.complete_byte_length)
    if not records:
        raise QualificationUnitError("qualification unit journal is empty")
    start = records[0]
    _expect_keys(
        start,
        {
            "type",
            "unit_schema_version",
            "binding",
            "binding_sha256",
            "journal_file_identity_sha256",
            "journal_parent_identity_sha256",
        },
    )
    if (
        start["type"] != "qualification_unit_started"
        or start["unit_schema_version"] != 1
    ):
        raise QualificationUnitError("qualification unit genesis is invalid")
    binding = _binding_from_dict(start["binding"])
    if start["binding_sha256"] != binding.sha256:
        raise QualificationUnitError("qualification unit binding digest changed")
    _require_digest(start["journal_file_identity_sha256"], "journal identity")
    _require_digest(start["journal_parent_identity_sha256"], "parent identity")

    status = QualificationUnitStatus.STARTED
    acquire_intent: str | None = None
    acquire_completion: str | None = None
    release_intent: str | None = None
    release_completion: str | None = None
    result_event: str | None = None
    result_value: QualificationUnitResult | None = None
    lifecycle: str | None = None
    planned_locator: str | None = None
    container: str | None = None
    incomplete_reason: str | None = None
    terminal = False

    for record in records[1:]:
        event_type = record.get("type")
        event_sha = _event_tag(record)
        if event_type == "container_acquire_intent":
            _expect_keys(
                record,
                {
                    "type",
                    "binding_sha256",
                    "lifecycle_identity_sha256",
                    "planned_locator_sha256",
                    "image_sha256",
                },
            )
            if status is not QualificationUnitStatus.STARTED:
                raise QualificationUnitError("acquire intent order is invalid")
            if (
                record["binding_sha256"] != binding.sha256
                or record["image_sha256"] != binding.image_sha256
            ):
                raise QualificationUnitError("acquire intent binding changed")
            lifecycle = _require_digest(
                record["lifecycle_identity_sha256"], "lifecycle identity"
            )
            planned_locator = _require_digest(
                record["planned_locator_sha256"], "planned locator"
            )
            acquire_intent = event_sha
            status = QualificationUnitStatus.ACQUIRE_PENDING
            continue
        if event_type == "container_acquire_completed":
            _expect_keys(
                record,
                {
                    "type",
                    "intent_event_sha256",
                    "lifecycle_identity_sha256",
                    "planned_locator_sha256",
                    "container_identity_sha256",
                    "image_sha256",
                    "profile_sha256",
                    "acquisition_receipt_sha256",
                },
            )
            if status is not QualificationUnitStatus.ACQUIRE_PENDING:
                raise QualificationUnitError("acquire completion order is invalid")
            if (
                record["intent_event_sha256"] != acquire_intent
                or record["lifecycle_identity_sha256"] != lifecycle
                or record["planned_locator_sha256"] != planned_locator
                or record["image_sha256"] != binding.image_sha256
            ):
                raise QualificationUnitError("acquire completion identity changed")
            container = _require_digest(
                record["container_identity_sha256"], "container identity"
            )
            _require_digest(record["profile_sha256"], "container profile")
            _require_digest(
                record["acquisition_receipt_sha256"], "acquisition receipt"
            )
            acquire_completion = event_sha
            status = QualificationUnitStatus.ACQUIRED
            continue
        if event_type == "qualification_unit_result":
            _expect_keys(
                record,
                {
                    "type",
                    "acquire_completion_event_sha256",
                    *QualificationUnitResult.__dataclass_fields__,
                },
            )
            if status is not QualificationUnitStatus.ACQUIRED:
                raise QualificationUnitError("qualification result order is invalid")
            if record["acquire_completion_event_sha256"] != acquire_completion:
                raise QualificationUnitError("qualification result acquire hash changed")
            try:
                result_value = QualificationUnitResult(
                    **{
                        name: record[name]
                        for name in QualificationUnitResult.__dataclass_fields__
                    }
                )
            except (TypeError, ValueError) as error:
                raise QualificationUnitError(
                    "qualification result fields are invalid"
                ) from error
            result_event = event_sha
            status = QualificationUnitStatus.RESULT_DURABLE
            continue
        if event_type == "container_release_intent":
            _expect_keys(
                record,
                {
                    "type",
                    "binding_sha256",
                    "acquire_intent_event_sha256",
                    "acquire_completion_event_sha256",
                    "result_event_sha256",
                    "lifecycle_identity_sha256",
                    "container_identity_sha256",
                    "recovery",
                },
            )
            if status not in (
                QualificationUnitStatus.STARTED,
                QualificationUnitStatus.ACQUIRE_PENDING,
                QualificationUnitStatus.ACQUIRED,
                QualificationUnitStatus.RESULT_DURABLE,
            ):
                raise QualificationUnitError("release intent order is invalid")
            if (
                record["binding_sha256"] != binding.sha256
                or record["acquire_intent_event_sha256"] != acquire_intent
                or record["acquire_completion_event_sha256"] != acquire_completion
                or record["result_event_sha256"] != result_event
                or record["lifecycle_identity_sha256"] != lifecycle
                or record["container_identity_sha256"] != container
                or type(record["recovery"]) is not bool
                or (result_event is None and record["recovery"] is not True)
            ):
                raise QualificationUnitError("release intent identity changed")
            release_intent = event_sha
            status = QualificationUnitStatus.RELEASE_PENDING
            continue
        if event_type == "container_release_completed":
            _expect_keys(
                record,
                {
                    "type",
                    "intent_event_sha256",
                    "lifecycle_identity_sha256",
                    "container_identity_sha256",
                    "container_present_before",
                    "container_absent_after",
                    "identity_match",
                    "profile_match",
                    "ambiguous",
                    "cleanup_receipt_sha256",
                },
            )
            if status is not QualificationUnitStatus.RELEASE_PENDING:
                raise QualificationUnitError("release completion order is invalid")
            bool_fields = (
                "container_present_before",
                "container_absent_after",
                "identity_match",
                "profile_match",
                "ambiguous",
            )
            if any(type(record[name]) is not bool for name in bool_fields):
                raise QualificationUnitError("cleanup receipt flags are invalid")
            _require_digest(record["cleanup_receipt_sha256"], "cleanup receipt")
            observed_container = record["container_identity_sha256"]
            if observed_container is not None:
                _require_digest(observed_container, "observed container identity")
            expected_container = container
            observed_container_is_exact = (
                observed_container == expected_container
                if expected_container is not None
                else (
                    (
                        record["container_present_before"] is True
                        and observed_container is not None
                    )
                    or (
                        record["container_present_before"] is False
                        and observed_container is None
                    )
                )
            )
            if (
                record["intent_event_sha256"] != release_intent
                or record["lifecycle_identity_sha256"] != lifecycle
                or not observed_container_is_exact
            ):
                incomplete_reason = "cleanup_identity_mismatch"
            elif (
                record["container_absent_after"] is not True
                or record["identity_match"] is not True
                or record["profile_match"] is not True
                or record["ambiguous"] is not False
            ):
                incomplete_reason = "cleanup_ambiguous"
            elif container is None:
                container = observed_container
            release_completion = event_sha
            status = (
                QualificationUnitStatus.INCOMPLETE
                if incomplete_reason is not None
                else QualificationUnitStatus.RELEASED
            )
            continue
        if event_type == "qualification_unit_completed":
            _expect_keys(
                record,
                {
                    "type",
                    "binding_sha256",
                    "result_event_sha256",
                    "release_completion_event_sha256",
                },
            )
            if (
                status is not QualificationUnitStatus.RELEASED
                or result_event is None
                or record["binding_sha256"] != binding.sha256
                or record["result_event_sha256"] != result_event
                or record["release_completion_event_sha256"] != release_completion
            ):
                raise QualificationUnitError("unit completion evidence is invalid")
            status = QualificationUnitStatus.COMPLETED
            terminal = True
            continue
        if event_type == "qualification_unit_aborted":
            _expect_keys(
                record,
                {
                    "type",
                    "binding_sha256",
                    "reason",
                    "release_completion_event_sha256",
                },
            )
            if (
                status is not QualificationUnitStatus.RELEASED
                or result_event is not None
                or binding.generation != 0
                or record["binding_sha256"] != binding.sha256
                or record["reason"] != "pre_result_interruption"
                or record["release_completion_event_sha256"] != release_completion
            ):
                raise QualificationUnitError("unit abort evidence is invalid")
            status = QualificationUnitStatus.ABORTED
            terminal = True
            continue
        if event_type == SEALED_EVENT_TYPE:
            _expect_keys(record, {"type", "sealed_event_count"})
            if not terminal:
                raise QualificationUnitError("unit sealed before terminal state")
            continue
        raise QualificationUnitError("qualification unit event type is unsupported")

    partial = (
        None
        if generic.partial_tail is None
        else "sha256:" + generic.partial_tail.sha256
    )
    inspection = QualificationUnitInspection(
        binding=binding,
        status=status,
        generation=binding.generation,
        sealed=generic.sealed,
        event_types=tuple(str(record["type"]) for record in records),
        chain_sha256="sha256:" + generic.last_event_sha256,
        terminal_root_sha256=(
            "sha256:" + generic.last_event_sha256 if generic.sealed else None
        ),
        result_event_sha256=result_event,
        release_completion_event_sha256=release_completion,
        result=result_value,
        lifecycle_identity_sha256=lifecycle,
        container_identity_sha256=container,
        partial_tail_sha256=partial,
        incomplete_reason=incomplete_reason,
    )
    return _UnitState(
        inspection,
        binding,
        acquire_intent,
        acquire_completion,
        release_intent,
        release_completion,
        result_event,
        lifecycle,
        container,
    )


def _synthetic_acquire_receipt(
    intent: object,
    *,
    container_identity_sha256: str,
    image_sha256: str,
    profile_sha256: str,
    acquisition_receipt_sha256: str,
) -> _AcquireReceipt:
    """Issue a test-only typed receipt; never measured Docker evidence."""

    if type(intent) is not _AcquireIntent:
        raise QualificationUnitError("acquire receipt requires a typed intent")
    container = _require_digest(container_identity_sha256, "container identity")
    image = _require_digest(image_sha256, "image identity")
    profile = _require_digest(profile_sha256, "container profile")
    receipt = _require_digest(
        acquisition_receipt_sha256, "acquisition receipt"
    )
    return _AcquireReceipt(
        intent._journal_identity,
        intent._intent_event_sha256,
        (container, image, profile, receipt),
    )


def _issue_trusted_acquire_receipt(
    intent: object,
    *,
    container_identity_sha256: str,
    image_sha256: str,
    profile_sha256: str,
    acquisition_receipt_sha256: str,
) -> _AcquireReceipt:
    """Bind adapter-inspected acquisition facts to an exact journal intent."""

    return _synthetic_acquire_receipt(
        intent,
        container_identity_sha256=container_identity_sha256,
        image_sha256=image_sha256,
        profile_sha256=profile_sha256,
        acquisition_receipt_sha256=acquisition_receipt_sha256,
    )


def _synthetic_cleanup_receipt(
    intent: object,
    *,
    lifecycle_identity_sha256: str | None,
    container_identity_sha256: str | None,
    container_present_before: bool,
    container_absent_after: bool,
    identity_match: bool,
    profile_match: bool,
    ambiguous: bool,
    cleanup_receipt_sha256: str,
) -> _CleanupReceipt:
    """Issue a test-only exact presence/absence reconciliation receipt."""

    if type(intent) is not _ReleaseIntent:
        raise QualificationUnitError("cleanup receipt requires a typed intent")
    lifecycle = (
        None
        if lifecycle_identity_sha256 is None
        else _require_digest(lifecycle_identity_sha256, "lifecycle identity")
    )
    container = (
        None
        if container_identity_sha256 is None
        else _require_digest(container_identity_sha256, "container identity")
    )
    flags = (
        container_present_before,
        container_absent_after,
        identity_match,
        profile_match,
        ambiguous,
    )
    if any(type(value) is not bool for value in flags):
        raise QualificationUnitError("cleanup receipt flags must be boolean")
    receipt = _require_digest(cleanup_receipt_sha256, "cleanup receipt")
    return _CleanupReceipt(
        intent._journal_identity,
        intent._intent_event_sha256,
        (
            lifecycle,
            container,
            *flags,
            receipt,
        ),
    )


def _issue_trusted_cleanup_receipt(
    intent: object,
    *,
    lifecycle_identity_sha256: str | None,
    container_identity_sha256: str | None,
    container_present_before: bool,
    container_absent_after: bool,
    identity_match: bool,
    profile_match: bool,
    ambiguous: bool,
    cleanup_receipt_sha256: str,
) -> _CleanupReceipt:
    """Bind adapter-inspected cleanup facts to an exact journal intent."""

    return _synthetic_cleanup_receipt(
        intent,
        lifecycle_identity_sha256=lifecycle_identity_sha256,
        container_identity_sha256=container_identity_sha256,
        container_present_before=container_present_before,
        container_absent_after=container_absent_after,
        identity_match=identity_match,
        profile_match=profile_match,
        ambiguous=ambiguous,
        cleanup_receipt_sha256=cleanup_receipt_sha256,
    )


@dataclass(frozen=True, slots=True)
class _AggregateReplay:
    task_id: str
    stratum: str
    replay_index: int
    generation: int
    source_capability_sha256: str
    lifecycle_identity_sha256: str
    container_identity_sha256: str
    image_sha256: str
    evaluator_sha256: str
    state_normalization_sha256: str
    acquire_intent_event_sha256: str
    result_event_sha256: str
    cleanup_event_sha256: str
    terminal_root_sha256: str
    result: QualificationUnitResult


@dataclass(frozen=True, slots=True)
class _UnitSnapshot:
    binding: QualificationUnitBinding
    file_name: str
    journal_bytes: bytes


class _QualificationAggregateEvidence:
    __slots__ = (
        "_index_bytes",
        "_replays",
        "_unit_snapshots",
        "aggregate_recovery_count",
        "aggregate_root_sha256",
        "evaluator_sha256",
        "image_sha256_by_stratum",
        "source_corpus_sha256",
        "source_population_sha256",
        "state_normalization_sha256",
        "static_exclusion_audit_sha256",
    )

    def __init__(
        self,
        *,
        index_bytes: bytes,
        unit_snapshots: tuple[_UnitSnapshot, ...],
        replays: tuple[_AggregateReplay, ...],
        aggregate_root_sha256: str,
        aggregate_recovery_count: int,
        _construction_seal: object | None = None,
    ) -> None:
        if _construction_seal is not _AGGREGATE_SEAL:
            raise QualificationUnitError(
                "qualification aggregate evidence is builder-sealed"
            )
        self._index_bytes = bytes(index_bytes)
        self._unit_snapshots = unit_snapshots
        self._replays = replays
        self.aggregate_root_sha256 = aggregate_root_sha256
        self.aggregate_recovery_count = aggregate_recovery_count
        first = unit_snapshots[0].binding.expectation
        self.source_population_sha256 = first.source_population_sha256
        self.source_corpus_sha256 = first.source_corpus_sha256
        self.static_exclusion_audit_sha256 = first.static_exclusion_audit_sha256
        self.evaluator_sha256 = first.evaluator_sha256
        self.state_normalization_sha256 = first.state_normalization_sha256
        images: dict[str, str] = {}
        for snapshot in unit_snapshots:
            expectation = snapshot.binding.expectation
            existing = images.setdefault(expectation.stratum, expectation.image_sha256)
            if existing != expectation.image_sha256:
                raise QualificationUnitError("aggregate image pins are inconsistent")
        self.image_sha256_by_stratum = MappingProxyType(images)

    def __repr__(self) -> str:
        return (
            "<QualificationAggregateEvidence "
            f"root={self.aggregate_root_sha256} "
            f"recoveries={self.aggregate_recovery_count}>"
        )

    def __reduce__(self) -> object:
        raise TypeError("qualification aggregate evidence cannot be serialized")


class _SyntheticQualificationAggregateEvidence(_QualificationAggregateEvidence):
    """Test-harness aggregate; exact-type checks exclude it from formal scoring."""

    __slots__ = ("_synthetic_layout_marker",)

    def __init__(self, **values: object) -> None:
        super().__init__(**values)  # type: ignore[arg-type]
        self._synthetic_layout_marker = _UNIT_AUTHORITY


class QualificationUnitRepository:
    """Own deterministic unit paths and the one-generation retry rule."""

    __slots__ = ("root", "_expectations", "_formal")

    def __init__(
        self,
        root: Path,
        expectations: tuple[QualificationUnitExpectation, ...],
        formal: bool = False,
        *,
        _authority: object | None = None,
    ) -> None:
        if _authority is not _UNIT_AUTHORITY:
            raise QualificationUnitError(
                "qualification unit repositories are trusted-boundary owned"
            )
        self.root = root
        self._expectations = _validate_expectations(expectations)
        if type(formal) is not bool:
            raise QualificationUnitError("qualification evidence class is invalid")
        if formal and len(self._expectations) != 400:
            raise QualificationUnitError(
                "formal qualification requires exactly 400 logical units"
            )
        self._formal = formal
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise QualificationUnitError(
                "qualification unit directory could not be prepared"
            ) from error
        parent = _open_parent(root)
        os.close(parent)

    @property
    def expectations(self) -> tuple[QualificationUnitExpectation, ...]:
        return tuple(self._expectations.values())

    def open_or_start(self, key: QualificationUnitKey) -> QualificationUnitAttempt:
        expectation = self._expectation(key)
        generation_one = self._path(key, 1)
        generation_zero = self._path(key, 0)
        if generation_one.exists():
            return QualificationUnitAttempt._open(
                generation_one, expectation.binding(1)
            )
        if generation_zero.exists():
            return QualificationUnitAttempt._open(
                generation_zero, expectation.binding(0)
            )
        return QualificationUnitAttempt._create(
            generation_zero, expectation.binding(0)
        )

    def open_generation(
        self,
        key: QualificationUnitKey,
        generation: int,
    ) -> QualificationUnitAttempt:
        expectation = self._expectation(key)
        binding = expectation.binding(generation)
        return QualificationUnitAttempt._open(self._path(key, generation), binding)

    def start_retry(self, key: QualificationUnitKey) -> QualificationUnitAttempt:
        expectation = self._expectation(key)
        generation_one = self._path(key, 1)
        if generation_one.exists():
            raise QualificationIncompleteError(
                "second pre-result interruption makes qualification incomplete"
            )
        generation_zero = self._path(key, 0)
        if not generation_zero.exists():
            raise QualificationIncompleteError(
                "generation one cannot exist without generation zero"
            )
        first = QualificationUnitAttempt._open(
            generation_zero, expectation.binding(0)
        ).inspect(require_sealed=True)
        if first.status is not QualificationUnitStatus.ABORTED:
            raise QualificationIncompleteError(
                "only one sealed pre-result abort permits generation one"
            )
        return QualificationUnitAttempt._create(
            generation_one, expectation.binding(1)
        )

    def _create_generation_for_test(
        self,
        key: QualificationUnitKey,
        generation: int,
    ) -> QualificationUnitAttempt:
        expectation = self._expectation(key)
        return QualificationUnitAttempt._create(
            self._path(key, generation), expectation.binding(generation)
        )

    def seal_aggregate(self, index_path: str | Path) -> _QualificationAggregateEvidence:
        snapshots, replays, recovery_count, references = _collect_from_disk(
            self.root,
            self.expectations,
        )
        index = Path(index_path)
        if index.exists():
            journal = _ExactJournal.open(index, maximum_bytes=_MAX_INDEX_BYTES)
            index_bytes = journal.read(maximum_bytes=_MAX_INDEX_BYTES)
            root = _verify_index(
                index_bytes,
                self.expectations,
                references,
                recovery_count,
            )
        else:
            expectation_root = _canonical_sha256(
                [item.to_dict() for item in self.expectations]
            )

            def first(file_identity: str, parent_identity: str) -> dict[str, object]:
                return {
                    "type": "qualification_aggregate_started",
                    "aggregate_schema_version": 1,
                    "expectation_root_sha256": expectation_root,
                    "expected_unit_count": len(self.expectations),
                    "journal_file_identity_sha256": file_identity,
                    "journal_parent_identity_sha256": parent_identity,
                }

            journal = _ExactJournal.create(
                index,
                first,
                _parse_index_generic,
                maximum_bytes=_MAX_INDEX_BYTES,
            )
            for reference in references:
                journal.append(
                    {"type": "qualification_aggregate_unit", **reference},
                    _parse_index_generic,
                    maximum_bytes=_MAX_INDEX_BYTES,
                )
            ordered_root = _canonical_sha256(references)
            journal.append(
                {
                    "type": "qualification_aggregate_completed",
                    "ordered_units_sha256": ordered_root,
                    "completed_unit_count": len(references),
                    "aggregate_recovery_count": recovery_count,
                },
                _parse_index_generic,
                maximum_bytes=_MAX_INDEX_BYTES,
            )
            journal.seal(_parse_index_generic, maximum_bytes=_MAX_INDEX_BYTES)
            index_bytes = journal.read(maximum_bytes=_MAX_INDEX_BYTES)
            root = _verify_index(
                index_bytes,
                self.expectations,
                references,
                recovery_count,
            )
        evidence_type = (
            _QualificationAggregateEvidence
            if self._formal
            else _SyntheticQualificationAggregateEvidence
        )
        return evidence_type(
            index_bytes=index_bytes,
            unit_snapshots=snapshots,
            replays=replays,
            aggregate_root_sha256=root,
            aggregate_recovery_count=recovery_count,
            _construction_seal=_AGGREGATE_SEAL,
        )

    def _expectation(self, key: QualificationUnitKey) -> QualificationUnitExpectation:
        if type(key) is not QualificationUnitKey:
            raise QualificationUnitError("qualification key must use exact type")
        expectation = self._expectations.get(key)
        if expectation is None:
            raise QualificationUnitError("qualification key is not frozen")
        return expectation

    def _path(self, key: QualificationUnitKey, generation: int) -> Path:
        if type(generation) is not int or generation not in (0, 1):
            raise QualificationUnitError("qualification generation is invalid")
        return self.root / (
            f"unit-{key.task_id}-r{key.replay_index}-g{generation}.jsonl"
        )


def _create_synthetic_unit_repository(
    root: str | Path,
    expectations: tuple[QualificationUnitExpectation, ...],
) -> QualificationUnitRepository:
    """Create a synthetic state-machine harness, never formal Docker proof."""

    return QualificationUnitRepository(
        Path(root), expectations, formal=False, _authority=_UNIT_AUTHORITY
    )


def _create_trusted_unit_repository(
    root: str | Path,
    expectations: tuple[QualificationUnitExpectation, ...],
) -> QualificationUnitRepository:
    """Create the formal repository issued only by the Docker trust boundary."""

    return QualificationUnitRepository(
        Path(root), expectations, formal=True, _authority=_UNIT_AUTHORITY
    )


def _validate_expectations(
    expectations: tuple[QualificationUnitExpectation, ...],
) -> dict[QualificationUnitKey, QualificationUnitExpectation]:
    if type(expectations) is not tuple or not expectations:
        raise QualificationUnitError(
            "qualification expectations must be a non-empty frozen tuple"
        )
    indexed: dict[QualificationUnitKey, QualificationUnitExpectation] = {}
    for expectation in expectations:
        if type(expectation) is not QualificationUnitExpectation:
            raise QualificationUnitError(
                "qualification expectations must use exact typed values"
            )
        expectation.__post_init__()
        if expectation.key in indexed:
            raise QualificationUnitError("qualification expectation key is duplicated")
        indexed[expectation.key] = expectation
    canonical = tuple(sorted(indexed, key=lambda key: (key.task_id, key.replay_index)))
    if tuple(indexed) != canonical:
        raise QualificationUnitError("qualification expectations are not canonical")
    shared_fields = (
        "suite_name",
        "source_revision",
        "source_population_sha256",
        "source_corpus_sha256",
        "static_exclusion_audit_sha256",
        "evaluator_sha256",
        "state_normalization_sha256",
    )
    first = expectations[0]
    if any(
        getattr(item, field) != getattr(first, field)
        for item in expectations[1:]
        for field in shared_fields
    ):
        raise QualificationUnitError("qualification shared pins are inconsistent")
    return indexed


def _collect_from_disk(
    root: Path,
    expectations: tuple[QualificationUnitExpectation, ...],
) -> tuple[
    tuple[_UnitSnapshot, ...],
    tuple[_AggregateReplay, ...],
    int,
    list[dict[str, object]],
]:
    indexed = _validate_expectations(expectations)
    try:
        actual_names = {
            entry.name
            for entry in root.iterdir()
            if entry.name not in (".", "..")
        }
    except OSError as error:
        raise QualificationIncompleteError(
            "qualification unit directory cannot be read"
        ) from error
    allowed_names = {
        f"unit-{key.task_id}-r{key.replay_index}-g{generation}.jsonl"
        for key in indexed
        for generation in (0, 1)
    }
    if actual_names - allowed_names:
        raise QualificationIncompleteError(
            "qualification unit directory contains foreign evidence"
        )

    snapshots: list[_UnitSnapshot] = []
    replays: list[_AggregateReplay] = []
    references: list[dict[str, object]] = []
    recovery_count = 0
    seen_lifecycles: set[str] = set()
    seen_containers: set[str] = set()
    for key, expectation in indexed.items():
        generation_zero_path = root / (
            f"unit-{key.task_id}-r{key.replay_index}-g0.jsonl"
        )
        if not generation_zero_path.exists():
            raise QualificationIncompleteError(
                "qualification is missing a generation-zero unit"
            )
        first_bytes, first_state = _read_unit_snapshot(
            generation_zero_path,
            expectation.binding(0),
        )
        snapshots.append(
            _UnitSnapshot(expectation.binding(0), generation_zero_path.name, first_bytes)
        )
        _require_unit_terminal(first_state)
        generation_one_path = root / (
            f"unit-{key.task_id}-r{key.replay_index}-g1.jsonl"
        )
        selected = first_state
        selected_generation = 0
        if first_state.inspection.status is QualificationUnitStatus.ABORTED:
            if not generation_one_path.exists():
                raise QualificationIncompleteError(
                    "aborted generation zero lacks its one replacement"
                )
            second_bytes, second_state = _read_unit_snapshot(
                generation_one_path,
                expectation.binding(1),
            )
            snapshots.append(
                _UnitSnapshot(
                    expectation.binding(1), generation_one_path.name, second_bytes
                )
            )
            _require_unit_terminal(second_state)
            if second_state.inspection.status is not QualificationUnitStatus.COMPLETED:
                raise QualificationIncompleteError(
                    "replacement generation is not sealed completed"
                )
            selected = second_state
            selected_generation = 1
            recovery_count += 1
        elif first_state.inspection.status is QualificationUnitStatus.COMPLETED:
            if generation_one_path.exists():
                raise QualificationIncompleteError(
                    "completed generation zero has an illegal replacement"
                )
        else:
            raise QualificationIncompleteError(
                "generation zero is neither completed nor retryable-aborted"
            )

        for state in (first_state, selected) if selected is not first_state else (first_state,):
            lifecycle = state.lifecycle_identity_sha256
            container = state.container_identity_sha256
            if lifecycle is not None:
                if lifecycle in seen_lifecycles:
                    raise QualificationIncompleteError(
                        "qualification lifecycle identity is globally reused"
                    )
                seen_lifecycles.add(lifecycle)
            if container is not None:
                if container in seen_containers:
                    raise QualificationIncompleteError(
                        "qualification container identity is globally reused"
                    )
                seen_containers.add(container)

        inspection = selected.inspection
        if (
            inspection.result is None
            or inspection.result_event_sha256 is None
            or inspection.release_completion_event_sha256 is None
            or inspection.terminal_root_sha256 is None
            or selected.acquire_intent_event_sha256 is None
            or selected.lifecycle_identity_sha256 is None
            or selected.container_identity_sha256 is None
        ):
            raise QualificationIncompleteError(
                "completed qualification unit lacks exact lifecycle evidence"
            )
        replay = _AggregateReplay(
            task_id=expectation.task_id,
            stratum=expectation.stratum,
            replay_index=expectation.replay_index,
            generation=selected_generation,
            source_capability_sha256=expectation.source_capability_sha256,
            lifecycle_identity_sha256=selected.lifecycle_identity_sha256,
            container_identity_sha256=selected.container_identity_sha256,
            image_sha256=expectation.image_sha256,
            evaluator_sha256=expectation.evaluator_sha256,
            state_normalization_sha256=expectation.state_normalization_sha256,
            acquire_intent_event_sha256=selected.acquire_intent_event_sha256,
            result_event_sha256=inspection.result_event_sha256,
            cleanup_event_sha256=inspection.release_completion_event_sha256,
            terminal_root_sha256=inspection.terminal_root_sha256,
            result=inspection.result,
        )
        replays.append(replay)
        references.append(
            {
                "task_id": replay.task_id,
                "replay_index": replay.replay_index,
                "completed_generation": replay.generation,
                "unit_terminal_root_sha256": replay.terminal_root_sha256,
                "result_event_sha256": replay.result_event_sha256,
            }
        )
    return tuple(snapshots), tuple(replays), recovery_count, references


def _read_unit_snapshot(
    path: Path,
    expected_binding: QualificationUnitBinding,
) -> tuple[bytes, _UnitState]:
    journal = _ExactJournal.open(path, maximum_bytes=_MAX_UNIT_BYTES)
    data = journal.read(maximum_bytes=_MAX_UNIT_BYTES)
    state = _parse_unit(data)
    if state.binding != expected_binding:
        raise QualificationIncompleteError(
            "qualification unit binding differs from its canonical key"
        )
    if state.inspection.partial_tail_sha256 is not None:
        raise QualificationIncompleteError(
            "qualification unit has an unquarantined partial tail"
        )
    return data, state


def _require_unit_terminal(state: _UnitState) -> None:
    if (
        not state.inspection.sealed
        or state.inspection.status
        not in (QualificationUnitStatus.COMPLETED, QualificationUnitStatus.ABORTED)
    ):
        raise QualificationIncompleteError(
            "qualification unit is not sealed in a valid terminal state"
        )


def _parse_index_generic(data: bytes) -> tuple[dict[str, object], ...]:
    generic = _generic_inspect(data, _MAX_INDEX_BYTES)
    return tuple(_records(data, generic.complete_byte_length))


def _verify_index(
    data: bytes,
    expectations: tuple[QualificationUnitExpectation, ...],
    references: list[dict[str, object]],
    recovery_count: int,
) -> str:
    generic = _generic_inspect(data, _MAX_INDEX_BYTES)
    records = _records(data, generic.complete_byte_length)
    if not generic.sealed or generic.partial_tail is not None:
        raise QualificationIncompleteError(
            "qualification aggregate index is not complete and sealed"
        )
    if len(records) != len(references) + 3:
        raise QualificationUnitError("aggregate index record count is invalid")
    start = records[0]
    _expect_keys(
        start,
        {
            "type",
            "aggregate_schema_version",
            "expectation_root_sha256",
            "expected_unit_count",
            "journal_file_identity_sha256",
            "journal_parent_identity_sha256",
        },
    )
    if (
        start["type"] != "qualification_aggregate_started"
        or start["aggregate_schema_version"] != 1
        or start["expectation_root_sha256"]
        != _canonical_sha256([item.to_dict() for item in expectations])
        or start["expected_unit_count"] != len(expectations)
    ):
        raise QualificationUnitError("aggregate index genesis changed")
    _require_digest(start["journal_file_identity_sha256"], "index identity")
    _require_digest(start["journal_parent_identity_sha256"], "index parent")
    for record, expected in zip(records[1:-2], references, strict=True):
        _expect_keys(
            record,
            {
                "type",
                "task_id",
                "replay_index",
                "completed_generation",
                "unit_terminal_root_sha256",
                "result_event_sha256",
            },
        )
        if record != {
            **expected,
            "type": "qualification_aggregate_unit",
            "sequence": record["sequence"],
            "previous_event_sha256": record["previous_event_sha256"],
            "event_sha256": record["event_sha256"],
        }:
            raise QualificationUnitError(
                "aggregate unit reference differs from reread evidence"
            )
    completed = records[-2]
    _expect_keys(
        completed,
        {
            "type",
            "ordered_units_sha256",
            "completed_unit_count",
            "aggregate_recovery_count",
        },
    )
    if (
        completed["type"] != "qualification_aggregate_completed"
        or completed["ordered_units_sha256"] != _canonical_sha256(references)
        or completed["completed_unit_count"] != len(references)
        or completed["aggregate_recovery_count"] != recovery_count
    ):
        raise QualificationUnitError("aggregate completion evidence changed")
    seal = records[-1]
    _expect_keys(seal, {"type", "sealed_event_count"})
    if seal["type"] != SEALED_EVENT_TYPE:
        raise QualificationUnitError("aggregate index seal is missing")
    return "sha256:" + str(seal["event_sha256"])


def _reverify_aggregate_evidence(
    evidence: object,
    expectations: tuple[QualificationUnitExpectation, ...],
) -> _QualificationAggregateEvidence:
    if type(evidence) not in (
        _QualificationAggregateEvidence,
        _SyntheticQualificationAggregateEvidence,
    ):
        raise QualificationUnitError(
            "qualification evidence must be a sealed aggregate"
        )
    indexed = _validate_expectations(expectations)
    by_key_generation: dict[tuple[QualificationUnitKey, int], _UnitState] = {}
    references: list[dict[str, object]] = []
    replays: list[_AggregateReplay] = []
    recovery_count = 0
    for snapshot in evidence._unit_snapshots:
        if type(snapshot) is not _UnitSnapshot:
            raise QualificationUnitError("aggregate unit snapshot type changed")
        state = _parse_unit(snapshot.journal_bytes)
        if state.binding != snapshot.binding:
            raise QualificationUnitError("aggregate unit snapshot binding changed")
        expected_name = (
            f"unit-{state.binding.key.task_id}-r"
            f"{state.binding.key.replay_index}-g{state.binding.generation}.jsonl"
        )
        if snapshot.file_name != expected_name:
            raise QualificationUnitError("aggregate unit snapshot name changed")
        if state.inspection.partial_tail_sha256 is not None:
            raise QualificationUnitError("aggregate unit snapshot has a partial tail")
        key = state.binding.key
        if indexed.get(key) != state.binding.expectation:
            raise QualificationUnitError("aggregate contains a foreign unit")
        pair = (key, state.binding.generation)
        if pair in by_key_generation:
            raise QualificationUnitError("aggregate duplicates a unit generation")
        by_key_generation[pair] = state
    seen_lifecycles: set[str] = set()
    seen_containers: set[str] = set()
    for key, expectation in indexed.items():
        first = by_key_generation.get((key, 0))
        second = by_key_generation.get((key, 1))
        if first is None:
            raise QualificationUnitError("aggregate omits generation zero")
        _require_unit_terminal(first)
        if first.inspection.status is QualificationUnitStatus.ABORTED:
            if second is None:
                raise QualificationUnitError("aggregate omits replacement generation")
            _require_unit_terminal(second)
            if second.inspection.status is not QualificationUnitStatus.COMPLETED:
                raise QualificationUnitError("aggregate replacement is not completed")
            selected = second
            recovery_count += 1
        else:
            if first.inspection.status is not QualificationUnitStatus.COMPLETED or second:
                raise QualificationUnitError("aggregate generation chain is invalid")
            selected = first
        for state in (first, selected) if selected is not first else (first,):
            lifecycle = state.lifecycle_identity_sha256
            container = state.container_identity_sha256
            if lifecycle is not None:
                if lifecycle in seen_lifecycles:
                    raise QualificationUnitError(
                        "aggregate reuses a lifecycle identity"
                    )
                seen_lifecycles.add(lifecycle)
            if container is not None:
                if container in seen_containers:
                    raise QualificationUnitError(
                        "aggregate reuses a container identity"
                    )
                seen_containers.add(container)
        inspection = selected.inspection
        matching = next(
            (
                replay
                for replay in evidence._replays
                if replay.task_id == key.task_id
                and replay.replay_index == key.replay_index
            ),
            None,
        )
        if (
            matching is None
            or inspection.terminal_root_sha256 != matching.terminal_root_sha256
            or inspection.result_event_sha256 != matching.result_event_sha256
            or inspection.result != matching.result
        ):
            raise QualificationUnitError("aggregate replay differs from unit bytes")
        replays.append(matching)
        references.append(
            {
                "task_id": key.task_id,
                "replay_index": key.replay_index,
                "completed_generation": selected.binding.generation,
                "unit_terminal_root_sha256": matching.terminal_root_sha256,
                "result_event_sha256": matching.result_event_sha256,
            }
        )
    if len(evidence._replays) != len(expectations):
        raise QualificationUnitError("aggregate replay count changed")
    root = _verify_index(
        evidence._index_bytes,
        expectations,
        references,
        recovery_count,
    )
    if (
        root != evidence.aggregate_root_sha256
        or recovery_count != evidence.aggregate_recovery_count
    ):
        raise QualificationUnitError("aggregate root or recovery count changed")
    return evidence


def _binding_from_dict(value: object) -> QualificationUnitBinding:
    expectation_fields = set(QualificationUnitExpectation.__dataclass_fields__)
    expected = expectation_fields | {"generation", "logical_unit_sha256"}
    if type(value) is not dict or set(value) != expected:
        raise QualificationUnitError("qualification binding fields are invalid")
    try:
        expectation = QualificationUnitExpectation(
            **{name: value[name] for name in expectation_fields}
        )
        binding = QualificationUnitBinding(
            expectation=expectation,
            generation=value["generation"],
        )
    except (TypeError, ValueError) as error:
        raise QualificationUnitError("qualification binding is invalid") from error
    if value["logical_unit_sha256"] != binding.key.sha256:
        raise QualificationUnitError("qualification logical-unit key changed")
    return binding


def _append_locked(
    descriptor: int,
    data: bytes,
    payload: Mapping[str, object],
    validator: Callable[[bytes], object],
    *,
    maximum_bytes: int,
) -> tuple[dict[str, object], bytes]:
    generic = _generic_inspect(data, maximum_bytes)
    if generic.partial_tail is not None:
        raise QualificationIncompleteError(
            "qualification journal has an unquarantined partial tail"
        )
    if generic.sealed:
        raise QualificationUnitError("qualification journal is already sealed")
    if generic.record_count >= _MAX_RECORDS:
        raise QualificationUnitError("qualification journal record limit exceeded")
    if not isinstance(payload, Mapping) or any(
        type(key) is not str for key in payload
    ):
        raise QualificationUnitError("qualification event must be a string-key mapping")
    event_type = payload.get("type")
    if type(event_type) is not str or not event_type:
        raise QualificationUnitError("qualification event type must be non-empty")
    if any(field in payload for field in _CHAIN_FIELDS):
        raise QualificationUnitError("qualification event uses a reserved chain field")
    if event_type == SEALED_EVENT_TYPE:
        if payload.get("sealed_event_count") != generic.record_count:
            raise QualificationUnitError("qualification seal count is invalid")
    elif "sealed_event_count" in payload:
        raise QualificationUnitError("non-seal event has seal metadata")
    record: dict[str, object] = {
        **payload,
        "sequence": generic.next_sequence,
        "previous_event_sha256": generic.last_event_sha256,
    }
    record["event_sha256"] = sha256(canonical_event_bytes(record)).hexdigest()
    serialized = _canonical_json(record) + b"\n"
    if len(serialized) > MAX_JOURNAL_EVENT_BYTES:
        raise QualificationUnitError("qualification event exceeds byte limit")
    candidate = data + serialized
    if len(candidate) > maximum_bytes:
        raise QualificationUnitError("qualification journal exceeds byte limit")
    validator(candidate)
    if _verify_file(descriptor).st_size != len(data):
        raise QualificationUnitError(
            "qualification journal changed outside its lock domain"
        )
    os.lseek(descriptor, 0, os.SEEK_END)
    _write_all(descriptor, serialized)
    os.fsync(descriptor)
    return record, candidate


def _generic_inspect(data: bytes, maximum_bytes: int):
    if len(data) > maximum_bytes:
        raise QualificationUnitError("qualification journal exceeds byte limit")
    try:
        return _inspect_bytes(data)
    except JournalError as error:
        raise QualificationUnitError(
            "qualification journal hash chain or framing is invalid"
        ) from error


def _records(data: bytes, complete_byte_length: int) -> list[dict[str, object]]:
    try:
        return [
            json.loads(line)
            for line in data[:complete_byte_length].splitlines()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationUnitError(
            "qualification journal complete record is invalid"
        ) from error


def _expect_keys(record: Mapping[str, object], payload: set[str]) -> None:
    expected = payload | set(_CHAIN_FIELDS)
    if set(record) != expected:
        raise QualificationUnitError("qualification journal event schema drift")


def _event_tag(record: Mapping[str, object]) -> str:
    value = record.get("event_sha256")
    if type(value) is not str or _BARE_SHA256.fullmatch(value) is None:
        raise QualificationUnitError("qualification event digest is invalid")
    return "sha256:" + value


def _require_digest(value: object, field: str) -> str:
    if type(value) is not str or _TAGGED_SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 reference")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise QualificationUnitError(
            "qualification value is not canonical JSON"
        ) from error


def _canonical_sha256(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value)).hexdigest()


def _metadata_digest(metadata: os.stat_result, kind: str) -> str:
    return _canonical_sha256(
        {
            "kind": kind,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "owner": metadata.st_uid,
            "mode": stat.S_IMODE(metadata.st_mode),
            "link_count": metadata.st_nlink if kind == "file" else None,
        }
    )


def _identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(metadata.st_dev, metadata.st_ino, metadata.st_uid)


def _open_parent(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise QualificationUnitError(
            "platform lacks secure qualification directory opens"
        )
    try:
        descriptor = os.open(
            os.fspath(path),
            os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise QualificationUnitError(
            "qualification journal parent is unsafe"
        ) from error
    try:
        _verify_parent(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _verify_parent(descriptor: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise QualificationUnitError("qualification parent is not a directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise QualificationUnitError(
            "qualification parent must be owner-controlled"
        )
    return metadata


def _create_file(name: str, parent: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise QualificationUnitError("platform lacks no-follow file opens")
    try:
        return os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | nofollow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
    except OSError as error:
        raise QualificationUnitError(
            "qualification journal target is not a new regular file"
        ) from error


def _open_file(name: str, parent: int, *, writable: bool) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise QualificationUnitError("platform lacks no-follow file opens")
    try:
        return os.open(
            name,
            (os.O_RDWR if writable else os.O_RDONLY)
            | nofollow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent,
        )
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            raise QualificationUnitError(
                "qualification journal target is missing or unsafe"
            ) from error
        raise QualificationUnitError(
            "qualification journal could not be opened"
        ) from error


def _verify_file(descriptor: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise QualificationUnitError("qualification journal is not regular")
    if metadata.st_uid != os.geteuid():
        raise QualificationUnitError("qualification journal owner changed")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise QualificationUnitError(
            "qualification journal must have exact mode 0600"
        )
    if metadata.st_nlink != 1:
        raise QualificationUnitError(
            "qualification journal must have exactly one link"
        )
    return metadata


def _verify_link(name: str, parent: int, expected_sha256: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as error:
        raise QualificationUnitError(
            "qualification journal link disappeared"
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or _metadata_digest(metadata, "file") != expected_sha256
    ):
        raise QualificationUnitError(
            "qualification journal link identity changed"
        )


def _read_descriptor(descriptor: int, maximum_bytes: int) -> bytes:
    metadata = _verify_file(descriptor)
    if metadata.st_size > maximum_bytes:
        raise QualificationUnitError("qualification journal exceeds byte limit")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining:
        try:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
        except InterruptedError:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) > maximum_bytes:
        raise QualificationUnitError("qualification journal exceeds byte limit")
    if _verify_file(descriptor).st_size != len(data):
        raise QualificationUnitError(
            "qualification journal changed outside its lock domain"
        )
    return data


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(descriptor, remaining)
        except InterruptedError:
            continue
        if written <= 0 or written > len(remaining):
            raise OSError("qualification journal write made no progress")
        remaining = remaining[written:]


__all__ = (
    "QualificationIncompleteError",
    "QualificationUnitError",
    "QualificationUnitExpectation",
    "QualificationUnitKey",
    "QualificationUnitResult",
    "QualificationUnitStatus",
)
