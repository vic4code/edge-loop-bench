"""Outcome-free, builder-sealed manifest for the v0.7 InterCode study.

The manifest is a pure data boundary.  It performs no filesystem, Docker,
Ollama, or network operation.  Callers first collect path-free identities at
their respective trusted boundaries, then seal those identities here before
any calibration model request is admitted.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass
from types import MappingProxyType

from .docker_action_executor import DockerActionLimits
from .docker_cli import DockerLimits, WRITABLE_LAYER_STORAGE_MODE
from .interactive_controller import INTERACTIVE_CONTROLLER_REVISION
from .intercode_campaign_ledger import (
    CAMPAIGN_ARMS,
    CAMPAIGN_ATTEMPT_CAP,
    CAMPAIGN_MODELS,
    CAMPAIGN_PROGRESS_REVISION,
    CAMPAIGN_SEED,
    CAMPAIGN_STRICT_EVALUATOR_REVISION,
    CAMPAIGN_TASK_IDS,
    CampaignSpec,
)
from .intercode_local_model import (
    LocalModelAttestation,
    OLLAMA_GENERATION_ENDPOINT_SHA256,
    OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
)
from .intercode_replay_environment import V07_STRICT_REPLAY_EVALUATOR_SHA256
from .intercode_source import (
    INTERCODE_REVISION,
    SOURCE_CORPUS_SHA256,
    STATIC_EXCLUSION_AUDIT_SHA256,
)
from .intercode_source_inventory import (
    VerifiedSourceInventory,
    derive_source_subset_sha256,
)
from .intercode_v07_calibration import (
    V07_CALIBRATION_DESIGN_SHA256,
    canonical_v07_calibration_design_record,
)
from .intercode_v07_protocol import V07_SAMPLE_MANIFEST_SHA256, V07_TASK_IDS
from .intercode_v07_qualification import VerifiedV07QualificationEvidence
from .model_adapter import (
    OllamaGenerationConfig,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
)


V07_MANIFEST_SCHEMA_REVISION = "intercode-v0.7-precalibration-manifest-v2"
V07_PROMPT_POLICY_REVISION = "intercode-v0.7-four-arm-prompt-policy-v1"
V07_TOKENIZER_POLICY_REVISION = "ollama-llama-tokenize-v1"
V07_LLAMA_CPP_COMMIT = "8c146a8366304c871efc26057cc90370ccf58dad"
V07_RUN_ID_POLICY_REVISION = (
    "intercode-v0.7-run-id-campaign-episode-role-sha256-v1"
)
V07_INTERVENTION_JOURNAL_REVISION = (
    "intercode-v0.7-operational-intervention-journal-v2"
)
V07_SCHEDULE_SHA256 = (
    "sha256:68325d5cb1edb7a0f01a338aa05cbfc92bd3c13381bb5c47fe3cf53a4fe27129"
)

_STRATA = ("fs1", "fs2", "fs3", "fs4")
_ALLOWED_PROFILES = {
    QWEN35_RAW_PROFILE.model: QWEN35_RAW_PROFILE,
    PHI4_MINI_RAW_PROFILE.model: PHI4_MINI_RAW_PROFILE,
}
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_STOP_SEQUENCE = re.compile(r"[A-Za-z0-9_<>|./:+-]{1,64}\Z")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]")
_EMBEDDED_LOCAL_PATH = re.compile(
    r"/(?:Users|Volumes|home|private|tmp|var/folders)(?:/|\Z)",
    re.IGNORECASE,
)
_CONSTRUCTION_SEAL = object()
_MODEL_IDENTITY_SEAL = object()
_QUALIFICATION_SEAL = object()
_ARTIFACT_SEAL = object()
_HOST_SAFETY_SEAL = object()
_EXECUTION_SEAL = object()
_PROMPT_POLICY_SOURCES = ("src/edgeloopbench/interactive_controller.py",)
_CONTROLLER_SOURCES = ("src/edgeloopbench/interactive_controller.py",)
_PROGRESS_EVALUATOR_SOURCES = (
    "src/edgeloopbench/intercode_evaluator.py",
    "src/edgeloopbench/intercode_replay_environment.py",
)
_HOST_SAFETY_SOURCES = (
    "src/edgeloopbench/intercode_host_safety.py",
    "src/edgeloopbench/intercode_v07_host_policy.py",
)


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


def _canonical_clone(value: object) -> object:
    """Detach nested mutable definitions through the canonical data boundary."""

    return json.loads(_canonical_json(value))


def _require_sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 reference")
    return value


def _require_exact(value: object, expected: object, field: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"{field} differs from the frozen v0.7 value")


@dataclass(frozen=True, slots=True)
class V07TokenizerPins:
    """Path-free identity of the exact tokenizer used for logical accounting."""

    helper_sha256: str
    model_artifact_sha256: str
    llama_cpp_commit: str
    policy_revision: str

    def __post_init__(self) -> None:
        _require_sha256(self.helper_sha256, "tokenizer helper")
        _require_sha256(self.model_artifact_sha256, "tokenizer model artifact")
        _require_exact(
            self.llama_cpp_commit,
            V07_LLAMA_CPP_COMMIT,
            "tokenizer source revision",
        )
        _require_exact(
            self.policy_revision,
            V07_TOKENIZER_POLICY_REVISION,
            "tokenizer policy revision",
        )

    def _core_record(self) -> dict[str, object]:
        return {
            "helper_sha256": self.helper_sha256,
            "llama_cpp_commit": self.llama_cpp_commit,
            "model_artifact_sha256": self.model_artifact_sha256,
            "policy_revision": self.policy_revision,
        }

    @property
    def identity_sha256(self) -> str:
        self.__post_init__()
        return _digest_record(self._core_record())


@dataclass(frozen=True, slots=True)
class V07ModelIdentityPins:
    """One small model's artifact, renderer, tokenizer, and runtime contract."""

    model_id: str
    generation: OllamaGenerationConfig
    model_config_sha256: str
    model_artifact_size_bytes: int
    local_attestation_sha256: str
    tokenizer: V07TokenizerPins
    weight_quantization: str
    kv_cache_quantization: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _MODEL_IDENTITY_SEAL:
            raise ValueError("v0.7 model identity pins are builder-sealed")
        if self.model_id not in _ALLOWED_PROFILES:
            raise ValueError("model_id is outside the preregistered small-model set")
        if type(self.generation) is not OllamaGenerationConfig:
            raise ValueError("generation must use the exact Ollama config type")
        self.generation.__post_init__()
        expected_profile = _ALLOWED_PROFILES[self.model_id]
        if self.generation.profile != expected_profile:
            raise ValueError("model renderer profile differs from its frozen identity")
        if self.generation.model != self.model_id:
            raise ValueError("generation and model identities differ")
        if self.generation.endpoint != "http://127.0.0.1:11434":
            raise ValueError("generation endpoint differs from the frozen loopback endpoint")
        if type(self.generation.stop) is not tuple or any(
            item.startswith(("/", "./", "../"))
            or "file://" in item.lower()
            or _WINDOWS_ABSOLUTE_PATH.match(item) is not None
            or _EMBEDDED_LOCAL_PATH.search(item) is not None
            or "/../" in item
            or item.endswith("/..")
            or "/./" in item
            or item.endswith("/.")
            or _STOP_SEQUENCE.fullmatch(item) is None
            for item in self.generation.stop
        ):
            raise ValueError(
                "generation stop sequences must use the immutable path-free pin surface"
            )
        _require_sha256(self.model_config_sha256, "model config")
        _require_sha256(self.local_attestation_sha256, "local model attestation")
        if (
            type(self.model_artifact_size_bytes) is not int
            or not 1 << 30 <= self.model_artifact_size_bytes <= 4 << 30
        ):
            raise ValueError("small-model artifact size is outside the one-to-four-GiB bound")
        if type(self.tokenizer) is not V07TokenizerPins:
            raise ValueError("tokenizer must use the exact v0.7 pin type")
        self.tokenizer.__post_init__()
        if self.tokenizer.model_artifact_sha256 != expected_profile.model_artifact_sha256:
            raise ValueError("tokenizer and model artifacts differ")
        _require_exact(
            self.weight_quantization,
            "Q4_K_M",
            "weight quantization",
        )
        if self.kv_cache_quantization != "q8_0":
            raise ValueError("KV-cache quantization differs from the frozen launch policy")

    def _core_record(self) -> dict[str, object]:
        profile = self.generation.profile
        tokenizer = self.tokenizer._core_record()
        tokenizer["tokenizer_identity_sha256"] = self.tokenizer.identity_sha256
        return {
            "generation": {
                "generation_config": _canonical_clone(
                    self.generation.canonical_definition
                ),
                "generation_config_sha256": self.generation.sha256,
            },
            "local_attestation_sha256": self.local_attestation_sha256,
            "model_artifact_sha256": profile.model_artifact_sha256,
            "model_artifact_size_bytes": self.model_artifact_size_bytes,
            "model_config_sha256": self.model_config_sha256,
            "model_id": self.model_id,
            "model_manifest_sha256": profile.model_manifest_sha256,
            "profile": {
                "algorithm_revision": profile.algorithm_revision,
                "ollama_source_commit": profile.ollama_commit,
                "renderer_profile_id": profile.profile_id,
                "renderer_profile_sha256": profile.sha256,
                "renderer_source_sha256": profile.ollama_source_sha256,
            },
            "quantization": {
                "kv_cache": self.kv_cache_quantization,
                "weight": self.weight_quantization,
            },
            "runtime": {
                "runtime_binary_sha256": self.generation.runtime_binary_sha256,
                "runtime_source_commit": profile.ollama_commit,
                "runtime_version": self.generation.runtime_version,
            },
            "tokenizer": tokenizer,
        }

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__(_MODEL_IDENTITY_SEAL)
        record = self._core_record()
        record["model_identity_sha256"] = _digest_record(record)
        return record


