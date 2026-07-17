"""Sealed accounting for actual human and operator interventions in v0.7."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import weakref
from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from enum import Enum
from pathlib import Path

from . import journal as _journal
from .intercode_campaign_ledger import (
    CAMPAIGN_ARMS,
    CAMPAIGN_MODELS,
    CAMPAIGN_SEED,
    CAMPAIGN_TASK_IDS,
    CampaignEpisode,
    CampaignSpec,
)
from .intercode_v07_manifest import (
    V07_INTERVENTION_JOURNAL_REVISION,
    V07_SCHEDULE_SHA256,
)
from .journal import JournalError, append_journal_event, seal_journal


_DECLARATION = "intervention_journal_declared"
_DECLARATION_SCHEMA = "intercode-v0.7-intervention-declaration-evidence-v2"
_SUMMARY_SCHEMA = "intercode-v0.7-intervention-summary-v2"
_MAX_JOURNAL_BYTES = 4 << 20
_MAX_JOURNAL_RECORDS = 4_096
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SUMMARY_AUTHORITY = object()
_DECLARATION_AUTHORITY = object()
_CATEGORY_TYPES = (
    "benchmark_model_human_prompt",
    "operational_action",
    "operational_reconciliation",
    "operational_restart",
    "orchestrator_operator_approval",
    "orchestrator_operator_instruction",
)
_ORCHESTRATOR_TYPES = frozenset(
    {"orchestrator_operator_approval", "orchestrator_operator_instruction"}
)
_OPERATIONAL_TYPES = frozenset(
    {"operational_action", "operational_reconciliation", "operational_restart"}
)
_CHAIN_FIELDS = frozenset(
    {"sequence", "previous_event_sha256", "event_sha256"}
)
_SPEC = CampaignSpec(CAMPAIGN_TASK_IDS)
if _SPEC.schedule_sha256 != V07_SCHEDULE_SHA256:
    raise RuntimeError("intervention accounting schedule differs from v0.7 manifest")


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


V07_INTERVENTION_STUDY_SHA256 = _digest(
    {
        "arms": list(CAMPAIGN_ARMS),
        "journal_revision": V07_INTERVENTION_JOURNAL_REVISION,
        "models": list(CAMPAIGN_MODELS),
        "schedule_sha256": V07_SCHEDULE_SHA256,
        "seed": CAMPAIGN_SEED,
        "study": "intercode-v0.7-30task",
        "task_ids": list(CAMPAIGN_TASK_IDS),
    }
)


class V07InterventionError(ValueError):
    """Intervention evidence is unsafe, malformed, or unsealed."""


class V07InterventionPhase(str, Enum):
    PREPARATION = "preparation"
    QUALIFICATION = "qualification"
    CALIBRATION = "calibration"
    CONFIRMATORY = "confirmatory"
    ANALYSIS = "analysis"
    REPORTING = "reporting"


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class VerifiedV07InterventionDeclaration:
    """Verifier-issued identity for the live, unsealed intervention journal."""

    study_identity_sha256: str
    schedule_sha256: str
    journal_revision: str
    journal_instance_sha256: str
    declaration_sha256: str
    declaration_evidence_sha256: str
    _authority: InitVar[object | None] = None

    def __post_init__(self, _authority: object | None) -> None:
        if _authority is not _DECLARATION_AUTHORITY:
            raise V07InterventionError(
                "intervention declarations must be verifier-built"
            )
        _validate_declaration(self)

    def _core_record(self) -> dict[str, object]:
        return {
            "declaration_sha256": self.declaration_sha256,
            "journal_instance_sha256": self.journal_instance_sha256,
            "journal_revision": self.journal_revision,
            "schedule_sha256": self.schedule_sha256,
            "schema": _DECLARATION_SCHEMA,
            "study_identity_sha256": self.study_identity_sha256,
        }

    def canonical_record(self) -> dict[str, object]:
        _require_issued_declaration(self)
        return {
            **self._core_record(),
            "declaration_evidence_sha256": self.declaration_evidence_sha256,
        }


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class VerifiedV07InterventionSummary:
    study_identity_sha256: str
    schedule_sha256: str
    journal_root_sha256: str
    journal_file_sha256: str
    journal_instance_sha256: str
    declaration_sha256: str
    journal_record_count: int
    intervention_event_count: int
    benchmark_model_human_prompt_count: int
    orchestrator_operator_event_count: int
    operational_event_count: int
    automatic_model_prompt_count: None
    automatic_model_prompt_source: str
    summary_sha256: str
    _category_counts: tuple[tuple[str, int], ...]
    _phase_counts: tuple[tuple[str, int], ...]
    _authority: InitVar[object | None] = None

    def __post_init__(self, _authority: object | None) -> None:
        if _authority is not _SUMMARY_AUTHORITY:
            raise V07InterventionError(
                "intervention summaries must be verifier-built"
            )
        _validate_summary(self)

    @property
    def counts_by_category(self) -> dict[str, int]:
        _require_issued_summary(self)
        return dict(self._category_counts)

    @property
    def counts_by_phase(self) -> dict[str, int]:
        _require_issued_summary(self)
        return dict(self._phase_counts)

    def _core_record(self) -> dict[str, object]:
        return {
            "automatic_model_prompt_count": None,
            "automatic_model_prompt_source": self.automatic_model_prompt_source,
            "benchmark_model_human_prompt_count": (
                self.benchmark_model_human_prompt_count
            ),
            "counts_by_category": dict(self._category_counts),
            "counts_by_phase": dict(self._phase_counts),
            "declaration_sha256": self.declaration_sha256,
            "intervention_event_count": self.intervention_event_count,
            "journal_file_sha256": self.journal_file_sha256,
            "journal_instance_sha256": self.journal_instance_sha256,
            "journal_record_count": self.journal_record_count,
            "journal_root_sha256": self.journal_root_sha256,
            "operational_event_count": self.operational_event_count,
            "operator_approval_is_benchmark_model_prompt": False,
            "orchestrator_operator_event_count": (
                self.orchestrator_operator_event_count
            ),
            "schedule_sha256": self.schedule_sha256,
            "schema": _SUMMARY_SCHEMA,
            "study_identity_sha256": self.study_identity_sha256,
            "unresolved_handoff_is_human_prompt": False,
        }

    def canonical_record(self) -> dict[str, object]:
        _require_issued_summary(self)
        return {**self._core_record(), "summary_sha256": self.summary_sha256}


_ISSUED_SUMMARIES: weakref.WeakSet[VerifiedV07InterventionSummary] = (
    weakref.WeakSet()
)
_ISSUED_DECLARATIONS: weakref.WeakSet[VerifiedV07InterventionDeclaration] = (
    weakref.WeakSet()
)


def declare_v07_intervention_journal(path: str | Path) -> None:
    """Create the exclusive declaration before any study-side mutation."""

    target = Path(path)
    _precreate_mode_0600(target)
    declaration = {
        "type": _DECLARATION,
        "automatic_model_prompt_source": "controller_evidence_only",
        "caller_entered_categories": list(_CATEGORY_TYPES),
        "journal_revision": V07_INTERVENTION_JOURNAL_REVISION,
        "journal_instance_sha256": (
            "sha256:" + hashlib.sha256(os.urandom(32)).hexdigest()
        ),
        "operator_approval_is_benchmark_model_prompt": False,
        "schedule_sha256": V07_SCHEDULE_SHA256,
        "study_identity_sha256": V07_INTERVENTION_STUDY_SHA256,
        "unresolved_handoff_is_human_prompt": False,
    }
    _append_exact(target, declaration, require_declaration=False)


def append_benchmark_model_human_prompt(
    path: str | Path,
    *,
    phase: V07InterventionPhase | None = None,
    model_id: str | None = None,
    episode: CampaignEpisode | None = None,
) -> None:
    _append_category(path, "benchmark_model_human_prompt", phase, model_id, episode)


def append_orchestrator_operator_instruction(
    path: str | Path,
    *,
    phase: V07InterventionPhase | None = None,
    model_id: str | None = None,
    episode: CampaignEpisode | None = None,
) -> None:
    _append_category(
        path, "orchestrator_operator_instruction", phase, model_id, episode
    )


def append_orchestrator_operator_approval(
    path: str | Path,
    *,
    phase: V07InterventionPhase | None = None,
    model_id: str | None = None,
    episode: CampaignEpisode | None = None,
) -> None:
    _append_category(path, "orchestrator_operator_approval", phase, model_id, episode)


def append_operational_action(
    path: str | Path,
    *,
    phase: V07InterventionPhase | None = None,
    model_id: str | None = None,
    episode: CampaignEpisode | None = None,
) -> None:
    _append_category(path, "operational_action", phase, model_id, episode)


def append_operational_restart(
    path: str | Path,
    *,
    phase: V07InterventionPhase | None = None,
    model_id: str | None = None,
    episode: CampaignEpisode | None = None,
) -> None:
    _append_category(path, "operational_restart", phase, model_id, episode)


def append_operational_reconciliation(
    path: str | Path,
    *,
    phase: V07InterventionPhase | None = None,
    model_id: str | None = None,
    episode: CampaignEpisode | None = None,
) -> None:
    _append_category(path, "operational_reconciliation", phase, model_id, episode)


def seal_v07_intervention_journal(path: str | Path) -> None:
    target = Path(path)
    records, _payload, _inspection = _read_secure_snapshot(target, sealed=False)
    _validate_records(records, require_sealed=False)
    before = _named_metadata(target)
    try:
        seal_journal(target)
    except (OSError, JournalError, ValueError) as error:
        raise V07InterventionError("intervention journal could not be sealed") from error
    _require_same_named_file(before, _named_metadata(target), allow_growth=True)
    records, _payload, _inspection = _read_secure_snapshot(target, sealed=True)
    _validate_records(records, require_sealed=True)


def verify_v07_intervention_journal(
    path: str | Path,
) -> VerifiedV07InterventionSummary:
    records, payload, inspection = _read_secure_snapshot(Path(path), sealed=True)
    events = _validate_records(records, require_sealed=True)
    category_counts = {category: 0 for category in _CATEGORY_TYPES}
    phase_counts = {
        **{phase.value: 0 for phase in V07InterventionPhase},
        "unspecified": 0,
    }
    for event in events:
        category = str(event["type"])
        category_counts[category] += 1
        phase_counts[str(event.get("phase", "unspecified"))] += 1
    core_values: dict[str, object] = {
        "study_identity_sha256": V07_INTERVENTION_STUDY_SHA256,
        "schedule_sha256": V07_SCHEDULE_SHA256,
        "journal_root_sha256": "sha256:" + inspection.last_event_sha256,
        "journal_file_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "journal_instance_sha256": str(records[0]["journal_instance_sha256"]),
        "declaration_sha256": "sha256:" + str(records[0]["event_sha256"]),
        "journal_record_count": len(records),
        "intervention_event_count": len(events),
        "benchmark_model_human_prompt_count": category_counts[
            "benchmark_model_human_prompt"
        ],
        "orchestrator_operator_event_count": sum(
            category_counts[category] for category in _ORCHESTRATOR_TYPES
        ),
        "operational_event_count": sum(
            category_counts[category] for category in _OPERATIONAL_TYPES
        ),
        "automatic_model_prompt_count": None,
        "automatic_model_prompt_source": "controller_evidence_only",
        "_category_counts": tuple(sorted(category_counts.items())),
        "_phase_counts": tuple(sorted(phase_counts.items())),
    }
    summary_core = _summary_core_record(core_values)
    summary = VerifiedV07InterventionSummary(
        **core_values,  # type: ignore[arg-type]
        summary_sha256=_digest(summary_core),
        _authority=_SUMMARY_AUTHORITY,
    )
    _ISSUED_SUMMARIES.add(summary)
    return summary


def verify_v07_intervention_declaration(
    path: str | Path,
) -> VerifiedV07InterventionDeclaration:
    """Verify the exact first record while the journal remains appendable."""

    records, _payload, _inspection = _read_secure_snapshot(
        Path(path),
        sealed=False,
    )
    _validate_records(records, require_sealed=False)
    first_digest = records[0].get("event_sha256")
    if (
        type(first_digest) is not str
        or re.fullmatch(r"[0-9a-f]{64}", first_digest) is None
    ):
        raise V07InterventionError("intervention declaration digest is invalid")
    values = {
        "study_identity_sha256": V07_INTERVENTION_STUDY_SHA256,
        "schedule_sha256": V07_SCHEDULE_SHA256,
        "journal_revision": V07_INTERVENTION_JOURNAL_REVISION,
        "journal_instance_sha256": str(records[0]["journal_instance_sha256"]),
        "declaration_sha256": "sha256:" + first_digest,
    }
    core = {
        "declaration_sha256": values["declaration_sha256"],
        "journal_instance_sha256": values["journal_instance_sha256"],
        "journal_revision": values["journal_revision"],
        "schedule_sha256": values["schedule_sha256"],
        "schema": _DECLARATION_SCHEMA,
        "study_identity_sha256": values["study_identity_sha256"],
    }
    declaration = VerifiedV07InterventionDeclaration(
        **values,
        declaration_evidence_sha256=_digest(core),
        _authority=_DECLARATION_AUTHORITY,
    )
    _ISSUED_DECLARATIONS.add(declaration)
    return declaration


def _append_category(
    path: str | Path,
    category: str,
    phase: V07InterventionPhase | None,
    model_id: str | None,
    episode: CampaignEpisode | None,
) -> None:
    if category not in _CATEGORY_TYPES:  # pragma: no cover - private callers fixed
        raise V07InterventionError("intervention event category is invalid")
    event = {"type": category, **_scope_fields(phase, model_id, episode)}
    _append_exact(Path(path), event, require_declaration=True)


def _scope_fields(
    phase: V07InterventionPhase | None,
    model_id: str | None,
    episode: CampaignEpisode | None,
) -> dict[str, object]:
    if phase is not None and type(phase) is not V07InterventionPhase:
        raise V07InterventionError("intervention phase must use the exact enum")
    if model_id is not None and model_id not in CAMPAIGN_MODELS:
        raise V07InterventionError("intervention model differs from v0.7")
    fields: dict[str, object] = {}
    if episode is not None:
        if type(episode) is not CampaignEpisode:
            raise V07InterventionError("intervention episode type is invalid")
        if (
            episode.episode_index <= 0
            or episode.episode_index > len(_SPEC.episodes)
            or _SPEC.episodes[episode.episode_index - 1] != episode
        ):
            raise V07InterventionError("intervention episode differs from schedule")
        if phase not in (None, V07InterventionPhase.CONFIRMATORY):
            raise V07InterventionError(
                "confirmatory episode cannot be assigned to another phase"
            )
        if model_id is not None and model_id != episode.model_id:
            raise V07InterventionError("intervention model and episode differ")
        phase = V07InterventionPhase.CONFIRMATORY
        model_id = episode.model_id
        fields.update(
            {
                "arm": episode.arm,
                "episode_index": episode.episode_index,
                "seed": episode.seed,
                "task_id": episode.task_id,
            }
        )
    if phase is not None:
        fields["phase"] = phase.value
    if model_id is not None:
        fields["model_id"] = model_id
    return fields


def _append_exact(
    path: Path,
    event: Mapping[str, object],
    *,
    require_declaration: bool,
) -> None:
    before_records, _payload, _inspection = _read_secure_snapshot(
        path,
        sealed=False,
        allow_empty=not require_declaration,
    )
    if require_declaration:
        _validate_records(before_records, require_sealed=False)
    elif before_records:
        raise V07InterventionError("intervention declaration file is not empty")
    before = _named_metadata(path)
    try:
        append_journal_event(path, event)
    except (OSError, JournalError, ValueError) as error:
        raise V07InterventionError("intervention journal append failed") from error
    _require_same_named_file(before, _named_metadata(path), allow_growth=True)
    after_records, _payload, _inspection = _read_secure_snapshot(path, sealed=False)
    if require_declaration:
        _validate_records(after_records, require_sealed=False)
    elif len(after_records) != 1:
        raise V07InterventionError("intervention declaration append is not exact")


def _precreate_mode_0600(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent = path.parent.lstat()
    except OSError as error:
        raise V07InterventionError("intervention journal parent is unavailable") from error
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise V07InterventionError(
            "intervention journal parent must be a real directory"
        )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise V07InterventionError("intervention journal requires no-follow opens")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(
        os, "O_CLOEXEC", 0
    )
    try:
        descriptor = os.open(os.fspath(path), flags, 0o600)
    except OSError as error:
        raise V07InterventionError(
            "intervention journal already exists or is unsafe"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise V07InterventionError("intervention journal is not regular")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_secure_snapshot(
    path: Path,
    *,
    sealed: bool,
    allow_empty: bool = False,
) -> tuple[tuple[dict[str, object], ...], bytes, _journal.JournalInspection]:
    before = _named_metadata(path)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise V07InterventionError("intervention journal requires no-follow opens")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise V07InterventionError(
            "intervention journal could not be securely opened"
        ) from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        opened = os.fstat(descriptor)
        _validate_metadata(opened)
        if _metadata_identity(opened) != _metadata_identity(before):
            raise V07InterventionError("intervention journal identity changed")
        payload = _read_all_from_descriptor(descriptor, _MAX_JOURNAL_BYTES)
        finished = os.fstat(descriptor)
        after = _named_metadata(path)
        if (
            _metadata_identity(finished) != _metadata_identity(opened)
            or _metadata_identity(after) != _metadata_identity(opened)
            or finished.st_size != len(payload)
        ):
            raise V07InterventionError("intervention journal identity changed")
    finally:
        os.close(descriptor)
    try:
        inspection = _journal._inspect_bytes(payload)
    except (JournalError, ValueError) as error:
        raise V07InterventionError(
            "intervention journal hash chain is invalid"
        ) from error
    if inspection.partial_tail is not None:
        raise V07InterventionError("intervention journal has a partial tail")
    if sealed and not inspection.sealed:
        raise V07InterventionError("intervention journal is not sealed")
    if not sealed and inspection.sealed:
        raise V07InterventionError("intervention journal is already sealed")
    if inspection.record_count > _MAX_JOURNAL_RECORDS:
        raise V07InterventionError("intervention journal exceeds its record bound")
    if not payload:
        if allow_empty:
            return (), payload, inspection
        raise V07InterventionError("intervention journal is empty")
    records = tuple(json.loads(line) for line in payload.splitlines())
    return records, payload, inspection


def _read_all_from_descriptor(descriptor: int, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > maximum_bytes:
            raise V07InterventionError("intervention journal exceeds its byte bound")
        chunks.append(chunk)


def _validate_records(
    records: tuple[dict[str, object], ...],
    *,
    require_sealed: bool,
) -> tuple[dict[str, object], ...]:
    if not records or records[0].get("type") != _DECLARATION:
        raise V07InterventionError(
            "intervention journal declaration must be the first record"
        )
    declaration = records[0]
    declaration_expected = {
        "automatic_model_prompt_source": "controller_evidence_only",
        "caller_entered_categories": list(_CATEGORY_TYPES),
        "journal_revision": V07_INTERVENTION_JOURNAL_REVISION,
        "journal_instance_sha256": declaration.get("journal_instance_sha256"),
        "operator_approval_is_benchmark_model_prompt": False,
        "schedule_sha256": V07_SCHEDULE_SHA256,
        "study_identity_sha256": V07_INTERVENTION_STUDY_SHA256,
        "type": _DECLARATION,
        "unresolved_handoff_is_human_prompt": False,
    }
    if (
        type(declaration_expected["journal_instance_sha256"]) is not str
        or _SHA256.fullmatch(declaration_expected["journal_instance_sha256"]) is None
    ):
        raise V07InterventionError("intervention journal instance digest is invalid")
    if {key: value for key, value in declaration.items() if key not in _CHAIN_FIELDS} != declaration_expected:
        raise V07InterventionError("intervention journal declaration differs")
    end = len(records) - 1 if require_sealed else len(records)
    events: list[dict[str, object]] = []
    for record in records[1:end]:
        category = record.get("type")
        if category not in _CATEGORY_TYPES:
            raise V07InterventionError("intervention event category is invalid")
        payload = {key: value for key, value in record.items() if key not in _CHAIN_FIELDS}
        allowed = {
            "type",
            "phase",
            "model_id",
            "episode_index",
            "task_id",
            "arm",
            "seed",
        }
        if not set(payload) <= allowed:
            raise V07InterventionError("intervention event contains a forbidden field")
        phase = payload.get("phase")
        if phase is not None and phase not in {item.value for item in V07InterventionPhase}:
            raise V07InterventionError("intervention event phase is invalid")
        model_id = payload.get("model_id")
        if model_id is not None and model_id not in CAMPAIGN_MODELS:
            raise V07InterventionError("intervention event model is invalid")
        episode_fields = {"episode_index", "task_id", "arm", "seed"}
        present = episode_fields.intersection(payload)
        if present:
            if present != episode_fields or phase != "confirmatory":
                raise V07InterventionError("intervention episode identity is incomplete")
            episode_index = payload["episode_index"]
            if type(episode_index) is not int or not 1 <= episode_index <= len(_SPEC.episodes):
                raise V07InterventionError("intervention episode index is invalid")
            expected = _SPEC.episodes[episode_index - 1]
            if payload != {
                "type": category,
                "phase": "confirmatory",
                "model_id": expected.model_id,
                "episode_index": expected.episode_index,
                "task_id": expected.task_id,
                "arm": expected.arm,
                "seed": expected.seed,
            }:
                raise V07InterventionError("intervention episode differs from schedule")
        events.append(record)
    if require_sealed and records[-1].get("type") != _journal.SEALED_EVENT_TYPE:
        raise V07InterventionError("intervention journal terminal seal is missing")
    return tuple(events)


def _named_metadata(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise V07InterventionError("intervention journal is unavailable") from error
    _validate_metadata(metadata)
    return metadata


def _validate_metadata(metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise V07InterventionError(
            "intervention journal must be a regular non-symlink file"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise V07InterventionError("intervention journal must use exact mode 0600")
    if metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
        raise V07InterventionError("intervention journal ownership is unsafe")
    if metadata.st_size < 0 or metadata.st_size > _MAX_JOURNAL_BYTES:
        raise V07InterventionError("intervention journal exceeds its byte bound")


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_same_named_file(
    before: os.stat_result,
    after: os.stat_result,
    *,
    allow_growth: bool,
) -> None:
    stable = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink")
    if any(getattr(before, field) != getattr(after, field) for field in stable):
        raise V07InterventionError("intervention journal identity changed")
    if allow_growth and after.st_size <= before.st_size:
        raise V07InterventionError("intervention journal append made no progress")


def _summary_core_record(values: Mapping[str, object]) -> dict[str, object]:
    return {
        "automatic_model_prompt_count": None,
        "automatic_model_prompt_source": values["automatic_model_prompt_source"],
        "benchmark_model_human_prompt_count": values[
            "benchmark_model_human_prompt_count"
        ],
        "counts_by_category": dict(values["_category_counts"]),  # type: ignore[arg-type]
        "counts_by_phase": dict(values["_phase_counts"]),  # type: ignore[arg-type]
        "declaration_sha256": values["declaration_sha256"],
        "intervention_event_count": values["intervention_event_count"],
        "journal_file_sha256": values["journal_file_sha256"],
        "journal_instance_sha256": values["journal_instance_sha256"],
        "journal_record_count": values["journal_record_count"],
        "journal_root_sha256": values["journal_root_sha256"],
        "operational_event_count": values["operational_event_count"],
        "operator_approval_is_benchmark_model_prompt": False,
        "orchestrator_operator_event_count": values[
            "orchestrator_operator_event_count"
        ],
        "schedule_sha256": values["schedule_sha256"],
        "schema": _SUMMARY_SCHEMA,
        "study_identity_sha256": values["study_identity_sha256"],
        "unresolved_handoff_is_human_prompt": False,
    }


def _validate_declaration(
    declaration: VerifiedV07InterventionDeclaration,
) -> None:
    for value in (
        declaration.study_identity_sha256,
        declaration.schedule_sha256,
        declaration.journal_instance_sha256,
        declaration.declaration_sha256,
        declaration.declaration_evidence_sha256,
    ):
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise V07InterventionError("intervention declaration digest is invalid")
    if (
        declaration.study_identity_sha256 != V07_INTERVENTION_STUDY_SHA256
        or declaration.schedule_sha256 != V07_SCHEDULE_SHA256
        or declaration.journal_revision != V07_INTERVENTION_JOURNAL_REVISION
    ):
        raise V07InterventionError("intervention declaration identity differs")
    if declaration.declaration_evidence_sha256 != _digest(
        declaration._core_record()
    ):
        raise V07InterventionError("intervention declaration evidence is invalid")


def _require_issued_declaration(
    declaration: VerifiedV07InterventionDeclaration,
) -> None:
    if type(declaration) is not VerifiedV07InterventionDeclaration:
        raise V07InterventionError("intervention declaration type is invalid")
    _validate_declaration(declaration)
    if declaration not in _ISSUED_DECLARATIONS:
        raise V07InterventionError("intervention declaration was not verifier-issued")


def _validate_summary(summary: VerifiedV07InterventionSummary) -> None:
    for value in (
        summary.study_identity_sha256,
        summary.schedule_sha256,
        summary.journal_root_sha256,
        summary.journal_file_sha256,
        summary.journal_instance_sha256,
        summary.declaration_sha256,
        summary.summary_sha256,
    ):
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise V07InterventionError("intervention summary digest is invalid")
    if (
        summary.study_identity_sha256 != V07_INTERVENTION_STUDY_SHA256
        or summary.schedule_sha256 != V07_SCHEDULE_SHA256
        or summary.automatic_model_prompt_count is not None
        or summary.automatic_model_prompt_source != "controller_evidence_only"
    ):
        raise V07InterventionError("intervention summary identity differs")
    category_counts = dict(summary._category_counts)
    phase_counts = dict(summary._phase_counts)
    if tuple(sorted(category_counts)) != _CATEGORY_TYPES:
        raise V07InterventionError("intervention summary categories differ")
    if any(type(value) is not int or value < 0 for value in (*category_counts.values(), *phase_counts.values())):
        raise V07InterventionError("intervention summary count is invalid")
    if (
        sum(category_counts.values()) != summary.intervention_event_count
        or category_counts["benchmark_model_human_prompt"]
        != summary.benchmark_model_human_prompt_count
        or sum(category_counts[item] for item in _ORCHESTRATOR_TYPES)
        != summary.orchestrator_operator_event_count
        or sum(category_counts[item] for item in _OPERATIONAL_TYPES)
        != summary.operational_event_count
        or sum(phase_counts.values()) != summary.intervention_event_count
        or summary.journal_record_count != summary.intervention_event_count + 2
    ):
        raise V07InterventionError("intervention summary accounting does not balance")
    if summary.summary_sha256 != _digest(summary._core_record()):
        raise V07InterventionError("intervention summary digest does not balance")


def _require_issued_summary(summary: VerifiedV07InterventionSummary) -> None:
    if type(summary) is not VerifiedV07InterventionSummary:
        raise V07InterventionError("intervention summary type is invalid")
    _validate_summary(summary)
    if summary not in _ISSUED_SUMMARIES:
        raise V07InterventionError("intervention summary was not builder-issued")


__all__ = (
    "V07_INTERVENTION_STUDY_SHA256",
    "V07InterventionError",
    "V07InterventionPhase",
    "VerifiedV07InterventionDeclaration",
    "VerifiedV07InterventionSummary",
    "append_benchmark_model_human_prompt",
    "append_operational_action",
    "append_operational_reconciliation",
    "append_operational_restart",
    "append_orchestrator_operator_approval",
    "append_orchestrator_operator_instruction",
    "declare_v07_intervention_journal",
    "seal_v07_intervention_journal",
    "verify_v07_intervention_declaration",
    "verify_v07_intervention_journal",
)
