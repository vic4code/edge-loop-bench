"""Outcome-independent confirmatory sampling from a qualified Bash manifest."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from types import MappingProxyType

from .intercode_qualification import QualificationManifest


SAMPLE_SCHEMA_VERSION = 1
SAMPLE_SALT = "edgeloopbench-v0.6-intercode-confirmatory-50-v1"
SELECTION_ALGORITHM = (
    "sha256(salt_utf8 || nul || selection_frame_sha256_utf8 || nul || task_id_utf8)"
)
CONFIRMATORY_QUOTAS: Mapping[str, int] = MappingProxyType(
    {"fs1": 15, "fs2": 13, "fs3": 15, "fs4": 7}
)
DIAGNOSTIC_QUOTAS: Mapping[str, int] = MappingProxyType(
    {"fs1": 4, "fs2": 3, "fs3": 3, "fs4": 2}
)

_STRATA = tuple(CONFIRMATORY_QUOTAS)
_TASK_ID_PATTERN = re.compile(r"^bash-(fs[1-4])-[0-9]{3}$")
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAMPLE_CONSTRUCTION_SEAL = object()


@dataclass(frozen=True, slots=True)
class ConfirmatorySampleManifest:
    """Gold-free, hash-ranked 50-task sample and nested diagnostic subset."""

    qualified_suite_sha256: str
    selection_frame_sha256: str
    sampling_salt: str
    selection_algorithm: str
    quotas: Mapping[str, int]
    diagnostic_quotas: Mapping[str, int]
    qualified_by_stratum: Mapping[str, int]
    task_ids: tuple[str, ...]
    diagnostic_task_ids: tuple[str, ...]
    sample_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _SAMPLE_CONSTRUCTION_SEAL:
            raise ValueError("confirmatory sample manifests are builder-sealed")
        object.__setattr__(self, "quotas", MappingProxyType(dict(self.quotas)))
        object.__setattr__(
            self,
            "diagnostic_quotas",
            MappingProxyType(dict(self.diagnostic_quotas)),
        )
        object.__setattr__(
            self,
            "qualified_by_stratum",
            MappingProxyType(dict(self.qualified_by_stratum)),
        )
        _validate_sample(self)

    @property
    def qualified_count(self) -> int:
        return sum(self.qualified_by_stratum.values())

    def canonical_bytes(self) -> bytes:
        _validate_sample(self)
        record = _sample_core_record(self)
        record["sample_sha256"] = self.sample_sha256
        return _canonical_json(record)


def build_confirmatory_sample(
    qualification: QualificationManifest,
) -> ConfirmatorySampleManifest:
    """Select the preregistered hash-randomized sample from an admitted manifest."""

    if type(qualification) is not QualificationManifest:
        raise ValueError("qualification must be an aggregator-sealed manifest")
    qualification.canonical_bytes()
    qualification.require_scoring_admitted()

    included_by_stratum: dict[str, list[str]] = {stratum: [] for stratum in _STRATA}
    for record in qualification.records:
        if record.included:
            included_by_stratum[record.stratum].append(record.task_id)

    selection_frame_sha256 = _qualification_selection_frame_sha256(qualification)
    selected: list[str] = []
    diagnostic: list[str] = []
    for stratum in _STRATA:
        ranked = sorted(
            included_by_stratum[stratum],
            key=lambda task_id: (
                _selection_digest(selection_frame_sha256, task_id),
                task_id,
            ),
        )
        quota = CONFIRMATORY_QUOTAS[stratum]
        diagnostic_quota = DIAGNOSTIC_QUOTAS[stratum]
        if len(ranked) < quota:
            raise ValueError("qualified stratum cannot satisfy the frozen sample quota")
        stratum_sample = ranked[:quota]
        selected.extend(stratum_sample)
        diagnostic.extend(stratum_sample[:diagnostic_quota])

    values: dict[str, object] = {
        "qualified_suite_sha256": qualification.suite_sha256,
        "selection_frame_sha256": selection_frame_sha256,
        "sampling_salt": SAMPLE_SALT,
        "selection_algorithm": SELECTION_ALGORITHM,
        "quotas": dict(CONFIRMATORY_QUOTAS),
        "diagnostic_quotas": dict(DIAGNOSTIC_QUOTAS),
        "qualified_by_stratum": dict(qualification.qualified_by_stratum),
        "task_ids": tuple(selected),
        "diagnostic_task_ids": tuple(diagnostic),
    }
    provisional = _sample_core_record_from_values(**values)
    sample_sha256 = "sha256:" + hashlib.sha256(_canonical_json(provisional)).hexdigest()
    return ConfirmatorySampleManifest(
        **values,  # type: ignore[arg-type]
        sample_sha256=sample_sha256,
        _construction_seal=_SAMPLE_CONSTRUCTION_SEAL,
    )


def _selection_digest(selection_frame_sha256: str, task_id: str) -> str:
    payload = (
        SAMPLE_SALT.encode("utf-8")
        + b"\0"
        + selection_frame_sha256.encode("ascii")
        + b"\0"
        + task_id.encode("ascii")
    )
    return hashlib.sha256(payload).hexdigest()


def _qualification_selection_frame_sha256(
    qualification: QualificationManifest,
) -> str:
    record = {
        "qualified_by_stratum": dict(qualification.qualified_by_stratum),
        "records": [
            {
                "exclusion_reasons": [
                    reason.value for reason in item.exclusion_reasons
                ],
                "included": item.included,
                "stratum": item.stratum,
                "task_id": item.task_id,
            }
            for item in qualification.records
        ],
        "schema": "edgeloopbench.intercode-selection-frame.v1",
        "source_population_sha256": qualification.source_population_sha256,
        "source_revision": qualification.source_revision,
        "static_exclusion_audit_sha256": (
            qualification.static_exclusion_audit_sha256
        ),
    }
    return "sha256:" + hashlib.sha256(_canonical_json(record)).hexdigest()


def _validate_sample(manifest: ConfirmatorySampleManifest) -> None:
    _require_sha256(manifest.qualified_suite_sha256, "qualified_suite_sha256")
    _require_sha256(manifest.selection_frame_sha256, "selection_frame_sha256")
    _require_sha256(manifest.sample_sha256, "sample_sha256")
    if manifest.sampling_salt != SAMPLE_SALT:
        raise ValueError("confirmatory sampling salt differs from the frozen value")
    if manifest.selection_algorithm != SELECTION_ALGORITHM:
        raise ValueError("confirmatory selection algorithm differs from the frozen value")
    if dict(manifest.quotas) != dict(CONFIRMATORY_QUOTAS):
        raise ValueError("confirmatory quotas differ from the frozen values")
    if dict(manifest.diagnostic_quotas) != dict(DIAGNOSTIC_QUOTAS):
        raise ValueError("diagnostic quotas differ from the frozen values")
    if set(manifest.qualified_by_stratum) != set(_STRATA):
        raise ValueError("qualified stratum counts are incomplete")
    for stratum, count in manifest.qualified_by_stratum.items():
        if type(count) is not int or count < CONFIRMATORY_QUOTAS[stratum]:
            raise ValueError("qualified stratum count cannot satisfy its quota")
    if (
        type(manifest.task_ids) is not tuple
        or type(manifest.diagnostic_task_ids) is not tuple
    ):
        raise ValueError("sample task orders must be frozen tuples")
    _validate_task_ids(manifest.task_ids, CONFIRMATORY_QUOTAS, "confirmatory")
    _validate_task_ids(manifest.diagnostic_task_ids, DIAGNOSTIC_QUOTAS, "diagnostic")
    if not set(manifest.diagnostic_task_ids) < set(manifest.task_ids):
        raise ValueError("diagnostic sample must be a proper nested subset")
    expected_order = tuple(
        task_id
        for stratum in _STRATA
        for task_id in sorted(
            (task for task in manifest.task_ids if task.startswith(f"bash-{stratum}-")),
            key=lambda task_id: (
                _selection_digest(manifest.selection_frame_sha256, task_id),
                task_id,
            ),
        )
    )
    if manifest.task_ids != expected_order:
        raise ValueError("confirmatory task order is not canonical")
    # Reconstruct the declared prefix independently for each stratum.
    expected_diagnostic = tuple(
        task_id
        for stratum in _STRATA
        for task_id in tuple(
            task for task in manifest.task_ids if task.startswith(f"bash-{stratum}-")
        )[: DIAGNOSTIC_QUOTAS[stratum]]
    )
    if manifest.diagnostic_task_ids != expected_diagnostic:
        raise ValueError("diagnostic sample is not the frozen stratum prefix")
    expected_sha256 = "sha256:" + hashlib.sha256(
        _canonical_json(_sample_core_record(manifest))
    ).hexdigest()
    if manifest.sample_sha256 != expected_sha256:
        raise ValueError("sample_sha256 differs from the canonical sample")


def _validate_task_ids(
    task_ids: tuple[str, ...], quotas: Mapping[str, int], label: str
) -> None:
    if len(task_ids) != sum(quotas.values()) or len(set(task_ids)) != len(task_ids):
        raise ValueError(f"{label} sample size or uniqueness is invalid")
    counts: Counter[str] = Counter()
    for task_id in task_ids:
        match = _TASK_ID_PATTERN.fullmatch(task_id) if type(task_id) is str else None
        if match is None:
            raise ValueError(f"{label} sample contains an invalid task ID")
        counts[match.group(1)] += 1
    if counts != Counter(quotas):
        raise ValueError(f"{label} sample strata differ from the frozen quotas")


def _sample_core_record(manifest: ConfirmatorySampleManifest) -> dict[str, object]:
    return _sample_core_record_from_values(
        qualified_suite_sha256=manifest.qualified_suite_sha256,
        selection_frame_sha256=manifest.selection_frame_sha256,
        sampling_salt=manifest.sampling_salt,
        selection_algorithm=manifest.selection_algorithm,
        quotas=manifest.quotas,
        diagnostic_quotas=manifest.diagnostic_quotas,
        qualified_by_stratum=manifest.qualified_by_stratum,
        task_ids=manifest.task_ids,
        diagnostic_task_ids=manifest.diagnostic_task_ids,
    )


def _sample_core_record_from_values(
    *,
    qualified_suite_sha256: str,
    selection_frame_sha256: str,
    sampling_salt: str,
    selection_algorithm: str,
    quotas: Mapping[str, int],
    diagnostic_quotas: Mapping[str, int],
    qualified_by_stratum: Mapping[str, int],
    task_ids: tuple[str, ...],
    diagnostic_task_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "diagnostic_quotas": dict(diagnostic_quotas),
        "diagnostic_task_ids": list(diagnostic_task_ids),
        "qualified_by_stratum": dict(qualified_by_stratum),
        "qualified_suite_sha256": qualified_suite_sha256,
        "selection_frame_sha256": selection_frame_sha256,
        "quotas": dict(quotas),
        "sampling_salt": sampling_salt,
        "schema_version": SAMPLE_SCHEMA_VERSION,
        "selection_algorithm": selection_algorithm,
        "task_ids": list(task_ids),
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_sha256(value: object, field: str) -> None:
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 reference")


__all__ = (
    "CONFIRMATORY_QUOTAS",
    "DIAGNOSTIC_QUOTAS",
    "SAMPLE_SCHEMA_VERSION",
    "SAMPLE_SALT",
    "SELECTION_ALGORITHM",
    "ConfirmatorySampleManifest",
    "build_confirmatory_sample",
)
