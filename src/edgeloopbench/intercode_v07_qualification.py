"""Selected-sample qualification evidence for the frozen v0.7 study.

The runtime-facing adapter supplies only typed, gold-free replay facts.  This
module validates the complete 30-task by two-replay matrix, writes one new
append-only journal, independently reparses it, and returns a path-free public
evidence root.  It performs no Docker, model, or network operation.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import InitVar, dataclass, fields
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

from .intercode_campaign_ledger import CAMPAIGN_TASK_IDS
from .intercode_replay_environment import V07_STRICT_REPLAY_EVALUATOR_SHA256
from .intercode_source import (
    INTERCODE_REVISION,
    PUBLIC_POPULATION_SHA256,
    SOURCE_CORPUS_SHA256,
    STATIC_EXCLUSION_AUDIT_SHA256,
    InterCodeSource,
    InterCodeSourceError,
)
from .intercode_v07_protocol import (
    V07_SAMPLE_MANIFEST_SHA256,
    V07_TASK_IDS,
    build_v07_sample,
)
from .intercode_v07_image_provenance import V07_STATE_NORMALIZATION_REVISION
from .journal import (
    GENESIS_EVENT_SHA256,
    SEALED_EVENT_TYPE,
    append_journal_event,
    canonical_event_bytes,
    seal_journal,
)


V07_QUALIFICATION_SCHEMA = "intercode-v0.7-selected-qualification-v2"
V07_QUALIFICATION_PLATFORM = "linux/arm64"
V07_QUALIFICATION_NETWORK_MODE = "none"
V07_QUALIFICATION_TASK_COUNT = 30
V07_QUALIFICATION_REPLAY_COUNT = 60
V07_QUALIFICATION_REPLAYS_PER_TASK = 2
V07_QUALIFICATION_MAX_JOURNAL_BYTES = 4 * 1024 * 1024

_STRATA = ("fs1", "fs2", "fs3", "fs4")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TASK_ID = re.compile(r"bash-(fs[1-4])-[0-9]{3}\Z")
_CHAIN_FIELDS = {"sequence", "previous_event_sha256", "event_sha256"}
_EVIDENCE_CONSTRUCTION_SEAL = object()
_REPLAY_CONSTRUCTION_SEAL = object()


class V07QualificationError(ValueError):
    """The selected-sample qualification proof is incomplete or unsafe."""


class _DuplicateKey(ValueError):
    pass


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + sha256(_canonical_json(value)).hexdigest()


def _require_digest(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise V07QualificationError(
            f"{field} must be a lowercase SHA-256 reference"
        )
    return value


def _require_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise V07QualificationError(f"{field} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class V07QualificationReplay:
    """Gold-free facts issued after one trusted clean gold replay.

    Digest fields bind private material without making the material itself
    representable on this interface.  The final public evidence omits even
    these per-replay digests.
    """

    task_id: str
    stratum: str
    replay_index: int
    source_capability_sha256: str
    image_id: str
    platform: str
    network_mode: str
    evaluator_sha256: str
    state_normalization_sha256: str
    lifecycle_identity_sha256: str
    container_identity_sha256: str
    container_absent_before: bool
    clean_initial_state: bool
    container_profile_match: bool
    infrastructure_valid: bool
    setup_valid: bool
    evaluator_valid: bool
    gold_replay_passed: bool
    exit_policy_sha256: str
    initial_state_sha256: str
    normalized_stdout_sha256: str
    normalized_stderr_sha256: str
    observable_state_sha256: str
    container_destroyed: bool
    container_absent_after: bool
    cleanup_verified: bool
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _REPLAY_CONSTRUCTION_SEAL:
            raise V07QualificationError(
                "qualification replay facts are trusted-adapter-sealed"
            )
        match = _TASK_ID.fullmatch(self.task_id) if type(self.task_id) is str else None
        if match is None:
            raise V07QualificationError("qualification task ID is not canonical")
        if type(self.stratum) is not str or self.stratum != match.group(1):
            raise V07QualificationError("qualification stratum differs from task ID")
        if type(self.replay_index) is not int or self.replay_index not in (1, 2):
            raise V07QualificationError("qualification replay index must be 1 or 2")
        if type(self.platform) is not str or not self.platform:
            raise V07QualificationError("qualification platform must be non-empty")
        if type(self.network_mode) is not str or not self.network_mode:
            raise V07QualificationError("qualification network mode must be non-empty")
        for field in (
            "source_capability_sha256",
            "image_id",
            "evaluator_sha256",
            "state_normalization_sha256",
            "lifecycle_identity_sha256",
            "container_identity_sha256",
            "exit_policy_sha256",
            "initial_state_sha256",
            "normalized_stdout_sha256",
            "normalized_stderr_sha256",
            "observable_state_sha256",
        ):
            _require_digest(getattr(self, field), field)
        for field in (
            "container_absent_before",
            "clean_initial_state",
            "container_profile_match",
            "infrastructure_valid",
            "setup_valid",
            "evaluator_valid",
            "gold_replay_passed",
            "container_destroyed",
            "container_absent_after",
            "cleanup_verified",
        ):
            _require_bool(getattr(self, field), field)

    @property
    def key(self) -> tuple[str, int]:
        return self.task_id, self.replay_index

    def _journal_record(self) -> dict[str, object]:
        return {
            "type": "v07_qualification_replay_completed",
            "task_id": self.task_id,
            "stratum": self.stratum,
            "replay_index": self.replay_index,
            "source_capability_sha256": self.source_capability_sha256,
            "image_id": self.image_id,
            "platform": self.platform,
            "network_mode": self.network_mode,
            "evaluator_sha256": self.evaluator_sha256,
            "state_normalization_sha256": self.state_normalization_sha256,
            "lifecycle_identity_sha256": self.lifecycle_identity_sha256,
            "container_identity_sha256": self.container_identity_sha256,
            "container_absent_before": self.container_absent_before,
            "clean_initial_state": self.clean_initial_state,
            "container_profile_match": self.container_profile_match,
            "infrastructure_valid": self.infrastructure_valid,
            "setup_valid": self.setup_valid,
            "evaluator_valid": self.evaluator_valid,
            "gold_replay_passed": self.gold_replay_passed,
            "exit_policy_sha256": self.exit_policy_sha256,
            "initial_state_sha256": self.initial_state_sha256,
            "normalized_stdout_sha256": self.normalized_stdout_sha256,
            "normalized_stderr_sha256": self.normalized_stderr_sha256,
            "observable_state_sha256": self.observable_state_sha256,
            "container_destroyed": self.container_destroyed,
            "container_absent_after": self.container_absent_after,
            "cleanup_verified": self.cleanup_verified,
        }


def _issue_trusted_v07_qualification_replay(
    **facts: object,
) -> V07QualificationReplay:
    """Issue typed facts at the future Docker adapter's trust boundary.

    Generic controllers and agent-facing modules must not re-export this
    underscored factory.  Its name makes the provenance boundary explicit in
    the same way as the existing v0.6 trusted qualification adapter.
    """

    try:
        return V07QualificationReplay(
            **facts,  # type: ignore[arg-type]
            _construction_seal=_REPLAY_CONSTRUCTION_SEAL,
        )
    except TypeError as error:
        raise V07QualificationError(
            "trusted qualification replay facts are incomplete"
        ) from error


@dataclass(frozen=True, slots=True)
class VerifiedV07QualificationEvidence:
    """Builder-sealed, path-free proof admitted for manifest construction."""

    source_revision: str
    source_population_sha256: str
    source_corpus_sha256: str
    static_exclusion_audit_sha256: str
    sample_manifest_sha256: str
    task_count: int
    replay_count: int
    replays_per_task: int
    qualified_task_ids: tuple[str, ...]
    source_capability_set_sha256: str
    platform: str
    network_mode: str
    source_inventory_sha256: str
    build_plan_sha256: str
    build_manifest_sha256: str
    build_verification_sha256: str
    image_set_sha256: str
    image_id_by_stratum: Mapping[str, str]
    evaluator_sha256: str
    state_normalization_revision: str
    state_normalization_source_sha256: str
    state_normalization_sha256: str
    lifecycle_identity_set_sha256: str
    container_identity_set_sha256: str
    journal_sha256: str
    journal_root_sha256: str
    evidence_root_sha256: str
    suite_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _EVIDENCE_CONSTRUCTION_SEAL:
            raise V07QualificationError(
                "verified v0.7 qualification evidence is builder-sealed"
            )
        object.__setattr__(
            self,
            "image_id_by_stratum",
            MappingProxyType(dict(self.image_id_by_stratum)),
        )
        self._validate()

    @property
    def qualification_evidence_root_sha256(self) -> str:
        """Alias used by the pre-calibration manifest boundary."""

        return self.evidence_root_sha256

    @property
    def image_sha256_by_stratum(self) -> Mapping[str, str]:
        """Compatibility alias retaining image-ID semantics."""

        return self.image_id_by_stratum

    def _core_record(self) -> dict[str, object]:
        return _public_core_from_values(
            source_revision=self.source_revision,
            source_population_sha256=self.source_population_sha256,
            source_corpus_sha256=self.source_corpus_sha256,
            static_exclusion_audit_sha256=self.static_exclusion_audit_sha256,
            sample_manifest_sha256=self.sample_manifest_sha256,
            task_count=self.task_count,
            replay_count=self.replay_count,
            replays_per_task=self.replays_per_task,
            qualified_task_ids=self.qualified_task_ids,
            source_capability_set_sha256=self.source_capability_set_sha256,
            platform=self.platform,
            network_mode=self.network_mode,
            source_inventory_sha256=self.source_inventory_sha256,
            build_plan_sha256=self.build_plan_sha256,
            build_manifest_sha256=self.build_manifest_sha256,
            build_verification_sha256=self.build_verification_sha256,
            image_set_sha256=self.image_set_sha256,
            image_id_by_stratum=self.image_id_by_stratum,
            evaluator_sha256=self.evaluator_sha256,
            state_normalization_revision=self.state_normalization_revision,
            state_normalization_source_sha256=(
                self.state_normalization_source_sha256
            ),
            state_normalization_sha256=self.state_normalization_sha256,
            lifecycle_identity_set_sha256=self.lifecycle_identity_set_sha256,
            container_identity_set_sha256=self.container_identity_set_sha256,
            journal_sha256=self.journal_sha256,
            journal_root_sha256=self.journal_root_sha256,
            evidence_root_sha256=self.evidence_root_sha256,
        )

    def _validate(self) -> None:
        expected = (
            self.source_revision == INTERCODE_REVISION
            and self.source_population_sha256 == PUBLIC_POPULATION_SHA256
            and self.source_corpus_sha256 == SOURCE_CORPUS_SHA256
            and self.static_exclusion_audit_sha256
            == STATIC_EXCLUSION_AUDIT_SHA256
            and self.sample_manifest_sha256
            == "sha256:" + V07_SAMPLE_MANIFEST_SHA256
            and self.task_count == V07_QUALIFICATION_TASK_COUNT
            and self.replay_count == V07_QUALIFICATION_REPLAY_COUNT
            and self.replays_per_task == V07_QUALIFICATION_REPLAYS_PER_TASK
            and self.qualified_task_ids == CAMPAIGN_TASK_IDS
            and self.platform == V07_QUALIFICATION_PLATFORM
            and self.network_mode == V07_QUALIFICATION_NETWORK_MODE
            and self.evaluator_sha256 == V07_STRICT_REPLAY_EVALUATOR_SHA256
            and self.state_normalization_revision
            == V07_STATE_NORMALIZATION_REVISION
        )
        if not expected:
            raise V07QualificationError(
                "verified qualification evidence differs from the frozen design"
            )
        _validate_images(self.image_id_by_stratum)
        for field in (
            "source_capability_set_sha256",
            "source_inventory_sha256",
            "build_plan_sha256",
            "build_manifest_sha256",
            "build_verification_sha256",
            "image_set_sha256",
            "state_normalization_source_sha256",
            "state_normalization_sha256",
            "lifecycle_identity_set_sha256",
            "container_identity_set_sha256",
            "journal_sha256",
            "journal_root_sha256",
            "evidence_root_sha256",
            "suite_sha256",
        ):
            _require_digest(getattr(self, field), field)
        if self.evidence_root_sha256 != self.journal_root_sha256:
            raise V07QualificationError("qualification evidence root is inconsistent")
        if _digest(self._core_record()) != self.suite_sha256:
            raise V07QualificationError("qualification suite root is inconsistent")

    def to_public_record(self) -> dict[str, object]:
        self._validate()
        return {**self._core_record(), "suite_sha256": self.suite_sha256}

    def require_admitted(self) -> None:
        self._validate()

    def __repr__(self) -> str:
        return (
            "<VerifiedV07QualificationEvidence "
            f"root={self.evidence_root_sha256} tasks={self.task_count}>"
        )


def _public_core_from_values(
    *,
    source_revision: str,
    source_population_sha256: str,
    source_corpus_sha256: str,
    static_exclusion_audit_sha256: str,
    sample_manifest_sha256: str,
    task_count: int,
    replay_count: int,
    replays_per_task: int,
    qualified_task_ids: tuple[str, ...],
    source_capability_set_sha256: str,
    platform: str,
    network_mode: str,
    source_inventory_sha256: str,
    build_plan_sha256: str,
    build_manifest_sha256: str,
    build_verification_sha256: str,
    image_set_sha256: str,
    image_id_by_stratum: Mapping[str, str],
    evaluator_sha256: str,
    state_normalization_revision: str,
    state_normalization_source_sha256: str,
    state_normalization_sha256: str,
    lifecycle_identity_set_sha256: str,
    container_identity_set_sha256: str,
    journal_sha256: str,
    journal_root_sha256: str,
    evidence_root_sha256: str,
) -> dict[str, object]:
    return {
        "schema": V07_QUALIFICATION_SCHEMA,
        "status": "qualified",
        "evidence_root_sha256": evidence_root_sha256,
        "source": {
            "revision": source_revision,
            "population_sha256": source_population_sha256,
            "corpus_sha256": source_corpus_sha256,
            "static_exclusion_audit_sha256": static_exclusion_audit_sha256,
            "capability_set_sha256": source_capability_set_sha256,
            "inventory_sha256": source_inventory_sha256,
        },
        "sample": {
            "manifest_sha256": sample_manifest_sha256,
            "task_count": task_count,
            "qualified_task_ids": list(qualified_task_ids),
        },
        "replays": {
            "count": replay_count,
            "per_task": replays_per_task,
            "platform": platform,
            "network_mode": network_mode,
            "lifecycle_identity_set_sha256": lifecycle_identity_set_sha256,
            "container_identity_set_sha256": container_identity_set_sha256,
        },
        "pins": {
            "image_build": {
                "plan_sha256": build_plan_sha256,
                "manifest_sha256": build_manifest_sha256,
                "verification_sha256": build_verification_sha256,
            },
            "image_set_sha256": image_set_sha256,
            "image_id_by_stratum": dict(image_id_by_stratum),
            "evaluator_sha256": evaluator_sha256,
            "state_normalization_revision": state_normalization_revision,
            "state_normalization_source_sha256": (
                state_normalization_source_sha256
            ),
            "state_normalization_sha256": state_normalization_sha256,
        },
        "journal": {
            "sha256": journal_sha256,
            "root_sha256": journal_root_sha256,
            "sealed": True,
            "mode": "0600",
        },
    }


def build_v07_qualification_evidence(
    *,
    source: InterCodeSource,
    journal_path: str | Path,
    source_inventory_sha256: str,
    build_plan_sha256: str,
    build_manifest_sha256: str,
    build_verification_sha256: str,
    image_set_sha256: str,
    image_id_by_stratum: Mapping[str, str],
    evaluator_sha256: str,
    state_normalization_revision: str,
    state_normalization_source_sha256: str,
    state_normalization_sha256: str,
    replays: tuple[V07QualificationReplay, ...],
) -> VerifiedV07QualificationEvidence:
    """Validate all replay facts, append one new journal, seal, and reverify."""

    images, evaluator, normalizer, provenance = _validate_inputs(
        source,
        source_inventory_sha256,
        build_plan_sha256,
        build_manifest_sha256,
        build_verification_sha256,
        image_set_sha256,
        image_id_by_stratum,
        evaluator_sha256,
        state_normalization_revision,
        state_normalization_source_sha256,
        state_normalization_sha256,
    )
    validated = _validate_replay_matrix(
        source,
        replays,
        images,
        evaluator,
        normalizer,
    )
    path = Path(journal_path)
    _precreate_journal(path)
    append_journal_event(
        path,
        {
            "type": "v07_qualification_started",
            "schema": V07_QUALIFICATION_SCHEMA,
            "source_revision": INTERCODE_REVISION,
            "source_population_sha256": PUBLIC_POPULATION_SHA256,
            "source_corpus_sha256": SOURCE_CORPUS_SHA256,
            "static_exclusion_audit_sha256": STATIC_EXCLUSION_AUDIT_SHA256,
            "sample_manifest_sha256": "sha256:" + V07_SAMPLE_MANIFEST_SHA256,
            "task_ids": list(CAMPAIGN_TASK_IDS),
            "expected_task_count": V07_QUALIFICATION_TASK_COUNT,
            "expected_replay_count": V07_QUALIFICATION_REPLAY_COUNT,
            "replays_per_task": V07_QUALIFICATION_REPLAYS_PER_TASK,
            "platform": V07_QUALIFICATION_PLATFORM,
            "network_mode": V07_QUALIFICATION_NETWORK_MODE,
            **provenance,
            "image_id_by_stratum": images,
            "evaluator_sha256": evaluator,
            "state_normalization_sha256": normalizer,
        },
    )
    replay_event_roots: list[str] = []
    for replay in validated:
        event = append_journal_event(path, replay._journal_record())
        replay_event_roots.append("sha256:" + str(event["event_sha256"]))
    roots = _matrix_roots(validated)
    append_journal_event(
        path,
        {
            "type": "v07_qualification_completed",
            "task_count": V07_QUALIFICATION_TASK_COUNT,
            "replay_count": V07_QUALIFICATION_REPLAY_COUNT,
            "ordered_replay_events_sha256": _digest(replay_event_roots),
            **roots,
        },
    )
    seal_journal(path)
    return verify_v07_qualification_evidence(
        source=source,
        journal_path=path,
        source_inventory_sha256=provenance["source_inventory_sha256"],
        build_plan_sha256=provenance["build_plan_sha256"],
        build_manifest_sha256=provenance["build_manifest_sha256"],
        build_verification_sha256=provenance["build_verification_sha256"],
        image_set_sha256=provenance["image_set_sha256"],
        image_id_by_stratum=images,
        evaluator_sha256=evaluator,
        state_normalization_revision=provenance[
            "state_normalization_revision"
        ],
        state_normalization_source_sha256=provenance[
            "state_normalization_source_sha256"
        ],
        state_normalization_sha256=normalizer,
    )


def verify_v07_qualification_evidence(
    *,
    source: InterCodeSource,
    journal_path: str | Path,
    source_inventory_sha256: str,
    build_plan_sha256: str,
    build_manifest_sha256: str,
    build_verification_sha256: str,
    image_set_sha256: str,
    image_id_by_stratum: Mapping[str, str],
    evaluator_sha256: str,
    state_normalization_revision: str,
    state_normalization_source_sha256: str,
    state_normalization_sha256: str,
) -> VerifiedV07QualificationEvidence:
    """Independently verify one sealed private journal and redact its evidence."""

    images, evaluator, normalizer, provenance = _validate_inputs(
        source,
        source_inventory_sha256,
        build_plan_sha256,
        build_manifest_sha256,
        build_verification_sha256,
        image_set_sha256,
        image_id_by_stratum,
        evaluator_sha256,
        state_normalization_revision,
        state_normalization_source_sha256,
        state_normalization_sha256,
    )
    journal_bytes = _read_secure_mode_0600(Path(journal_path))
    records = _decode_journal(journal_bytes)
    if not records or records[-1].get("type") != SEALED_EVENT_TYPE:
        raise V07QualificationError("qualification journal is not sealed")
    if len(records) != 63:
        raise V07QualificationError(
            "qualification journal does not contain the exact replay matrix"
        )

    start = records[0]
    _expect_keys(start, _START_FIELDS)
    if _without_chain(start) != {
        "type": "v07_qualification_started",
        "schema": V07_QUALIFICATION_SCHEMA,
        "source_revision": INTERCODE_REVISION,
        "source_population_sha256": PUBLIC_POPULATION_SHA256,
        "source_corpus_sha256": SOURCE_CORPUS_SHA256,
        "static_exclusion_audit_sha256": STATIC_EXCLUSION_AUDIT_SHA256,
        "sample_manifest_sha256": "sha256:" + V07_SAMPLE_MANIFEST_SHA256,
        "task_ids": list(CAMPAIGN_TASK_IDS),
        "expected_task_count": V07_QUALIFICATION_TASK_COUNT,
        "expected_replay_count": V07_QUALIFICATION_REPLAY_COUNT,
        "replays_per_task": V07_QUALIFICATION_REPLAYS_PER_TASK,
        "platform": V07_QUALIFICATION_PLATFORM,
        "network_mode": V07_QUALIFICATION_NETWORK_MODE,
        **provenance,
        "image_id_by_stratum": images,
        "evaluator_sha256": evaluator,
        "state_normalization_sha256": normalizer,
    }:
        raise V07QualificationError(
            "qualification journal genesis differs from the frozen pins"
        )

    replay_records = records[1:61]
    replays: list[V07QualificationReplay] = []
    replay_event_roots: list[str] = []
    for record in replay_records:
        _expect_keys(record, _REPLAY_FIELDS)
        payload = _without_chain(record)
        if payload.pop("type", None) != "v07_qualification_replay_completed":
            raise V07QualificationError("qualification replay event type is invalid")
        try:
            replay = _issue_trusted_v07_qualification_replay(**payload)
        except (TypeError, V07QualificationError) as error:
            raise V07QualificationError(
                "qualification replay event fields are invalid"
            ) from error
        replays.append(replay)
        replay_event_roots.append("sha256:" + str(record["event_sha256"]))
    validated = _validate_replay_matrix(
        source,
        tuple(replays),
        images,
        evaluator,
        normalizer,
    )
    roots = _matrix_roots(validated)

    completed = records[61]
    _expect_keys(completed, _COMPLETED_FIELDS)
    expected_completed = {
        "type": "v07_qualification_completed",
        "task_count": V07_QUALIFICATION_TASK_COUNT,
        "replay_count": V07_QUALIFICATION_REPLAY_COUNT,
        "ordered_replay_events_sha256": _digest(replay_event_roots),
        **roots,
    }
    if _without_chain(completed) != expected_completed:
        raise V07QualificationError(
            "qualification completion summary differs from raw events"
        )

    seal = records[62]
    _expect_keys(seal, _SEAL_FIELDS)
    if seal.get("sealed_event_count") != 62:
        raise V07QualificationError("qualification journal seal count is invalid")
    journal_sha256 = "sha256:" + sha256(journal_bytes).hexdigest()
    journal_root_sha256 = "sha256:" + str(seal["event_sha256"])
    values = {
        "source_revision": INTERCODE_REVISION,
        "source_population_sha256": PUBLIC_POPULATION_SHA256,
        "source_corpus_sha256": SOURCE_CORPUS_SHA256,
        "static_exclusion_audit_sha256": STATIC_EXCLUSION_AUDIT_SHA256,
        "sample_manifest_sha256": "sha256:" + V07_SAMPLE_MANIFEST_SHA256,
        "task_count": V07_QUALIFICATION_TASK_COUNT,
        "replay_count": V07_QUALIFICATION_REPLAY_COUNT,
        "replays_per_task": V07_QUALIFICATION_REPLAYS_PER_TASK,
        "qualified_task_ids": CAMPAIGN_TASK_IDS,
        "platform": V07_QUALIFICATION_PLATFORM,
        "network_mode": V07_QUALIFICATION_NETWORK_MODE,
        **provenance,
        "image_id_by_stratum": images,
        "evaluator_sha256": evaluator,
        "state_normalization_sha256": normalizer,
        "journal_sha256": journal_sha256,
        "journal_root_sha256": journal_root_sha256,
        "evidence_root_sha256": journal_root_sha256,
        **roots,
    }
    suite_sha256 = _digest(_public_core_from_values(**values))
    return VerifiedV07QualificationEvidence(
        **values,
        suite_sha256=suite_sha256,
        _construction_seal=_EVIDENCE_CONSTRUCTION_SEAL,
    )


def _validate_inputs(
    source: InterCodeSource,
    source_inventory_sha256: str,
    build_plan_sha256: str,
    build_manifest_sha256: str,
    build_verification_sha256: str,
    image_set_sha256: str,
    image_id_by_stratum: Mapping[str, str],
    evaluator_sha256: str,
    state_normalization_revision: str,
    state_normalization_source_sha256: str,
    state_normalization_sha256: str,
) -> tuple[dict[str, str], str, str, dict[str, str]]:
    _validate_source(source)
    provenance = {
        "source_inventory_sha256": _require_digest(
            source_inventory_sha256,
            "qualification source inventory",
        ),
        "build_plan_sha256": _require_digest(
            build_plan_sha256,
            "qualification image-build plan",
        ),
        "build_manifest_sha256": _require_digest(
            build_manifest_sha256,
            "qualification image-build manifest",
        ),
        "build_verification_sha256": _require_digest(
            build_verification_sha256,
            "qualification image-build verification",
        ),
        "image_set_sha256": _require_digest(
            image_set_sha256,
            "qualification image set",
        ),
        "state_normalization_revision": state_normalization_revision,
        "state_normalization_source_sha256": _require_digest(
            state_normalization_source_sha256,
            "qualification normalizer source",
        ),
    }
    if state_normalization_revision != V07_STATE_NORMALIZATION_REVISION:
        raise V07QualificationError(
            "qualification normalizer revision differs from the frozen design"
        )
    images = _validate_images(image_id_by_stratum)
    evaluator = _require_digest(evaluator_sha256, "qualification evaluator")
    if evaluator != V07_STRICT_REPLAY_EVALUATOR_SHA256:
        raise V07QualificationError(
            "qualification evaluator differs from the frozen strict evaluator"
        )
    normalizer = _require_digest(
        state_normalization_sha256,
        "qualification normalizer",
    )
    return images, evaluator, normalizer, provenance


def _validate_source(source: InterCodeSource) -> None:
    if type(source) is not InterCodeSource:
        raise V07QualificationError(
            "qualification source must be an exact verified InterCodeSource"
        )
    if (
        source.population_sha256 != PUBLIC_POPULATION_SHA256
        or source.source_sha256 != SOURCE_CORPUS_SHA256
        or source.static_exclusion_audit_sha256
        != STATIC_EXCLUSION_AUDIT_SHA256
    ):
        raise V07QualificationError("qualification source identity drifted")
    try:
        selected = build_v07_sample(source)
    except (ValueError, RuntimeError) as error:
        raise V07QualificationError(
            "qualification source does not reproduce the frozen sample"
        ) from error
    if selected != CAMPAIGN_TASK_IDS or selected != V07_TASK_IDS:
        raise V07QualificationError("qualification sample identity drifted")


def _validate_images(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise V07QualificationError("qualification images must be a mapping")
    images = dict(value)
    if tuple(sorted(images)) != _STRATA:
        raise V07QualificationError(
            "qualification requires exactly four stratum image IDs"
        )
    ordered = {
        stratum: _require_digest(images[stratum], f"{stratum} image")
        for stratum in _STRATA
    }
    if len(set(ordered.values())) != len(_STRATA):
        raise V07QualificationError(
            "qualification requires four distinct image IDs"
        )
    return ordered


def _validate_replay_matrix(
    source: InterCodeSource,
    replays: tuple[V07QualificationReplay, ...],
    images: Mapping[str, str],
    evaluator: str,
    normalizer: str,
) -> tuple[V07QualificationReplay, ...]:
    if type(replays) is not tuple or any(
        type(replay) is not V07QualificationReplay for replay in replays
    ):
        raise V07QualificationError(
            "qualification replays must be an exact frozen tuple"
        )
    expected_keys = tuple(
        (task_id, replay_index)
        for task_id in CAMPAIGN_TASK_IDS
        for replay_index in (1, 2)
    )
    if tuple(replay.key for replay in replays) != expected_keys:
        raise V07QualificationError(
            "qualification replays must use the exact frozen order"
        )
    used_lifecycles: set[str] = set()
    used_containers: set[str] = set()
    for replay in replays:
        try:
            reference = source.private_reference(replay.task_id)
            task, capability = source.qualification_identity(reference)
        except InterCodeSourceError as error:
            raise V07QualificationError(
                "qualification replay has an unknown source capability"
            ) from error
        if replay.stratum != task.stratum:
            raise V07QualificationError("qualification replay stratum drifted")
        if replay.source_capability_sha256 != capability:
            raise V07QualificationError(
                "qualification replay source capability drifted"
            )
        if replay.image_id != images[replay.stratum]:
            raise V07QualificationError("qualification replay image pin drifted")
        if replay.evaluator_sha256 != evaluator:
            raise V07QualificationError("qualification replay evaluator pin drifted")
        if replay.state_normalization_sha256 != normalizer:
            raise V07QualificationError("qualification replay normalizer pin drifted")
        if replay.platform != V07_QUALIFICATION_PLATFORM:
            raise V07QualificationError(
                "qualification replay platform is not native linux/arm64"
            )
        if replay.network_mode != V07_QUALIFICATION_NETWORK_MODE:
            raise V07QualificationError(
                "qualification replay network mode is not none"
            )
        if not replay.container_absent_before or not replay.clean_initial_state:
            raise V07QualificationError(
                "qualification replay lacks a clean initial container state"
            )
        if not replay.container_profile_match:
            raise V07QualificationError(
                "qualification replay container profile is invalid"
            )
        if not replay.infrastructure_valid:
            raise V07QualificationError(
                "qualification replay infrastructure is invalid"
            )
        if not replay.setup_valid:
            raise V07QualificationError("qualification replay setup is invalid")
        if not replay.evaluator_valid:
            raise V07QualificationError("qualification replay evaluator is invalid")
        if not replay.gold_replay_passed:
            raise V07QualificationError("qualification gold replay did not pass")
        if not (
            replay.container_destroyed
            and replay.container_absent_after
            and replay.cleanup_verified
        ):
            raise V07QualificationError(
                "qualification replay cleanup was not verified"
            )
        if replay.lifecycle_identity_sha256 in used_lifecycles:
            raise V07QualificationError(
                "qualification lifecycle identity is not unique"
            )
        if replay.container_identity_sha256 in used_containers:
            raise V07QualificationError(
                "qualification container identity is not unique"
            )
        used_lifecycles.add(replay.lifecycle_identity_sha256)
        used_containers.add(replay.container_identity_sha256)

    equality_fields = (
        "exit_policy_sha256",
        "initial_state_sha256",
        "normalized_stdout_sha256",
        "normalized_stderr_sha256",
        "observable_state_sha256",
    )
    for index in range(0, len(replays), 2):
        first, second = replays[index : index + 2]
        if any(
            getattr(first, field) != getattr(second, field)
            for field in equality_fields
        ):
            raise V07QualificationError(
                f"qualification gold replays disagree for {first.task_id}"
            )
    return replays


def _matrix_roots(
    replays: tuple[V07QualificationReplay, ...],
) -> dict[str, str]:
    capabilities = [
        {
            "task_id": replay.task_id,
            "source_capability_sha256": replay.source_capability_sha256,
        }
        for replay in replays[::2]
    ]
    return {
        "source_capability_set_sha256": _digest(capabilities),
        "lifecycle_identity_set_sha256": _digest(
            [replay.lifecycle_identity_sha256 for replay in replays]
        ),
        "container_identity_set_sha256": _digest(
            [replay.container_identity_sha256 for replay in replays]
        ),
    }


def _precreate_journal(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent = path.parent.lstat()
    except OSError as error:
        raise V07QualificationError(
            "qualification journal parent is unavailable"
        ) from error
    if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode):
        raise V07QualificationError(
            "qualification journal parent must be a real directory"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise V07QualificationError("qualification journal requires no-follow opens")
    flags |= nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.fspath(path), flags, 0o600)
    except FileExistsError:
        raise V07QualificationError(
            "qualification journal already exists; it will not be overwritten"
        ) from None
    except OSError as error:
        raise V07QualificationError(
            "qualification journal could not be created safely"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise V07QualificationError(
                "qualification journal is not a regular file"
            )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_secure_mode_0600(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise V07QualificationError("qualification journal is unavailable") from error
    _validate_journal_metadata(before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise V07QualificationError("qualification journal requires no-follow opens")
    try:
        descriptor = os.open(os.fspath(path), flags | nofollow)
    except OSError as error:
        raise V07QualificationError(
            "qualification journal could not be opened safely"
        ) from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        opened = os.fstat(descriptor)
        _validate_journal_metadata(opened)
        if _journal_metadata_identity(opened) != _journal_metadata_identity(before):
            raise V07QualificationError("qualification journal identity changed")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > V07_QUALIFICATION_MAX_JOURNAL_BYTES:
                raise V07QualificationError(
                    "qualification journal exceeds its byte bound"
                )
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        after = path.lstat()
        _validate_journal_metadata(finished)
        _validate_journal_metadata(after)
        if (
            _journal_metadata_identity(finished)
            != _journal_metadata_identity(opened)
            or _journal_metadata_identity(after)
            != _journal_metadata_identity(opened)
            or finished.st_size != size
        ):
            raise V07QualificationError("qualification journal identity changed")
        return b"".join(chunks)
    except OSError as error:
        raise V07QualificationError("qualification journal read failed") from error
    finally:
        os.close(descriptor)


def _validate_journal_metadata(metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise V07QualificationError(
            "qualification journal must be a regular non-symlink file"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise V07QualificationError("qualification journal must use mode 0600")
    if metadata.st_uid != os.getuid():
        raise V07QualificationError("qualification journal must be owner-owned")


def _journal_metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _decode_journal(payload: bytes) -> list[dict[str, object]]:
    if not payload or not payload.endswith(b"\n"):
        raise V07QualificationError("qualification journal is not sealed or complete")
    records: list[dict[str, object]] = []
    expected_sequence = 1
    previous_sha256 = GENESIS_EVENT_SHA256
    sealed = False
    for line in payload.splitlines():
        try:
            text = line.decode("utf-8")
            raw = json.loads(text, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey) as error:
            raise V07QualificationError(
                "qualification journal contains invalid JSON"
            ) from error
        if not isinstance(raw, dict):
            raise V07QualificationError(
                "qualification journal record must be an object"
            )
        if _canonical_json(raw) != line:
            raise V07QualificationError(
                "qualification journal record is not canonical"
            )
        if raw.get("sequence") != expected_sequence:
            raise V07QualificationError(
                "qualification journal sequence is invalid"
            )
        if raw.get("previous_event_sha256") != previous_sha256:
            raise V07QualificationError(
                "qualification journal previous hash is invalid"
            )
        event_sha256 = raw.get("event_sha256")
        if type(event_sha256) is not str or re.fullmatch(
            r"[0-9a-f]{64}", event_sha256
        ) is None:
            raise V07QualificationError(
                "qualification journal event hash is invalid"
            )
        expected_hash = sha256(canonical_event_bytes(raw)).hexdigest()
        if event_sha256 != expected_hash:
            raise V07QualificationError(
                "qualification journal event hash does not match"
            )
        event_type = raw.get("type")
        if sealed:
            raise V07QualificationError(
                "qualification journal has records after its seal"
            )
        if event_type == SEALED_EVENT_TYPE:
            if raw.get("sealed_event_count") != expected_sequence - 1:
                raise V07QualificationError(
                    "qualification journal seal count is invalid"
                )
            sealed = True
        previous_sha256 = event_sha256
        expected_sequence += 1
        records.append(raw)
    if not sealed:
        raise V07QualificationError("qualification journal is not sealed")
    return records


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey(key)
        value[key] = item
    return value


def _without_chain(record: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in record.items() if key not in _CHAIN_FIELDS}


def _expect_keys(record: Mapping[str, object], fields: set[str]) -> None:
    if set(record) != fields | _CHAIN_FIELDS:
        raise V07QualificationError(
            "qualification journal record fields are invalid"
        )


_START_FIELDS = {
    "type",
    "schema",
    "source_revision",
    "source_population_sha256",
    "source_corpus_sha256",
    "static_exclusion_audit_sha256",
    "sample_manifest_sha256",
    "task_ids",
    "expected_task_count",
    "expected_replay_count",
    "replays_per_task",
    "platform",
    "network_mode",
    "source_inventory_sha256",
    "build_plan_sha256",
    "build_manifest_sha256",
    "build_verification_sha256",
    "image_set_sha256",
    "image_id_by_stratum",
    "evaluator_sha256",
    "state_normalization_revision",
    "state_normalization_source_sha256",
    "state_normalization_sha256",
}
_REPLAY_FIELDS = {field.name for field in fields(V07QualificationReplay)} | {"type"}
_COMPLETED_FIELDS = {
    "type",
    "task_count",
    "replay_count",
    "ordered_replay_events_sha256",
    "source_capability_set_sha256",
    "lifecycle_identity_set_sha256",
    "container_identity_set_sha256",
}
_SEAL_FIELDS = {"type", "sealed_event_count"}
