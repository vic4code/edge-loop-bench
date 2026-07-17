"""Verifier-sealed publication binding for the v0.7 confirmatory study.

This module performs no Docker, Ollama generation, or network operation.  It
cross-checks authority objects that were issued by the qualification,
calibration, runtime, intervention, manifest, and authorization boundaries.
Only path-free roots are serialized.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import InitVar, dataclass, field
from pathlib import Path

from .intercode_campaign_ledger import (
    CAMPAIGN_MODELS,
    CAMPAIGN_TASK_IDS,
    CampaignBeforeNewIntent,
    CampaignEpisode,
    CampaignSpec,
)
from .intercode_v07_authorization import V07CampaignAuthorization
from .intercode_v07_calibration import (
    V07_CALIBRATION_TASK_IDS,
    VerifiedV07CalibrationEvidence,
    evaluate_v07_calibration,
)
from .intercode_v07_docker_qualification import (
    V07CalibrationGoldResult,
    V07DockerQualificationResult,
    V07TrustedGoldMaterial,
)
from .intercode_v07_interventions import (
    VerifiedV07InterventionDeclaration,
    verify_v07_intervention_declaration,
)
from .intercode_v07_manifest import (
    V07ExecutionPins,
    V07PrecalibrationManifest,
)
from .intercode_v07_runtime_factory import V07ModelRuntime, V07RuntimeSession


V07_STUDY_BINDING_SCHEMA_REVISION = "intercode-v0.7-study-binding-v2"
V07_FORMAL_CAMPAIGN_AUTHORITY_REVISION = (
    "intercode-v0.7-formal-campaign-authority-v2"
)

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BINDING_SEAL = object()
_PREPARED_SEAL = object()


class V07StudyBindingError(RuntimeError):
    """Study authorities do not identify one exact v0.7 campaign."""


@dataclass(frozen=True, slots=True, repr=False)
class V07StudyBinding:
    """Path-free publication identity spanning all pre-formal authorities."""

    authorization_sha256: str
    qualification_campaign_sha256: str
    calibration_gold_campaign_sha256: str
    runtime_session_sha256: str
    intervention_declaration_sha256: str
    intervention_declaration_evidence_sha256: str
    intervention_journal_instance_sha256: str
    manifest_sha256: str
    execution_pins_sha256: str
    formal_schedule_sha256: str
    formal_campaign_sha256: str
    study_binding_sha256: str
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _BINDING_SEAL:
            raise V07StudyBindingError("v0.7 study bindings are verifier-sealed")
        _validate_binding(self)

    def _core_record(self) -> dict[str, object]:
        return {
            "authorization_sha256": self.authorization_sha256,
            "calibration_gold_campaign_sha256": (
                self.calibration_gold_campaign_sha256
            ),
            "execution_pins_sha256": self.execution_pins_sha256,
            "formal_campaign_sha256": self.formal_campaign_sha256,
            "formal_schedule_sha256": self.formal_schedule_sha256,
            "intervention_declaration_evidence_sha256": (
                self.intervention_declaration_evidence_sha256
            ),
            "intervention_declaration_sha256": (
                self.intervention_declaration_sha256
            ),
            "intervention_journal_instance_sha256": (
                self.intervention_journal_instance_sha256
            ),
            "manifest_sha256": self.manifest_sha256,
            "qualification_campaign_sha256": (
                self.qualification_campaign_sha256
            ),
            "runtime_session_sha256": self.runtime_session_sha256,
            "schema": V07_STUDY_BINDING_SCHEMA_REVISION,
        }

    def canonical_record(self) -> dict[str, object]:
        _validate_binding(self)
        return {
            **self._core_record(),
            "study_binding_sha256": self.study_binding_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_record()) + b"\n"

    def __repr__(self) -> str:
        return f"<V07StudyBinding root={self.study_binding_sha256}>"


@dataclass(frozen=True, slots=True, repr=False)
class V07PreparedStudy:
    """Private authority handles paired with one path-free study binding."""

    binding: V07StudyBinding
    _authorization: V07CampaignAuthorization = field(repr=False, compare=False)
    _qualification: V07DockerQualificationResult = field(repr=False, compare=False)
    _calibration_gold: V07CalibrationGoldResult = field(repr=False, compare=False)
    _calibration_evidence: VerifiedV07CalibrationEvidence = field(
        repr=False,
        compare=False,
    )
    _runtime_session: V07RuntimeSession = field(repr=False, compare=False)
    _intervention_declaration: VerifiedV07InterventionDeclaration = field(
        repr=False,
        compare=False,
    )
    _manifest: V07PrecalibrationManifest = field(repr=False, compare=False)
    _execution_pins: V07ExecutionPins = field(repr=False, compare=False)
    _campaign_spec: CampaignSpec = field(repr=False, compare=False)
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _PREPARED_SEAL:
            raise V07StudyBindingError("v0.7 prepared studies are verifier-sealed")
        _validate_prepared(self, require_live=False)

    @property
    def formal_campaign_sha256(self) -> str:
        return self.binding.formal_campaign_sha256

    @property
    def study_binding_sha256(self) -> str:
        return self.binding.study_binding_sha256

    @property
    def campaign_spec(self) -> CampaignSpec:
        """Return the exact schedule-only spec used to derive the binding."""

        _validate_prepared(self, require_live=False)
        return self._campaign_spec

    @property
    def bound_campaign_spec(self) -> CampaignSpec:
        """Return the durable formal spec bound to this exact study root."""

        _validate_prepared(self, require_live=False)
        return self._campaign_spec.bind(self.study_binding_sha256)

    @property
    def execution_pins(self) -> V07ExecutionPins:
        """Return the exact manifest-bound execution contract."""

        _validate_prepared(self, require_live=False)
        return self._execution_pins

    @property
    def tokenizer_artifacts_by_model(self) -> tuple[tuple[str, str], ...]:
        """Return the manifest-pinned helper used for logical token accounting."""

        _validate_prepared(self, require_live=False)
        return tuple(
            (identity.model_id, identity.tokenizer.helper_sha256)
            for identity in self._manifest.models
        )

    def model_runtime(self, model_id: str) -> V07ModelRuntime:
        """Delegate exact-model selection to the sealed live runtime session."""

        _validate_prepared(self, require_live=True)
        return self._runtime_session.model_runtime(model_id)

    def trusted_gold_for_episode(
        self,
        episode: CampaignEpisode,
    ) -> V07TrustedGoldMaterial:
        """Return only the opaque qualification gold for an exact episode."""

        _require_scheduled_episode(self._campaign_spec, episode)
        _validate_prepared(self, require_live=False)
        material = self._qualification.trusted_gold_by_task_id.get(episode.task_id)
        if (
            type(material) is not V07TrustedGoldMaterial
            or material.task_id != episode.task_id
        ):
            raise V07StudyBindingError(
                "formal episode lacks bound trusted gold material"
            )
        return material

    def canonical_record(self) -> dict[str, object]:
        _validate_prepared(self, require_live=False)
        return {
            "binding": self.binding.canonical_record(),
            "schema": "intercode-v0.7-prepared-study-v2",
        }

    def before_new_intent(
        self,
        episode: CampaignEpisode,
        *,
        repository_root: Path,
        intervention_journal_path: Path,
    ) -> None:
        """Revalidate all live authority before campaign code writes an intent."""

        _require_scheduled_episode(self._campaign_spec, episode)
        try:
            self._authorization.revalidate(repository_root)
            self._runtime_session.require_live()
            current_declaration = verify_v07_intervention_declaration(
                intervention_journal_path
            )
            _validate_prepared(self, require_live=True)
            if (
                current_declaration.canonical_record()
                != self._intervention_declaration.canonical_record()
                or current_declaration.declaration_sha256
                != self.binding.intervention_declaration_sha256
                or current_declaration.journal_instance_sha256
                != self.binding.intervention_journal_instance_sha256
            ):
                raise V07StudyBindingError(
                    "intervention declaration differs from the study binding"
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise V07StudyBindingError(
                "v0.7 pre-intent authority revalidation failed"
            ) from None

    def before_new_intent_callback(
        self,
        *,
        repository_root: Path,
        intervention_journal_path: Path,
    ) -> V07BeforeNewIntent:
        """Create the one-argument callback consumed by ``advance_campaign``."""

        if not isinstance(repository_root, Path) or not isinstance(
            intervention_journal_path, Path
        ):
            raise V07StudyBindingError("pre-intent callback paths must be exact Paths")
        callback: CampaignBeforeNewIntent = V07BeforeNewIntent(
            _prepared=self,
            _repository_root=repository_root,
            _intervention_journal_path=intervention_journal_path,
        )
        assert type(callback) is V07BeforeNewIntent
        return callback


@dataclass(frozen=True, slots=True, repr=False)
class V07BeforeNewIntent:
    """Local path-bearing operational handle; it has no serializable record."""

    _prepared: V07PreparedStudy = field(repr=False)
    _repository_root: Path = field(repr=False)
    _intervention_journal_path: Path = field(repr=False)

    def __post_init__(self) -> None:
        if type(self._prepared) is not V07PreparedStudy:
            raise V07StudyBindingError("pre-intent callback study type is invalid")
        if not isinstance(self._repository_root, Path) or not isinstance(
            self._intervention_journal_path, Path
        ):
            raise V07StudyBindingError("pre-intent callback paths must be exact Paths")

    def __call__(self, episode: CampaignEpisode) -> None:
        self._prepared.before_new_intent(
            episode,
            repository_root=self._repository_root,
            intervention_journal_path=self._intervention_journal_path,
        )


def prepare_v07_study(
    *,
    authorization: V07CampaignAuthorization,
    qualification: V07DockerQualificationResult,
    calibration_gold: V07CalibrationGoldResult,
    calibration_evidence: VerifiedV07CalibrationEvidence,
    runtime_session: V07RuntimeSession,
    intervention_declaration: VerifiedV07InterventionDeclaration,
    manifest: V07PrecalibrationManifest,
    execution_pins: V07ExecutionPins,
    campaign_spec: CampaignSpec,
) -> V07PreparedStudy:
    """Cross-check every verifier-issued prerequisite and retain its authority."""

    binding = _build_binding(
        authorization=authorization,
        qualification=qualification,
        calibration_gold=calibration_gold,
        calibration_evidence=calibration_evidence,
        runtime_session=runtime_session,
        intervention_declaration=intervention_declaration,
        manifest=manifest,
        execution_pins=execution_pins,
        campaign_spec=campaign_spec,
        require_live=True,
    )
    return V07PreparedStudy(
        binding=binding,
        _authorization=authorization,
        _qualification=qualification,
        _calibration_gold=calibration_gold,
        _calibration_evidence=calibration_evidence,
        _runtime_session=runtime_session,
        _intervention_declaration=intervention_declaration,
        _manifest=manifest,
        _execution_pins=execution_pins,
        _campaign_spec=campaign_spec,
        _seal=_PREPARED_SEAL,
    )


def _build_binding(
    *,
    authorization: V07CampaignAuthorization,
    qualification: V07DockerQualificationResult,
    calibration_gold: V07CalibrationGoldResult,
    calibration_evidence: VerifiedV07CalibrationEvidence,
    runtime_session: V07RuntimeSession,
    intervention_declaration: VerifiedV07InterventionDeclaration,
    manifest: V07PrecalibrationManifest,
    execution_pins: V07ExecutionPins,
    campaign_spec: CampaignSpec,
    require_live: bool,
) -> V07StudyBinding:
    _validate_authorities(
        authorization=authorization,
        qualification=qualification,
        calibration_gold=calibration_gold,
        calibration_evidence=calibration_evidence,
        runtime_session=runtime_session,
        intervention_declaration=intervention_declaration,
        manifest=manifest,
        execution_pins=execution_pins,
        campaign_spec=campaign_spec,
        require_live=require_live,
    )
    values: dict[str, str] = {
        "authorization_sha256": authorization.authorization_sha256,
        "qualification_campaign_sha256": (
            qualification.qualification_campaign_sha256
        ),
        "calibration_gold_campaign_sha256": (
            calibration_gold.calibration_campaign_sha256
        ),
        "runtime_session_sha256": runtime_session.session_sha256,
        "intervention_declaration_sha256": (
            intervention_declaration.declaration_sha256
        ),
        "intervention_declaration_evidence_sha256": (
            intervention_declaration.declaration_evidence_sha256
        ),
        "intervention_journal_instance_sha256": (
            intervention_declaration.journal_instance_sha256
        ),
        "manifest_sha256": manifest.manifest_sha256,
        "execution_pins_sha256": execution_pins.execution_pins_sha256,
        "formal_schedule_sha256": campaign_spec.schedule_sha256,
    }
    formal_campaign_sha256 = _digest(_formal_campaign_core(values))
    core = _binding_core(values, formal_campaign_sha256=formal_campaign_sha256)
    return V07StudyBinding(
        **values,
        formal_campaign_sha256=formal_campaign_sha256,
        study_binding_sha256=_digest(core),
        _seal=_BINDING_SEAL,
    )


def _validate_authorities(
    *,
    authorization: V07CampaignAuthorization,
    qualification: V07DockerQualificationResult,
    calibration_gold: V07CalibrationGoldResult,
    calibration_evidence: VerifiedV07CalibrationEvidence,
    runtime_session: V07RuntimeSession,
    intervention_declaration: VerifiedV07InterventionDeclaration,
    manifest: V07PrecalibrationManifest,
    execution_pins: V07ExecutionPins,
    campaign_spec: CampaignSpec,
    require_live: bool,
) -> None:
    exact_types = (
        (authorization, V07CampaignAuthorization, "authorization"),
        (qualification, V07DockerQualificationResult, "qualification"),
        (calibration_gold, V07CalibrationGoldResult, "calibration gold"),
        (
            calibration_evidence,
            VerifiedV07CalibrationEvidence,
            "calibration evidence",
        ),
        (runtime_session, V07RuntimeSession, "runtime session"),
        (
            intervention_declaration,
            VerifiedV07InterventionDeclaration,
            "intervention declaration",
        ),
        (manifest, V07PrecalibrationManifest, "manifest"),
        (execution_pins, V07ExecutionPins, "execution pins"),
        (campaign_spec, CampaignSpec, "campaign spec"),
    )
    for value, expected, label in exact_types:
        if type(value) is not expected:
            raise V07StudyBindingError(f"study requires exact {label} authority")

    try:
        authorization_record = authorization.canonical_record()
        qualification.evidence.require_admitted()
        manifest_record = manifest.canonical_record()
        execution_pins.canonical_record()
        declaration_record = intervention_declaration.canonical_record()
        if require_live:
            runtime_session.require_live()
        runtime_record = runtime_session.canonical_record()
        dispositions = tuple(
            evaluate_v07_calibration(calibration_evidence, model_id)
            for model_id in CAMPAIGN_MODELS
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise V07StudyBindingError("study authority validation failed") from None

    if (
        qualification.evidence.evidence_root_sha256
        != authorization.qualification_evidence_root_sha256
        or qualification.evidence.suite_sha256
        != authorization.qualification_suite_sha256
        or manifest.artifacts.qualification.evidence_root_sha256
        != qualification.evidence.evidence_root_sha256
        or manifest.artifacts.qualification.suite_sha256
        != qualification.evidence.suite_sha256
    ):
        raise V07StudyBindingError(
            "qualification campaign differs from authorization or manifest"
        )
    if tuple(qualification.trusted_gold_by_task_id) != CAMPAIGN_TASK_IDS:
        raise V07StudyBindingError("qualification campaign gold set is incomplete")

    if (
        calibration_gold.calibration_campaign_sha256
        != calibration_evidence.calibration_campaign_sha256
    ):
        raise V07StudyBindingError(
            "calibration gold campaign differs from calibration evidence"
        )
    if tuple(calibration_gold.trusted_gold_by_task_id) != V07_CALIBRATION_TASK_IDS:
        raise V07StudyBindingError("calibration gold campaign set is incomplete")
    if (
        calibration_evidence.precalibration_manifest_sha256
        != manifest.manifest_sha256
        or calibration_evidence.evidence_sha256
        != authorization.calibration_evidence_sha256
        or calibration_evidence.calibration_journal_sha256
        != authorization.calibration_journal_sha256
        or calibration_evidence.controller_log_set_sha256
        != authorization.calibration_controller_log_set_sha256
        or tuple(item.disposition_sha256 for item in dispositions)
        != authorization.calibration_disposition_sha256s
    ):
        raise V07StudyBindingError(
            "calibration evidence differs from authorization or manifest"
        )

    if (
        manifest.manifest_sha256 != authorization.manifest_sha256
        or execution_pins != manifest.execution
        or execution_pins.execution_pins_sha256
        != authorization.execution_root_sha256
        or authorization_record["execution_root_sha256"]
        != execution_pins.execution_pins_sha256
    ):
        raise V07StudyBindingError(
            "execution pins differ from authorization or manifest"
        )

    runtime_models = runtime_record.get("models")
    if type(runtime_models) is not list or any(
        type(item) is not dict for item in runtime_models
    ):
        raise V07StudyBindingError("runtime session public model set is invalid")
    runtime_model_identities = tuple(
        item.get("model_identity") for item in runtime_models
    )
    if (
        runtime_session.session_sha256 != runtime_record.get("session_sha256")
        or runtime_record.get("host_identity")
        != manifest.host_identity.canonical_record()
        or runtime_model_identities
        != tuple(item.canonical_record() for item in manifest.models)
    ):
        raise V07StudyBindingError("runtime session differs from manifest")

    if (
        campaign_spec.task_ids != CAMPAIGN_TASK_IDS
        or campaign_spec.schedule_sha256 != manifest.artifacts.schedule_sha256
        or declaration_record.get("schedule_sha256")
        != campaign_spec.schedule_sha256
    ):
        raise V07StudyBindingError(
            "formal schedule differs from manifest or intervention declaration"
        )
    if manifest_record.get("manifest_sha256") != manifest.manifest_sha256:
        raise V07StudyBindingError("manifest root is internally inconsistent")


def _validate_binding(binding: V07StudyBinding) -> None:
    for field_name in (
        "authorization_sha256",
        "qualification_campaign_sha256",
        "calibration_gold_campaign_sha256",
        "runtime_session_sha256",
        "intervention_declaration_sha256",
        "intervention_declaration_evidence_sha256",
        "intervention_journal_instance_sha256",
        "manifest_sha256",
        "execution_pins_sha256",
        "formal_schedule_sha256",
        "formal_campaign_sha256",
        "study_binding_sha256",
    ):
        value = getattr(binding, field_name)
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise V07StudyBindingError(f"study {field_name} is not a SHA-256 root")
    expected_formal = _formal_campaign_sha256(binding)
    if binding.formal_campaign_sha256 != expected_formal:
        raise V07StudyBindingError("formal campaign authority root is inconsistent")
    if binding.study_binding_sha256 != _digest(binding._core_record()):
        raise V07StudyBindingError("study binding root is inconsistent")


def _validate_prepared(
    prepared: V07PreparedStudy,
    *,
    require_live: bool,
) -> None:
    if type(prepared.binding) is not V07StudyBinding:
        raise V07StudyBindingError("prepared study binding type is invalid")
    _validate_binding(prepared.binding)
    expected = _build_binding(
        authorization=prepared._authorization,
        qualification=prepared._qualification,
        calibration_gold=prepared._calibration_gold,
        calibration_evidence=prepared._calibration_evidence,
        runtime_session=prepared._runtime_session,
        intervention_declaration=prepared._intervention_declaration,
        manifest=prepared._manifest,
        execution_pins=prepared._execution_pins,
        campaign_spec=prepared._campaign_spec,
        require_live=require_live,
    )
    if expected != prepared.binding:
        raise V07StudyBindingError("prepared study authorities differ from binding")


def _formal_campaign_sha256(binding: V07StudyBinding) -> str:
    values = {
        "authorization_sha256": binding.authorization_sha256,
        "qualification_campaign_sha256": binding.qualification_campaign_sha256,
        "calibration_gold_campaign_sha256": (
            binding.calibration_gold_campaign_sha256
        ),
        "runtime_session_sha256": binding.runtime_session_sha256,
        "intervention_declaration_sha256": (
            binding.intervention_declaration_sha256
        ),
        "intervention_declaration_evidence_sha256": (
            binding.intervention_declaration_evidence_sha256
        ),
        "intervention_journal_instance_sha256": (
            binding.intervention_journal_instance_sha256
        ),
        "manifest_sha256": binding.manifest_sha256,
        "execution_pins_sha256": binding.execution_pins_sha256,
        "formal_schedule_sha256": binding.formal_schedule_sha256,
    }
    return _digest(_formal_campaign_core(values))


def _formal_campaign_core(values: dict[str, str]) -> dict[str, object]:
    return {
        "authorization_sha256": values["authorization_sha256"],
        "calibration_gold_campaign_sha256": values[
            "calibration_gold_campaign_sha256"
        ],
        "execution_pins_sha256": values["execution_pins_sha256"],
        "formal_schedule_sha256": values["formal_schedule_sha256"],
        "intervention_declaration_evidence_sha256": values[
            "intervention_declaration_evidence_sha256"
        ],
        "intervention_declaration_sha256": values[
            "intervention_declaration_sha256"
        ],
        "intervention_journal_instance_sha256": values[
            "intervention_journal_instance_sha256"
        ],
        "manifest_sha256": values["manifest_sha256"],
        "qualification_campaign_sha256": values[
            "qualification_campaign_sha256"
        ],
        "revision": V07_FORMAL_CAMPAIGN_AUTHORITY_REVISION,
        "runtime_session_sha256": values["runtime_session_sha256"],
    }


def _binding_core(
    values: dict[str, str],
    *,
    formal_campaign_sha256: str,
) -> dict[str, object]:
    return {
        "authorization_sha256": values["authorization_sha256"],
        "calibration_gold_campaign_sha256": values[
            "calibration_gold_campaign_sha256"
        ],
        "execution_pins_sha256": values["execution_pins_sha256"],
        "formal_campaign_sha256": formal_campaign_sha256,
        "formal_schedule_sha256": values["formal_schedule_sha256"],
        "intervention_declaration_evidence_sha256": values[
            "intervention_declaration_evidence_sha256"
        ],
        "intervention_declaration_sha256": values[
            "intervention_declaration_sha256"
        ],
        "intervention_journal_instance_sha256": values[
            "intervention_journal_instance_sha256"
        ],
        "manifest_sha256": values["manifest_sha256"],
        "qualification_campaign_sha256": values[
            "qualification_campaign_sha256"
        ],
        "runtime_session_sha256": values["runtime_session_sha256"],
        "schema": V07_STUDY_BINDING_SCHEMA_REVISION,
    }


def _require_scheduled_episode(
    spec: CampaignSpec,
    episode: CampaignEpisode,
) -> None:
    if type(episode) is not CampaignEpisode:
        raise V07StudyBindingError("pre-intent episode type is invalid")
    if (
        isinstance(episode.episode_index, bool)
        or type(episode.episode_index) is not int
        or not 1 <= episode.episode_index <= len(spec.episodes)
        or spec.episodes[episode.episode_index - 1] != episode
    ):
        raise V07StudyBindingError("pre-intent episode differs from formal schedule")


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
    "V07_FORMAL_CAMPAIGN_AUTHORITY_REVISION",
    "V07_STUDY_BINDING_SCHEMA_REVISION",
    "V07BeforeNewIntent",
    "V07PreparedStudy",
    "V07StudyBinding",
    "V07StudyBindingError",
    "prepare_v07_study",
)