def bind_v07_model_identity(
    *,
    attestation: LocalModelAttestation,
    generation: OllamaGenerationConfig,
    tokenizer: V07TokenizerPins,
) -> V07ModelIdentityPins:
    """Project one verifier-sealed local attestation into path-free pins."""

    if type(attestation) is not LocalModelAttestation:
        raise ValueError("model attestation must be verifier-sealed")
    record = attestation.canonical_record()
    if type(generation) is not OllamaGenerationConfig:
        raise ValueError("generation must use the exact Ollama config type")
    generation.__post_init__()
    if generation.model != record["model"]:
        raise ValueError("model attestation and generation identities differ")
    profile = generation.profile
    comparisons = (
        (
            record["renderer_profile_sha256"],
            profile.sha256,
            "renderer profile",
        ),
        (
            record["model_manifest_sha256"],
            profile.model_manifest_sha256,
            "model manifest",
        ),
        (
            record["model_artifact_sha256"],
            profile.model_artifact_sha256,
            "model artifact",
        ),
        (
            record["runtime_version"],
            generation.runtime_version,
            "runtime version",
        ),
        (
            record["runtime_binary_sha256"],
            generation.runtime_binary_sha256,
            "runtime binary",
        ),
    )
    for observed, expected, field in comparisons:
        if observed != expected:
            raise ValueError(f"model attestation {field} differs from generation")
    return V07ModelIdentityPins(
        model_id=attestation.model,
        generation=generation,
        model_config_sha256=attestation.model_config_sha256,
        model_artifact_size_bytes=attestation.model_artifact_size_bytes,
        local_attestation_sha256=attestation.attestation_sha256,
        tokenizer=tokenizer,
        weight_quantization=attestation.weight_quantization,
        kv_cache_quantization=attestation.kv_cache_quantization,
        _construction_seal=_MODEL_IDENTITY_SEAL,
    )


