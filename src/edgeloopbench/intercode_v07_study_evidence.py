"""Aggregate publication gate for one fully bound v0.7 study.

This is the report-facing evidence boundary.  It independently verifies the
formal campaign/controller journals, every execution envelope, and the final
intervention journal against one exact :class:`V07PreparedStudy`.  The result
contains path-free roots and accounting only; it performs no Docker, Ollama,
model-generation, or network operation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass, field
from pathlib import Path

from .intercode_campaign_evidence import (
    VerifiedCampaignEvidence,
    verify_campaign_evidence,
)
from .intercode_campaign_ledger import (
    CAMPAIGN_EPISODE_COUNT,
    CAMPAIGN_MODELS,
    CampaignSpec,
)
from .intercode_host_safety import DockerDaemonIdentity
from .intercode_v07_analysis import (
    V07EffectivenessAnalysis,
    analyze_v07_effectiveness,
)
from .intercode_v07_execution_evidence import (
    VerifiedV07ExecutionEnvelopeSet,
    verify_v07_execution_envelope_set,
)
from .intercode_v07_interventions import (
    VerifiedV07InterventionSummary,
    verify_v07_intervention_journal,
)
from .intercode_v07_manifest import V07_INTERVENTION_JOURNAL_REVISION
from .intercode_v07_study_binding import V07PreparedStudy, V07StudyBinding


V07_STUDY_EVIDENCE_REVISION = "intercode-v0.7-study-evidence-v6"
V07_INTERVENTION_DECLARATION_EVIDENCE_REVISION = (
    "intercode-v0.7-intervention-declaration-evidence-v2"
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_AUTHORITY = object()


class V07StudyEvidenceError(ValueError):
    """The supplied evidence surfaces do not form one publishable study."""


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedV07StudyEvidence:
    """Verifier-sealed, path-free authority for publication and reporting."""

    authorization_sha256: str
    qualification_campaign_sha256: str
    calibration_gold_campaign_sha256: str
    runtime_session_sha256: str
    manifest_sha256: str
    execution_pins_sha256: str
    host_identity_sha256: str
    docker_daemon_identity_sha256: str
    formal_campaign_sha256: str
    study_binding_sha256: str
    schedule_sha256: str
    campaign_log_sha256: str
    episode_log_set_sha256: str
    execution_set_sha256: str
    tokenizer_artifacts_by_model: tuple[tuple[str, str], ...]
    intervention_declaration_sha256: str
    intervention_declaration_evidence_sha256: str
    intervention_journal_instance_sha256: str
    intervention_journal_root_sha256: str
    intervention_journal_file_sha256: str
    intervention_summary_sha256: str
    verified_episode_count: int
    verified_envelope_count: int
    automatic_model_prompt_count: int
    model_issued_environment_action_count: int
    replayed_environment_action_count: int
    physical_environment_action_count: int
    benchmark_model_human_prompt_count: int
    orchestrator_operator_event_count: int
    operational_event_count: int
    intervention_event_count: int
    study_evidence_sha256: str
    _campaign_evidence: VerifiedCampaignEvidence = field(
        repr=False,
        compare=False,
    )
    _execution_evidence: VerifiedV07ExecutionEnvelopeSet = field(
        repr=False,
        compare=False,
    )
    _intervention_summary: VerifiedV07InterventionSummary = field(
        repr=False,
        compare=False,
    )
    _authority: InitVar[object | None] = None

    def __post_init__(self, _authority: object | None) -> None:
        if _authority is not _AUTHORITY:
            raise V07StudyEvidenceError(
                "v0.7 study evidence must be verifier-issued"
            )
        _validate_study_evidence(self)

    def _core_record(self) -> dict[str, object]:
        return {
            "authorization_sha256": self.authorization_sha256,
            "automatic_model_prompt_count": self.automatic_model_prompt_count,
            "benchmark_model_human_prompt_count": (
                self.benchmark_model_human_prompt_count
            ),
            "calibration_gold_campaign_sha256": (
                self.calibration_gold_campaign_sha256
            ),
            "campaign_log_sha256": self.campaign_log_sha256,
            "docker_daemon_identity_sha256": (
                self.docker_daemon_identity_sha256
            ),
            "episode_log_set_sha256": self.episode_log_set_sha256,
            "execution_pins_sha256": self.execution_pins_sha256,
            "execution_set_sha256": self.execution_set_sha256,
            "formal_campaign_sha256": self.formal_campaign_sha256,
            "host_identity_sha256": self.host_identity_sha256,
            "intervention_declaration_evidence_sha256": (
                self.intervention_declaration_evidence_sha256
            ),
            "intervention_declaration_sha256": (
                self.intervention_declaration_sha256
            ),
            "intervention_event_count": self.intervention_event_count,
            "intervention_journal_instance_sha256": (
                self.intervention_journal_instance_sha256
            ),
            "intervention_journal_file_sha256": (
                self.intervention_journal_file_sha256
            ),
            "intervention_journal_root_sha256": (
                self.intervention_journal_root_sha256
            ),
            "intervention_summary_sha256": self.intervention_summary_sha256,
            "manifest_sha256": self.manifest_sha256,
            "model_issued_environment_action_count": (
                self.model_issued_environment_action_count
            ),
            "operational_event_count": self.operational_event_count,
            "orchestrator_operator_event_count": (
                self.orchestrator_operator_event_count
            ),
            "qualification_campaign_sha256": (
                self.qualification_campaign_sha256
            ),
            "runtime_session_sha256": self.runtime_session_sha256,
            "physical_environment_action_count": (
                self.physical_environment_action_count
            ),
            "replayed_environment_action_count": (
                self.replayed_environment_action_count
            ),
            "schedule_sha256": self.schedule_sha256,
            "schema": V07_STUDY_EVIDENCE_REVISION,
            "study_binding_sha256": self.study_binding_sha256,
            "tokenizer_artifacts_by_model": [
                {
                    "model_id": model_id,
                    "tokenizer_artifact_sha256": artifact,
                }
                for model_id, artifact in self.tokenizer_artifacts_by_model
            ],
            "verified_envelope_count": self.verified_envelope_count,
            "verified_episode_count": self.verified_episode_count,
        }

    def canonical_record(self) -> dict[str, object]:
        _validate_study_evidence(self)
        return {
            **self._core_record(),
            "study_evidence_sha256": self.study_evidence_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_record()) + b"\n"

    def __repr__(self) -> str:
        return f"<VerifiedV07StudyEvidence root={self.study_evidence_sha256}>"


def verify_v07_study_evidence(
    prepared: V07PreparedStudy,
    *,
    campaign_journal_path: str | Path,
    episode_log_directory: str | Path,
    execution_envelope_directory: str | Path,
    intervention_journal_path: str | Path,
) -> VerifiedV07StudyEvidence:
    """Verify every final evidence surface against one exact prepared study."""

    if type(prepared) is not V07PreparedStudy:
        raise ValueError("v0.7 study evidence requires exact V07PreparedStudy")
    try:
        binding = prepared.binding
        if type(binding) is not V07StudyBinding:
            raise V07StudyEvidenceError("prepared study binding type is invalid")
        binding.canonical_record()
        spec = prepared.bound_campaign_spec
        _require_bound_spec(spec, binding)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise V07StudyEvidenceError(
            "v0.7 prepared study binding verification failed"
        ) from None

    try:
        campaign = verify_campaign_evidence(
            campaign_journal_path,
            episode_log_directory,
            spec,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise V07StudyEvidenceError(
            "v0.7 campaign/controller evidence verification failed"
        ) from None
    expected_tokenizers = prepared.tokenizer_artifacts_by_model
    if campaign.tokenizer_artifacts_by_model != expected_tokenizers:
        raise V07StudyEvidenceError(
            "campaign tokenizer artifacts differ from the prepared study"
        )

    try:
        executions = verify_v07_execution_envelope_set(
            execution_envelope_directory,
            spec,
            campaign,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise V07StudyEvidenceError(
            "v0.7 execution-envelope evidence verification failed"
        ) from None

    host_identity = prepared.execution_pins.host_safety.host_identity
    host_identity_record = host_identity.canonical_record()
    expected_daemon = DockerDaemonIdentity(
        binary_sha256=host_identity.docker_binary_sha256,
        endpoint_sha256=host_identity.docker_endpoint_sha256,
        client_version=host_identity.docker_client_version,
        server_version=host_identity.docker_server_version,
    )
    docker_daemon_identity_sha256 = _digest(expected_daemon.to_record())
    _require_campaign_host_identity(
        campaign,
        docker_daemon_identity_sha256,
    )

    try:
        interventions = verify_v07_intervention_journal(
            intervention_journal_path
        )
        interventions.canonical_record()
        declaration_evidence_sha256 = _declaration_evidence_sha256(
            interventions
        )
        if (
            interventions.schedule_sha256 != binding.formal_schedule_sha256
            or interventions.declaration_sha256
            != binding.intervention_declaration_sha256
            or declaration_evidence_sha256
            != binding.intervention_declaration_evidence_sha256
            or interventions.journal_instance_sha256
            != binding.intervention_journal_instance_sha256
        ):
            raise V07StudyEvidenceError(
                "final intervention declaration differs from the prepared study"
            )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise V07StudyEvidenceError(
            "v0.7 final intervention evidence verification failed"
        ) from None

    values: dict[str, object] = {
        "authorization_sha256": binding.authorization_sha256,
        "qualification_campaign_sha256": (
            binding.qualification_campaign_sha256
        ),
        "calibration_gold_campaign_sha256": (
            binding.calibration_gold_campaign_sha256
        ),
        "runtime_session_sha256": binding.runtime_session_sha256,
        "manifest_sha256": binding.manifest_sha256,
        "execution_pins_sha256": binding.execution_pins_sha256,
        "host_identity_sha256": host_identity_record["host_identity_sha256"],
        "docker_daemon_identity_sha256": docker_daemon_identity_sha256,
        "formal_campaign_sha256": binding.formal_campaign_sha256,
        "study_binding_sha256": binding.study_binding_sha256,
        "schedule_sha256": spec.schedule_sha256,
        "campaign_log_sha256": campaign.campaign_log_sha256,
        "episode_log_set_sha256": campaign.episode_log_set_sha256,
        "execution_set_sha256": executions.execution_set_sha256,
        "tokenizer_artifacts_by_model": expected_tokenizers,
        "intervention_declaration_sha256": interventions.declaration_sha256,
        "intervention_declaration_evidence_sha256": (
            declaration_evidence_sha256
        ),
        "intervention_journal_instance_sha256": (
            interventions.journal_instance_sha256
        ),
        "intervention_journal_root_sha256": (
            interventions.journal_root_sha256
        ),
        "intervention_journal_file_sha256": (
            interventions.journal_file_sha256
        ),
        "intervention_summary_sha256": interventions.summary_sha256,
        "verified_episode_count": campaign.verified_episode_count,
        "verified_envelope_count": executions.verified_envelope_count,
        "automatic_model_prompt_count": campaign.total_model_calls,
        "model_issued_environment_action_count": (
            campaign.total_environment_actions
        ),
        "replayed_environment_action_count": (
            campaign.total_replayed_environment_actions
        ),
        "physical_environment_action_count": (
            campaign.total_physical_environment_actions
        ),
        "benchmark_model_human_prompt_count": (
            interventions.benchmark_model_human_prompt_count
        ),
        "orchestrator_operator_event_count": (
            interventions.orchestrator_operator_event_count
        ),
        "operational_event_count": interventions.operational_event_count,
        "intervention_event_count": interventions.intervention_event_count,
    }
    core = _study_evidence_core(values)
    return VerifiedV07StudyEvidence(
        **values,  # type: ignore[arg-type]
        study_evidence_sha256=_digest(core),
        _campaign_evidence=campaign,
        _execution_evidence=executions,
        _intervention_summary=interventions,
        _authority=_AUTHORITY,
    )


def analyze_v07_study_effectiveness(
    evidence: VerifiedV07StudyEvidence,
) -> V07EffectivenessAnalysis:
    """Run frozen effectiveness math only through aggregate study evidence."""

    if type(evidence) is not VerifiedV07StudyEvidence:
        raise ValueError(
            "v0.7 publication analysis requires VerifiedV07StudyEvidence"
        )
    _validate_study_evidence(evidence)
    return analyze_v07_effectiveness(evidence._campaign_evidence)


def _require_bound_spec(spec: CampaignSpec, binding: V07StudyBinding) -> None:
    if (
        type(spec) is not CampaignSpec
        or spec.study_binding_sha256 != binding.study_binding_sha256
        or spec.schedule_sha256 != binding.formal_schedule_sha256
        or len(spec.episodes) != CAMPAIGN_EPISODE_COUNT
    ):
        raise V07StudyEvidenceError(
            "formal schedule differs from the prepared study binding"
        )


def _declaration_evidence_sha256(
    summary: VerifiedV07InterventionSummary,
) -> str:
    """Reconstruct the declaration identity retained by the final summary."""

    return _digest(
        {
            "declaration_sha256": summary.declaration_sha256,
            "journal_instance_sha256": summary.journal_instance_sha256,
            "journal_revision": V07_INTERVENTION_JOURNAL_REVISION,
            "schedule_sha256": summary.schedule_sha256,
            "schema": V07_INTERVENTION_DECLARATION_EVIDENCE_REVISION,
            "study_identity_sha256": summary.study_identity_sha256,
        }
    )


def _require_campaign_host_identity(
    campaign: VerifiedCampaignEvidence,
    expected_docker_daemon_identity_sha256: str,
) -> None:
    if (
        type(campaign) is not VerifiedCampaignEvidence
        or type(expected_docker_daemon_identity_sha256) is not str
        or _SHA256.fullmatch(expected_docker_daemon_identity_sha256) is None
    ):
        raise V07StudyEvidenceError(
            "aggregate host identity authority is invalid"
        )
    for row in campaign.matrix.episodes:
        for sample in (row.before_host_sample, row.after_host_sample):
            daemon = sample.docker_daemon
            if (
                type(daemon) is not DockerDaemonIdentity
                or _digest(daemon.to_record())
                != expected_docker_daemon_identity_sha256
            ):
                raise V07StudyEvidenceError(
                    "aggregate host identity differs from the prepared study"
                )


def _validate_study_evidence(evidence: VerifiedV07StudyEvidence) -> None:
    for field_name in (
        "authorization_sha256",
        "qualification_campaign_sha256",
        "calibration_gold_campaign_sha256",
        "runtime_session_sha256",
        "manifest_sha256",
        "execution_pins_sha256",
        "host_identity_sha256",
        "docker_daemon_identity_sha256",
        "formal_campaign_sha256",
        "study_binding_sha256",
        "schedule_sha256",
        "campaign_log_sha256",
        "episode_log_set_sha256",
        "execution_set_sha256",
        "intervention_declaration_sha256",
        "intervention_declaration_evidence_sha256",
        "intervention_journal_instance_sha256",
        "intervention_journal_root_sha256",
        "intervention_journal_file_sha256",
        "intervention_summary_sha256",
        "study_evidence_sha256",
    ):
        value = getattr(evidence, field_name)
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise V07StudyEvidenceError(
                f"v0.7 aggregate {field_name} is not a SHA-256 root"
            )
    if (
        type(evidence.tokenizer_artifacts_by_model) is not tuple
        or tuple(
            model_id
            for model_id, _artifact in evidence.tokenizer_artifacts_by_model
        )
        != CAMPAIGN_MODELS
        or any(
            type(artifact) is not str or _SHA256.fullmatch(artifact) is None
            for _model_id, artifact in evidence.tokenizer_artifacts_by_model
        )
    ):
        raise V07StudyEvidenceError(
            "v0.7 aggregate tokenizer artifact set is invalid"
        )
    for field_name in (
        "verified_episode_count",
        "verified_envelope_count",
        "automatic_model_prompt_count",
        "model_issued_environment_action_count",
        "replayed_environment_action_count",
        "physical_environment_action_count",
        "benchmark_model_human_prompt_count",
        "orchestrator_operator_event_count",
        "operational_event_count",
        "intervention_event_count",
    ):
        value = getattr(evidence, field_name)
        if type(value) is not int or value < 0:
            raise V07StudyEvidenceError(
                f"v0.7 aggregate {field_name} is invalid"
            )
    if (
        evidence.verified_episode_count != CAMPAIGN_EPISODE_COUNT
        or evidence.verified_envelope_count != CAMPAIGN_EPISODE_COUNT
    ):
        raise V07StudyEvidenceError(
            "v0.7 aggregate requires exactly 240 episodes and envelopes"
        )
    if type(evidence._campaign_evidence) is not VerifiedCampaignEvidence:
        raise V07StudyEvidenceError("aggregate campaign authority type is invalid")
    if (
        type(evidence._execution_evidence)
        is not VerifiedV07ExecutionEnvelopeSet
    ):
        raise V07StudyEvidenceError("aggregate execution authority type is invalid")
    if (
        type(evidence._intervention_summary)
        is not VerifiedV07InterventionSummary
    ):
        raise V07StudyEvidenceError(
            "aggregate intervention authority type is invalid"
        )

    campaign = evidence._campaign_evidence
    executions = evidence._execution_evidence
    interventions = evidence._intervention_summary
    executions.canonical_record()
    interventions.canonical_record()
    _require_campaign_host_identity(
        campaign,
        evidence.docker_daemon_identity_sha256,
    )
    if (
        campaign.verified_episode_count != evidence.verified_episode_count
        or len(campaign.matrix.episodes) != CAMPAIGN_EPISODE_COUNT
        or campaign.study_binding_sha256 != evidence.study_binding_sha256
        or campaign.schedule_sha256 != evidence.schedule_sha256
        or campaign.campaign_log_sha256 != evidence.campaign_log_sha256
        or campaign.episode_log_set_sha256 != evidence.episode_log_set_sha256
        or campaign.tokenizer_artifacts_by_model
        != evidence.tokenizer_artifacts_by_model
        or campaign.total_model_calls != evidence.automatic_model_prompt_count
        or campaign.total_environment_actions
        != evidence.model_issued_environment_action_count
        or campaign.total_replayed_environment_actions
        != evidence.replayed_environment_action_count
        or campaign.total_physical_environment_actions
        != evidence.physical_environment_action_count
    ):
        raise V07StudyEvidenceError(
            "aggregate campaign evidence differs from its retained roots"
        )
    if (
        executions.verified_envelope_count != evidence.verified_envelope_count
        or executions.study_binding_sha256 != evidence.study_binding_sha256
        or executions.schedule_sha256 != evidence.schedule_sha256
        or executions.campaign_log_sha256 != evidence.campaign_log_sha256
        or executions.episode_log_set_sha256 != evidence.episode_log_set_sha256
        or executions.execution_set_sha256 != evidence.execution_set_sha256
    ):
        raise V07StudyEvidenceError(
            "aggregate execution evidence differs from its retained roots"
        )
    if (
        interventions.schedule_sha256 != evidence.schedule_sha256
        or interventions.declaration_sha256
        != evidence.intervention_declaration_sha256
        or _declaration_evidence_sha256(interventions)
        != evidence.intervention_declaration_evidence_sha256
        or interventions.journal_instance_sha256
        != evidence.intervention_journal_instance_sha256
        or interventions.journal_root_sha256
        != evidence.intervention_journal_root_sha256
        or interventions.journal_file_sha256
        != evidence.intervention_journal_file_sha256
        or interventions.summary_sha256 != evidence.intervention_summary_sha256
        or interventions.benchmark_model_human_prompt_count
        != evidence.benchmark_model_human_prompt_count
        or interventions.orchestrator_operator_event_count
        != evidence.orchestrator_operator_event_count
        or interventions.operational_event_count
        != evidence.operational_event_count
        or interventions.intervention_event_count
        != evidence.intervention_event_count
    ):
        raise V07StudyEvidenceError(
            "aggregate intervention evidence differs from its retained roots"
        )
    if evidence.study_evidence_sha256 != _digest(evidence._core_record()):
        raise V07StudyEvidenceError("aggregate study evidence root is inconsistent")


def _study_evidence_core(values: dict[str, object]) -> dict[str, object]:
    record = dict(values)
    tokenizer_artifacts = values["tokenizer_artifacts_by_model"]
    if type(tokenizer_artifacts) is not tuple:
        raise V07StudyEvidenceError(
            "study tokenizer artifact set is not canonical"
        )
    record["tokenizer_artifacts_by_model"] = [
        {
            "model_id": model_id,
            "tokenizer_artifact_sha256": artifact,
        }
        for model_id, artifact in tokenizer_artifacts
    ]
    record["schema"] = V07_STUDY_EVIDENCE_REVISION
    return record


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


__all__ = (
    "V07_INTERVENTION_DECLARATION_EVIDENCE_REVISION",
    "V07_STUDY_EVIDENCE_REVISION",
    "V07StudyEvidenceError",
    "VerifiedV07StudyEvidence",
    "analyze_v07_study_effectiveness",
    "verify_v07_study_evidence",
)
