"""Sealed calibration evidence and mechanics gates for the v0.7 study."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from .interactive_controller import (
    INTERACTIVE_CONTROLLER_REVISION,
    InteractiveResult,
    candidate_seed,
)
from .intercode_campaign_ledger import CAMPAIGN_ARMS, CAMPAIGN_MODELS, CAMPAIGN_SEED
from .intercode_host_safety import (
    HostSafetySample,
    ResidentModel,
    parse_host_safety_sample,
)
from .intercode_replay_environment import V07_STRICT_REPLAY_EVALUATOR_SHA256
from .intercode_source import CALIBRATION_POPULATION_SHA256, InterCodeSource
from .journal import JournalError, inspect_journal
from .model_adapter import PHI4_MINI_RAW_PROFILE, QWEN35_RAW_PROFILE


V07_CALIBRATION_TASK_IDS = tuple(
    f"bash-calibration-{index:03d}" for index in range(4)
)
V07_CALIBRATION_ARMS = CAMPAIGN_ARMS
V07_CALIBRATION_REQUEST_CAPS = (1, 4, 4, 4)
V07_CALIBRATION_MAX_PROMPTS_PER_MODEL = sum(V07_CALIBRATION_REQUEST_CAPS)
V07_CALIBRATION_MAX_PROMPTS_TWO_MODELS = (
    len(CAMPAIGN_MODELS) * V07_CALIBRATION_MAX_PROMPTS_PER_MODEL
)
V07_CALIBRATION_EPISODE_COUNT = len(CAMPAIGN_MODELS) * len(V07_CALIBRATION_TASK_IDS)
V07_CALIBRATION_JOURNAL_SCHEMA = (
    "edgeloopbench.intercode-v0.7-calibration-journal.v3"
)
V07_CONFIRMATORY_TASK_MULTIPLIER = 30
V07_PLANNING_MULTIPLIER_NUMERATOR = 3
V07_PLANNING_MULTIPLIER_DENOMINATOR = 2
V07_ACTIVE_TIME_LIMIT_NS = 18 * 60 * 60 * 1_000_000_000

_DESIGN_SCHEMA = "edgeloopbench.intercode-v0.7-calibration-design.v1"
_JOURNAL_SCHEMA = V07_CALIBRATION_JOURNAL_SCHEMA
_EVIDENCE_SCHEMA = "edgeloopbench.intercode-v0.7-calibration-evidence.v3"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]")
_DESIGN_AUTHORITY = object()
_EVIDENCE_AUTHORITY = object()
_DISPOSITION_AUTHORITY = object()
_MAX_CONTROLLER_LOG_BYTES = 4 * 1024 * 1024
_MAX_CONTROLLER_RECORDS = 128
_MAX_CALIBRATION_LOG_BYTES = 2 * 1024 * 1024
_MAX_LOGICAL_PROMPT_TOKENS = 16_380
_MAX_LOGICAL_COMPLETION_TOKENS = 2_048
_MAX_PER_CALL_CONTEXT_TOKENS = 4_096
_MAX_OUTPUT_TOKENS = 512
_MAX_EVALUATOR_CALLS = 5
_ADMISSION_FREE_PERCENT_MINIMUM = 25
_RUNNING_FREE_PERCENT_MINIMUM = 12
_ADMISSION_DISK_FREE_BYTES_MINIMUM = 32 << 30
_RUNNING_DISK_FREE_BYTES_MINIMUM = 24 << 30
_MAX_EPISODE_SWAP_GROWTH_BYTES = 512 << 20
_CHAIN_FIELDS = frozenset(
    {"sequence", "previous_event_sha256", "event_sha256"}
)
_RESULT_FIELDS = tuple(field.name for field in fields(InteractiveResult))
_RESULT_BOOLEAN_FIELDS = ("official_success", "strict_success")
_RESULT_INTEGER_FIELDS = _RESULT_FIELDS[4:]
_RUN_STATUSES = frozenset(
    {"completed", "budget_exhausted", "infrastructure_error"}
)
_STOP_REASONS = frozenset(
    {
        "action_pipeline_budget_exhausted",
        "attempt_budget_exhausted",
        "checkpoint_restore_budget_exhausted",
        "direct_action_policy_failure",
        "direct_complete",
        "direct_parser_failure",
        "generation_telemetry_budget_violation",
        "logical_completion_token_budget_exhausted",
        "logical_prompt_token_budget_exhausted",
        "no_progress_guard",
        "per_call_context_token_budget_exhausted",
        "prompt_token_telemetry_mismatch",
        "rendered_prompt_byte_budget_exhausted",
    }
)
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "action",
        "command",
        "evaluator_path",
        "expected",
        "expected_output",
        "gold",
        "gold_command",
        "host_path",
        "model_text",
        "observation",
        "output",
        "query",
        "raw_model_text",
        "raw_prompt",
        "raw_text",
        "response",
        "stderr",
        "stdout",
        "text",
    }
)
_COMMON_CONTROLLER_FIELDS = frozenset(
    {
        "type",
        "task_id",
        "strategy",
        "replicate_seed",
        "execution_authority_sha256",
    }
)
_STATIC_CONTROLLER_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "controller_started": frozenset({"controller_revision"}),
    "model_preflighted": frozenset(
        {
            "attempt",
            "prompt_sha256",
            "prompt_tokens",
            "token_ids_sha256",
            "renderer_profile_sha256",
            "tokenizer_artifact_sha256",
            "model_artifact_sha256",
        }
    ),
    "model_requested": frozenset(
        {
            "attempt",
            "prompt_sha256",
            "logical_model_calls_after",
            "logical_prompt_tokens_after",
            "candidate_seed",
            "context_sha256",
            "max_output_tokens",
        }
    ),
    "model_completed": frozenset(
        {
            "attempt",
            "response_sha256",
            "prompt_tokens",
            "completion_tokens",
            "total_duration_ns",
        }
    ),
    "action_rejected": frozenset({"attempt", "reason"}),
    "environment_create_requested": frozenset({"attempt", "scope"}),
    "environment_created": frozenset({"attempt", "scope"}),
    "action_requested": frozenset({"attempt", "action_sha256"}),
    "action_completed": frozenset(
        {
            "attempt",
            "action_sha256",
            "output_sha256",
            "state_sha256",
            "exit_code",
            "admissible",
            "state_changed",
            "policy_failure",
            "safety_recovery_performed",
        }
    ),
    "safety_recovery_completed": frozenset(
        {"attempt", "state_sha256", "recovery_evidence_sha256"}
    ),
    "attempt_defaulted": frozenset(
        {
            "attempt",
            "reward",
            "official_success",
            "evaluation_kind",
            "policy_failure",
        }
    ),
    "checkpoint_create_requested": frozenset({"attempt"}),
    "checkpoint_created": frozenset({"attempt", "state_sha256"}),
    "attempt_evaluation_requested": frozenset({"attempt", "state_sha256"}),
    "attempt_evaluated": frozenset(
        {"attempt", "reward", "official_success", "evaluation_kind"}
    ),
    "checkpoint_restore_requested": frozenset({"attempt", "state_sha256"}),
    "checkpoint_restored": frozenset({"attempt", "state_sha256"}),
    "environment_close_requested": frozenset({"scope"}),
    "environment_closed": frozenset({"scope"}),
    "strict_evaluation_planned": frozenset(
        {"selected_attempt", "state_sha256"}
    ),
    "strict_evaluation_defaulted": frozenset(
        {"selected_attempt", "reason", "strict_success"}
    ),
    "terminal_finalization_requested": frozenset(
        {
            "selected_attempt",
            "evaluation_kind",
            "aborted",
            "remaining_evaluator_calls",
        }
    ),
    "terminal_finalized": frozenset(
        {"strict_evaluator_calls", "posthoc_evaluator_calls"}
    ),
    "strict_evaluation_completed": frozenset(
        {"strict_success", "evaluator_sha256"}
    ),
    "controller_stopped": frozenset(
        {"stop_reason", "selected_attempt", "official_success"}
    ),
}
_MODEL_REJECTION_FIELDS: dict[str, frozenset[str]] = {
    "rendered_prompt_byte_budget": frozenset(
        {
            "attempt",
            "reason",
            "prompt_sha256",
            "renderer_profile_sha256",
            "observed_prompt_bytes",
            "prompt_byte_limit",
        }
    ),
    "prompt_budget": frozenset(
        {
            "attempt",
            "reason",
            "prompt_sha256",
            "prompt_tokens",
            "remaining_prompt_tokens",
        }
    ),
    "per_call_context_budget": frozenset(
        {
            "attempt",
            "reason",
            "prompt_sha256",
            "prompt_tokens",
            "remaining_context_tokens",
        }
    ),
}
_INFRASTRUCTURE_INVALID_FIELDS: dict[str, frozenset[str]] = {
    "prompt_token_telemetry_mismatch": frozenset(
        {
            "attempt",
            "reason",
            "preflight_prompt_tokens",
            "telemetry_prompt_tokens",
        }
    ),
    "generation_telemetry_budget_violation": frozenset(
        {
            "attempt",
            "reason",
            "allowed_completion_tokens",
            "telemetry_completion_tokens",
            "allowed_context_tokens",
            "telemetry_context_tokens",
        }
    ),
}


class V07CalibrationEvidenceError(ValueError):
    """The calibration artifacts are not safe to use as admission evidence."""


@dataclass(frozen=True, slots=True)
class V07CalibrationEpisode:
    task_id: str
    arm: str
    request_cap: int
    seed: int


@dataclass(frozen=True, slots=True, init=False)
class V07CalibrationDesign:
    task_ids: tuple[str, ...]
    arms: tuple[str, ...]
    request_caps: tuple[int, ...]
    seed: int
    max_prompts_per_model: int
    max_prompts_two_models: int
    calibration_population_sha256: str
    schedule_sha256: str
    design_sha256: str

    def __init__(
        self,
        *,
        task_ids: tuple[str, ...],
        arms: tuple[str, ...],
        request_caps: tuple[int, ...],
        seed: int,
        max_prompts_per_model: int,
        max_prompts_two_models: int,
        calibration_population_sha256: str,
        schedule_sha256: str,
        design_sha256: str,
        _authority: object,
    ) -> None:
        if _authority is not _DESIGN_AUTHORITY:
            raise TypeError("v0.7 calibration design must be builder-sealed")
        for field_name, value in {
            "task_ids": task_ids,
            "arms": arms,
            "request_caps": request_caps,
            "seed": seed,
            "max_prompts_per_model": max_prompts_per_model,
            "max_prompts_two_models": max_prompts_two_models,
            "calibration_population_sha256": calibration_population_sha256,
            "schedule_sha256": schedule_sha256,
            "design_sha256": design_sha256,
        }.items():
            object.__setattr__(self, field_name, value)

    @property
    def episodes(self) -> tuple[V07CalibrationEpisode, ...]:
        return tuple(
            V07CalibrationEpisode(task_id, arm, cap, self.seed)
            for task_id, arm, cap in zip(
                self.task_ids,
                self.arms,
                self.request_caps,
                strict=True,
            )
        )

    def to_record(self) -> dict[str, object]:
        return {
            "arms": list(self.arms),
            "calibration_population_sha256": self.calibration_population_sha256,
            "design_sha256": self.design_sha256,
            "max_prompts_per_model": self.max_prompts_per_model,
            "max_prompts_two_models": self.max_prompts_two_models,
            "request_caps": list(self.request_caps),
            "schedule_sha256": self.schedule_sha256,
            "schema": _DESIGN_SCHEMA,
            "seed": self.seed,
            "task_ids": list(self.task_ids),
        }


@dataclass(frozen=True, slots=True)
class _VerifiedV07CalibrationEpisode:
    episode_index: int
    model_id: str
    task_id: str
    arm: str
    result: InteractiveResult
    execution_authority_sha256: str
    controller_log_sha256: str
    active_wall_time_ns: int
    first_response_parsed: bool
    first_action_admissible: bool
    before_host_sample_sha256: str
    after_host_sample_sha256: str

    def to_record(self) -> dict[str, object]:
        return {
            "active_wall_time_ns": self.active_wall_time_ns,
            "after_host_sample_sha256": self.after_host_sample_sha256,
            "arm": self.arm,
            "before_host_sample_sha256": self.before_host_sample_sha256,
            "controller_log_sha256": self.controller_log_sha256,
            "execution_authority_sha256": self.execution_authority_sha256,
            "episode_index": self.episode_index,
            "first_action_admissible": self.first_action_admissible,
            "first_response_parsed": self.first_response_parsed,
            "model_id": self.model_id,
            "result": _serialize_result(self.result),
            "task_id": self.task_id,
        }


@dataclass(frozen=True, slots=True, init=False)
class VerifiedV07CalibrationEvidence:
    """Path-free evidence available only after all nine journals verify."""

    episodes: tuple[_VerifiedV07CalibrationEpisode, ...]
    design_sha256: str
    schedule_sha256: str
    precalibration_manifest_sha256: str
    calibration_campaign_sha256: str
    calibration_journal_sha256: str
    controller_log_set_sha256: str
    evidence_sha256: str

    def __init__(
        self,
        *,
        episodes: tuple[_VerifiedV07CalibrationEpisode, ...],
        design_sha256: str,
        schedule_sha256: str,
        precalibration_manifest_sha256: str,
        calibration_campaign_sha256: str,
        calibration_journal_sha256: str,
        controller_log_set_sha256: str,
        evidence_sha256: str,
        _authority: object,
    ) -> None:
        if _authority is not _EVIDENCE_AUTHORITY:
            raise TypeError(
                "VerifiedV07CalibrationEvidence must be created by its verifier"
            )
        for field_name, value in {
            "episodes": episodes,
            "design_sha256": design_sha256,
            "schedule_sha256": schedule_sha256,
            "precalibration_manifest_sha256": precalibration_manifest_sha256,
            "calibration_campaign_sha256": calibration_campaign_sha256,
            "calibration_journal_sha256": calibration_journal_sha256,
            "controller_log_set_sha256": controller_log_set_sha256,
            "evidence_sha256": evidence_sha256,
        }.items():
            object.__setattr__(self, field_name, value)

    @property
    def episode_count(self) -> int:
        return len(self.episodes)

    @property
    def total_model_prompts(self) -> int:
        return sum(episode.result.model_calls for episode in self.episodes)

    @property
    def controller_log_sha256s(self) -> tuple[str, ...]:
        return tuple(episode.controller_log_sha256 for episode in self.episodes)


@dataclass(frozen=True, slots=True, init=False)
class V07CalibrationDisposition:
    model_id: str
    admitted: bool
    reasons: tuple[str, ...]
    episode_count: int
    parsed_and_admissible_first_responses: int
    strict_successes: int
    total_model_prompts: int
    active_wall_time_ns: int
    evidence_sha256: str
    design_sha256: str
    precalibration_manifest_sha256: str
    calibration_journal_sha256: str
    disposition_sha256: str

    def __init__(
        self,
        *,
        model_id: str,
        admitted: bool,
        reasons: tuple[str, ...],
        episode_count: int,
        parsed_and_admissible_first_responses: int,
        strict_successes: int,
        total_model_prompts: int,
        active_wall_time_ns: int,
        evidence_sha256: str,
        design_sha256: str,
        precalibration_manifest_sha256: str,
        calibration_journal_sha256: str,
        disposition_sha256: str,
        _authority: object,
    ) -> None:
        if _authority is not _DISPOSITION_AUTHORITY:
            raise TypeError("v0.7 calibration disposition must be evaluator-sealed")
        for field_name, value in locals().copy().items():
            if field_name not in {"self", "_authority"}:
                object.__setattr__(self, field_name, value)


@dataclass(frozen=True, slots=True)
class V07PlanningGate:
    estimated_confirmatory_active_time_ns: int
    planning_bound_ns: int
    active_time_limit_ns: int
    allowed: bool
    reason: str | None
    evidence_sha256: str
    precalibration_manifest_sha256: str


def canonical_v07_calibration_design_record() -> dict[str, object]:
    """Return the detached, manifest-embeddable v0.7 calibration design."""

    assignments = [
        {
            "arm": arm,
            "request_cap": request_cap,
            "task_id": task_id,
        }
        for task_id, arm, request_cap in zip(
            V07_CALIBRATION_TASK_IDS,
            V07_CALIBRATION_ARMS,
            V07_CALIBRATION_REQUEST_CAPS,
            strict=True,
        )
    ]
    return {
        "accounting_must_balance": True,
        "active_time_limit_seconds": V07_ACTIVE_TIME_LIMIT_NS // 1_000_000_000,
        "assignments_per_model": assignments,
        "calibration_population_sha256": CALIBRATION_POPULATION_SHA256,
        "confirmatory_task_multiplier": V07_CONFIRMATORY_TASK_MULTIPLIER,
        "evaluator_material_must_remain_private": True,
        "first_response_parse_and_admissibility_minimum": 3,
        "infrastructure_valid_episodes_required": len(V07_CALIBRATION_TASK_IDS),
        "maximum_prompts_per_model": V07_CALIBRATION_MAX_PROMPTS_PER_MODEL,
        "maximum_prompts_two_models": V07_CALIBRATION_MAX_PROMPTS_TWO_MODELS,
        "model_order": list(CAMPAIGN_MODELS),
        "host_safety_must_hold": True,
        "planning_multiplier_numerator": V07_PLANNING_MULTIPLIER_NUMERATOR,
        "planning_multiplier_denominator": V07_PLANNING_MULTIPLIER_DENOMINATOR,
        "seed": CAMPAIGN_SEED,
        "terminal_correctness_gate": "none",
    }


def build_v07_calibration_design(source: InterCodeSource) -> V07CalibrationDesign:
    """Bind the four disjoint public calibration IDs without reading gold."""

    if type(source) is not InterCodeSource:
        raise ValueError("v0.7 calibration design requires a verified source")
    if source.calibration_population_sha256 != CALIBRATION_POPULATION_SHA256:
        raise ValueError("v0.7 calibration population identity differs")
    observed = tuple(task.task_id for task in source.calibration_tasks[:4])
    if observed != V07_CALIBRATION_TASK_IDS:
        raise ValueError("v0.7 calibration task IDs differ from the frozen frame")
    return V07CalibrationDesign(
        task_ids=V07_CALIBRATION_TASK_IDS,
        arms=V07_CALIBRATION_ARMS,
        request_caps=V07_CALIBRATION_REQUEST_CAPS,
        seed=CAMPAIGN_SEED,
        max_prompts_per_model=V07_CALIBRATION_MAX_PROMPTS_PER_MODEL,
        max_prompts_two_models=V07_CALIBRATION_MAX_PROMPTS_TWO_MODELS,
        calibration_population_sha256=CALIBRATION_POPULATION_SHA256,
        schedule_sha256=V07_CALIBRATION_SCHEDULE_SHA256,
        design_sha256=V07_CALIBRATION_DESIGN_SHA256,
        _authority=_DESIGN_AUTHORITY,
    )


def verify_v07_calibration_evidence(
    design: V07CalibrationDesign,
    *,
    precalibration_manifest_sha256: str,
    calibration_campaign_sha256: str,
    calibration_journal_path: str | Path,
    controller_log_paths: tuple[str | Path, ...],
) -> VerifiedV07CalibrationEvidence:
    """Verify the sealed calibration journal and all eight controller journals."""

    _validate_design(design)
    _require_sha256(precalibration_manifest_sha256, "precalibration manifest")
    _require_sha256(calibration_campaign_sha256, "calibration campaign")
    if type(controller_log_paths) is not tuple or len(controller_log_paths) != (
        V07_CALIBRATION_EPISODE_COUNT
    ):
        raise V07CalibrationEvidenceError(
            "calibration requires exactly eight controller log paths"
        )

    calibration_records, calibration_root, calibration_identity = (
        _read_secure_sealed_journal(
            calibration_journal_path,
            label="calibration journal",
            maximum_bytes=_MAX_CALIBRATION_LOG_BYTES,
            maximum_records=V07_CALIBRATION_EPISODE_COUNT + 2,
            exact_records=V07_CALIBRATION_EPISODE_COUNT + 2,
        )
    )
    _reject_forbidden_material(calibration_records)
    rows = _parse_calibration_journal(
        calibration_records,
        design=design,
        precalibration_manifest_sha256=precalibration_manifest_sha256,
        calibration_campaign_sha256=calibration_campaign_sha256,
    )

    verified: list[_VerifiedV07CalibrationEpisode] = []
    controller_roots: list[str] = []
    controller_identities: set[tuple[int, int]] = set()
    tokenizer_by_model: dict[str, str] = {}
    campaign_boot: int | None = None
    prior_after_monotonic: int | None = None
    campaign_daemon: object | None = None
    for path, row in zip(controller_log_paths, rows, strict=True):
        records, controller_root, identity = _read_secure_sealed_journal(
            path,
            label="controller log",
            maximum_bytes=_MAX_CONTROLLER_LOG_BYTES,
            maximum_records=_MAX_CONTROLLER_RECORDS,
        )
        if identity == calibration_identity or identity in controller_identities:
            raise V07CalibrationEvidenceError(
                "calibration journals must be distinct regular files"
            )
        controller_identities.add(identity)
        if controller_root != row["controller_log_sha256"]:
            raise V07CalibrationEvidenceError(
                "controller log root differs from the calibration journal"
            )
        result = row["result"]
        assert type(result) is InteractiveResult
        first_parsed, first_admissible = _verify_controller_records(
            records,
            model_id=str(row["model_id"]),
            task_id=str(row["task_id"]),
            arm=str(row["arm"]),
            execution_authority_sha256=str(
                row["execution_authority_sha256"]
            ),
            result=result,
            tokenizer_by_model=tokenizer_by_model,
        )
        before = row["before_host_sample"]
        after = row["after_host_sample"]
        assert type(before) is HostSafetySample and type(after) is HostSafetySample
        _validate_host_pair(
            before,
            after,
            model_id=str(row["model_id"]),
            active_wall_time_ns=int(row["active_wall_time_ns"]),
        )
        if campaign_boot is None:
            campaign_boot = before.boot_time_unix_microseconds
            campaign_daemon = before.docker_daemon
        elif (
            before.boot_time_unix_microseconds != campaign_boot
            or before.docker_daemon != campaign_daemon
            or prior_after_monotonic is None
            or before.captured_monotonic_ns < prior_after_monotonic
        ):
            raise V07CalibrationEvidenceError(
                "calibration host evidence changes boot, daemon, or schedule order"
            )
        prior_after_monotonic = after.captured_monotonic_ns
        controller_roots.append(controller_root)
        verified.append(
            _VerifiedV07CalibrationEpisode(
                episode_index=int(row["episode_index"]),
                model_id=str(row["model_id"]),
                task_id=str(row["task_id"]),
                arm=str(row["arm"]),
                result=result,
                execution_authority_sha256=str(
                    row["execution_authority_sha256"]
                ),
                controller_log_sha256=controller_root,
                active_wall_time_ns=int(row["active_wall_time_ns"]),
                first_response_parsed=first_parsed,
                first_action_admissible=first_admissible,
                before_host_sample_sha256=before.sha256,
                after_host_sample_sha256=after.sha256,
            )
        )

    controller_log_set_sha256 = _digest(
        {
            "controller_log_sha256s": controller_roots,
            "schema": _EVIDENCE_SCHEMA,
        }
    )
    evidence_core = {
        "calibration_campaign_sha256": calibration_campaign_sha256,
        "calibration_journal_sha256": calibration_root,
        "controller_log_set_sha256": controller_log_set_sha256,
        "design_sha256": design.design_sha256,
        "episodes": [episode.to_record() for episode in verified],
        "precalibration_manifest_sha256": precalibration_manifest_sha256,
        "schedule_sha256": design.schedule_sha256,
        "schema": _EVIDENCE_SCHEMA,
    }
    return VerifiedV07CalibrationEvidence(
        episodes=tuple(verified),
        design_sha256=design.design_sha256,
        schedule_sha256=design.schedule_sha256,
        precalibration_manifest_sha256=precalibration_manifest_sha256,
        calibration_campaign_sha256=calibration_campaign_sha256,
        calibration_journal_sha256=calibration_root,
        controller_log_set_sha256=controller_log_set_sha256,
        evidence_sha256=_digest(evidence_core),
        _authority=_EVIDENCE_AUTHORITY,
    )


def evaluate_v07_calibration(
    evidence: VerifiedV07CalibrationEvidence,
    model_id: str,
) -> V07CalibrationDisposition:
    """Apply the preregistered mechanics gate to one model's verified rows."""

    _validate_verified_evidence(evidence)
    if model_id not in CAMPAIGN_MODELS:
        raise ValueError("v0.7 calibration model is not frozen")
    rows = tuple(row for row in evidence.episodes if row.model_id == model_id)
    expected = tuple(zip(V07_CALIBRATION_TASK_IDS, V07_CALIBRATION_ARMS, strict=True))
    if tuple((row.task_id, row.arm) for row in rows) != expected:
        raise V07CalibrationEvidenceError(
            "verified model rows differ from the calibration schedule"
        )
    reasons: list[str] = []
    if any(row.result.run_status == "infrastructure_error" for row in rows):
        reasons.append("infrastructure_invalid")
    parsed_and_admissible = sum(
        row.first_response_parsed and row.first_action_admissible for row in rows
    )
    if parsed_and_admissible < 3:
        reasons.append("first_action_fit_below_three_of_four")
    total_prompts = sum(row.result.model_calls for row in rows)
    if total_prompts > V07_CALIBRATION_MAX_PROMPTS_PER_MODEL:
        raise V07CalibrationEvidenceError("verified calibration prompt cap is inconsistent")
    values: dict[str, object] = {
        "active_wall_time_ns": sum(row.active_wall_time_ns for row in rows),
        "admitted": not reasons,
        "calibration_journal_sha256": evidence.calibration_journal_sha256,
        "design_sha256": evidence.design_sha256,
        "episode_count": len(rows),
        "evidence_sha256": evidence.evidence_sha256,
        "model_id": model_id,
        "parsed_and_admissible_first_responses": parsed_and_admissible,
        "precalibration_manifest_sha256": evidence.precalibration_manifest_sha256,
        "reasons": tuple(reasons),
        "strict_successes": sum(row.result.strict_success for row in rows),
        "total_model_prompts": total_prompts,
    }
    disposition_sha256 = _digest(
        {**values, "reasons": list(reasons), "schema": _EVIDENCE_SCHEMA + ".disposition"}
    )
    return V07CalibrationDisposition(
        **values,  # type: ignore[arg-type]
        disposition_sha256=disposition_sha256,
        _authority=_DISPOSITION_AUTHORITY,
    )