@dataclass(frozen=True, slots=True)
class V07HostIdentityPins:
    """Complete path-free Docker and Ollama identity pins."""

    docker_binary_sha256: str
    docker_endpoint_sha256: str
    docker_client_version: str
    docker_server_version: str
    ollama_runtime_binary_sha256: str
    ollama_server_version: str
    ollama_launch_environment_sha256: str
    ollama_generation_endpoint_sha256: str

    def __post_init__(self) -> None:
        docker_values = (
            self.docker_binary_sha256,
            self.docker_endpoint_sha256,
            self.docker_client_version,
            self.docker_server_version,
        )
        if any(value is None for value in docker_values):
            raise ValueError("Docker identity pins must be supplied together")
        _require_sha256(self.docker_binary_sha256, "Docker identity binary")
        _require_sha256(self.docker_endpoint_sha256, "Docker identity endpoint")
        for field in ("docker_client_version", "docker_server_version"):
            value = getattr(self, field)
            if type(value) is not str or _VERSION.fullmatch(value) is None:
                raise ValueError(f"Docker identity {field} is invalid")
        _require_sha256(
            self.ollama_runtime_binary_sha256,
            "Ollama runtime binary",
        )
        _require_exact(
            self.ollama_server_version,
            "0.31.1",
            "Ollama version",
        )
        _require_exact(
            self.ollama_launch_environment_sha256,
            OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
            "Ollama launch environment",
        )
        _require_exact(
            self.ollama_generation_endpoint_sha256,
            OLLAMA_GENERATION_ENDPOINT_SHA256,
            "Ollama generation endpoint",
        )

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__()
        core = {
            "docker": {
                "binary_sha256": self.docker_binary_sha256,
                "client_version": self.docker_client_version,
                "endpoint_sha256": self.docker_endpoint_sha256,
                "server_version": self.docker_server_version,
            },
            "ollama": {
                "generation_endpoint_sha256": self.ollama_generation_endpoint_sha256,
                "launch_environment_sha256": self.ollama_launch_environment_sha256,
                "runtime_binary_sha256": self.ollama_runtime_binary_sha256,
                "server_version": self.ollama_server_version,
            },
        }
        core["host_identity_sha256"] = _digest_record(core)
        return core


@dataclass(frozen=True, slots=True)
class V07QualificationPins:
    """Outcome-free projection of selected-sample qualification evidence."""

    evidence_root_sha256: str
    suite_sha256: str
    image_id_by_stratum: Mapping[str, str]
    evaluator_sha256: str
    state_normalization_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _QUALIFICATION_SEAL:
            raise ValueError("v0.7 qualification pins are builder-sealed")
        for field, label in (
            ("evidence_root_sha256", "qualification evidence root"),
            ("suite_sha256", "qualification suite"),
            ("evaluator_sha256", "qualification evaluator"),
            ("state_normalization_sha256", "qualification normalizer"),
        ):
            _require_sha256(getattr(self, field), label)
        if not isinstance(self.image_id_by_stratum, Mapping):
            raise ValueError("qualification image IDs must be a four-stratum mapping")
        images = dict(self.image_id_by_stratum)
        if tuple(sorted(images)) != _STRATA:
            raise ValueError("qualification image IDs must be a four-stratum mapping")
        for stratum in _STRATA:
            _require_sha256(images[stratum], f"{stratum} image")
        object.__setattr__(
            self,
            "image_id_by_stratum",
            MappingProxyType({stratum: images[stratum] for stratum in _STRATA}),
        )

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__(_QUALIFICATION_SEAL)
        return {
            "evaluator_sha256": self.evaluator_sha256,
            "evidence_root_sha256": self.evidence_root_sha256,
            "image_id_by_stratum": dict(self.image_id_by_stratum),
            "state_normalization_sha256": self.state_normalization_sha256,
            "suite_sha256": self.suite_sha256,
        }


def bind_v07_qualification_manifest(
    evidence: VerifiedV07QualificationEvidence,
) -> V07QualificationPins:
    """Reverify the exact 30-task evidence and retain no task outcomes."""

    if type(evidence) is not VerifiedV07QualificationEvidence:
        raise ValueError(
            "qualification must be verified selected-sample evidence"
        )
    evidence.require_admitted()
    return V07QualificationPins(
        evidence_root_sha256=evidence.evidence_root_sha256,
        suite_sha256=evidence.suite_sha256,
        image_id_by_stratum=evidence.image_id_by_stratum,
        evaluator_sha256=evidence.evaluator_sha256,
        state_normalization_sha256=evidence.state_normalization_sha256,
        _construction_seal=_QUALIFICATION_SEAL,
    )


