"""Trusted, crash-evident qualification for the pinned InterCode Bash corpus.

The public manifest is deliberately not the private evidence format.  A trusted
collector appends source-capability-bound replay lifecycles to a hash-chained
JSONL journal.  Aggregation reparses that sealed journal and emits a redacted
manifest containing only inclusion decisions and aggregate provenance roots.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from edgeloopbench.intercode_source import (
    CALIBRATION_POPULATION_SHA256,
    EXPECTED_CALIBRATION_COUNT,
    EXPECTED_SOURCE_COUNTS,
    INTERCODE_REVISION,
    PUBLIC_POPULATION_SHA256,
    SOURCE_CORPUS_SHA256,
    STATIC_EXCLUSION_AUDIT_SHA256,
    InterCodeSource,
    InterCodeSourceError,
    PrivateTaskReference,
    PublicBashTask,
)
from edgeloopbench.intercode_qualification_units import (
    QualificationIncompleteError,
    QualificationUnitAttempt,
    QualificationUnitError,
    QualificationUnitExpectation,
    QualificationUnitKey,
    QualificationUnitResult,
    QualificationUnitStatus,
    _AggregateReplay,
    _QualificationAggregateEvidence,
    _create_trusted_unit_repository,
    _issue_trusted_acquire_receipt,
    _issue_trusted_cleanup_receipt,
    _reverify_aggregate_evidence,
)
from edgeloopbench.journal import (
    GENESIS_EVENT_SHA256,
    MAX_JOURNAL_EVENT_BYTES,
    SEALED_EVENT_TYPE,
    JournalError,
    append_journal_event,
    canonical_event_bytes,
    inspect_journal,
    seal_journal,
)


QUALIFIED_SUITE_NAME = "InterCode-Bash-qualified@c3e46d8"
QUALIFICATION_SCHEMA_VERSION = 3
QUALIFICATION_JOURNAL_SCHEMA_VERSION = 1
MIN_QUALIFIED_COUNT = 160
MIN_QUALIFIED_PER_STRATUM: Mapping[str, int] = MappingProxyType(
    {"fs1": 48, "fs2": 42, "fs3": 48, "fs4": 21}
)
MAX_QUALIFICATION_JOURNAL_BYTES = 64 * 1024 * 1024

# Frozen after an evaluator-side audit of the pinned private source corpus.
# Only task IDs are retained here: no private command, output, or diagnostic is
# representable in either the code constant or the committed audit artifact.
CLOCK_DEPENDENT_TASK_IDS: tuple[str, ...] = (
    "bash-fs1-011",
    "bash-fs1-012",
    "bash-fs1-046",
    "bash-fs1-047",
    "bash-fs2-017",
    "bash-fs2-018",
    "bash-fs2-019",
    "bash-fs2-025",
    "bash-fs2-032",
    "bash-fs2-039",
    "bash-fs2-041",
    "bash-fs2-042",
    "bash-fs3-000",
    "bash-fs3-002",
    "bash-fs3-003",
    "bash-fs3-012",
    "bash-fs4-018",
    "bash-fs4-021",
    "bash-fs4-022",
)
UNSUPPORTED_METADATA_TASK_IDS: tuple[str, ...] = ("bash-fs1-018",)

_STRATA = tuple(EXPECTED_SOURCE_COUNTS)
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID_PATTERN = re.compile(r"^bash-(fs[1-4])-[0-9]{3}$")
_CLOCK_DEPENDENT_TASK_ID_SET = frozenset(CLOCK_DEPENDENT_TASK_IDS)
_UNSUPPORTED_METADATA_TASK_ID_SET = frozenset(UNSUPPORTED_METADATA_TASK_IDS)
_GOLD_REPLAY_CONSTRUCTION_SEAL = object()
_EVIDENCE_CONSTRUCTION_SEAL = object()
_INTENT_CONSTRUCTION_SEAL = object()
_MANIFEST_CONSTRUCTION_SEAL = object()
_TRUSTED_QUALIFICATION_AUTHORITY = object()
_CHAIN_FIELDS = frozenset(
    {"sequence", "previous_event_sha256", "event_sha256"}
)

__all__ = (
    "CLOCK_DEPENDENT_TASK_IDS",
    "MIN_QUALIFIED_COUNT",
    "MIN_QUALIFIED_PER_STRATUM",
    "QUALIFIED_SUITE_NAME",
    "QUALIFICATION_SCHEMA_VERSION",
    "STATIC_EXCLUSION_AUDIT_SHA256",
    "UNSUPPORTED_METADATA_TASK_IDS",
    "GoldReplay",
    "QualificationError",
    "QualificationManifest",
    "QualificationReason",
    "QualificationRecord",
    "build_qualification_manifest",
)


class QualificationError(ValueError):
    """A qualification input or provenance gate is incomplete or unsafe."""


class QualificationReason(str, Enum):
    CLOCK_DEPENDENT = "clock_dependent"
    UNSUPPORTED_METADATA = "unsupported_metadata"
    NETWORK_REQUIRED = "network_required"
    SETUP_INVALID = "setup_invalid"
    UNSUPPORTED_STATE = "unsupported_state"
    EVALUATOR_INVALID = "evaluator_invalid"
    INFRASTRUCTURE_INVALID = "infrastructure_invalid"
    GOLD_EXIT_POLICY_FAILED = "gold_exit_policy_failed"
    OFFICIAL_REWARD_FAILED = "official_reward_failed"
    STRICT_GOLD_REPLAY_FAILED = "strict_gold_replay_failed"
    NONDETERMINISTIC_INITIAL_STATE = "nondeterministic_initial_state"
    NONDETERMINISTIC_OUTPUT = "nondeterministic_output"
    NONDETERMINISTIC_OBSERVABLE_STATE = "nondeterministic_observable_state"


_REASON_ORDER = tuple(QualificationReason)
_STATIC_EXCLUSION_REASON_MAP: Mapping[str, tuple[QualificationReason, ...]] = (
    MappingProxyType(
        {
            **{
                task_id: (QualificationReason.CLOCK_DEPENDENT,)
                for task_id in CLOCK_DEPENDENT_TASK_IDS
            },
            **{
                task_id: (QualificationReason.UNSUPPORTED_METADATA,)
                for task_id in UNSUPPORTED_METADATA_TASK_IDS
            },
        }
    )
)


def _require_sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise QualificationError(f"{field} must be a lowercase SHA-256 reference")
    return value


def _require_bare_sha256(value: object, field: str) -> str:
    if type(value) is not str or _BARE_SHA256_PATTERN.fullmatch(value) is None:
        raise QualificationError(f"{field} must be lowercase SHA-256")
    return value


def _require_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise QualificationError(f"{field} must be boolean")
    return value


def _require_replay_index(value: object) -> int:
    if type(value) is not int or value not in (1, 2):
        raise QualificationError("replay_index must be exactly 1 or 2")
    return value


@dataclass(frozen=True, slots=True)
class _QualificationReplayResult:
    """Gold-free typed output accepted from the trusted replay runner."""

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
        for field in (
            "infrastructure_valid",
            "setup_valid",
            "network_required",
            "state_supported",
            "evaluator_valid",
            "exit_policy_passed",
            "strict_success",
        ):
            _require_bool(getattr(self, field), field)
        if (
            isinstance(self.official_reward, bool)
            or not isinstance(self.official_reward, (int, float))
            or not math.isfinite(float(self.official_reward))
            or not 0.0 <= float(self.official_reward) <= 1.0
        ):
            raise QualificationError("official_reward must be finite in [0, 1]")
        _require_sha256(self.initial_state_sha256, "initial_state_sha256")
        _require_sha256(self.normalized_output_sha256, "normalized_output_sha256")
        _require_sha256(self.observable_state_sha256, "observable_state_sha256")

    def _private_record(self) -> dict[str, object]:
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
class GoldReplay:
    """Collector-sealed private replay evidence; never a public manifest row."""

    task_id: str
    stratum: str
    replay_index: int
    source_capability_sha256: str
    lifecycle_identity_sha256: str
    container_identity_sha256: str
    image_sha256: str
    evaluator_sha256: str
    state_normalization_sha256: str
    intent_event_sha256: str
    result_event_sha256: str
    cleanup_event_sha256: str
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
    _collector_seal: InitVar[object | None] = None

    def __post_init__(self, _collector_seal: object | None) -> None:
        if _collector_seal is not _GOLD_REPLAY_CONSTRUCTION_SEAL:
            raise QualificationError(
                "GoldReplay construction is collector-sealed"
            )
        match = (
            _TASK_ID_PATTERN.fullmatch(self.task_id)
            if type(self.task_id) is str
            else None
        )
        if match is None:
            raise QualificationError("replay task_id is not canonical")
        if self.stratum not in _STRATA or match.group(1) != self.stratum:
            raise QualificationError("replay stratum differs from its task ID")
        _require_replay_index(self.replay_index)
        for field in (
            "source_capability_sha256",
            "lifecycle_identity_sha256",
            "container_identity_sha256",
            "image_sha256",
            "evaluator_sha256",
            "state_normalization_sha256",
            "intent_event_sha256",
            "result_event_sha256",
            "cleanup_event_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        _QualificationReplayResult(
            infrastructure_valid=self.infrastructure_valid,
            setup_valid=self.setup_valid,
            network_required=self.network_required,
            state_supported=self.state_supported,
            evaluator_valid=self.evaluator_valid,
            exit_policy_passed=self.exit_policy_passed,
            official_reward=self.official_reward,
            strict_success=self.strict_success,
            initial_state_sha256=self.initial_state_sha256,
            normalized_output_sha256=self.normalized_output_sha256,
            observable_state_sha256=self.observable_state_sha256,
        )


class _QualificationReplayIntent:
    """Opaque collector-owned handle for one durable replay intent."""

    __slots__ = (
        "_acquire_intent",
        "_attempt",
        "_collector_identity",
        "task_id",
        "stratum",
        "replay_index",
        "source_capability_sha256",
        "lifecycle_identity_sha256",
        "container_identity_sha256",
        "planned_locator_sha256",
        "image_sha256",
        "evaluator_sha256",
        "state_normalization_sha256",
        "intent_event_sha256",
    )

    def __init__(
        self,
        *,
        collector_identity: object,
        task_id: str,
        stratum: str,
        replay_index: int,
        source_capability_sha256: str,
        lifecycle_identity_sha256: str,
        planned_locator_sha256: str,
        image_sha256: str,
        evaluator_sha256: str,
        state_normalization_sha256: str,
        intent_event_sha256: str,
        attempt: QualificationUnitAttempt,
        acquire_intent: object,
        _construction_seal: object | None = None,
    ) -> None:
        if _construction_seal is not _INTENT_CONSTRUCTION_SEAL:
            raise QualificationError("qualification intents are collector-owned")
        self._collector_identity = collector_identity
        self.task_id = task_id
        self.stratum = stratum
        self.replay_index = replay_index
        self.source_capability_sha256 = source_capability_sha256
        self.lifecycle_identity_sha256 = lifecycle_identity_sha256
        self.container_identity_sha256: str | None = None
        self.planned_locator_sha256 = planned_locator_sha256
        self.image_sha256 = image_sha256
        self.evaluator_sha256 = evaluator_sha256
        self.state_normalization_sha256 = state_normalization_sha256
        self.intent_event_sha256 = intent_event_sha256
        self._attempt = attempt
        self._acquire_intent = acquire_intent

    def __repr__(self) -> str:
        return "<QualificationReplayIntent opaque>"

    def __reduce__(self) -> Any:
        raise TypeError("qualification replay intents cannot be serialized")


class _QualificationEvidence:
    """Verified private journal snapshot with no public path or gold field."""

    __slots__ = (
        "_journal_bytes",
        "_replays",
        "evaluator_sha256",
        "image_sha256_by_stratum",
        "journal_root_sha256",
        "source_corpus_sha256",
        "source_population_sha256",
        "state_normalization_sha256",
        "static_exclusion_audit_sha256",
    )

    def __init__(
        self,
        *,
        journal_bytes: bytes,
        replays: tuple[GoldReplay, ...],
        journal_root_sha256: str,
        source_population_sha256: str,
        source_corpus_sha256: str,
        static_exclusion_audit_sha256: str,
        image_sha256_by_stratum: Mapping[str, str],
        evaluator_sha256: str,
        state_normalization_sha256: str,
        _construction_seal: object | None = None,
    ) -> None:
        if _construction_seal is not _EVIDENCE_CONSTRUCTION_SEAL:
            raise QualificationError("qualification evidence is collector-sealed")
        self._journal_bytes = bytes(journal_bytes)
        self._replays = replays
        self.journal_root_sha256 = journal_root_sha256
        self.source_population_sha256 = source_population_sha256
        self.source_corpus_sha256 = source_corpus_sha256
        self.static_exclusion_audit_sha256 = static_exclusion_audit_sha256
        self.image_sha256_by_stratum = MappingProxyType(
            dict(image_sha256_by_stratum)
        )
        self.evaluator_sha256 = evaluator_sha256
        self.state_normalization_sha256 = state_normalization_sha256

    def private_bytes(self) -> bytes:
        """Return the complete private sealed JSONL evidence."""

        return self._journal_bytes

    def __repr__(self) -> str:
        return (
            "<QualificationEvidence "
            f"root={self.journal_root_sha256} replays={len(self._replays)}>"
        )


@dataclass(frozen=True, slots=True)
class QualificationRecord:
    """One redacted public inclusion decision."""

    task_id: str
    stratum: str
    included: bool
    exclusion_reasons: tuple[QualificationReason, ...]

    def __post_init__(self) -> None:
        match = (
            _TASK_ID_PATTERN.fullmatch(self.task_id)
            if type(self.task_id) is str
            else None
        )
        if match is None or self.stratum not in _STRATA or match.group(1) != self.stratum:
            raise QualificationError("qualification record identity is invalid")
        _require_bool(self.included, "qualification included flag")
        if type(self.exclusion_reasons) is not tuple:
            raise QualificationError("qualification reasons must be a frozen tuple")
        if any(type(reason) is not QualificationReason for reason in self.exclusion_reasons):
            raise QualificationError(
                "qualification reasons must be QualificationReason values"
            )
        if len(set(self.exclusion_reasons)) != len(self.exclusion_reasons):
            raise QualificationError("qualification reasons must be unique")
        if tuple(sorted(self.exclusion_reasons, key=_REASON_ORDER.index)) != self.exclusion_reasons:
            raise QualificationError("qualification reasons are not canonical")
        if self.included != (not self.exclusion_reasons):
            raise QualificationError("included must equal absence of exclusion reasons")

    def _public_record(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "stratum": self.stratum,
            "included": self.included,
            "exclusion_reasons": [reason.value for reason in self.exclusion_reasons],
        }


@dataclass(frozen=True, slots=True)
class QualificationManifest:
    """Redacted public qualification manifest."""

    suite_name: str
    source_revision: str
    source_population_sha256: str
    source_corpus_sha256: str
    static_exclusion_audit_sha256: str
    evidence_root_sha256: str
    aggregate_recovery_count: int
    image_sha256_by_stratum: Mapping[str, str]
    evaluator_sha256: str
    state_normalization_sha256: str
    records: tuple[QualificationRecord, ...]
    qualified_count: int
    qualified_by_stratum: Mapping[str, int]
    scoring_admitted: bool
    suite_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _MANIFEST_CONSTRUCTION_SEAL:
            raise QualificationError("qualification manifests are aggregator-sealed")
        images = _validate_manifest(self)
        object.__setattr__(self, "image_sha256_by_stratum", MappingProxyType(images))
        object.__setattr__(
            self,
            "qualified_by_stratum",
            MappingProxyType(dict(self.qualified_by_stratum)),
        )

    def canonical_bytes(self) -> bytes:
        """Return only the redacted public projection."""

        _validate_manifest(self)
        record = _qualification_public_core_record(self)
        record["suite_sha256"] = self.suite_sha256
        return _canonical_json(record)

    def require_scoring_admitted(self) -> None:
        _validate_manifest(self)
        if self.scoring_admitted:
            return
        failures: list[str] = []
        if self.qualified_count < MIN_QUALIFIED_COUNT:
            failures.append(f"total {self.qualified_count} < {MIN_QUALIFIED_COUNT}")
        for stratum, minimum in MIN_QUALIFIED_PER_STRATUM.items():
            actual = self.qualified_by_stratum[stratum]
            if actual < minimum:
                failures.append(f"{stratum} {actual} < {minimum}")
        raise QualificationError(
            "confirmatory scoring is not admitted: " + ", ".join(failures)
        )


def _qualification_unit_expectations(
    source: InterCodeSource,
    images: Mapping[str, str],
    evaluator_sha256: str,
    state_normalization_sha256: str,
) -> tuple[QualificationUnitExpectation, ...]:
    expectations: list[QualificationUnitExpectation] = []
    try:
        for task in source.tasks:
            reference = source.private_reference(task.task_id)
            bound_task, capability = source.qualification_identity(reference)
            if bound_task != task:
                raise QualificationError(
                    "source capability resolves to a different qualification task"
                )
            for replay_index in (1, 2):
                expectations.append(
                    QualificationUnitExpectation(
                        task_id=task.task_id,
                        stratum=task.stratum,
                        replay_index=replay_index,
                        source_capability_sha256=capability,
                        suite_name=QUALIFIED_SUITE_NAME,
                        source_revision=INTERCODE_REVISION,
                        source_population_sha256=PUBLIC_POPULATION_SHA256,
                        source_corpus_sha256=SOURCE_CORPUS_SHA256,
                        static_exclusion_audit_sha256=STATIC_EXCLUSION_AUDIT_SHA256,
                        image_sha256=images[task.stratum],
                        evaluator_sha256=evaluator_sha256,
                        state_normalization_sha256=state_normalization_sha256,
                    )
                )
    except (InterCodeSourceError, KeyError, ValueError) as error:
        raise QualificationError(
            "qualification expectations cannot be source-capability-bound"
        ) from error
    return tuple(expectations)


class _QualificationEvidenceCollector:
    """Trusted adapter facade over the independent logical-unit journals."""

    __slots__ = (
        "_active_intent",
        "_active_result",
        "_collector_identity",
        "_journal_path",
        "_repository",
        "_sealed",
        "_source",
        "_used_container_identities",
        "_used_lifecycle_identities",
        "evaluator_sha256",
        "image_sha256_by_stratum",
        "state_normalization_sha256",
    )

    def __init__(
        self,
        *,
        source: InterCodeSource,
        journal_path: str | Path,
        image_sha256_by_stratum: Mapping[str, str],
        evaluator_sha256: str,
        state_normalization_sha256: str,
        _authority: object | None = None,
    ) -> None:
        if _authority is not _TRUSTED_QUALIFICATION_AUTHORITY:
            raise QualificationError(
                "qualification collectors are reserved for the trusted Docker adapter"
            )
        _validate_source(source)
        _validate_static_exclusion_binding(source)
        images = _validate_image_pins(image_sha256_by_stratum)
        evaluator = _require_sha256(evaluator_sha256, "evaluator_sha256")
        normalizer = _require_sha256(
            state_normalization_sha256, "state_normalization_sha256"
        )
        path = Path(journal_path)
        self._source = source
        self._journal_path = path
        self.image_sha256_by_stratum = MappingProxyType(images)
        self.evaluator_sha256 = evaluator
        self.state_normalization_sha256 = normalizer
        self._collector_identity = object()
        self._active_intent: _QualificationReplayIntent | None = None
        self._active_result: tuple[_QualificationReplayResult, str] | None = None
        self._used_lifecycle_identities: set[str] = set()
        self._used_container_identities: set[str] = set()
        self._sealed = False
        try:
            self._repository = _create_trusted_unit_repository(
                Path(f"{path}.units"),
                _qualification_unit_expectations(
                    source,
                    images,
                    evaluator,
                    normalizer,
                ),
            )
        except QualificationUnitError as error:
            raise QualificationError("qualification unit repository is unsafe") from error

    def _prepare_replay(
        self,
        *,
        task_reference: PrivateTaskReference,
        replay_index: int,
        lifecycle_identity_sha256: str,
        planned_locator_sha256: str,
    ) -> _QualificationReplayIntent:
        self._require_open()
        if self._active_intent is not None:
            raise QualificationError("an active replay must finish before the next intent")
        replay = _require_replay_index(replay_index)
        lifecycle = _require_sha256(
            lifecycle_identity_sha256, "lifecycle identity"
        )
        planned_locator = _require_sha256(
            planned_locator_sha256, "planned deterministic locator"
        )
        if lifecycle in self._used_lifecycle_identities:
            raise QualificationError("lifecycle identity must be globally distinct")
        if self._journal_path.exists():
            raise QualificationError("qualification aggregate is already sealed")
        try:
            task, source_capability_sha256 = self._source.qualification_identity(
                task_reference
            )
        except InterCodeSourceError as error:
            raise QualificationError(
                "task reference is not a source-owned qualification capability"
            ) from error
        if task not in self._source.tasks:
            raise QualificationError("calibration tasks cannot enter qualification")
        image = self.image_sha256_by_stratum[task.stratum]
        key = QualificationUnitKey(task.task_id, replay)
        try:
            attempt = self._repository.open_or_start(key)
            inspection = attempt.inspect()
            if inspection.sealed:
                if inspection.status is QualificationUnitStatus.ABORTED:
                    attempt = self._repository.start_retry(key)
                    inspection = attempt.inspect()
                else:
                    raise QualificationError("qualification replay already completed")
            if inspection.status is not QualificationUnitStatus.STARTED:
                raise QualificationError(
                    "qualification replay requires explicit recovery reconciliation"
                )
            acquire_intent = attempt.record_acquire_intent(
                lifecycle_identity_sha256=lifecycle,
                planned_locator_sha256=planned_locator,
            )
        except QualificationUnitError as error:
            raise QualificationError("qualification replay intent failed") from error
        intent = _QualificationReplayIntent(
            collector_identity=self._collector_identity,
            task_id=task.task_id,
            stratum=task.stratum,
            replay_index=replay,
            source_capability_sha256=source_capability_sha256,
            lifecycle_identity_sha256=lifecycle,
            planned_locator_sha256=planned_locator,
            image_sha256=image,
            evaluator_sha256=self.evaluator_sha256,
            state_normalization_sha256=self.state_normalization_sha256,
            intent_event_sha256=acquire_intent._intent_event_sha256,
            attempt=attempt,
            acquire_intent=acquire_intent,
            _construction_seal=_INTENT_CONSTRUCTION_SEAL,
        )
        self._used_lifecycle_identities.add(lifecycle)
        self._active_intent = intent
        self._active_result = None
        return intent

    def _record_acquisition(
        self,
        intent: _QualificationReplayIntent,
        *,
        container_identity_sha256: str,
        profile_sha256: str,
        acquisition_receipt_sha256: str,
    ) -> None:
        """Complete a previously durable intent from inspected create facts."""

        self._require_active(intent)
        container = _require_sha256(
            container_identity_sha256, "container identity"
        )
        profile = _require_sha256(profile_sha256, "container profile")
        acquisition_receipt = _require_sha256(
            acquisition_receipt_sha256, "acquisition receipt"
        )
        if intent.container_identity_sha256 is not None:
            raise QualificationError("qualification acquisition is already recorded")
        if container in self._used_container_identities:
            raise QualificationError("container identity must be globally distinct")
        try:
            receipt = _issue_trusted_acquire_receipt(
                intent._acquire_intent,
                container_identity_sha256=container,
                image_sha256=intent.image_sha256,
                profile_sha256=profile,
                acquisition_receipt_sha256=acquisition_receipt,
            )
            intent._attempt.record_acquire_completion(
                intent._acquire_intent,
                receipt,
            )
        except QualificationUnitError as error:
            raise QualificationError(
                "qualification replay acquisition failed"
            ) from error
        intent.container_identity_sha256 = container
        self._used_container_identities.add(container)

    def _record_result(
        self,
        intent: _QualificationReplayIntent,
        result: _QualificationReplayResult,
    ) -> None:
        self._require_active(intent)
        if type(result) is not _QualificationReplayResult:
            raise QualificationError("replay result must use the exact typed surface")
        result.__post_init__()
        if self._active_result is not None:
            raise QualificationError("replay result is already recorded")
        if intent.container_identity_sha256 is None:
            raise QualificationError(
                "qualification result cannot precede acquisition completion"
            )
        try:
            result_event_sha256 = intent._attempt.record_result(
                QualificationUnitResult(**result._private_record())
            )
        except (QualificationUnitError, ValueError) as error:
            raise QualificationError("qualification result could not be recorded") from error
        self._active_result = (result, result_event_sha256)

    def _record_cleanup(
        self,
        intent: _QualificationReplayIntent,
        *,
        container_destroyed: bool,
        cleanup_verified: bool,
    ) -> GoldReplay:
        self._require_active(intent)
        destroyed = _require_bool(container_destroyed, "container_destroyed")
        verified = _require_bool(cleanup_verified, "cleanup_verified")
        if self._active_result is None:
            raise QualificationError("cleanup cannot precede the replay result")
        container = intent.container_identity_sha256
        if container is None:
            raise QualificationError("cleanup cannot precede acquisition completion")
        result, result_event_sha256 = self._active_result
        try:
            release_intent = intent._attempt.record_release_intent(recovery=False)
            cleanup_receipt = _canonical_sha256(
                {
                    "kind": "qualification_container_cleanup",
                    "task_id": intent.task_id,
                    "replay_index": intent.replay_index,
                    "lifecycle_identity_sha256": intent.lifecycle_identity_sha256,
                    "container_identity_sha256": container,
                    "container_destroyed": destroyed,
                    "cleanup_verified": verified,
                },
                "qualification cleanup receipt",
            )
            receipt = _issue_trusted_cleanup_receipt(
                release_intent,
                lifecycle_identity_sha256=intent.lifecycle_identity_sha256,
                container_identity_sha256=container,
                container_present_before=True,
                container_absent_after=destroyed,
                identity_match=True,
                profile_match=verified,
                ambiguous=not verified,
                cleanup_receipt_sha256=cleanup_receipt,
            )
            intent._attempt.record_release_completion(release_intent, receipt)
            intent._attempt.mark_completed()
            intent._attempt.seal()
            inspection = intent._attempt.inspect(require_sealed=True)
        except QualificationUnitError as error:
            raise QualificationError(
                "qualification replay cleanup was not verified"
            ) from error
        if (
            inspection.release_completion_event_sha256 is None
            or inspection.result_event_sha256 != result_event_sha256
        ):
            raise QualificationError("qualification replay terminal evidence changed")
        replay = GoldReplay(
            task_id=intent.task_id,
            stratum=intent.stratum,
            replay_index=intent.replay_index,
            source_capability_sha256=intent.source_capability_sha256,
            lifecycle_identity_sha256=intent.lifecycle_identity_sha256,
            container_identity_sha256=container,
            image_sha256=intent.image_sha256,
            evaluator_sha256=intent.evaluator_sha256,
            state_normalization_sha256=intent.state_normalization_sha256,
            intent_event_sha256=intent.intent_event_sha256,
            result_event_sha256=result_event_sha256,
            cleanup_event_sha256=inspection.release_completion_event_sha256,
            **result._private_record(),
            _collector_seal=_GOLD_REPLAY_CONSTRUCTION_SEAL,
        )
        self._active_intent = None
        self._active_result = None
        return replay

    def _reconcile_interrupted_replay(
        self,
        *,
        task_reference: PrivateTaskReference,
        replay_index: int,
        lifecycle_identity_sha256: str | None,
        container_identity_sha256: str | None,
        container_present_before: bool,
        container_absent_after: bool,
        identity_match: bool,
        profile_match: bool,
        ambiguous: bool,
        cleanup_receipt_sha256: str,
    ) -> QualificationUnitStatus:
        """Finish exact cleanup without regenerating a durable result."""

        self._require_open()
        if self._active_intent is not None:
            raise QualificationError("active replay cannot enter reboot recovery")
        replay = _require_replay_index(replay_index)
        try:
            task, _capability = self._source.qualification_identity(task_reference)
        except InterCodeSourceError as error:
            raise QualificationError(
                "recovery task is not source-capability-bound"
            ) from error
        if task not in self._source.tasks:
            raise QualificationError("calibration tasks cannot enter recovery")
        lifecycle = (
            None
            if lifecycle_identity_sha256 is None
            else _require_sha256(
                lifecycle_identity_sha256, "recovery lifecycle identity"
            )
        )
        container = (
            None
            if container_identity_sha256 is None
            else _require_sha256(
                container_identity_sha256, "recovery container identity"
            )
        )
        present = _require_bool(
            container_present_before, "container_present_before"
        )
        absent = _require_bool(container_absent_after, "container_absent_after")
        identity = _require_bool(identity_match, "identity_match")
        profile = _require_bool(profile_match, "profile_match")
        is_ambiguous = _require_bool(ambiguous, "ambiguous")
        receipt_sha256 = _require_sha256(
            cleanup_receipt_sha256, "cleanup receipt"
        )
        try:
            attempt = self._repository.open_or_start(
                QualificationUnitKey(task.task_id, replay)
            )
            inspection = attempt.inspect()
            if inspection.partial_tail_sha256 is not None:
                raise QualificationIncompleteError(
                    "interrupted qualification unit has a partial tail"
                )
            if inspection.sealed:
                return inspection.status
            if inspection.status in (
                QualificationUnitStatus.STARTED,
                QualificationUnitStatus.ACQUIRE_PENDING,
                QualificationUnitStatus.ACQUIRED,
                QualificationUnitStatus.RESULT_DURABLE,
            ):
                release_intent = attempt.record_release_intent(recovery=True)
            elif inspection.status is QualificationUnitStatus.RELEASE_PENDING:
                release_intent = attempt.resume_release_intent()
            elif inspection.status in (
                QualificationUnitStatus.RELEASED,
                QualificationUnitStatus.COMPLETED,
                QualificationUnitStatus.ABORTED,
            ):
                release_intent = None
            else:
                raise QualificationIncompleteError(
                    "interrupted qualification cleanup is not exact"
                )
            if release_intent is not None:
                cleanup_receipt = _issue_trusted_cleanup_receipt(
                    release_intent,
                    lifecycle_identity_sha256=lifecycle,
                    container_identity_sha256=container,
                    container_present_before=present,
                    container_absent_after=absent,
                    identity_match=identity,
                    profile_match=profile,
                    ambiguous=is_ambiguous,
                    cleanup_receipt_sha256=receipt_sha256,
                )
                attempt.record_release_completion(
                    release_intent,
                    cleanup_receipt,
                )
            inspection = attempt.inspect()
            if inspection.status is QualificationUnitStatus.RELEASED:
                if inspection.result is None:
                    attempt.mark_aborted()
                else:
                    attempt.mark_completed()
                inspection = attempt.inspect()
            if inspection.status in (
                QualificationUnitStatus.COMPLETED,
                QualificationUnitStatus.ABORTED,
            ) and not inspection.sealed:
                attempt.seal()
                inspection = attempt.inspect(require_sealed=True)
            if not inspection.sealed:
                raise QualificationIncompleteError(
                    "interrupted qualification unit is not terminal"
                )
            return inspection.status
        except QualificationUnitError as error:
            raise QualificationError(
                "qualification recovery is incomplete"
            ) from error

    def _seal(self) -> _QualificationAggregateEvidence:
        self._require_open()
        if self._active_intent is not None:
            raise QualificationError("qualification has an active replay")
        try:
            evidence = self._repository.seal_aggregate(self._journal_path)
        except QualificationIncompleteError as error:
            raise QualificationError("qualification is incomplete") from error
        except QualificationUnitError as error:
            raise QualificationError("qualification aggregate is unsafe") from error
        self._sealed = True
        return evidence

    def _require_open(self) -> None:
        if self._sealed:
            raise QualificationError("qualification collector is already sealed")

    def _require_active(self, intent: _QualificationReplayIntent) -> None:
        self._require_open()
        if type(intent) is not _QualificationReplayIntent:
            raise QualificationError("replay intent must be collector-owned")
        if (
            intent._collector_identity is not self._collector_identity
            or intent is not self._active_intent
        ):
            raise QualificationError("replay intent does not belong to this collector")


def _create_docker_qualification_collector(
    *,
    source: InterCodeSource,
    journal_path: str | Path,
    image_sha256_by_stratum: Mapping[str, str],
    evaluator_sha256: str,
    state_normalization_sha256: str,
) -> _QualificationEvidenceCollector:
    """Issue the collector capability reserved for the trusted Docker adapter.

    This underscored factory is the module trust boundary. It must never be
    re-exported by a CLI, generic controller, plugin, or agent-facing module.
    The Docker qualification adapter will own the returned capability and is
    responsible for deriving lifecycle/result facts from inspected execution.
    """

    return _QualificationEvidenceCollector(
        source=source,
        journal_path=journal_path,
        image_sha256_by_stratum=image_sha256_by_stratum,
        evaluator_sha256=evaluator_sha256,
        state_normalization_sha256=state_normalization_sha256,
        _authority=_TRUSTED_QUALIFICATION_AUTHORITY,
    )


def _load_qualification_evidence(
    *,
    source: InterCodeSource,
    journal_bytes: bytes,
    _authority: object | None = None,
) -> _QualificationEvidence:
    """Reparse a complete private journal and reconstruct collector evidence."""

    if _authority is not _TRUSTED_QUALIFICATION_AUTHORITY:
        raise QualificationError(
            "qualification evidence loading is reserved for the trusted adapter"
        )
    _validate_source(source)
    _validate_static_exclusion_binding(source)
    if type(journal_bytes) is not bytes:
        raise QualificationError("qualification journal snapshot must be bytes")
    if not journal_bytes or len(journal_bytes) > MAX_QUALIFICATION_JOURNAL_BYTES:
        raise QualificationError("qualification journal snapshot has unsafe size")
    if not journal_bytes.endswith(b"\n"):
        raise QualificationError("qualification journal is not record-complete")

    records = _decode_and_verify_journal(journal_bytes)
    if not records or records[-1].get("type") != SEALED_EVENT_TYPE:
        raise QualificationError("qualification journal is not sealed")
    start = records[0]
    _expect_event_keys(
        start,
        {
            "type",
            "journal_schema_version",
            "suite_name",
            "source_revision",
            "source_population_sha256",
            "source_corpus_sha256",
            "static_exclusion_audit_sha256",
            "image_sha256_by_stratum",
            "evaluator_sha256",
            "state_normalization_sha256",
            "expected_task_count",
            "expected_replay_count",
        },
    )
    if (
        start.get("type") != "qualification_started"
        or start.get("journal_schema_version") != QUALIFICATION_JOURNAL_SCHEMA_VERSION
        or start.get("suite_name") != QUALIFIED_SUITE_NAME
        or start.get("source_revision") != INTERCODE_REVISION
        or start.get("source_population_sha256") != PUBLIC_POPULATION_SHA256
        or start.get("source_corpus_sha256") != SOURCE_CORPUS_SHA256
        or start.get("static_exclusion_audit_sha256")
        != STATIC_EXCLUSION_AUDIT_SHA256
        or start.get("expected_task_count") != len(source.tasks)
        or start.get("expected_replay_count") != len(source.tasks) * 2
    ):
        raise QualificationError("qualification start record differs from frozen pins")
    images = _validate_image_pins(start.get("image_sha256_by_stratum"))
    evaluator = _require_sha256(start.get("evaluator_sha256"), "evaluator_sha256")
    normalizer = _require_sha256(
        start.get("state_normalization_sha256"),
        "state_normalization_sha256",
    )

    expected_tasks = {task.task_id: task for task in source.tasks}
    used_lifecycles: set[str] = set()
    used_containers: set[str] = set()
    completed: set[tuple[str, int]] = set()
    replays: list[GoldReplay] = []
    active: dict[str, object] | None = None
    active_result: _QualificationReplayResult | None = None
    active_result_event_sha256: str | None = None

    for record in records[1:-1]:
        event_type = record.get("type")
        if event_type == "qualification_replay_intent":
            if active is not None:
                raise QualificationError("qualification journal has overlapping replays")
            _expect_event_keys(
                record,
                {
                    "type",
                    "task_id",
                    "stratum",
                    "replay_index",
                    "source_capability_sha256",
                    "lifecycle_identity_sha256",
                    "container_identity_sha256",
                    "image_sha256",
                    "evaluator_sha256",
                    "state_normalization_sha256",
                },
            )
            task_id = record.get("task_id")
            task = expected_tasks.get(task_id) if type(task_id) is str else None
            replay_index = _require_replay_index(record.get("replay_index"))
            if task is None or record.get("stratum") != task.stratum:
                raise QualificationError("qualification intent has unknown task identity")
            key = (task.task_id, replay_index)
            if key in completed:
                raise QualificationError("qualification journal duplicates a replay")
            reference = source.private_reference(task.task_id)
            bound_task, expected_capability = source.qualification_identity(reference)
            if bound_task != task or record.get("source_capability_sha256") != expected_capability:
                raise QualificationError("qualification intent is not source-capability-bound")
            lifecycle = _require_sha256(
                record.get("lifecycle_identity_sha256"), "lifecycle identity"
            )
            container = _require_sha256(
                record.get("container_identity_sha256"), "container identity"
            )
            if lifecycle in used_lifecycles:
                raise QualificationError("lifecycle identity is reused")
            if container in used_containers:
                raise QualificationError("container identity is reused")
            if (
                record.get("image_sha256") != images[task.stratum]
                or record.get("evaluator_sha256") != evaluator
                or record.get("state_normalization_sha256") != normalizer
            ):
                raise QualificationError("qualification replay pin drift")
            used_lifecycles.add(lifecycle)
            used_containers.add(container)
            active = record
            active_result = None
            active_result_event_sha256 = None
            continue

        if event_type == "qualification_replay_result":
            if active is None or active_result is not None:
                raise QualificationError("qualification result has no unique intent")
            _expect_event_keys(
                record,
                {
                    "type",
                    "task_id",
                    "stratum",
                    "replay_index",
                    "intent_event_sha256",
                    "infrastructure_valid",
                    "setup_valid",
                    "network_required",
                    "state_supported",
                    "evaluator_valid",
                    "exit_policy_passed",
                    "official_reward",
                    "strict_success",
                    "initial_state_sha256",
                    "normalized_output_sha256",
                    "observable_state_sha256",
                },
            )
            _require_same_lifecycle_identity(record, active)
            expected_intent_event = "sha256:" + str(active["event_sha256"])
            if record.get("intent_event_sha256") != expected_intent_event:
                raise QualificationError("qualification result intent hash drift")
            active_result = _QualificationReplayResult(
                infrastructure_valid=record.get("infrastructure_valid"),  # type: ignore[arg-type]
                setup_valid=record.get("setup_valid"),  # type: ignore[arg-type]
                network_required=record.get("network_required"),  # type: ignore[arg-type]
                state_supported=record.get("state_supported"),  # type: ignore[arg-type]
                evaluator_valid=record.get("evaluator_valid"),  # type: ignore[arg-type]
                exit_policy_passed=record.get("exit_policy_passed"),  # type: ignore[arg-type]
                official_reward=record.get("official_reward"),  # type: ignore[arg-type]
                strict_success=record.get("strict_success"),  # type: ignore[arg-type]
                initial_state_sha256=record.get("initial_state_sha256"),  # type: ignore[arg-type]
                normalized_output_sha256=record.get(  # type: ignore[arg-type]
                    "normalized_output_sha256"
                ),
                observable_state_sha256=record.get(  # type: ignore[arg-type]
                    "observable_state_sha256"
                ),
            )
            active_result_event_sha256 = "sha256:" + str(record["event_sha256"])
            continue

        if event_type == "qualification_replay_cleanup":
            if active is None or active_result is None or active_result_event_sha256 is None:
                raise QualificationError("qualification cleanup has no complete replay")
            _expect_event_keys(
                record,
                {
                    "type",
                    "task_id",
                    "stratum",
                    "replay_index",
                    "intent_event_sha256",
                    "result_event_sha256",
                    "lifecycle_identity_sha256",
                    "container_identity_sha256",
                    "container_destroyed",
                    "cleanup_verified",
                },
            )
            _require_same_lifecycle_identity(record, active)
            if (
                record.get("intent_event_sha256")
                != "sha256:" + str(active["event_sha256"])
                or record.get("result_event_sha256") != active_result_event_sha256
                or record.get("lifecycle_identity_sha256")
                != active.get("lifecycle_identity_sha256")
                or record.get("container_identity_sha256")
                != active.get("container_identity_sha256")
                or record.get("container_destroyed") is not True
                or record.get("cleanup_verified") is not True
            ):
                raise QualificationError("qualification cleanup verification failed")
            replay = GoldReplay(
                task_id=str(active["task_id"]),
                stratum=str(active["stratum"]),
                replay_index=int(active["replay_index"]),
                source_capability_sha256=str(active["source_capability_sha256"]),
                lifecycle_identity_sha256=str(active["lifecycle_identity_sha256"]),
                container_identity_sha256=str(active["container_identity_sha256"]),
                image_sha256=str(active["image_sha256"]),
                evaluator_sha256=str(active["evaluator_sha256"]),
                state_normalization_sha256=str(active["state_normalization_sha256"]),
                intent_event_sha256="sha256:" + str(active["event_sha256"]),
                result_event_sha256=active_result_event_sha256,
                cleanup_event_sha256="sha256:" + str(record["event_sha256"]),
                **active_result._private_record(),
                _collector_seal=_GOLD_REPLAY_CONSTRUCTION_SEAL,
            )
            completed.add((replay.task_id, replay.replay_index))
            replays.append(replay)
            active = None
            active_result = None
            active_result_event_sha256 = None
            continue

        raise QualificationError("qualification journal contains an unknown event")

    if active is not None:
        raise QualificationError("qualification journal ends with an active replay")
    required_keys = {
        (task.task_id, replay_index)
        for task in source.tasks
        for replay_index in (1, 2)
    }
    if completed != required_keys or len(replays) != len(required_keys):
        raise QualificationError("qualification journal does not cover every replay")

    seal = records[-1]
    _expect_event_keys(seal, {"type", "sealed_event_count"})
    if seal.get("sealed_event_count") != len(records) - 1:
        raise QualificationError("qualification terminal seal count is invalid")
    root = "sha256:" + str(seal["event_sha256"])
    ordered_replays = tuple(
        sorted(
            replays,
            key=lambda replay: (
                list(EXPECTED_SOURCE_COUNTS).index(replay.stratum),
                replay.task_id,
                replay.replay_index,
            ),
        )
    )
    return _QualificationEvidence(
        journal_bytes=journal_bytes,
        replays=ordered_replays,
        journal_root_sha256=root,
        source_population_sha256=PUBLIC_POPULATION_SHA256,
        source_corpus_sha256=SOURCE_CORPUS_SHA256,
        static_exclusion_audit_sha256=STATIC_EXCLUSION_AUDIT_SHA256,
        image_sha256_by_stratum=images,
        evaluator_sha256=evaluator,
        state_normalization_sha256=normalizer,
        _construction_seal=_EVIDENCE_CONSTRUCTION_SEAL,
    )


def _gold_replay_from_aggregate(replay: _AggregateReplay) -> GoldReplay:
    result = replay.result
    return GoldReplay(
        task_id=replay.task_id,
        stratum=replay.stratum,
        replay_index=replay.replay_index,
        source_capability_sha256=replay.source_capability_sha256,
        lifecycle_identity_sha256=replay.lifecycle_identity_sha256,
        container_identity_sha256=replay.container_identity_sha256,
        image_sha256=replay.image_sha256,
        evaluator_sha256=replay.evaluator_sha256,
        state_normalization_sha256=replay.state_normalization_sha256,
        intent_event_sha256=replay.acquire_intent_event_sha256,
        result_event_sha256=replay.result_event_sha256,
        cleanup_event_sha256=replay.cleanup_event_sha256,
        **result.to_dict(),
        _collector_seal=_GOLD_REPLAY_CONSTRUCTION_SEAL,
    )


def build_qualification_manifest(
    *,
    source: InterCodeSource,
    evidence: object,
    image_sha256_by_stratum: Mapping[str, str],
    evaluator_sha256: str,
    state_normalization_sha256: str,
) -> QualificationManifest:
    """Aggregate only independently reverified collector provenance."""

    _validate_source(source)
    _validate_static_exclusion_binding(source)
    expected_images = _validate_image_pins(image_sha256_by_stratum)
    expected_evaluator = _require_sha256(evaluator_sha256, "evaluator_sha256")
    expected_normalizer = _require_sha256(
        state_normalization_sha256, "state_normalization_sha256"
    )
    if type(evidence) is not _QualificationAggregateEvidence:
        raise QualificationError(
            "evidence must be the trusted collector's sealed aggregate"
        )
    if dict(evidence.image_sha256_by_stratum) != expected_images:
        raise QualificationError("qualification image pins differ from collector evidence")
    if evidence.evaluator_sha256 != expected_evaluator:
        raise QualificationError("qualification evaluator pin differs from collector evidence")
    if evidence.state_normalization_sha256 != expected_normalizer:
        raise QualificationError("qualification normalizer pin differs from collector evidence")
    expectations = _qualification_unit_expectations(
        source,
        expected_images,
        expected_evaluator,
        expected_normalizer,
    )
    try:
        verified = _reverify_aggregate_evidence(evidence, expectations)
    except QualificationUnitError as error:
        raise QualificationError(
            "qualification sealed aggregate failed independent reverification"
        ) from error

    indexed = {
        (replay.task_id, replay.replay_index): _gold_replay_from_aggregate(replay)
        for replay in verified._replays
    }
    records: list[QualificationRecord] = []
    for task in source.tasks:
        pair = (indexed[(task.task_id, 1)], indexed[(task.task_id, 2)])
        reasons = _classify_pair(pair)
        records.append(
            QualificationRecord(
                task_id=task.task_id,
                stratum=task.stratum,
                included=not reasons,
                exclusion_reasons=reasons,
            )
        )

    frozen_records = tuple(records)
    qualified = tuple(record for record in frozen_records if record.included)
    counts = Counter(record.stratum for record in qualified)
    qualified_by_stratum = {stratum: counts[stratum] for stratum in _STRATA}
    scoring_admitted = len(qualified) >= MIN_QUALIFIED_COUNT and all(
        qualified_by_stratum[stratum] >= minimum
        for stratum, minimum in MIN_QUALIFIED_PER_STRATUM.items()
    )
    values = {
        "suite_name": QUALIFIED_SUITE_NAME,
        "source_revision": INTERCODE_REVISION,
        "source_population_sha256": PUBLIC_POPULATION_SHA256,
        "source_corpus_sha256": SOURCE_CORPUS_SHA256,
        "static_exclusion_audit_sha256": STATIC_EXCLUSION_AUDIT_SHA256,
        "evidence_root_sha256": verified.aggregate_root_sha256,
        "aggregate_recovery_count": verified.aggregate_recovery_count,
        "image_sha256_by_stratum": expected_images,
        "evaluator_sha256": expected_evaluator,
        "state_normalization_sha256": expected_normalizer,
        "records": frozen_records,
        "qualified_count": len(qualified),
        "qualified_by_stratum": qualified_by_stratum,
        "scoring_admitted": scoring_admitted,
    }
    suite_sha256 = "sha256:" + hashlib.sha256(
        _canonical_json(_qualification_public_core_record_from_values(**values))
    ).hexdigest()
    return QualificationManifest(
        **values,
        suite_sha256=suite_sha256,
        _construction_seal=_MANIFEST_CONSTRUCTION_SEAL,
    )


def _classify_pair(
    pair: tuple[GoldReplay, GoldReplay],
) -> tuple[QualificationReason, ...]:
    first, second = pair
    reasons: set[QualificationReason] = set(
        _STATIC_EXCLUSION_REASON_MAP.get(first.task_id, ())
    )
    if any(row.network_required for row in pair):
        reasons.add(QualificationReason.NETWORK_REQUIRED)
    if not all(row.setup_valid for row in pair):
        reasons.add(QualificationReason.SETUP_INVALID)
    if not all(row.state_supported for row in pair):
        reasons.add(QualificationReason.UNSUPPORTED_STATE)
    if not all(row.evaluator_valid for row in pair):
        reasons.add(QualificationReason.EVALUATOR_INVALID)
    if not all(row.infrastructure_valid for row in pair):
        reasons.add(QualificationReason.INFRASTRUCTURE_INVALID)
    if not all(row.exit_policy_passed for row in pair):
        reasons.add(QualificationReason.GOLD_EXIT_POLICY_FAILED)
    if not all(float(row.official_reward) == 1.0 for row in pair):
        reasons.add(QualificationReason.OFFICIAL_REWARD_FAILED)
    if not all(row.strict_success for row in pair):
        reasons.add(QualificationReason.STRICT_GOLD_REPLAY_FAILED)
    if first.initial_state_sha256 != second.initial_state_sha256:
        reasons.add(QualificationReason.NONDETERMINISTIC_INITIAL_STATE)
    if first.normalized_output_sha256 != second.normalized_output_sha256:
        reasons.add(QualificationReason.NONDETERMINISTIC_OUTPUT)
    if first.observable_state_sha256 != second.observable_state_sha256:
        reasons.add(QualificationReason.NONDETERMINISTIC_OBSERVABLE_STATE)
    return tuple(reason for reason in _REASON_ORDER if reason in reasons)


def _validate_static_exclusion_binding(source: InterCodeSource) -> None:
    exclusions = {
        task_id: [reason.value for reason in reasons]
        for task_id, reasons in _STATIC_EXCLUSION_REASON_MAP.items()
    }
    artifact = {
        "audit_schema_version": 1,
        "exclusions": exclusions,
        "source_corpus_sha256": SOURCE_CORPUS_SHA256,
        "source_revision": INTERCODE_REVISION,
    }
    actual = "sha256:" + hashlib.sha256(_canonical_json(artifact) + b"\n").hexdigest()
    if actual != STATIC_EXCLUSION_AUDIT_SHA256:
        raise QualificationError("static exclusion map differs from its audit artifact")
    if source.static_exclusion_audit_sha256 != STATIC_EXCLUSION_AUDIT_SHA256:
        raise QualificationError("source static exclusion audit digest drift")
    source_ids = {task.task_id for task in source.tasks}
    if set(_STATIC_EXCLUSION_REASON_MAP) - source_ids:
        raise QualificationError("static exclusion audit contains an unknown task")


def _validate_source(source: InterCodeSource) -> None:
    if type(source) is not InterCodeSource:
        raise QualificationError("source must be the exact InterCodeSource loader type")
    counts = Counter(task.stratum for task in source.tasks)
    if len(source.tasks) != sum(EXPECTED_SOURCE_COUNTS.values()) or counts != Counter(
        EXPECTED_SOURCE_COUNTS
    ):
        raise QualificationError("source population shape differs from the frozen pin")
    expected_ids = tuple(
        f"bash-{stratum}-{index:03d}"
        for stratum, count in EXPECTED_SOURCE_COUNTS.items()
        for index in range(count)
    )
    if tuple(task.task_id for task in source.tasks) != expected_ids:
        raise QualificationError("source task order differs from the frozen pin")

    expected_calibration_ids = tuple(
        f"bash-calibration-{index:03d}" for index in range(EXPECTED_CALIBRATION_COUNT)
    )
    if (
        len(source.calibration_tasks) != EXPECTED_CALIBRATION_COUNT
        or tuple(task.task_id for task in source.calibration_tasks)
        != expected_calibration_ids
        or any(task.stratum != "calibration" for task in source.calibration_tasks)
    ):
        raise QualificationError("calibration population shape differs from the frozen pin")

    public_records = _validated_public_records(source.tasks, label="public population")
    calibration_records = _validated_public_records(
        source.calibration_tasks, label="calibration population"
    )
    actual_public_sha256 = _canonical_sha256(public_records, "public population")
    actual_calibration_sha256 = _canonical_sha256(
        calibration_records, "calibration population"
    )
    if (
        source.population_sha256 != PUBLIC_POPULATION_SHA256
        or actual_public_sha256 != PUBLIC_POPULATION_SHA256
    ):
        raise QualificationError("public population differs from the frozen pin")
    if (
        source.calibration_population_sha256 != CALIBRATION_POPULATION_SHA256
        or actual_calibration_sha256 != CALIBRATION_POPULATION_SHA256
    ):
        raise QualificationError("calibration population differs from the frozen pin")

    private_records: list[dict[str, str]] = []
    try:
        for task, public_record in zip(
            source.tasks + source.calibration_tasks,
            public_records + calibration_records,
            strict=True,
        ):
            reference = source.private_reference(task.task_id)
            gold = source.gold_for_evaluator(reference)
            if not isinstance(gold, str) or not gold.strip() or "\x00" in gold:
                raise QualificationError("source corpus contains an invalid private record")
            private_records.append({**public_record, "gold": gold})
    except (InterCodeSourceError, TypeError, ValueError):
        raise QualificationError(
            "source corpus cannot be resolved through source-bound references"
        ) from None
    actual_source_sha256 = _canonical_sha256(private_records, "source corpus")
    if (
        source.source_sha256 != SOURCE_CORPUS_SHA256
        or actual_source_sha256 != SOURCE_CORPUS_SHA256
    ):
        raise QualificationError("source corpus differs from the frozen pin")


def _validated_public_records(
    tasks: Sequence[PublicBashTask], *, label: str
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for task in tasks:
        if type(task) is not PublicBashTask:
            raise QualificationError(f"{label} must contain exact PublicBashTask values")
        if (
            type(task.task_id) is not str
            or type(task.query) is not str
            or not task.query.strip()
            or "\x00" in task.query
            or type(task.stratum) is not str
        ):
            raise QualificationError(f"{label} contains an invalid public record")
        records.append(
            {"task_id": task.task_id, "query": task.query, "stratum": task.stratum}
        )
    return records


def _validate_manifest(manifest: QualificationManifest) -> dict[str, str]:
    if manifest.suite_name != QUALIFIED_SUITE_NAME:
        raise QualificationError("suite name differs from the frozen pin")
    if manifest.source_revision != INTERCODE_REVISION:
        raise QualificationError("source revision differs from the frozen pin")
    if manifest.source_population_sha256 != PUBLIC_POPULATION_SHA256:
        raise QualificationError("source population digest differs from the frozen pin")
    if manifest.source_corpus_sha256 != SOURCE_CORPUS_SHA256:
        raise QualificationError("source corpus digest differs from the frozen pin")
    if manifest.static_exclusion_audit_sha256 != STATIC_EXCLUSION_AUDIT_SHA256:
        raise QualificationError("static exclusion audit digest differs from the frozen pin")
    _require_sha256(manifest.evidence_root_sha256, "evidence_root_sha256")
    if (
        type(manifest.aggregate_recovery_count) is not int
        or not 0 <= manifest.aggregate_recovery_count <= len(manifest.records) * 2
    ):
        raise QualificationError("aggregate_recovery_count is invalid")
    images = _validate_image_pins(manifest.image_sha256_by_stratum)
    _require_sha256(manifest.evaluator_sha256, "evaluator_sha256")
    _require_sha256(
        manifest.state_normalization_sha256, "state_normalization_sha256"
    )
    if type(manifest.records) is not tuple:
        raise QualificationError("record order must be a frozen tuple")
    for record in manifest.records:
        if type(record) is not QualificationRecord:
            raise QualificationError("records must contain exact public record values")
        record.__post_init__()
    expected_identity = tuple(
        (f"bash-{stratum}-{index:03d}", stratum)
        for stratum, count in EXPECTED_SOURCE_COUNTS.items()
        for index in range(count)
    )
    if tuple((record.task_id, record.stratum) for record in manifest.records) != expected_identity:
        raise QualificationError("record order differs from the frozen population")
    qualified = tuple(record for record in manifest.records if record.included)
    if type(manifest.qualified_count) is not int or manifest.qualified_count != len(qualified):
        raise QualificationError("qualified_count differs from records")
    counts = Counter(record.stratum for record in qualified)
    expected_counts = {stratum: counts[stratum] for stratum in _STRATA}
    actual_counts = _validate_qualified_counts(manifest.qualified_by_stratum)
    if actual_counts != expected_counts:
        raise QualificationError("qualified_by_stratum differs from records")
    expected_admitted = len(qualified) >= MIN_QUALIFIED_COUNT and all(
        expected_counts[stratum] >= minimum
        for stratum, minimum in MIN_QUALIFIED_PER_STRATUM.items()
    )
    if (
        type(manifest.scoring_admitted) is not bool
        or manifest.scoring_admitted != expected_admitted
    ):
        raise QualificationError("scoring_admitted differs from frozen gates")
    _require_sha256(manifest.suite_sha256, "suite_sha256")
    expected_suite = "sha256:" + hashlib.sha256(
        _canonical_json(_qualification_public_core_record(manifest))
    ).hexdigest()
    if manifest.suite_sha256 != expected_suite:
        raise QualificationError("suite_sha256 differs from canonical public manifest")
    return images


def _qualification_public_core_record(
    manifest: QualificationManifest,
) -> dict[str, object]:
    return _qualification_public_core_record_from_values(
        suite_name=manifest.suite_name,
        source_revision=manifest.source_revision,
        source_population_sha256=manifest.source_population_sha256,
        source_corpus_sha256=manifest.source_corpus_sha256,
        static_exclusion_audit_sha256=manifest.static_exclusion_audit_sha256,
        evidence_root_sha256=manifest.evidence_root_sha256,
        aggregate_recovery_count=manifest.aggregate_recovery_count,
        image_sha256_by_stratum=manifest.image_sha256_by_stratum,
        evaluator_sha256=manifest.evaluator_sha256,
        state_normalization_sha256=manifest.state_normalization_sha256,
        records=manifest.records,
        qualified_count=manifest.qualified_count,
        qualified_by_stratum=manifest.qualified_by_stratum,
        scoring_admitted=manifest.scoring_admitted,
    )


def _qualification_public_core_record_from_values(
    *,
    suite_name: str,
    source_revision: str,
    source_population_sha256: str,
    source_corpus_sha256: str,
    static_exclusion_audit_sha256: str,
    evidence_root_sha256: str,
    aggregate_recovery_count: int,
    image_sha256_by_stratum: Mapping[str, str],
    evaluator_sha256: str,
    state_normalization_sha256: str,
    records: Sequence[QualificationRecord],
    qualified_count: int,
    qualified_by_stratum: Mapping[str, int],
    scoring_admitted: bool,
) -> dict[str, object]:
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "suite_name": suite_name,
        "source_revision": source_revision,
        "source_population_sha256": source_population_sha256,
        "source_corpus_sha256": source_corpus_sha256,
        "static_exclusion_audit_sha256": static_exclusion_audit_sha256,
        "evidence_root_sha256": evidence_root_sha256,
        "aggregate_recovery_count": aggregate_recovery_count,
        "image_sha256_by_stratum": dict(image_sha256_by_stratum),
        "evaluator_sha256": evaluator_sha256,
        "state_normalization_sha256": state_normalization_sha256,
        "records": [record._public_record() for record in records],
        "qualified_count": qualified_count,
        "qualified_by_stratum": dict(qualified_by_stratum),
        "minimum_qualified_count": MIN_QUALIFIED_COUNT,
        "minimum_qualified_by_stratum": dict(MIN_QUALIFIED_PER_STRATUM),
        "scoring_admitted": scoring_admitted,
    }


def _read_private_journal(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise QualificationError("private qualification journal cannot be opened") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise QualificationError("private qualification journal is not regular")
        chunks: list[bytes] = []
        remaining = MAX_QUALIFICATION_JOURNAL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_QUALIFICATION_JOURNAL_BYTES:
        raise QualificationError("private qualification journal exceeds safety limit")
    return payload


def _decode_and_verify_journal(journal_bytes: bytes) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    previous = GENESIS_EVENT_SHA256
    for expected_sequence, line in enumerate(journal_bytes.splitlines(keepends=True), 1):
        if not line.endswith(b"\n") or len(line) > MAX_JOURNAL_EVENT_BYTES:
            raise QualificationError("qualification journal framing is invalid")
        body = line[:-1]
        try:
            text = body.decode("utf-8")
            record = json.loads(text, object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
            raise QualificationError("qualification journal record is invalid") from error
        if type(record) is not dict or _canonical_json(record) != body:
            raise QualificationError("qualification journal record is not canonical")
        if record.get("sequence") != expected_sequence:
            raise QualificationError("qualification journal sequence is invalid")
        if record.get("previous_event_sha256") != previous:
            raise QualificationError("qualification journal hash chain is broken")
        event_hash = _require_bare_sha256(
            record.get("event_sha256"), "journal event hash"
        )
        expected_hash = hashlib.sha256(canonical_event_bytes(record)).hexdigest()
        if event_hash != expected_hash:
            raise QualificationError("qualification journal event hash mismatch")
        if records and records[-1].get("type") == SEALED_EVENT_TYPE:
            raise QualificationError("qualification journal seal is not terminal")
        records.append(record)
        previous = event_hash
    return records


def _expect_event_keys(record: Mapping[str, object], payload: set[str]) -> None:
    expected = payload | set(_CHAIN_FIELDS)
    if record.get("type") == SEALED_EVENT_TYPE:
        expected.add("sealed_event_count")
    if set(record) != expected:
        raise QualificationError("qualification journal event schema drift")


def _require_same_lifecycle_identity(
    record: Mapping[str, object], intent: Mapping[str, object]
) -> None:
    if any(
        record.get(field) != intent.get(field)
        for field in ("task_id", "stratum", "replay_index")
    ):
        raise QualificationError("qualification replay lifecycle identity drift")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_sha256(records: object, label: str) -> str:
    try:
        payload = _canonical_json(records)
    except (TypeError, ValueError, UnicodeError):
        raise QualificationError(f"{label} cannot be canonically encoded") from None
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _validate_qualified_counts(value: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(_STRATA):
        raise QualificationError(
            "qualified_by_stratum must contain exactly fs1, fs2, fs3, fs4"
        )
    counts: dict[str, int] = {}
    for stratum in _STRATA:
        count = value[stratum]
        if type(count) is not int or count < 0:
            raise QualificationError(
                "qualified_by_stratum values must be non-negative integers"
            )
        counts[stratum] = count
    return counts


def _validate_image_pins(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(_STRATA):
        raise QualificationError("image pins must contain exactly fs1, fs2, fs3, fs4")
    return {
        stratum: _require_sha256(value[stratum], f"image pin {stratum}")
        for stratum in _STRATA
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