def evaluate_v07_planning_gate(
    dispositions: tuple[V07CalibrationDisposition, ...],
) -> V07PlanningGate:
    """Apply the prespecified 30x projection and 1.5x planning bound."""

    if type(dispositions) is not tuple or len(dispositions) != len(CAMPAIGN_MODELS):
        raise ValueError("v0.7 planning gate requires both model dispositions")
    for disposition in dispositions:
        _validate_disposition(disposition)
    if tuple(item.model_id for item in dispositions) != CAMPAIGN_MODELS:
        raise ValueError("v0.7 planning dispositions are out of model order")
    provenance = {
        (
            item.evidence_sha256,
            item.design_sha256,
            item.precalibration_manifest_sha256,
            item.calibration_journal_sha256,
        )
        for item in dispositions
    }
    if len(provenance) != 1:
        raise ValueError("v0.7 planning dispositions do not share exact evidence")
    first = dispositions[0]
    if any(not item.admitted for item in dispositions):
        return V07PlanningGate(
            0,
            0,
            V07_ACTIVE_TIME_LIMIT_NS,
            False,
            "model_not_admitted",
            first.evidence_sha256,
            first.precalibration_manifest_sha256,
        )
    estimated = V07_CONFIRMATORY_TASK_MULTIPLIER * sum(
        item.active_wall_time_ns for item in dispositions
    )
    bound = (
        estimated * V07_PLANNING_MULTIPLIER_NUMERATOR
        + V07_PLANNING_MULTIPLIER_DENOMINATOR
        - 1
    ) // V07_PLANNING_MULTIPLIER_DENOMINATOR
    allowed = bound <= V07_ACTIVE_TIME_LIMIT_NS
    return V07PlanningGate(
        estimated,
        bound,
        V07_ACTIVE_TIME_LIMIT_NS,
        allowed,
        None if allowed else "planning_bound_exceeds_18_active_hours",
        first.evidence_sha256,
        first.precalibration_manifest_sha256,
    )