@dataclass(frozen=True, slots=True)
class V07ArtifactPins:
    """Frozen source, sample, qualification, and executable-code identities."""

    source_inventory_sha256: str
    source_head_commit: str
    source_head_tree: str
    source_revision: str
    source_corpus_sha256: str
    static_exclusion_audit_sha256: str
    task_ids: tuple[str, ...]
    sample_manifest_sha256: str
    schedule_sha256: str
    qualification: V07QualificationPins
    prompt_policy_sha256: str
    controller_source_sha256: str
    progress_evaluator_source_sha256: str
    strict_evaluator_sha256: str
    calibration_design_sha256: str
    source_code_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _ARTIFACT_SEAL:
            raise ValueError("v0.7 artifact pins are builder-sealed")
        _require_sha256(self.source_inventory_sha256, "source inventory")
        if (
            type(self.source_head_commit) is not str
            or type(self.source_head_tree) is not str
            or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", self.source_head_commit)
            is None
            or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", self.source_head_tree)
            is None
            or len(self.source_head_commit) != len(self.source_head_tree)
        ):
            raise ValueError("source Git identities are invalid")
        _require_exact(self.source_revision, INTERCODE_REVISION, "source revision")
        _require_exact(
            self.source_corpus_sha256,
            SOURCE_CORPUS_SHA256,
            "source corpus",
        )
        _require_exact(
            self.static_exclusion_audit_sha256,
            STATIC_EXCLUSION_AUDIT_SHA256,
            "static audit",
        )
        if type(self.task_ids) is not tuple or self.task_ids != V07_TASK_IDS:
            raise ValueError("task sample differs from the frozen 30-task order")
        _require_exact(
            self.sample_manifest_sha256,
            "sha256:" + V07_SAMPLE_MANIFEST_SHA256,
            "sample manifest",
        )
        _require_exact(self.schedule_sha256, V07_SCHEDULE_SHA256, "schedule")
        if type(self.qualification) is not V07QualificationPins:
            raise ValueError("qualification must use verified v0.7 pins")
        self.qualification.__post_init__(_QUALIFICATION_SEAL)
        for field, label in (
            ("prompt_policy_sha256", "prompt policy source"),
            ("controller_source_sha256", "controller source"),
            ("progress_evaluator_source_sha256", "progress evaluator source"),
            ("source_code_sha256", "source code"),
        ):
            _require_sha256(getattr(self, field), label)
        if self.source_code_sha256 != self.source_inventory_sha256:
            raise ValueError("source code and inventory roots differ")
        _require_exact(
            self.strict_evaluator_sha256,
            V07_STRICT_REPLAY_EVALUATOR_SHA256,
            "strict evaluator",
        )
        _require_exact(
            self.calibration_design_sha256,
            V07_CALIBRATION_DESIGN_SHA256,
            "calibration design",
        )


def build_v07_artifact_pins(
    *,
    source_inventory: VerifiedSourceInventory,
    qualification_evidence: VerifiedV07QualificationEvidence,
) -> V07ArtifactPins:
    """Derive every executable-code pin from one verified clean checkout."""

    if type(source_inventory) is not VerifiedSourceInventory:
        raise ValueError("artifact pins require verified source inventory")
    source_record = source_inventory.canonical_record()
    qualification = bind_v07_qualification_manifest(qualification_evidence)
    return V07ArtifactPins(
        source_inventory_sha256=source_inventory.inventory_sha256,
        source_head_commit=source_inventory.head_commit,
        source_head_tree=source_inventory.head_tree,
        source_revision=INTERCODE_REVISION,
        source_corpus_sha256=SOURCE_CORPUS_SHA256,
        static_exclusion_audit_sha256=STATIC_EXCLUSION_AUDIT_SHA256,
        task_ids=V07_TASK_IDS,
        sample_manifest_sha256="sha256:" + V07_SAMPLE_MANIFEST_SHA256,
        schedule_sha256=V07_SCHEDULE_SHA256,
        qualification=qualification,
        prompt_policy_sha256=_derive_role_source_sha256(
            source_inventory, "prompt_policy", _PROMPT_POLICY_SOURCES
        ),
        controller_source_sha256=_derive_role_source_sha256(
            source_inventory, "controller", _CONTROLLER_SOURCES
        ),
        progress_evaluator_source_sha256=_derive_role_source_sha256(
            source_inventory,
            "progress_evaluator",
            _PROGRESS_EVALUATOR_SOURCES,
        ),
        strict_evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
        calibration_design_sha256=V07_CALIBRATION_DESIGN_SHA256,
        source_code_sha256=str(source_record["inventory_sha256"]),
        _construction_seal=_ARTIFACT_SEAL,
    )


@dataclass(frozen=True, slots=True)
class V07BudgetPins:
    """Exact shared per-episode ceilings for all four arms."""

    attempts: int = 4
    logical_prompt_tokens: int = 16_380
    completion_tokens: int = 2_048
    per_call_context_tokens: int = 4_096
    max_output_tokens: int = 512
    model_calls: int = 4
    environment_actions: int = 4
    evaluator_calls: int = 5
    checkpoint_creates: int = 4
    checkpoint_restores: int = 4
    safety_recoveries: int = 4

    def __post_init__(self) -> None:
        expected = {
            "attempts": 4,
            "logical_prompt_tokens": 16_380,
            "completion_tokens": 2_048,
            "per_call_context_tokens": 4_096,
            "max_output_tokens": 512,
            "model_calls": 4,
            "environment_actions": 4,
            "evaluator_calls": 5,
            "checkpoint_creates": 4,
            "checkpoint_restores": 4,
            "safety_recoveries": 4,
        }
        for field, value in expected.items():
            _require_exact(getattr(self, field), value, field)

    def canonical_record(self) -> dict[str, int]:
        self.__post_init__()
        return {
            field: getattr(self, field)
            for field in (
                "attempts",
                "checkpoint_creates",
                "checkpoint_restores",
                "completion_tokens",
                "environment_actions",
                "evaluator_calls",
                "logical_prompt_tokens",
                "max_output_tokens",
                "model_calls",
                "per_call_context_tokens",
                "safety_recoveries",
            )
        }


@dataclass(frozen=True, slots=True)
class V07DesignPins:
    """Exact causal, inference, and campaign-wide request design."""

    models: tuple[str, ...] = CAMPAIGN_MODELS
    arms: tuple[str, ...] = CAMPAIGN_ARMS
    seed: int = 11
    attempt_cap: int = 4
    bootstrap_seed: int = 20_260_716
    bootstrap_replicates: int = 10_000
    calibration_prompt_cap: int = 26
    confirmatory_prompt_cap: int = 780

    def __post_init__(self) -> None:
        expected = {
            "models": CAMPAIGN_MODELS,
            "arms": CAMPAIGN_ARMS,
            "seed": CAMPAIGN_SEED,
            "attempt_cap": CAMPAIGN_ATTEMPT_CAP,
            "bootstrap_seed": 20_260_716,
            "bootstrap_replicates": 10_000,
            "calibration_prompt_cap": 26,
            "confirmatory_prompt_cap": 780,
        }
        for field, value in expected.items():
            _require_exact(getattr(self, field), value, field)


