"""Builder-sealed pre-calibration identities for the v0.6 InterCode study.

This manifest intentionally contains no calibration outcome.  It binds every
input that could otherwise become a hidden tuning knob before any model output
is admitted: the model bytes and renderer, complete Ollama request contract,
tokenizer, separate weight/KV quantization, qualified suite/sample/schedule,
controller policies, budgets, calibration thresholds, and host safety caps.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from types import MappingProxyType

from .intercode_source import (
    INTERCODE_REVISION,
    PUBLIC_POPULATION_SHA256,
    SOURCE_CORPUS_SHA256,
    STATIC_EXCLUSION_AUDIT_SHA256,
)
from .model_adapter import (
    OllamaGenerationConfig,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
)


GATE_SCHEMA_VERSION = 1
CONFIRMATORY_SEEDS = (11, 29)
SEED_PROBE_SEEDS = (11, 29, 47, 83)
ATTEMPT_CURVES = (1, 2, 4, 6)
INTERACTIVE_ARMS = (
    "direct",
    "independent_verified_sampling",
    "raw_feedback_loop",
    "engineered_loop",
)
_STRATA = ("fs1", "fs2", "fs3", "fs4")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION = re.compile(r"^[a-z0-9][a-z0-9._/-]*-v[1-9][0-9]*$")
_LLAMA_CPP_COMMIT = "8c146a8366304c871efc26057cc90370ccf58dad"
_CONSTRUCTION_SEAL = object()

__all__ = (
    "ConfirmatoryBudgetPins",
    "HostSafetyPins",
    "InterCodeArtifactPins",
    "PrecalibrationGateManifest",
    "QuantizationPins",
    "TokenizerPins",
    "build_precalibration_gate_manifest",
)


def _require_sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 reference")
    return value


def _require_positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class QuantizationPins:
    """Weight and KV-cache quantization remain distinct experimental fields."""

    weight_quantization: str
    kv_cache_quantization: str

    def __post_init__(self) -> None:
        for field in ("weight_quantization", "kv_cache_quantization"):
            value = getattr(self, field)
            if (
                type(value) is not str
                or not value
                or value.strip() != value
                or any(character.isspace() for character in value)
            ):
                raise ValueError(f"{field} must be a separately recorded pin")


@dataclass(frozen=True, slots=True)
class TokenizerPins:
    """Portable tokenizer identities; local filesystem paths are not published."""

    helper_sha256: str
    model_artifact_sha256: str
    llama_cpp_commit: str
    policy_revision: str

    def __post_init__(self) -> None:
        _require_sha256(self.helper_sha256, "tokenizer helper")
        _require_sha256(self.model_artifact_sha256, "tokenizer model artifact")
        if self.llama_cpp_commit != _LLAMA_CPP_COMMIT:
            raise ValueError("llama.cpp commit differs from the frozen tokenizer source")
        if type(self.policy_revision) is not str or _REVISION.fullmatch(
            self.policy_revision
        ) is None:
            raise ValueError("tokenizer policy revision must be immutable")


@dataclass(frozen=True, slots=True)
class InterCodeArtifactPins:
    """Gold-free, publishable identities of the qualified evaluation surface."""

    qualification_suite_sha256: str
    confirmatory_sample_sha256: str
    block_schedule_sha256: str
    qualification_evidence_root_sha256: str
    image_sha256_by_stratum: Mapping[str, str]
    evaluator_sha256: str
    state_normalization_sha256: str
    prompt_policy_sha256: str
    controller_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "qualification_suite_sha256",
            "confirmatory_sample_sha256",
            "block_schedule_sha256",
            "qualification_evidence_root_sha256",
            "evaluator_sha256",
            "state_normalization_sha256",
            "prompt_policy_sha256",
            "controller_sha256",
        ):
            _require_sha256(getattr(self, field), field)
        if not isinstance(self.image_sha256_by_stratum, Mapping):
            raise ValueError("image pins must be a four-stratum mapping")
        images = dict(self.image_sha256_by_stratum)
        if tuple(sorted(images)) != _STRATA:
            raise ValueError("image pins must contain exactly fs1, fs2, fs3, and fs4")
        for stratum in _STRATA:
            _require_sha256(images[stratum], f"{stratum} image")
        object.__setattr__(
            self,
            "image_sha256_by_stratum",
            MappingProxyType({stratum: images[stratum] for stratum in _STRATA}),
        )


@dataclass(frozen=True, slots=True)
class ConfirmatoryBudgetPins:
    """One shared arm ceiling plus the predeclared diagnostic evaluator ceiling."""

    logical_prompt_tokens: int
    completion_tokens: int
    per_call_context_tokens: int
    max_output_tokens: int
    model_calls: int = 6
    environment_actions: int = 6
    evaluator_calls: int = 7
    diagnostic_evaluator_calls: int = 13
    checkpoint_creates: int = 6
    checkpoint_restores: int = 6
    safety_recoveries: int = 6

    def __post_init__(self) -> None:
        for field in (
            "logical_prompt_tokens",
            "completion_tokens",
            "per_call_context_tokens",
            "max_output_tokens",
            "model_calls",
            "environment_actions",
            "evaluator_calls",
            "diagnostic_evaluator_calls",
            "checkpoint_creates",
            "checkpoint_restores",
            "safety_recoveries",
        ):
            _require_positive_int(getattr(self, field), field)
        exact = {
            "model_calls": 6,
            "environment_actions": 6,
            "evaluator_calls": 7,
            "diagnostic_evaluator_calls": 13,
            "checkpoint_creates": 6,
            "checkpoint_restores": 6,
            "safety_recoveries": 6,
        }
        for field, expected in exact.items():
            if getattr(self, field) != expected:
                raise ValueError(f"{field} must equal the frozen value {expected}")
        if self.max_output_tokens >= self.per_call_context_tokens:
            raise ValueError("max_output_tokens must leave positive prompt context")


@dataclass(frozen=True, slots=True)
class HostSafetyPins:
    """Study-wide request/requeue ceilings and immutable telemetry policies."""

    policy_sha256: str
    telemetry_collector_sha256: str
    require_ac_power: bool = True
    require_low_power_mode_off: bool = True
    require_no_thermal_warnings: bool = True
    required_vm_pressure_level: int = 1
    admission_free_percent_minimum: int = 25
    abort_free_percent_below: int = 12
    cooldown_free_percent_minimum: int = 20
    admission_disk_free_bytes_minimum: int = 32 << 30
    abort_disk_free_bytes_below: int = 24 << 30
    max_phase_swap_growth_bytes: int = 1 << 30
    max_block_swap_growth_bytes: int = 512 << 20
    cooldown_max_swap_growth_bytes: int = 64 << 20
    sample_interval_seconds: int = 30
    cooldown_consecutive_samples: int = 2
    cooldown_timeout_seconds: int = 600
    max_global_block_requeues: int = 12
    max_requeues_per_block: int = 1
    hard_request_cap_two_models: int = 4_460
    qualification_gold_replays: int = 400
    calibration_and_pilot_max_requests: int = 304
    host_load_max_requests: int = 128
    confirmatory_max_requests: int = 3_800

    def __post_init__(self) -> None:
        _require_sha256(self.policy_sha256, "host safety policy")
        _require_sha256(
            self.telemetry_collector_sha256, "telemetry collector"
        )
        exact = {
            "require_ac_power": True,
            "require_low_power_mode_off": True,
            "require_no_thermal_warnings": True,
            "required_vm_pressure_level": 1,
            "admission_free_percent_minimum": 25,
            "abort_free_percent_below": 12,
            "cooldown_free_percent_minimum": 20,
            "admission_disk_free_bytes_minimum": 32 << 30,
            "abort_disk_free_bytes_below": 24 << 30,
            "max_phase_swap_growth_bytes": 1 << 30,
            "max_block_swap_growth_bytes": 512 << 20,
            "cooldown_max_swap_growth_bytes": 64 << 20,
            "sample_interval_seconds": 30,
            "cooldown_consecutive_samples": 2,
            "cooldown_timeout_seconds": 600,
            "max_global_block_requeues": 12,
            "max_requeues_per_block": 1,
            "hard_request_cap_two_models": 4_460,
            "qualification_gold_replays": 400,
            "calibration_and_pilot_max_requests": 304,
            "host_load_max_requests": 128,
            "confirmatory_max_requests": 3_800,
        }
        for field, expected in exact.items():
            if type(getattr(self, field)) is not type(expected) or getattr(self, field) != expected:
                raise ValueError(f"{field} must equal the frozen value {expected}")


@dataclass(frozen=True, slots=True)
class PrecalibrationGateManifest:
    """A model-specific, outcome-free gate that cannot be directly constructed."""

    generation: OllamaGenerationConfig
    tokenizer: TokenizerPins
    quantization: QuantizationPins
    artifacts: InterCodeArtifactPins
    budgets: ConfirmatoryBudgetPins
    host_safety: HostSafetyPins
    gate_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _CONSTRUCTION_SEAL:
            raise ValueError("pre-calibration gate manifests are builder-sealed")
        _validate_components(
            self.generation,
            self.tokenizer,
            self.quantization,
            self.artifacts,
            self.budgets,
            self.host_safety,
        )
        _require_sha256(self.gate_sha256, "gate_sha256")
        if self.gate_sha256 != _digest(_core_record(self)):
            raise ValueError("gate_sha256 differs from the canonical gate")

    def canonical_record(self) -> dict[str, object]:
        """Return a fresh JSON-compatible projection and recheck its identity."""

        self.__post_init__(_CONSTRUCTION_SEAL)
        record = _core_record(self)
        record["gate_sha256"] = self.gate_sha256
        return record

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_record()) + b"\n"


def build_precalibration_gate_manifest(
    *,
    generation: OllamaGenerationConfig,
    tokenizer: TokenizerPins,
    quantization: QuantizationPins,
    artifacts: InterCodeArtifactPins,
    budgets: ConfirmatoryBudgetPins,
    host_safety: HostSafetyPins,
) -> PrecalibrationGateManifest:
    """Freeze a model gate without accepting calibration-derived fields."""

    _validate_components(
        generation,
        tokenizer,
        quantization,
        artifacts,
        budgets,
        host_safety,
    )
    gate_sha256 = _digest(
        _core_record_from_components(
            generation,
            tokenizer,
            quantization,
            artifacts,
            budgets,
            host_safety,
        )
    )
    return PrecalibrationGateManifest(
        generation=generation,
        tokenizer=tokenizer,
        quantization=quantization,
        artifacts=artifacts,
        budgets=budgets,
        host_safety=host_safety,
        gate_sha256=gate_sha256,
        _construction_seal=_CONSTRUCTION_SEAL,
    )


def _validate_components(
    generation: object,
    tokenizer: object,
    quantization: object,
    artifacts: object,
    budgets: object,
    host_safety: object,
) -> None:
    if type(generation) is not OllamaGenerationConfig:
        raise ValueError("generation must be the exact frozen Ollama config type")
    generation.__post_init__()
    if generation.profile not in (QWEN35_RAW_PROFILE, PHI4_MINI_RAW_PROFILE):
        raise ValueError("generation profile is not admitted for this small-model study")
    for value, expected_type, field in (
        (tokenizer, TokenizerPins, "tokenizer"),
        (quantization, QuantizationPins, "quantization"),
        (artifacts, InterCodeArtifactPins, "artifacts"),
        (budgets, ConfirmatoryBudgetPins, "budgets"),
        (host_safety, HostSafetyPins, "host_safety"),
    ):
        if type(value) is not expected_type:
            raise ValueError(f"{field} must use its exact typed pin surface")
        value.__post_init__()
    assert isinstance(tokenizer, TokenizerPins)
    assert isinstance(budgets, ConfirmatoryBudgetPins)
    if tokenizer.model_artifact_sha256 != generation.profile.model_artifact_sha256:
        raise ValueError("tokenizer and generation model artifacts differ")
    if budgets.per_call_context_tokens != generation.context_tokens:
        raise ValueError("budget context and generation context must be identical")


def _core_record(manifest: PrecalibrationGateManifest) -> dict[str, object]:
    return _core_record_from_components(
        manifest.generation,
        manifest.tokenizer,
        manifest.quantization,
        manifest.artifacts,
        manifest.budgets,
        manifest.host_safety,
    )


def _core_record_from_components(
    generation: OllamaGenerationConfig,
    tokenizer: TokenizerPins,
    quantization: QuantizationPins,
    artifacts: InterCodeArtifactPins,
    budgets: ConfirmatoryBudgetPins,
    host_safety: HostSafetyPins,
) -> dict[str, object]:
    profile = generation.profile
    return {
        "artifacts": {
            "block_schedule_sha256": artifacts.block_schedule_sha256,
            "confirmatory_sample_sha256": (
                artifacts.confirmatory_sample_sha256
            ),
            "controller_sha256": artifacts.controller_sha256,
            "evaluator_sha256": artifacts.evaluator_sha256,
            "image_sha256_by_stratum": dict(
                artifacts.image_sha256_by_stratum
            ),
            "prompt_policy_sha256": artifacts.prompt_policy_sha256,
            "qualification_evidence_root_sha256": (
                artifacts.qualification_evidence_root_sha256
            ),
            "qualification_suite_sha256": (
                artifacts.qualification_suite_sha256
            ),
            "state_normalization_sha256": (
                artifacts.state_normalization_sha256
            ),
        },
        "budgets": {
            field: getattr(budgets, field)
            for field in (
                "logical_prompt_tokens",
                "completion_tokens",
                "per_call_context_tokens",
                "max_output_tokens",
                "model_calls",
                "environment_actions",
                "evaluator_calls",
                "diagnostic_evaluator_calls",
                "checkpoint_creates",
                "checkpoint_restores",
                "safety_recoveries",
            )
        },
        "calibration_gates": {
            "action_admissibility_minimum": 0.80,
            "direct_executed_requests": 72,
            "direct_strict_success_maximum": 57,
            "direct_strict_success_minimum": 8,
            "host_load_duration_seconds": 1_800,
            "host_load_requests_per_model": 64,
            "parsed_seed_probe_minimum": 87,
            "pilot_max_requests_per_model": 56,
            "seed_diverse_task_minimum": 12,
            "seed_probe_requests": 96,
            "seed_probe_unique_action_sum_minimum": 48,
        },
        "design": {
            "arms": list(INTERACTIVE_ARMS),
            "attempt_curves": list(ATTEMPT_CURVES),
            "bootstrap_replicates": 10_000,
            "bootstrap_seed": 20_260_715,
            "confirmatory_seeds": list(CONFIRMATORY_SEEDS),
            "max_attempts": 6,
            "seed_probe_seeds": list(SEED_PROBE_SEEDS),
        },
        "host_safety": {
            field: getattr(host_safety, field)
            for field in (
                "policy_sha256",
                "telemetry_collector_sha256",
                "require_ac_power",
                "require_low_power_mode_off",
                "require_no_thermal_warnings",
                "required_vm_pressure_level",
                "admission_free_percent_minimum",
                "abort_free_percent_below",
                "cooldown_free_percent_minimum",
                "admission_disk_free_bytes_minimum",
                "abort_disk_free_bytes_below",
                "max_phase_swap_growth_bytes",
                "max_block_swap_growth_bytes",
                "cooldown_max_swap_growth_bytes",
                "sample_interval_seconds",
                "cooldown_consecutive_samples",
                "cooldown_timeout_seconds",
                "max_global_block_requeues",
                "max_requeues_per_block",
                "hard_request_cap_two_models",
                "qualification_gold_replays",
                "calibration_and_pilot_max_requests",
                "host_load_max_requests",
                "confirmatory_max_requests",
            )
        },
        "model": {
            "model": profile.model,
            "model_artifact_sha256": profile.model_artifact_sha256,
            "model_manifest_sha256": profile.model_manifest_sha256,
            "renderer_profile_id": profile.profile_id,
            "renderer_profile_sha256": profile.sha256,
            "renderer_source_sha256": profile.ollama_source_sha256,
        },
        "phase": "pre_calibration_gate",
        "quantization": {
            "kv_cache_quantization": quantization.kv_cache_quantization,
            "weight_quantization": quantization.weight_quantization,
        },
        "runtime": {
            "generation_config": generation.canonical_definition,
            "generation_config_sha256": generation.sha256,
        },
        "schema": "edgeloopbench.intercode-precalibration-gate",
        "schema_version": GATE_SCHEMA_VERSION,
        "source": {
            "corpus_sha256": SOURCE_CORPUS_SHA256,
            "public_population_sha256": PUBLIC_POPULATION_SHA256,
            "revision": INTERCODE_REVISION,
            "static_exclusion_audit_sha256": STATIC_EXCLUSION_AUDIT_SHA256,
        },
        "tokenizer": {
            "helper_sha256": tokenizer.helper_sha256,
            "llama_cpp_commit": tokenizer.llama_cpp_commit,
            "model_artifact_sha256": tokenizer.model_artifact_sha256,
            "policy_revision": tokenizer.policy_revision,
        },
    }