def _schedule_record() -> dict[str, object]:
    return {
        "episodes": [
            {
                "arm": arm,
                "request_cap": cap,
                "seed": CAMPAIGN_SEED,
                "task_id": task_id,
            }
            for task_id, arm, cap in zip(
                V07_CALIBRATION_TASK_IDS,
                V07_CALIBRATION_ARMS,
                V07_CALIBRATION_REQUEST_CAPS,
                strict=True,
            )
        ],
        "schema": "edgeloopbench.intercode-v0.7-calibration-schedule.v1",
    }


def _serialize_result(result: InteractiveResult) -> dict[str, object]:
    return {field_name: getattr(result, field_name) for field_name in _RESULT_FIELDS}


def _validate_design(design: V07CalibrationDesign) -> None:
    if type(design) is not V07CalibrationDesign:
        raise ValueError("v0.7 calibration verifier requires a sealed design")
    schedule_sha256 = _digest(_schedule_record())
    expected = {
        "arms": V07_CALIBRATION_ARMS,
        "calibration_population_sha256": CALIBRATION_POPULATION_SHA256,
        "design_sha256": V07_CALIBRATION_DESIGN_SHA256,
        "max_prompts_per_model": V07_CALIBRATION_MAX_PROMPTS_PER_MODEL,
        "max_prompts_two_models": V07_CALIBRATION_MAX_PROMPTS_TWO_MODELS,
        "request_caps": V07_CALIBRATION_REQUEST_CAPS,
        "schedule_sha256": schedule_sha256,
        "seed": CAMPAIGN_SEED,
        "task_ids": V07_CALIBRATION_TASK_IDS,
    }
    if any(getattr(design, field_name) != value for field_name, value in expected.items()):
        raise ValueError("v0.7 calibration design differs from its canonical record")