@dataclass(frozen=True, slots=True)
class V07HostSafetyPins:
    """Exact v0.7 host thresholds without v0.6 requeue/request semantics."""

    host_identity: V07HostIdentityPins
    policy_source_sha256: str
    telemetry_collector_source_sha256: str
    require_ac_power: bool = True
    require_low_power_mode_off: bool = True
    require_no_thermal_warnings: bool = True
    require_no_performance_warnings: bool = True
    required_vm_pressure_level: int = 1
    admission_free_percent_minimum: int = 25
    abort_free_percent_below: int = 12
    cooldown_free_percent_minimum: int = 20
    admission_disk_free_bytes_minimum: int = 32 << 30
    abort_disk_free_bytes_below: int = 24 << 30
    max_phase_swap_growth_bytes: int = 1 << 30
    max_episode_swap_growth_bytes: int = 512 << 20
    cooldown_max_swap_growth_bytes: int = 64 << 20
    sample_interval_seconds: int = 30
    cooldown_consecutive_samples: int = 2
    cooldown_timeout_seconds: int = 600
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _HOST_SAFETY_SEAL:
            raise ValueError("v0.7 host-safety pins are builder-sealed")
        if type(self.host_identity) is not V07HostIdentityPins:
            raise ValueError("v0.7 host safety requires exact host identity")
        self.host_identity.__post_init__()
        _require_sha256(self.policy_source_sha256, "host safety policy source")
        _require_sha256(
            self.telemetry_collector_source_sha256,
            "host telemetry collector source",
        )
        exact = {
            "require_ac_power": True,
            "require_low_power_mode_off": True,
            "require_no_thermal_warnings": True,
            "require_no_performance_warnings": True,
            "required_vm_pressure_level": 1,
            "admission_free_percent_minimum": 25,
            "abort_free_percent_below": 12,
            "cooldown_free_percent_minimum": 20,
            "admission_disk_free_bytes_minimum": 32 << 30,
            "abort_disk_free_bytes_below": 24 << 30,
            "max_phase_swap_growth_bytes": 1 << 30,
            "max_episode_swap_growth_bytes": 512 << 20,
            "cooldown_max_swap_growth_bytes": 64 << 20,
            "sample_interval_seconds": 30,
            "cooldown_consecutive_samples": 2,
            "cooldown_timeout_seconds": 600,
        }
        for field, expected in exact.items():
            _require_exact(getattr(self, field), expected, f"host safety {field}")

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__(_HOST_SAFETY_SEAL)
        return {
            "abort_disk_free_bytes_below": self.abort_disk_free_bytes_below,
            "abort_free_percent_below": self.abort_free_percent_below,
            "admission_disk_free_bytes_minimum": (
                self.admission_disk_free_bytes_minimum
            ),
            "admission_free_percent_minimum": self.admission_free_percent_minimum,
            "cooldown_consecutive_samples": self.cooldown_consecutive_samples,
            "cooldown_free_percent_minimum": self.cooldown_free_percent_minimum,
            "cooldown_max_swap_growth_bytes": self.cooldown_max_swap_growth_bytes,
            "cooldown_timeout_seconds": self.cooldown_timeout_seconds,
            "host_identity": self.host_identity.canonical_record(),
            "max_episode_swap_growth_bytes": self.max_episode_swap_growth_bytes,
            "max_phase_swap_growth_bytes": self.max_phase_swap_growth_bytes,
            "policy_source_sha256": self.policy_source_sha256,
            "require_ac_power": self.require_ac_power,
            "require_low_power_mode_off": self.require_low_power_mode_off,
            "require_no_performance_warnings": self.require_no_performance_warnings,
            "require_no_thermal_warnings": self.require_no_thermal_warnings,
            "required_vm_pressure_level": self.required_vm_pressure_level,
            "sample_interval_seconds": self.sample_interval_seconds,
            "telemetry_collector_source_sha256": (
                self.telemetry_collector_source_sha256
            ),
        }


@dataclass(frozen=True, slots=True)
class V07ExecutionPins:
    """Frozen Docker, action, safety, run-ID, intervention, and phase caps."""

    source_inventory_sha256: str
    docker_limits: DockerLimits
    docker_action_limits: DockerActionLimits
    host_safety: V07HostSafetyPins
    run_id_policy_revision: str
    intervention_journal_revision: str
    qualification_replay_actions: int
    calibration_model_prompts: int
    confirmatory_model_prompts: int
    execution_pins_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _EXECUTION_SEAL:
            raise ValueError("v0.7 execution pins are builder-sealed")
        _require_sha256(self.source_inventory_sha256, "execution source inventory")
        if type(self.docker_limits) is not DockerLimits:
            raise ValueError("v0.7 execution requires exact Docker limits")
        self.docker_limits.__post_init__()
        if _docker_limits_record(self.docker_limits) != _docker_limits_record(
            _frozen_docker_limits()
        ):
            raise ValueError("Docker limits differ from frozen v0.7 values")
        if type(self.docker_action_limits) is not DockerActionLimits:
            raise ValueError("v0.7 execution requires exact Docker action limits")
        self.docker_action_limits.__post_init__()
        if _docker_action_limits_record(
            self.docker_action_limits
        ) != _docker_action_limits_record(_frozen_docker_action_limits()):
            raise ValueError("Docker action limits differ from frozen v0.7 values")
        if type(self.host_safety) is not V07HostSafetyPins:
            raise ValueError("v0.7 execution requires exact host-safety pins")
        self.host_safety.__post_init__(_HOST_SAFETY_SEAL)
        _require_exact(
            self.run_id_policy_revision,
            V07_RUN_ID_POLICY_REVISION,
            "run-ID policy revision",
        )
        _require_exact(
            self.intervention_journal_revision,
            V07_INTERVENTION_JOURNAL_REVISION,
            "intervention journal revision",
        )
        for field, expected in (
            ("qualification_replay_actions", 60),
            ("calibration_model_prompts", 26),
            ("confirmatory_model_prompts", 780),
        ):
            _require_exact(getattr(self, field), expected, field)
        _require_sha256(self.execution_pins_sha256, "execution pins")
        if self.execution_pins_sha256 != _digest_record(self._core_record()):
            raise ValueError("execution pins SHA-256 differs from canonical record")

    def _core_record(self) -> dict[str, object]:
        return _execution_core_record(
            source_inventory_sha256=self.source_inventory_sha256,
            docker_limits=self.docker_limits,
            docker_action_limits=self.docker_action_limits,
            host_safety=self.host_safety,
            run_id_policy_revision=self.run_id_policy_revision,
            intervention_journal_revision=self.intervention_journal_revision,
            qualification_replay_actions=self.qualification_replay_actions,
            calibration_model_prompts=self.calibration_model_prompts,
            confirmatory_model_prompts=self.confirmatory_model_prompts,
        )

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__(_EXECUTION_SEAL)
        record = self._core_record()
        record["execution_pins_sha256"] = self.execution_pins_sha256
        return record


