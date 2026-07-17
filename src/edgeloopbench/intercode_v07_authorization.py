"""Builder-sealed production authorization foundation for v0.7.

This module admits no task, gold, model, Docker, or journal factory.  It binds
only verifier-issued provenance roots and the exact outcome-free manifest, then
retains the trusted source inventory needed to revalidate a clean committed
checkout before a later production boundary opens another campaign intent.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass
from pathlib import Path

from .intercode_campaign_ledger import CAMPAIGN_MODELS
from .intercode_source_inventory import (
    SourceInventoryError,
    VerifiedSourceInventory,
    revalidate_source_inventory,
)
from .intercode_v07_calibration import (
    V07CalibrationDisposition,
    V07PlanningGate,
    VerifiedV07CalibrationEvidence,
    evaluate_v07_calibration,
    evaluate_v07_planning_gate,
)
from .intercode_v07_manifest import (
    V07PrecalibrationManifest,
    bind_v07_qualification_manifest,
    build_v07_artifact_pins,
    build_v07_execution_pins,
)
from .intercode_v07_qualification import VerifiedV07QualificationEvidence


V07_AUTHORIZATION_SCHEMA_REVISION = "intercode-v0.7-campaign-authorization-v1"

_AUTHORIZATION_SEAL = object()
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class V07AuthorizationError(ValueError):
    """The supplied provenance cannot authorize v0.7 production execution."""


@dataclass(frozen=True, slots=True, repr=False)
class V07CampaignAuthorization:
    """Path-free, outcome-free roots admitted for later production composition."""

    manifest_sha256: str
    qualification_evidence_root_sha256: str
    qualification_suite_sha256: str
    qualification_root_sha256: str
    calibration_evidence_sha256: str
    calibration_journal_sha256: str
    calibration_controller_log_set_sha256: str
    calibration_disposition_sha256s: tuple[str, ...]
    planning_gate_sha256: str
    calibration_root_sha256: str
    source_inventory_sha256: str
    code_root_sha256: str
    runtime_root_sha256: str
    execution_root_sha256: str
    authorization_sha256: str
    _source_inventory: VerifiedSourceInventory | None = None
    _manifest: V07PrecalibrationManifest | None = None
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _AUTHORIZATION_SEAL:
            raise V07AuthorizationError("v0.7 campaign authorizations are builder-sealed")
        _validate_authorization(self)

    def canonical_record(self) -> dict[str, object]:
        _validate_authorization(self)
        record = _authorization_core(
            manifest_sha256=self.manifest_sha256,
            qualification_evidence_root_sha256=(
                self.qualification_evidence_root_sha256
            ),
            qualification_suite_sha256=self.qualification_suite_sha256,
            qualification_root_sha256=self.qualification_root_sha256,
            calibration_evidence_sha256=self.calibration_evidence_sha256,
            calibration_journal_sha256=self.calibration_journal_sha256,
            calibration_controller_log_set_sha256=(
                self.calibration_controller_log_set_sha256
            ),
            calibration_disposition_sha256s=(
                self.calibration_disposition_sha256s
            ),
            planning_gate_sha256=self.planning_gate_sha256,
            calibration_root_sha256=self.calibration_root_sha256,
            source_inventory_sha256=self.source_inventory_sha256,
            code_root_sha256=self.code_root_sha256,
            runtime_root_sha256=self.runtime_root_sha256,
            execution_root_sha256=self.execution_root_sha256,
        )
        record["authorization_sha256"] = self.authorization_sha256
        return record

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_record()) + b"\n"

    def revalidate(self, repository_root: Path) -> V07CampaignAuthorization:
        """Reprove exact committed source and sealed manifest before a new intent."""

        try:
            _validate_authorization(self)
            assert self._source_inventory is not None and self._manifest is not None
            revalidate_source_inventory(self._source_inventory, repository_root)
            expected_execution = build_v07_execution_pins(
                source_inventory=self._source_inventory,
                host_identity=self._manifest.host_identity,
            )
            if expected_execution != self._manifest.execution:
                raise V07AuthorizationError(
                    "authorization execution pins changed during revalidation"
                )
            self._manifest.canonical_record()
        except (OSError, SourceInventoryError, ValueError):
            raise V07AuthorizationError("v0.7 authorization revalidation failed") from None
        return self

    def __repr__(self) -> str:
        return (
            "<V07CampaignAuthorization "
            f"manifest={self.manifest_sha256} root={self.authorization_sha256}>"
        )


def build_v07_campaign_authorization(
    *,
    manifest: V07PrecalibrationManifest,
    qualification_evidence: VerifiedV07QualificationEvidence,
    calibration_evidence: VerifiedV07CalibrationEvidence,
    dispositions: tuple[V07CalibrationDisposition, ...],
    planning_gate: V07PlanningGate,
    source_inventory: VerifiedSourceInventory,
) -> V07CampaignAuthorization:
    """Validate every prerequisite and seal only their path-free public roots."""

    if type(manifest) is not V07PrecalibrationManifest:
        raise V07AuthorizationError("authorization requires exact pre-calibration manifest")
    try:
        manifest.canonical_record()
    except ValueError:
        raise V07AuthorizationError("pre-calibration manifest is invalid") from None
    if type(source_inventory) is not VerifiedSourceInventory:
        raise V07AuthorizationError("authorization requires verified source inventory")
    try:
        source_inventory.canonical_record()
    except SourceInventoryError:
        raise V07AuthorizationError("authorization source inventory is invalid") from None

    if type(qualification_evidence) is not VerifiedV07QualificationEvidence:
        raise V07AuthorizationError(
            "authorization requires selected-sample qualification evidence"
        )
    try:
        qualification_evidence.require_admitted()
        expected_artifacts = build_v07_artifact_pins(
            source_inventory=source_inventory,
            qualification_evidence=qualification_evidence,
        )
    except ValueError:
        raise V07AuthorizationError("qualification evidence is not admitted") from None
    if expected_artifacts != manifest.artifacts:
        raise V07AuthorizationError(
            "qualification or code provenance differs from manifest"
        )
    expected_qualification = bind_v07_qualification_manifest(qualification_evidence)
    if expected_qualification != manifest.artifacts.qualification:
        raise V07AuthorizationError("qualification evidence differs from manifest")

    expected_execution = build_v07_execution_pins(
        source_inventory=source_inventory,
        host_identity=manifest.host_identity,
    )
    if expected_execution != manifest.execution:
        raise V07AuthorizationError("runtime or execution pins differ from manifest")

    if type(calibration_evidence) is not VerifiedV07CalibrationEvidence:
        raise V07AuthorizationError("authorization requires verified calibration evidence")
    if calibration_evidence.precalibration_manifest_sha256 != manifest.manifest_sha256:
        raise V07AuthorizationError("calibration evidence differs from manifest")
    if type(dispositions) is not tuple or len(dispositions) != len(CAMPAIGN_MODELS):
        raise V07AuthorizationError("authorization requires both model dispositions")
    if any(type(item) is not V07CalibrationDisposition for item in dispositions):
        raise V07AuthorizationError("authorization requires sealed model dispositions")
    if tuple(item.model_id for item in dispositions) != CAMPAIGN_MODELS:
        raise V07AuthorizationError("calibration dispositions differ from model order")
    try:
        expected_dispositions = tuple(
            evaluate_v07_calibration(calibration_evidence, model_id)
            for model_id in CAMPAIGN_MODELS
        )
    except ValueError:
        raise V07AuthorizationError("calibration evidence is invalid") from None
    if dispositions != expected_dispositions:
        raise V07AuthorizationError("calibration dispositions are not evaluator-sealed")
    if any(not item.admitted for item in dispositions):
        raise V07AuthorizationError("calibration did not admit both models")
    if type(planning_gate) is not V07PlanningGate:
        raise V07AuthorizationError("authorization requires exact planning gate")
    try:
        expected_gate = evaluate_v07_planning_gate(dispositions)
    except ValueError:
        raise V07AuthorizationError("calibration planning gate is invalid") from None
    if planning_gate != expected_gate:
        raise V07AuthorizationError("calibration planning gate differs from evidence")
    if not planning_gate.allowed or planning_gate.reason is not None:
        raise V07AuthorizationError("calibration planning gate is not allowed")
    if (
        planning_gate.precalibration_manifest_sha256 != manifest.manifest_sha256
        or planning_gate.evidence_sha256 != calibration_evidence.evidence_sha256
    ):
        raise V07AuthorizationError("calibration planning gate provenance differs")

    qualification_root = _qualification_root(
        qualification_evidence.evidence_root_sha256,
        qualification_evidence.suite_sha256,
        manifest,
    )
    planning_root = _planning_gate_root(planning_gate)
    disposition_roots = tuple(item.disposition_sha256 for item in dispositions)
    calibration_root = _calibration_root(
        calibration_evidence,
        disposition_roots,
        planning_root,
    )
    code_root = _code_root(manifest)
    runtime_root = _runtime_root(manifest)
    execution_root = manifest.execution.execution_pins_sha256
    values: dict[str, object] = {
        "manifest_sha256": manifest.manifest_sha256,
        "qualification_evidence_root_sha256": (
            qualification_evidence.evidence_root_sha256
        ),
        "qualification_suite_sha256": qualification_evidence.suite_sha256,
        "qualification_root_sha256": qualification_root,
        "calibration_evidence_sha256": calibration_evidence.evidence_sha256,
        "calibration_journal_sha256": (
            calibration_evidence.calibration_journal_sha256
        ),
        "calibration_controller_log_set_sha256": (
            calibration_evidence.controller_log_set_sha256
        ),
        "calibration_disposition_sha256s": disposition_roots,
        "planning_gate_sha256": planning_root,
        "calibration_root_sha256": calibration_root,
        "source_inventory_sha256": source_inventory.inventory_sha256,
        "code_root_sha256": code_root,
        "runtime_root_sha256": runtime_root,
        "execution_root_sha256": execution_root,
    }
    authorization_sha256 = _digest_record(
        _authorization_core(**values)  # type: ignore[arg-type]
    )
    return V07CampaignAuthorization(
        **values,  # type: ignore[arg-type]
        authorization_sha256=authorization_sha256,
        _source_inventory=source_inventory,
        _manifest=manifest,
        _construction_seal=_AUTHORIZATION_SEAL,
    )


def _validate_authorization(authorization: V07CampaignAuthorization) -> None:
    if type(authorization._source_inventory) is not VerifiedSourceInventory:
        raise V07AuthorizationError("authorization lacks sealed source inventory")
    if type(authorization._manifest) is not V07PrecalibrationManifest:
        raise V07AuthorizationError("authorization lacks sealed manifest")
    source = authorization._source_inventory
    manifest = authorization._manifest
    try:
        source.canonical_record()
        manifest.canonical_record()
    except ValueError:
        raise V07AuthorizationError("authorization retains invalid sealed provenance") from None
    for field in (
        "manifest_sha256",
        "qualification_evidence_root_sha256",
        "qualification_suite_sha256",
        "qualification_root_sha256",
        "calibration_evidence_sha256",
        "calibration_journal_sha256",
        "calibration_controller_log_set_sha256",
        "planning_gate_sha256",
        "calibration_root_sha256",
        "source_inventory_sha256",
        "code_root_sha256",
        "runtime_root_sha256",
        "execution_root_sha256",
        "authorization_sha256",
    ):
        _require_sha256(getattr(authorization, field), field)
    if (
        type(authorization.calibration_disposition_sha256s) is not tuple
        or len(authorization.calibration_disposition_sha256s) != len(CAMPAIGN_MODELS)
    ):
        raise V07AuthorizationError("authorization disposition roots are incomplete")
    for root in authorization.calibration_disposition_sha256s:
        _require_sha256(root, "calibration disposition")
    if authorization.manifest_sha256 != manifest.manifest_sha256:
        raise V07AuthorizationError("authorization manifest root is inconsistent")
    if authorization.source_inventory_sha256 != source.inventory_sha256:
        raise V07AuthorizationError("authorization source root is inconsistent")
    if (
        manifest.artifacts.source_inventory_sha256 != source.inventory_sha256
        or manifest.artifacts.source_head_commit != source.head_commit
        or manifest.artifacts.source_head_tree != source.head_tree
        or manifest.artifacts.source_code_sha256 != source.inventory_sha256
    ):
        raise V07AuthorizationError("authorization code provenance is inconsistent")
    qualification = manifest.artifacts.qualification
    expected_qualification_root = _qualification_root(
        authorization.qualification_evidence_root_sha256,
        authorization.qualification_suite_sha256,
        manifest,
    )
    if (
        qualification.evidence_root_sha256
        != authorization.qualification_evidence_root_sha256
        or qualification.suite_sha256 != authorization.qualification_suite_sha256
        or expected_qualification_root != authorization.qualification_root_sha256
    ):
        raise V07AuthorizationError("authorization qualification root is inconsistent")
    if _code_root(manifest) != authorization.code_root_sha256:
        raise V07AuthorizationError("authorization code root is inconsistent")
    if _runtime_root(manifest) != authorization.runtime_root_sha256:
        raise V07AuthorizationError("authorization runtime root is inconsistent")
    if manifest.execution.execution_pins_sha256 != authorization.execution_root_sha256:
        raise V07AuthorizationError("authorization execution root is inconsistent")
    if (
        _calibration_root_from_values(
            evidence_sha256=authorization.calibration_evidence_sha256,
            journal_sha256=authorization.calibration_journal_sha256,
            controller_log_set_sha256=(
                authorization.calibration_controller_log_set_sha256
            ),
            disposition_roots=authorization.calibration_disposition_sha256s,
            planning_gate_sha256=authorization.planning_gate_sha256,
        )
        != authorization.calibration_root_sha256
    ):
        raise V07AuthorizationError("authorization calibration root is inconsistent")
    expected_authorization = _digest_record(
        _authorization_core(
            manifest_sha256=authorization.manifest_sha256,
            qualification_evidence_root_sha256=(
                authorization.qualification_evidence_root_sha256
            ),
            qualification_suite_sha256=authorization.qualification_suite_sha256,
            qualification_root_sha256=authorization.qualification_root_sha256,
            calibration_evidence_sha256=authorization.calibration_evidence_sha256,
            calibration_journal_sha256=authorization.calibration_journal_sha256,
            calibration_controller_log_set_sha256=(
                authorization.calibration_controller_log_set_sha256
            ),
            calibration_disposition_sha256s=(
                authorization.calibration_disposition_sha256s
            ),
            planning_gate_sha256=authorization.planning_gate_sha256,
            calibration_root_sha256=authorization.calibration_root_sha256,
            source_inventory_sha256=authorization.source_inventory_sha256,
            code_root_sha256=authorization.code_root_sha256,
            runtime_root_sha256=authorization.runtime_root_sha256,
            execution_root_sha256=authorization.execution_root_sha256,
        )
    )
    if authorization.authorization_sha256 != expected_authorization:
        raise V07AuthorizationError("authorization root is inconsistent")


def _qualification_root(
    evidence_root_sha256: str,
    suite_sha256: str,
    manifest: V07PrecalibrationManifest,
) -> str:
    return _digest_record(
        {
            "evidence_root_sha256": evidence_root_sha256,
            "manifest_projection": manifest.artifacts.qualification.canonical_record(),
            "suite_sha256": suite_sha256,
        }
    )


def _planning_gate_root(gate: V07PlanningGate) -> str:
    return _digest_record(
        {
            "active_time_limit_ns": gate.active_time_limit_ns,
            "allowed": gate.allowed,
            "estimated_confirmatory_active_time_ns": (
                gate.estimated_confirmatory_active_time_ns
            ),
            "evidence_sha256": gate.evidence_sha256,
            "planning_bound_ns": gate.planning_bound_ns,
            "precalibration_manifest_sha256": gate.precalibration_manifest_sha256,
            "reason": gate.reason,
        }
    )


def _calibration_root(
    evidence: VerifiedV07CalibrationEvidence,
    disposition_roots: tuple[str, ...],
    planning_gate_sha256: str,
) -> str:
    return _calibration_root_from_values(
        evidence_sha256=evidence.evidence_sha256,
        journal_sha256=evidence.calibration_journal_sha256,
        controller_log_set_sha256=evidence.controller_log_set_sha256,
        disposition_roots=disposition_roots,
        planning_gate_sha256=planning_gate_sha256,
    )


def _calibration_root_from_values(
    *,
    evidence_sha256: str,
    journal_sha256: str,
    controller_log_set_sha256: str,
    disposition_roots: tuple[str, ...],
    planning_gate_sha256: str,
) -> str:
    return _digest_record(
        {
            "calibration_controller_log_set_sha256": controller_log_set_sha256,
            "calibration_disposition_sha256s": list(disposition_roots),
            "calibration_evidence_sha256": evidence_sha256,
            "calibration_journal_sha256": journal_sha256,
            "planning_gate_sha256": planning_gate_sha256,
        }
    )


def _code_root(manifest: V07PrecalibrationManifest) -> str:
    record = manifest.canonical_record()
    return _digest_record(record["code"])


def _runtime_root(manifest: V07PrecalibrationManifest) -> str:
    return _digest_record(
        {
            "host_identity": manifest.host_identity.canonical_record(),
            "models": [item.canonical_record() for item in manifest.models],
        }
    )


def _authorization_core(
    *,
    manifest_sha256: str,
    qualification_evidence_root_sha256: str,
    qualification_suite_sha256: str,
    qualification_root_sha256: str,
    calibration_evidence_sha256: str,
    calibration_journal_sha256: str,
    calibration_controller_log_set_sha256: str,
    calibration_disposition_sha256s: tuple[str, ...],
    planning_gate_sha256: str,
    calibration_root_sha256: str,
    source_inventory_sha256: str,
    code_root_sha256: str,
    runtime_root_sha256: str,
    execution_root_sha256: str,
) -> dict[str, object]:
    return {
        "calibration_controller_log_set_sha256": (
            calibration_controller_log_set_sha256
        ),
        "calibration_disposition_sha256s": list(
            calibration_disposition_sha256s
        ),
        "calibration_evidence_sha256": calibration_evidence_sha256,
        "calibration_journal_sha256": calibration_journal_sha256,
        "calibration_root_sha256": calibration_root_sha256,
        "code_root_sha256": code_root_sha256,
        "execution_root_sha256": execution_root_sha256,
        "manifest_sha256": manifest_sha256,
        "planning_gate_sha256": planning_gate_sha256,
        "qualification_evidence_root_sha256": (
            qualification_evidence_root_sha256
        ),
        "qualification_root_sha256": qualification_root_sha256,
        "qualification_suite_sha256": qualification_suite_sha256,
        "runtime_root_sha256": runtime_root_sha256,
        "schema": V07_AUTHORIZATION_SCHEMA_REVISION,
        "source_inventory_sha256": source_inventory_sha256,
    }


def _require_sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise V07AuthorizationError(f"authorization {field} is not a SHA-256 root")
    return value


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


__all__ = (
    "V07_AUTHORIZATION_SCHEMA_REVISION",
    "V07AuthorizationError",
    "V07CampaignAuthorization",
    "build_v07_campaign_authorization",
)