def _read_secure_sealed_journal(
    path: str | Path,
    *,
    label: str,
    maximum_bytes: int,
    maximum_records: int,
    exact_records: int | None = None,
) -> tuple[tuple[dict[str, object], ...], str, tuple[int, int]]:
    target = Path(path)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise V07CalibrationEvidenceError("platform lacks no-follow file opens")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.fspath(target), flags)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            raise V07CalibrationEvidenceError(
                f"{label} must be a regular non-symlink file"
            ) from error
        raise V07CalibrationEvidenceError(f"{label} could not be securely opened") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise V07CalibrationEvidenceError(
                f"{label} must be a regular non-symlink file"
            )
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise V07CalibrationEvidenceError(f"{label} must have exact mode 0600")
        if not 0 < before.st_size <= maximum_bytes:
            raise V07CalibrationEvidenceError(f"{label} exceeds its bounded size")
        try:
            inspection = inspect_journal(target, require_sealed=True)
        except (OSError, JournalError, ValueError) as error:
            raise V07CalibrationEvidenceError(
                f"{label} is not a valid sealed journal"
            ) from error
        try:
            named = target.lstat()
        except OSError as error:
            raise V07CalibrationEvidenceError(
                f"{label} identity could not be reopened"
            ) from error
        if stat.S_ISLNK(named.st_mode) or (
            named.st_dev,
            named.st_ino,
            named.st_size,
        ) != (before.st_dev, before.st_ino, before.st_size):
            raise V07CalibrationEvidenceError(f"{label} identity changed")
        raw = _read_bounded(descriptor, before.st_size, label)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ):
            raise V07CalibrationEvidenceError(f"{label} changed during verification")
    finally:
        os.close(descriptor)
    if (
        inspection.partial_tail is not None
        or inspection.file_byte_length != len(raw)
        or inspection.record_count > maximum_records
        or (exact_records is not None and inspection.record_count != exact_records)
    ):
        raise V07CalibrationEvidenceError(f"{label} record set is not exact")
    records = _decode_records(raw, label)
    if len(records) != inspection.record_count:
        raise V07CalibrationEvidenceError(f"{label} changed after inspection")
    return (
        records,
        "sha256:" + inspection.last_event_sha256,
        (before.st_dev, before.st_ino),
    )