def build_v07_execution_pins(
    *,
    source_inventory: VerifiedSourceInventory,
    host_identity: V07HostIdentityPins,
) -> V07ExecutionPins:
    """Derive and seal the exact v0.7 production execution contract."""

    if type(source_inventory) is not VerifiedSourceInventory:
        raise ValueError("execution pins require verified source inventory")
    source_inventory.canonical_record()
    if type(host_identity) is not V07HostIdentityPins:
        raise ValueError("execution pins require exact host identity")
    host_identity.__post_init__()
    policy_source = _derive_role_source_sha256(
        source_inventory, "host_safety_policy", _HOST_SAFETY_SOURCES
    )
    telemetry_source = _derive_role_source_sha256(
        source_inventory, "host_telemetry_collector", _HOST_SAFETY_SOURCES
    )
    host_safety = V07HostSafetyPins(
        host_identity=host_identity,
        policy_source_sha256=policy_source,
        telemetry_collector_source_sha256=telemetry_source,
        _construction_seal=_HOST_SAFETY_SEAL,
    )
    values: dict[str, object] = {
        "source_inventory_sha256": source_inventory.inventory_sha256,
        "docker_limits": _frozen_docker_limits(),
        "docker_action_limits": _frozen_docker_action_limits(),
        "host_safety": host_safety,
        "run_id_policy_revision": V07_RUN_ID_POLICY_REVISION,
        "intervention_journal_revision": V07_INTERVENTION_JOURNAL_REVISION,
        "qualification_replay_actions": 60,
        "calibration_model_prompts": 26,
        "confirmatory_model_prompts": 780,
    }
    execution_sha256 = _digest_record(
        _execution_core_record(**values)  # type: ignore[arg-type]
    )
    return V07ExecutionPins(
        **values,  # type: ignore[arg-type]
        execution_pins_sha256=execution_sha256,
        _construction_seal=_EXECUTION_SEAL,
    )


@dataclass(frozen=True, slots=True)
class V07PrecalibrationManifest:
    """Immutable manifest that can only be constructed through the builder."""

    artifacts: V07ArtifactPins
    models: tuple[V07ModelIdentityPins, ...]
    host_identity: V07HostIdentityPins
    execution: V07ExecutionPins
    budgets: V07BudgetPins
    design: V07DesignPins
    manifest_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _CONSTRUCTION_SEAL:
            raise ValueError("v0.7 pre-calibration manifests are builder-sealed")
        _validate_components(
            self.artifacts,
            self.models,
            self.host_identity,
            self.execution,
            self.budgets,
            self.design,
        )
        _require_sha256(self.manifest_sha256, "manifest")
        core = _core_record(
            self.artifacts,
            self.models,
            self.host_identity,
            self.execution,
            self.budgets,
            self.design,
        )
        if self.manifest_sha256 != _digest_record(core):
            raise ValueError("manifest SHA-256 differs from its canonical record")

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__(_CONSTRUCTION_SEAL)
        record = _core_record(
            self.artifacts,
            self.models,
            self.host_identity,
            self.execution,
            self.budgets,
            self.design,
        )
        record["manifest_sha256"] = self.manifest_sha256
        return record

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_record()) + b"\n"


def build_v07_precalibration_manifest(
    *,
    artifacts: V07ArtifactPins,
    models: Sequence[V07ModelIdentityPins],
    host_identity: V07HostIdentityPins,
    execution: V07ExecutionPins,
    budgets: V07BudgetPins,
    design: V07DesignPins,
) -> V07PrecalibrationManifest:
    """Seal a complete two-model manifest without accepting measured output."""

    if isinstance(models, (str, bytes)) or not isinstance(models, Sequence):
        raise ValueError("models must be the exact two-model set")
    model_tuple = tuple(models)
    ordered = tuple(
        sorted(
            model_tuple,
            key=lambda item: CAMPAIGN_MODELS.index(item.model_id)
            if isinstance(item, V07ModelIdentityPins) and item.model_id in CAMPAIGN_MODELS
            else len(CAMPAIGN_MODELS),
        )
    )
    _validate_components(
        artifacts, ordered, host_identity, execution, budgets, design
    )
    core = _core_record(
        artifacts, ordered, host_identity, execution, budgets, design
    )
    return V07PrecalibrationManifest(
        artifacts=artifacts,
        models=ordered,
        host_identity=host_identity,
        execution=execution,
        budgets=budgets,
        design=design,
        manifest_sha256=_digest_record(core),
        _construction_seal=_CONSTRUCTION_SEAL,
    )


def _validate_components(
    artifacts: object,
    models: object,
    host_identity: object,
    execution: object,
    budgets: object,
    design: object,
) -> None:
    if type(artifacts) is not V07ArtifactPins:
        raise ValueError("artifacts must use the exact v0.7 pin type")
    artifacts.__post_init__(_ARTIFACT_SEAL)
    if type(models) is not tuple or len(models) != 2:
        raise ValueError("models must be the exact two-model set")
    if any(type(item) is not V07ModelIdentityPins for item in models):
        raise ValueError("models must use exact v0.7 identity pin values")
    if tuple(item.model_id for item in models) != CAMPAIGN_MODELS:
        raise ValueError("models must be the exact two-model set")
    for item in models:
        item.__post_init__(_MODEL_IDENTITY_SEAL)
    if type(host_identity) is not V07HostIdentityPins:
        raise ValueError("host_identity must use the exact v0.7 pin type")
    host_identity.__post_init__()
    if type(execution) is not V07ExecutionPins:
        raise ValueError("execution must use the exact v0.7 pin type")
    execution.__post_init__(_EXECUTION_SEAL)
    if execution.source_inventory_sha256 != artifacts.source_inventory_sha256:
        raise ValueError("execution and code source inventories differ")
    if execution.host_safety.host_identity != host_identity:
        raise ValueError("execution and manifest host identities differ")
    if type(budgets) is not V07BudgetPins:
        raise ValueError("budgets must use the exact v0.7 pin type")
    budgets.__post_init__()
    if type(design) is not V07DesignPins:
        raise ValueError("design must use the exact v0.7 pin type")
    design.__post_init__()
    for item in models:
        if item.generation.runtime_binary_sha256 != host_identity.ollama_runtime_binary_sha256:
            raise ValueError("model runtime binary differs from the host identity")
        if item.generation.runtime_version != host_identity.ollama_server_version:
            raise ValueError("model runtime version differs from the host identity")
        if item.generation.context_tokens != budgets.per_call_context_tokens:
            raise ValueError("generation context differs from the episode budget")
    candidate = _core_record_unchecked(
        artifacts,
        models,
        host_identity,
        execution,
        budgets,
        design,
    )
    _reject_private_or_outcome_material(candidate)


def _core_record(
    artifacts: V07ArtifactPins,
    models: tuple[V07ModelIdentityPins, ...],
    host_identity: V07HostIdentityPins,
    execution: V07ExecutionPins,
    budgets: V07BudgetPins,
    design: V07DesignPins,
) -> dict[str, object]:
    record = _core_record_unchecked(
        artifacts,
        models,
        host_identity,
        execution,
        budgets,
        design,
    )
    _reject_private_or_outcome_material(record)
    return record


def _core_record_unchecked(
    artifacts: V07ArtifactPins,
    models: tuple[V07ModelIdentityPins, ...],
    host_identity: V07HostIdentityPins,
    execution: V07ExecutionPins,
    budgets: V07BudgetPins,
    design: V07DesignPins,
) -> dict[str, object]:
    calibration = canonical_v07_calibration_design_record()
    calibration["calibration_design_sha256"] = artifacts.calibration_design_sha256
    return {
        "analysis": {
            "bootstrap_replicates": design.bootstrap_replicates,
            "bootstrap_seed": design.bootstrap_seed,
            "cluster_unit": "task",
            "stratified": True,
        },
        "calibration": calibration,
        "code": {
            "controller_revision": INTERACTIVE_CONTROLLER_REVISION,
            "controller_source_sha256": artifacts.controller_source_sha256,
            "progress_evaluator_revision": CAMPAIGN_PROGRESS_REVISION,
            "progress_evaluator_source_sha256": (
                artifacts.progress_evaluator_source_sha256
            ),
            "prompt_policy_revision": V07_PROMPT_POLICY_REVISION,
            "prompt_policy_sha256": artifacts.prompt_policy_sha256,
            "source_code_sha256": artifacts.source_code_sha256,
            "source_head_commit": artifacts.source_head_commit,
            "source_head_tree": artifacts.source_head_tree,
            "source_inventory_sha256": artifacts.source_inventory_sha256,
            "strict_evaluator_revision": CAMPAIGN_STRICT_EVALUATOR_REVISION,
            "strict_evaluator_sha256": artifacts.strict_evaluator_sha256,
        },
        "design": {
            "arms": list(design.arms),
            "attempt_cap": design.attempt_cap,
            "model_order": list(design.models),
            "seed": design.seed,
        },
        "episode_budgets": budgets.canonical_record(),
        "execution": execution.canonical_record(),
        "hard_prompt_caps": {
            "calibration": design.calibration_prompt_cap,
            "confirmatory": design.confirmatory_prompt_cap,
        },
        "host_identity": host_identity.canonical_record(),
        "models": [item.canonical_record() for item in models],
        "phase": "pre_calibration",
        "qualification": {
            **artifacts.qualification.canonical_record(),
            "container_platform": "linux/arm64",
            "network_mode": "none",
            "reference_replays_per_task": 2,
            "required_task_count": 30,
        },
        "sample": {
            "sample_manifest_sha256": artifacts.sample_manifest_sha256,
            "schedule_sha256": artifacts.schedule_sha256,
            "task_count": len(artifacts.task_ids),
            "task_ids": list(artifacts.task_ids),
        },
        "schema": V07_MANIFEST_SCHEMA_REVISION,
        "source": {
            "corpus_sha256": artifacts.source_corpus_sha256,
            "revision": artifacts.source_revision,
            "static_exclusion_audit_sha256": (
                artifacts.static_exclusion_audit_sha256
            ),
        },
    }