def _read_bounded(descriptor: int, expected_size: int, label: str) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = expected_size
    while remaining:
        try:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
        except InterruptedError:
            continue
        if not chunk:
            raise V07CalibrationEvidenceError(f"{label} ended during verification")
        chunks.append(chunk)
        remaining -= len(chunk)
    try:
        extra = os.read(descriptor, 1)
    except InterruptedError:
        extra = os.read(descriptor, 1)
    if extra:
        raise V07CalibrationEvidenceError(f"{label} grew during verification")
    return b"".join(chunks)


def _decode_records(raw: bytes, label: str) -> tuple[dict[str, object], ...]:
    if not raw.endswith(b"\n"):
        raise V07CalibrationEvidenceError(f"{label} is not newline terminated")
    records: list[dict[str, object]] = []
    try:
        for line in raw.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise V07CalibrationEvidenceError(f"{label} record is not an object")
            records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise V07CalibrationEvidenceError(f"{label} could not be decoded") from error
    return tuple(records)


def _parse_calibration_journal(
    records: tuple[dict[str, object], ...],
    *,
    design: V07CalibrationDesign,
    precalibration_manifest_sha256: str,
    calibration_campaign_sha256: str,
) -> tuple[dict[str, object], ...]:
    payloads = tuple(_without_chain(record) for record in records)
    expected_declaration = {
        "design_sha256": design.design_sha256,
        "episode_count": V07_CALIBRATION_EPISODE_COUNT,
        "models": list(CAMPAIGN_MODELS),
        "precalibration_manifest_sha256": precalibration_manifest_sha256,
        "calibration_campaign_sha256": calibration_campaign_sha256,
        "schedule_sha256": design.schedule_sha256,
        "schema": _JOURNAL_SCHEMA,
        "type": "calibration_declared",
    }
    if payloads[0].get("calibration_campaign_sha256") != (
        calibration_campaign_sha256
    ):
        raise V07CalibrationEvidenceError(
            "calibration campaign authority differs from the declaration"
        )
    if payloads[0] != expected_declaration:
        raise V07CalibrationEvidenceError(
            "calibration journal declaration or manifest differs"
        )
    if payloads[-1] != {
        "sealed_event_count": len(payloads) - 1,
        "type": "journal_sealed",
    }:
        raise V07CalibrationEvidenceError("calibration journal seal is not exact")
    expected_schedule = tuple(
        (model_id, episode.task_id, episode.arm)
        for model_id in CAMPAIGN_MODELS
        for episode in design.episodes
    )
    parsed: list[dict[str, object]] = []
    for episode_index, (payload, expected) in enumerate(
        zip(payloads[1:-1], expected_schedule, strict=True),
        1,
    ):
        expected_fields = {
            "active_wall_time_ns",
            "after_host_sample",
            "arm",
            "before_host_sample",
            "controller_log_sha256",
            "execution_authority_sha256",
            "episode_index",
            "model_id",
            "result",
            "seed",
            "task_id",
            "type",
        }
        if set(payload) != expected_fields:
            raise V07CalibrationEvidenceError(
                "calibration episode journal fields differ from the schema"
            )
        model_id, task_id, arm = expected
        if (
            payload.get("type") != "calibration_episode_recorded"
            or payload.get("episode_index") != episode_index
            or payload.get("model_id") != model_id
            or payload.get("task_id") != task_id
            or payload.get("arm") != arm
            or payload.get("seed") != CAMPAIGN_SEED
            or payload.get("execution_authority_sha256")
            != precalibration_manifest_sha256
        ):
            raise V07CalibrationEvidenceError(
                "calibration episode identity differs from the exact schedule"
            )
        active_wall_time_ns = _positive_integer(
            payload.get("active_wall_time_ns"), "active wall time"
        )
        try:
            before = parse_host_safety_sample(payload.get("before_host_sample"))
            after = parse_host_safety_sample(payload.get("after_host_sample"))
        except ValueError as error:
            raise V07CalibrationEvidenceError(
                "calibration host safety sample is invalid"
            ) from error
        parsed.append(
            {
                "active_wall_time_ns": active_wall_time_ns,
                "after_host_sample": after,
                "arm": arm,
                "before_host_sample": before,
                "controller_log_sha256": _require_sha256(
                    payload.get("controller_log_sha256"), "controller log"
                ),
                "execution_authority_sha256": _require_sha256(
                    payload.get("execution_authority_sha256"),
                    "execution authority",
                ),
                "episode_index": episode_index,
                "model_id": model_id,
                "result": _parse_result(payload.get("result")),
                "task_id": task_id,
            }
        )
    return tuple(parsed)


def _parse_result(value: object) -> InteractiveResult:
    if not isinstance(value, Mapping) or set(value) != set(_RESULT_FIELDS):
        raise V07CalibrationEvidenceError("calibration result schema is invalid")
    record = dict(value)
    if record["run_status"] not in _RUN_STATUSES:
        raise V07CalibrationEvidenceError("calibration result run status is invalid")
    if record["stop_reason"] not in _STOP_REASONS:
        raise V07CalibrationEvidenceError("calibration result stop reason is invalid")
    for field_name in _RESULT_BOOLEAN_FIELDS:
        if type(record[field_name]) is not bool:
            raise V07CalibrationEvidenceError(
                f"calibration result {field_name} must be boolean"
            )
    for field_name in _RESULT_INTEGER_FIELDS:
        _nonnegative_integer(record[field_name], f"result {field_name}")
    if record["official_success"] is not False or record["human_prompts"] != 0:
        raise V07CalibrationEvidenceError(
            "calibration result violates official or human accounting"
        )
    return InteractiveResult(**record)  # type: ignore[arg-type]


def _verify_controller_records(
    records: tuple[dict[str, object], ...],
    *,
    model_id: str,
    task_id: str,
    arm: str,
    execution_authority_sha256: str,
    result: InteractiveResult,
    tokenizer_by_model: dict[str, str],
) -> tuple[bool, bool]:
    if not 4 <= len(records) <= _MAX_CONTROLLER_RECORDS:
        raise V07CalibrationEvidenceError("controller journal record count is invalid")
    _reject_forbidden_material(records)
    payloads = tuple(_without_chain(record) for record in records)
    if payloads[-1] != {
        "sealed_event_count": len(payloads) - 1,
        "type": "journal_sealed",
    }:
        raise V07CalibrationEvidenceError("controller journal seal is not exact")
    events = payloads[:-1]
    if events[0].get("type") != "controller_started" or events[-1].get(
        "type"
    ) != "controller_stopped":
        raise V07CalibrationEvidenceError(
            "controller start and stop events are not terminally ordered"
        )
    positions: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for position, event in enumerate(events):
        if (
            event.get("task_id") != task_id
            or event.get("strategy") != arm
            or event.get("replicate_seed") != CAMPAIGN_SEED
            or event.get("execution_authority_sha256")
            != execution_authority_sha256
        ):
            raise V07CalibrationEvidenceError(
                "controller event identity differs from calibration schedule"
            )
        _validate_controller_event(event)
        if "official_success" in event and event["official_success"] is not False:
            raise V07CalibrationEvidenceError(
                "controller official_success must remain false in v0.7"
            )
        if event.get("type") == "attempt_evaluated" and (
            float(event["reward"]) not in {0.0, 0.2, 0.4, 0.6, 0.8}
            or event["evaluation_kind"] != "evaluator_derived"
        ):
            raise V07CalibrationEvidenceError(
                "controller progress event differs from the frozen policy"
            )
        if event.get("type") == "attempt_defaulted" and (
            event["reward"] != 0.0
            or event["evaluation_kind"] != "action_policy_failure"
        ):
            raise V07CalibrationEvidenceError(
                "controller default progress event differs from the frozen policy"
            )
        event_type = str(event["type"])
        positions.setdefault(event_type, []).append((position, event))
    if len(positions.get("controller_started", ())) != 1 or len(
        positions.get("controller_stopped", ())
    ) != 1:
        raise V07CalibrationEvidenceError("controller start/stop cardinality is invalid")
    if (
        positions["controller_started"][0][1]["controller_revision"]
        != INTERACTIVE_CONTROLLER_REVISION
    ):
        raise V07CalibrationEvidenceError("controller revision differs from v0.7")

    profile = _profile_for_model(model_id)
    preflights = _unique_attempts(positions.get("model_preflighted", []), "preflight")
    requested = _unique_attempts(positions.get("model_requested", []), "request")
    completed = _unique_attempts(positions.get("model_completed", []), "completion")
    calls = len(requested)
    cap = V07_CALIBRATION_REQUEST_CAPS[V07_CALIBRATION_ARMS.index(arm)]
    if not 1 <= calls <= cap or set(requested) != set(completed):
        raise V07CalibrationEvidenceError(
            "controller model calls differ from the frozen arm cap"
        )
    if set(requested) != set(range(1, calls + 1)) or not set(requested) <= set(
        preflights
    ):
        raise V07CalibrationEvidenceError("controller model attempts are not contiguous")

    cumulative_prompt_tokens = 0
    logical_prompt_tokens = 0
    logical_completion_tokens = 0
    for ordinal in range(1, calls + 1):
        preflight_position, preflight = preflights[ordinal]
        request_position, request = requested[ordinal]
        completion_position, completion = completed[ordinal]
        if not preflight_position < request_position < completion_position:
            raise V07CalibrationEvidenceError(
                "controller model event order is invalid"
            )
        if not (
            preflight["prompt_sha256"] == request["prompt_sha256"]
            and preflight["prompt_tokens"] == completion["prompt_tokens"]
        ):
            raise V07CalibrationEvidenceError("controller prompt evidence changed")
        if (
            preflight["renderer_profile_sha256"] != profile.sha256
            or preflight["model_artifact_sha256"] != profile.model_artifact_sha256
        ):
            raise V07CalibrationEvidenceError(
                "controller rendering or model artifact differs from v0.7"
            )
        tokenizer = _require_sha256(
            preflight["tokenizer_artifact_sha256"], "tokenizer artifact"
        )
        prior_tokenizer = tokenizer_by_model.setdefault(model_id, tokenizer)
        if tokenizer != prior_tokenizer:
            raise V07CalibrationEvidenceError(
                "controller tokenizer artifact changed within one model"
            )
        prompt_tokens = _positive_integer(
            completion["prompt_tokens"], "model prompt tokens"
        )
        completion_tokens = _nonnegative_integer(
            completion["completion_tokens"], "model completion tokens"
        )
        if prompt_tokens + completion_tokens > _MAX_PER_CALL_CONTEXT_TOKENS:
            raise V07CalibrationEvidenceError(
                "controller model call exceeds the context budget"
            )
        if (
            _positive_integer(request["max_output_tokens"], "max output tokens")
            > _MAX_OUTPUT_TOKENS
        ):
            raise V07CalibrationEvidenceError(
                "controller model call exceeds the output-token budget"
            )
        cumulative_prompt_tokens += prompt_tokens
        if (
            request["logical_model_calls_after"] != ordinal
            or request["logical_prompt_tokens_after"] != cumulative_prompt_tokens
            or request["candidate_seed"] != candidate_seed(CAMPAIGN_SEED, ordinal)
        ):
            raise V07CalibrationEvidenceError(
                "controller logical counters or candidate seed differ"
            )
        logical_prompt_tokens += prompt_tokens
        logical_completion_tokens += completion_tokens

    rejected = _unique_attempts(positions.get("action_rejected", []), "rejection")
    actions = _unique_attempts(positions.get("action_completed", []), "action")
    action_requests = _unique_attempts(
        positions.get("action_requested", []), "action request"
    )
    if set(action_requests) != set(actions):
        raise V07CalibrationEvidenceError(
            "controller action request/completion attempts differ"
        )
    for attempt in range(1, calls + 1):
        if (attempt in rejected) == (attempt in actions):
            raise V07CalibrationEvidenceError(
                "controller response lacks one exact parser/action outcome"
            )
        completion_position = completed[attempt][0]
        if attempt in rejected:
            rejection_position, rejection = rejected[attempt]
            if (
                rejection["reason"] != "parser_failure"
                or rejection_position <= completion_position
                or attempt in action_requests
            ):
                raise V07CalibrationEvidenceError(
                    "controller parser-failure evidence is inconsistent"
                )
        else:
            request_position, action_request = action_requests.get(attempt, (-1, {}))
            action_position, action = actions[attempt]
            if not completion_position < request_position < action_position:
                raise V07CalibrationEvidenceError(
                    "controller action event order is invalid"
                )
            if action_request.get("action_sha256") != action["action_sha256"]:
                raise V07CalibrationEvidenceError("controller action digest changed")

    admissible_attempts: set[int] = set()
    inadmissible_attempts: set[int] = set()
    for attempt, (_position, action) in actions.items():
        if action["admissible"] is True:
            if (
                action["policy_failure"] is not None
                or action["safety_recovery_performed"] is not False
            ):
                raise V07CalibrationEvidenceError(
                    "controller admissible action has policy-failure evidence"
                )
            admissible_attempts.add(attempt)
        else:
            if (
                action["policy_failure"]
                not in {
                    "timeout",
                    "output_overflow",
                    "invalid_text",
                    "residual_process",
                    "container_terminated",
                }
                or action["safety_recovery_performed"] is not True
            ):
                raise V07CalibrationEvidenceError(
                    "controller inadmissible action lacks recovery evidence"
                )
            inadmissible_attempts.add(attempt)
    checkpoint_requests = _unique_attempts(
        positions.get("checkpoint_create_requested", []), "checkpoint request"
    )
    checkpoints = _unique_attempts(
        positions.get("checkpoint_created", []), "checkpoint"
    )
    evaluation_requests = _unique_attempts(
        positions.get("attempt_evaluation_requested", []), "evaluation request"
    )
    evaluations = _unique_attempts(
        positions.get("attempt_evaluated", []), "evaluation"
    )
    if not (
        set(checkpoint_requests)
        == set(checkpoints)
        == set(evaluation_requests)
        == set(evaluations)
        == admissible_attempts
    ):
        raise V07CalibrationEvidenceError(
            "controller checkpoint/evaluator accounting is inconsistent"
        )
    recoveries = _unique_attempts(
        positions.get("safety_recovery_completed", []), "safety recovery"
    )
    defaults = _unique_attempts(
        positions.get("attempt_defaulted", []), "attempt default"
    )
    if set(recoveries) != inadmissible_attempts or set(defaults) != inadmissible_attempts:
        raise V07CalibrationEvidenceError(
            "controller policy-failure recovery accounting is inconsistent"
        )

    terminal_rows = positions.get("terminal_finalized", [])
    stop_rows = positions.get("controller_stopped", [])
    if len(terminal_rows) != 1 or len(stop_rows) != 1:
        raise V07CalibrationEvidenceError("controller terminal evidence is incomplete")
    terminal = terminal_rows[0][1]
    stopped = stop_rows[0][1]
    strict_calls = _nonnegative_integer(
        terminal["strict_evaluator_calls"], "strict evaluator calls"
    )
    posthoc_calls = _nonnegative_integer(
        terminal["posthoc_evaluator_calls"], "posthoc evaluator calls"
    )
    strict_rows = positions.get("strict_evaluation_completed", [])
    planned_rows = positions.get("strict_evaluation_planned", [])
    finalization_requests = positions.get("terminal_finalization_requested", [])
    if (
        posthoc_calls != 0
        or strict_calls not in {0, 1}
        or len(strict_rows) != strict_calls
        or len(planned_rows) != strict_calls
        or len(finalization_requests) != 1
    ):
        raise V07CalibrationEvidenceError("controller strict evaluator accounting differs")
    strict_success = False
    if strict_rows:
        plan_position, plan = planned_rows[0]
        request_position, finalization_request = finalization_requests[0]
        terminal_position = terminal_rows[0][0]
        strict_position, strict_event = strict_rows[0]
        stop_position = stop_rows[0][0]
        selected_attempt = _positive_integer(
            plan["selected_attempt"], "strict selected attempt"
        )
        if (
            selected_attempt not in checkpoints
            or plan["state_sha256"] != checkpoints[selected_attempt][1]["state_sha256"]
            or finalization_request["selected_attempt"] != selected_attempt
            or stopped["selected_attempt"] != selected_attempt
            or not plan_position
            < request_position
            < terminal_position
            < strict_position
            < stop_position
        ):
            raise V07CalibrationEvidenceError(
                "controller strict selection provenance is inconsistent"
            )
        if strict_event["evaluator_sha256"] != V07_STRICT_REPLAY_EVALUATOR_SHA256:
            raise V07CalibrationEvidenceError("strict evaluator identity differs from v0.7")
        strict_success = bool(strict_event["strict_success"])
    elif finalization_requests[0][1]["selected_attempt"] is not None:
        raise V07CalibrationEvidenceError(
            "controller unevaluated terminal selection is inconsistent"
        )
    if stopped["official_success"] is not False or stopped["stop_reason"] != result.stop_reason:
        raise V07CalibrationEvidenceError("controller stop evidence differs from result")

    derived = {
        "attempts": calls,
        "model_calls": calls,
        "logical_prompt_tokens": logical_prompt_tokens,
        "logical_completion_tokens": logical_completion_tokens,
        "environment_actions": len(actions),
        "evaluator_calls": len(positions.get("attempt_evaluated", []))
        + strict_calls
        + posthoc_calls,
        "checkpoint_creates": len(positions.get("checkpoint_created", [])),
        "checkpoint_restores": len(positions.get("checkpoint_restored", [])),
        "safety_recoveries": len(positions.get("safety_recovery_completed", [])),
        "parser_failures": len(rejected),
        "initial_prompts": 1,
        "independent_sample_prompts": (
            calls - 1 if arm == "independent_verified_sampling" else 0
        ),
        "feedback_followups": (
            calls - 1 if arm in {"raw_feedback_loop", "engineered_loop"} else 0
        ),
        "human_prompts": 0,
    }
    for field_name, observed in derived.items():
        if getattr(result, field_name) != observed:
            raise V07CalibrationEvidenceError(
                f"event-derived {field_name} differs from calibration result"
            )
    expected_run_status = (
        "infrastructure_error"
        if positions.get("infrastructure_invalid")
        else "budget_exhausted"
        if "budget_exhausted" in result.stop_reason
        else "completed"
    )
    if (
        result.run_status != expected_run_status
        or result.strict_success is not strict_success
        or result.official_success is not False
    ):
        raise V07CalibrationEvidenceError(
            "event-derived terminal result differs from calibration result"
        )
    if (
        logical_prompt_tokens > _MAX_LOGICAL_PROMPT_TOKENS
        or logical_completion_tokens > _MAX_LOGICAL_COMPLETION_TOKENS
        or len(actions) > 4
        or derived["evaluator_calls"] > _MAX_EVALUATOR_CALLS
        or derived["checkpoint_creates"] > 4
        or derived["checkpoint_restores"] > 4
        or derived["safety_recoveries"] > 4
    ):
        raise V07CalibrationEvidenceError(
            "event-derived calibration accounting exceeds a v0.7 budget"
        )
    first_rejected = 1 in rejected
    return (not first_rejected, not first_rejected and actions[1][1]["admissible"] is True)