def _derive_role_source_sha256(
    inventory: VerifiedSourceInventory,
    role: str,
    tracked_files: tuple[str, ...],
) -> str:
    subset = derive_source_subset_sha256(inventory, tracked_files)
    return _digest_record(
        {
            "role": role,
            "source_inventory_sha256": inventory.inventory_sha256,
            "source_subset_sha256": subset,
        }
    )


def _frozen_docker_limits() -> DockerLimits:
    return DockerLimits(
        memory_bytes=512 << 20,
        memory_swap_bytes=512 << 20,
        writable_layer_watchdog_bytes=256 << 20,
        nano_cpus=1_000_000_000,
        pids_limit=64,
        nofile_soft=1024,
        nofile_hard=1024,
        nproc_soft=64,
        nproc_hard=64,
        fsize_soft=16 << 20,
        fsize_hard=16 << 20,
        storage_enforcement_mode=WRITABLE_LAYER_STORAGE_MODE,
    )


def _frozen_docker_action_limits() -> DockerActionLimits:
    return DockerActionLimits(
        deadline_seconds=10.0,
        private_stream_limit_bytes=4096,
        observation_limit_bytes=2048,
        read_chunk_bytes=4096,
        io_queue_chunks=8,
        writable_layer_sample_interval_seconds=0.25,
        writable_layer_probe_timeout_seconds=1.0,
    )


def _docker_limits_record(limits: DockerLimits) -> dict[str, int | str]:
    return {
        "fsize_hard": limits.fsize_hard,
        "fsize_soft": limits.fsize_soft,
        "memory_bytes": limits.memory_bytes,
        "memory_swap_bytes": limits.memory_swap_bytes,
        "nano_cpus": limits.nano_cpus,
        "nofile_hard": limits.nofile_hard,
        "nofile_soft": limits.nofile_soft,
        "nproc_hard": limits.nproc_hard,
        "nproc_soft": limits.nproc_soft,
        "pids_limit": limits.pids_limit,
        "storage_enforcement_mode": limits.storage_enforcement_mode,
        "writable_layer_watchdog_bytes": limits.writable_layer_watchdog_bytes,
    }


def _docker_action_limits_record(
    limits: DockerActionLimits,
) -> dict[str, float | int]:
    return {
        "deadline_seconds": limits.deadline_seconds,
        "io_queue_chunks": limits.io_queue_chunks,
        "observation_limit_bytes": limits.observation_limit_bytes,
        "private_stream_limit_bytes": limits.private_stream_limit_bytes,
        "read_chunk_bytes": limits.read_chunk_bytes,
        "writable_layer_probe_timeout_seconds": (
            limits.writable_layer_probe_timeout_seconds
        ),
        "writable_layer_sample_interval_seconds": (
            limits.writable_layer_sample_interval_seconds
        ),
    }


def _execution_core_record(
    *,
    source_inventory_sha256: str,
    docker_limits: DockerLimits,
    docker_action_limits: DockerActionLimits,
    host_safety: V07HostSafetyPins,
    run_id_policy_revision: str,
    intervention_journal_revision: str,
    qualification_replay_actions: int,
    calibration_model_prompts: int,
    confirmatory_model_prompts: int,
) -> dict[str, object]:
    return {
        "docker_action_limits": _docker_action_limits_record(docker_action_limits),
        "docker_limits": _docker_limits_record(docker_limits),
        "host_safety": host_safety.canonical_record(),
        "intervention_journal_revision": intervention_journal_revision,
        "phase_caps": {
            "calibration_model_prompts": calibration_model_prompts,
            "confirmatory_model_prompts": confirmatory_model_prompts,
            "qualification_replay_actions": qualification_replay_actions,
        },
        "run_id_policy": {
            "canonical_inputs": ["campaign_id", "episode_index", "role"],
            "format": "v07-<first20-lowercase-sha256>",
            "revision": run_id_policy_revision,
        },
        "source_inventory_sha256": source_inventory_sha256,
    }


def _reject_private_or_outcome_material(value: object, *, field: str = "manifest") -> None:
    forbidden_keys = {
        "calibration_outcome",
        "expected_output",
        "gold",
        "gold_command",
        "outcome",
        "strict_success",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{field} contains a non-text JSON key")
            lowered = key.lower()
            if lowered in forbidden_keys or lowered == "path" or lowered.endswith("_path"):
                raise ValueError(f"{field} contains forbidden evaluator or outcome material")
            _reject_private_or_outcome_material(item, field=f"{field}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_private_or_outcome_material(item, field=f"{field}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.lower()
        if (
            value.startswith("/")
            or "file://" in lowered
            or _WINDOWS_ABSOLUTE_PATH.match(value) is not None
            or _EMBEDDED_LOCAL_PATH.search(value) is not None
        ):
            raise ValueError(f"{field} contains a local filesystem path")


if tuple(V07_TASK_IDS) != CAMPAIGN_TASK_IDS:
    raise RuntimeError("v0.7 protocol and campaign task samples differ")
if CampaignSpec(CAMPAIGN_TASK_IDS).schedule_sha256 != V07_SCHEDULE_SHA256:
    raise RuntimeError("v0.7 campaign schedule differs from the preregistered digest")


__all__ = (
    "V07_CALIBRATION_DESIGN_SHA256",
    "V07_INTERVENTION_JOURNAL_REVISION",
    "V07_RUN_ID_POLICY_REVISION",
    "V07_SCHEDULE_SHA256",
    "V07ArtifactPins",
    "V07BudgetPins",
    "V07DesignPins",
    "V07ExecutionPins",
    "V07HostIdentityPins",
    "V07HostSafetyPins",
    "V07ModelIdentityPins",
    "V07PrecalibrationManifest",
    "V07QualificationPins",
    "V07TokenizerPins",
    "bind_v07_model_identity",
    "bind_v07_qualification_manifest",
    "build_v07_artifact_pins",
    "build_v07_execution_pins",
    "build_v07_precalibration_manifest",
)