def _validate_controller_event(event: dict[str, object]) -> None:
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise V07CalibrationEvidenceError("controller event type is invalid")
    if event_type == "model_request_rejected":
        extras = _MODEL_REJECTION_FIELDS.get(event.get("reason"))  # type: ignore[arg-type]
    elif event_type == "infrastructure_invalid":
        extras = _INFRASTRUCTURE_INVALID_FIELDS.get(event.get("reason"))  # type: ignore[arg-type]
    else:
        extras = _STATIC_CONTROLLER_EVENT_FIELDS.get(event_type)
    if extras is None or set(event) != _COMMON_CONTROLLER_FIELDS | extras:
        raise V07CalibrationEvidenceError(
            "controller event type or fields differ from the frozen schema"
        )
    for key, value in event.items():
        if key.endswith("_sha256"):
            _require_sha256(value, key)
    for key in ("attempt", "selected_attempt"):
        if key in event and event[key] is not None:
            attempt = _positive_integer(event[key], key)
            if attempt > 4:
                raise V07CalibrationEvidenceError(
                    "controller attempt exceeds the frozen cap"
                )
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "logical_model_calls_after",
        "logical_prompt_tokens_after",
        "max_output_tokens",
        "total_duration_ns",
        "observed_prompt_bytes",
        "prompt_byte_limit",
        "remaining_prompt_tokens",
        "remaining_context_tokens",
        "preflight_prompt_tokens",
        "telemetry_prompt_tokens",
        "allowed_completion_tokens",
        "telemetry_completion_tokens",
        "allowed_context_tokens",
        "telemetry_context_tokens",
        "remaining_evaluator_calls",
        "strict_evaluator_calls",
        "posthoc_evaluator_calls",
    ):
        if key in event:
            _nonnegative_integer(event[key], key)
    for key in (
        "admissible",
        "state_changed",
        "safety_recovery_performed",
        "strict_success",
        "aborted",
        "official_success",
    ):
        if key in event and type(event[key]) is not bool:
            raise V07CalibrationEvidenceError(f"controller {key} must be boolean")
    if "exit_code" in event and event["exit_code"] is not None and type(
        event["exit_code"]
    ) is not int:
        raise V07CalibrationEvidenceError("controller exit code must be integer or null")
    if "reward" in event:
        reward = event["reward"]
        if (
            isinstance(reward, bool)
            or not isinstance(reward, (int, float))
            or not math.isfinite(float(reward))
        ):
            raise V07CalibrationEvidenceError("controller reward must be finite numeric")


def _unique_attempts(
    positioned: list[tuple[int, dict[str, object]]], label: str
) -> dict[int, tuple[int, dict[str, object]]]:
    observed: dict[int, tuple[int, dict[str, object]]] = {}
    for position, event in positioned:
        attempt = _positive_integer(event["attempt"], f"{label} attempt")
        if attempt > 4 or attempt in observed:
            raise V07CalibrationEvidenceError(
                f"controller repeats or exceeds {label} attempt"
            )
        observed[attempt] = (position, event)
    return observed


def _validate_host_pair(
    before: HostSafetySample,
    after: HostSafetySample,
    *,
    model_id: str,
    active_wall_time_ns: int,
) -> None:
    for sample in (before, after):
        try:
            reparsed = parse_host_safety_sample(sample.to_record())
        except ValueError as error:  # pragma: no cover - parser constructed these values
            raise V07CalibrationEvidenceError(
                "calibration host safety sample is not canonical"
            ) from error
        if type(reparsed) is not HostSafetySample or reparsed != sample:
            raise V07CalibrationEvidenceError(
                "calibration host safety sample is not canonical"
            )
    if (
        before.boot_time_unix_microseconds != after.boot_time_unix_microseconds
        or before.captured_monotonic_ns > after.captured_monotonic_ns
        or after.captured_monotonic_ns - before.captured_monotonic_ns
        < active_wall_time_ns
    ):
        raise V07CalibrationEvidenceError(
            "calibration host samples do not enclose active wall time"
        )
    profile = _profile_for_model(model_id)
    expected_models = (
        ResidentModel(model_id, profile.model_manifest_sha256.removeprefix("sha256:")),
    )
    if (
        before.resident_models != expected_models
        or after.resident_models != expected_models
        or before.running_container_ids
        or after.running_container_ids
        or before.docker_daemon is None
        or after.docker_daemon is None
        or before.docker_daemon != after.docker_daemon
    ):
        raise V07CalibrationEvidenceError(
            "calibration host safety resources differ from the expected model"
        )
    before_safe = (
        before.on_ac_power
        and not before.low_power_mode_enabled
        and before.vm_pressure_level == 1
        and before.free_memory_percent >= _ADMISSION_FREE_PERCENT_MINIMUM
        and before.disk_free_bytes >= _ADMISSION_DISK_FREE_BYTES_MINIMUM
        and not before.thermal_warning
        and not before.performance_warning
    )
    after_safe = (
        after.on_ac_power
        and not after.low_power_mode_enabled
        and after.vm_pressure_level == 1
        and after.free_memory_percent >= _RUNNING_FREE_PERCENT_MINIMUM
        and after.disk_free_bytes >= _RUNNING_DISK_FREE_BYTES_MINIMUM
        and not after.thermal_warning
        and not after.performance_warning
        and after.swap_used_bytes - before.swap_used_bytes
        <= _MAX_EPISODE_SWAP_GROWTH_BYTES
    )
    if not before_safe or not after_safe:
        raise V07CalibrationEvidenceError(
            "calibration host safety thresholds are not satisfied"
        )


def _profile_for_model(model_id: str):  # type: ignore[no-untyped-def]
    if model_id == QWEN35_RAW_PROFILE.model:
        return QWEN35_RAW_PROFILE
    if model_id == PHI4_MINI_RAW_PROFILE.model:
        return PHI4_MINI_RAW_PROFILE
    raise V07CalibrationEvidenceError("calibration model is not frozen")


def _validate_verified_evidence(evidence: VerifiedV07CalibrationEvidence) -> None:
    if type(evidence) is not VerifiedV07CalibrationEvidence:
        raise ValueError("calibration admission requires verified sealed evidence")
    if (
        len(evidence.episodes) != V07_CALIBRATION_EPISODE_COUNT
        or evidence.design_sha256 != V07_CALIBRATION_DESIGN_SHA256
        or evidence.schedule_sha256 != V07_CALIBRATION_SCHEDULE_SHA256
    ):
        raise V07CalibrationEvidenceError("verified calibration evidence is incomplete")
    expected_schedule = tuple(
        (index, model_id, task_id, arm)
        for index, (model_id, task_id, arm) in enumerate(
            (
                (model_id, task_id, arm)
                for model_id in CAMPAIGN_MODELS
                for task_id, arm in zip(
                    V07_CALIBRATION_TASK_IDS, V07_CALIBRATION_ARMS, strict=True
                )
            ),
            1,
        )
    )
    observed_schedule = tuple(
        (row.episode_index, row.model_id, row.task_id, row.arm)
        for row in evidence.episodes
    )
    if observed_schedule != expected_schedule:
        raise V07CalibrationEvidenceError("verified calibration schedule differs")
    core = {
        "calibration_campaign_sha256": evidence.calibration_campaign_sha256,
        "calibration_journal_sha256": evidence.calibration_journal_sha256,
        "controller_log_set_sha256": evidence.controller_log_set_sha256,
        "design_sha256": evidence.design_sha256,
        "episodes": [episode.to_record() for episode in evidence.episodes],
        "precalibration_manifest_sha256": evidence.precalibration_manifest_sha256,
        "schedule_sha256": evidence.schedule_sha256,
        "schema": _EVIDENCE_SCHEMA,
    }
    _require_sha256(
        evidence.calibration_campaign_sha256,
        "calibration campaign",
    )
    if evidence.evidence_sha256 != _digest(core):
        raise V07CalibrationEvidenceError("verified calibration evidence digest differs")


def _validate_disposition(disposition: V07CalibrationDisposition) -> None:
    if type(disposition) is not V07CalibrationDisposition:
        raise ValueError("planning gate requires evaluator-sealed dispositions")
    if (
        disposition.model_id not in CAMPAIGN_MODELS
        or disposition.episode_count != len(V07_CALIBRATION_TASK_IDS)
        or not 0 <= disposition.parsed_and_admissible_first_responses <= 4
        or not 0 <= disposition.strict_successes <= 4
        or not 1 <= disposition.total_model_prompts
        <= V07_CALIBRATION_MAX_PROMPTS_PER_MODEL
        or type(disposition.active_wall_time_ns) is not int
        or disposition.active_wall_time_ns <= 0
        or disposition.admitted is not (not disposition.reasons)
    ):
        raise ValueError("planning disposition invariants are invalid")
    values = {
        "active_wall_time_ns": disposition.active_wall_time_ns,
        "admitted": disposition.admitted,
        "calibration_journal_sha256": disposition.calibration_journal_sha256,
        "design_sha256": disposition.design_sha256,
        "episode_count": disposition.episode_count,
        "evidence_sha256": disposition.evidence_sha256,
        "model_id": disposition.model_id,
        "parsed_and_admissible_first_responses": (
            disposition.parsed_and_admissible_first_responses
        ),
        "precalibration_manifest_sha256": (
            disposition.precalibration_manifest_sha256
        ),
        "reasons": list(disposition.reasons),
        "strict_successes": disposition.strict_successes,
        "total_model_prompts": disposition.total_model_prompts,
        "schema": _EVIDENCE_SCHEMA + ".disposition",
    }
    for field_name in (
        "calibration_journal_sha256",
        "design_sha256",
        "evidence_sha256",
        "precalibration_manifest_sha256",
    ):
        _require_sha256(values[field_name], field_name)
    if disposition.disposition_sha256 != _digest(values):
        raise ValueError("planning disposition digest differs")


def _without_chain(record: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in record.items() if key not in _CHAIN_FIELDS}


def _reject_forbidden_material(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = key.lower()
            if (
                lowered in _FORBIDDEN_FIELD_NAMES
                or lowered.endswith("_path")
                or lowered.startswith("expected_")
                or "gold" in lowered
                or lowered.startswith("raw_")
            ):
                raise V07CalibrationEvidenceError(
                    "calibration journal contains forbidden evaluator material"
                )
            _reject_forbidden_material(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _reject_forbidden_material(child)
        return
    if isinstance(value, str) and (
        value.startswith(("/", "~/", "~\\"))
        or _WINDOWS_ABSOLUTE_PATH.match(value) is not None
    ):
        raise V07CalibrationEvidenceError(
            "calibration journal contains a host absolute path"
        )


def _require_sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise V07CalibrationEvidenceError(
            f"calibration {field_name} is not a lowercase SHA-256 reference"
        )
    return value


def _nonnegative_integer(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise V07CalibrationEvidenceError(
            f"calibration {field_name} must be a non-negative integer"
        )
    return value


def _positive_integer(value: object, field_name: str) -> int:
    observed = _nonnegative_integer(value, field_name)
    if observed == 0:
        raise V07CalibrationEvidenceError(
            f"calibration {field_name} must be a positive integer"
        )
    return observed


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


V07_CALIBRATION_SCHEDULE_SHA256 = _digest(_schedule_record())
V07_CALIBRATION_DESIGN_SHA256 = _digest(canonical_v07_calibration_design_record())
