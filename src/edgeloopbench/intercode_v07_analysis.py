"""Frozen effectiveness analysis for complete v0.7 campaign evidence."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from .intercode_campaign_evidence import VerifiedCampaignEvidence
from .intercode_campaign_ledger import (
    CAMPAIGN_ARMS,
    CAMPAIGN_EPISODE_COUNT,
    CAMPAIGN_MODELS,
    CampaignEpisodeResult,
)


V07_ANALYSIS_REVISION = "intercode-v0.7-effectiveness-analysis-v5"
V07_BOOTSTRAP_SEED = 20_260_716
V07_BOOTSTRAP_REPETITIONS = 10_000
V07_PRACTICAL_THRESHOLD_PP = 5.0
V07_STRATUM_POPULATION = {"fs1": 55, "fs2": 45, "fs3": 56, "fs4": 24}
V07_STRATA = tuple(V07_STRATUM_POPULATION)
V07_POPULATION_SIZE = sum(V07_STRATUM_POPULATION.values())
_CONTRAST_ARMS = (
    ("raw_feedback_loop", "engineered_loop"),
    ("independent_verified_sampling", "raw_feedback_loop"),
    ("direct", "independent_verified_sampling"),
)


@dataclass(frozen=True, slots=True)
class V07ArmSummary:
    model_id: str
    arm: str
    task_count: int
    strict_successes: int
    weighted_strict_success_rate: float
    unresolved_handoffs: int
    total_model_prompts: int
    total_initial_prompts: int
    total_independent_sample_prompts: int
    total_feedback_followups: int
    total_logical_prompt_tokens: int
    total_logical_completion_tokens: int
    total_environment_actions: int
    total_replayed_environment_actions: int
    total_physical_environment_actions: int
    total_evaluator_calls: int
    total_checkpoint_creates: int
    total_checkpoint_restores: int
    total_safety_recoveries: int
    total_parser_failures: int
    total_human_prompts: int
    total_active_wall_time_ns: int
    weighted_mean_model_prompts: float
    weighted_mean_logical_tokens: float
    weighted_mean_environment_actions: float
    weighted_mean_replayed_environment_actions: float
    weighted_mean_physical_environment_actions: float
    weighted_mean_active_wall_seconds: float


@dataclass(frozen=True, slots=True)
class V07ContrastSummary:
    model_id: str
    role: str
    inference_scope: str
    baseline_arm: str
    candidate_arm: str
    paired_task_count: int
    point_estimate_pp: float
    bootstrap_ci_low_pp: float
    bootstrap_ci_high_pp: float
    rescued: int
    regressed: int
    unchanged: int
    net_rescues: int
    exact_mcnemar_p_value: float
    weighted_model_prompt_delta: float
    weighted_logical_token_delta: float
    weighted_physical_environment_action_delta: float
    weighted_active_wall_seconds_delta: float
    unresolved_handoff_delta: float
    avoided_unresolved_handoffs: int
    decision_classification: str
    positive_result: bool


@dataclass(frozen=True, slots=True)
class V07EffectivenessAnalysis:
    analysis_revision: str
    bootstrap_seed: int
    bootstrap_repetitions: int
    campaign_log_sha256: str
    schedule_sha256: str
    episode_log_set_sha256: str
    verified_episode_count: int
    conclusion_scope: str
    primary_claim_status: str
    cross_model_claim_status: str
    interpretation: str
    total_model_prompts: int
    total_initial_prompts: int
    total_independent_sample_prompts: int
    total_feedback_followups: int
    total_automatic_followups: int
    total_logical_prompt_tokens: int
    total_logical_completion_tokens: int
    total_environment_actions: int
    total_replayed_environment_actions: int
    total_physical_environment_actions: int
    total_human_prompts: int
    total_unresolved_handoffs: int
    total_active_wall_time_ns: int
    arm_summaries: tuple[V07ArmSummary, ...]
    contrasts: tuple[V07ContrastSummary, ...]
    analysis_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "analysis_revision": self.analysis_revision,
            "analysis_sha256": self.analysis_sha256,
            "arm_summaries": [asdict(item) for item in self.arm_summaries],
            "bootstrap_repetitions": self.bootstrap_repetitions,
            "bootstrap_seed": self.bootstrap_seed,
            "campaign_log_sha256": self.campaign_log_sha256,
            "conclusion_scope": self.conclusion_scope,
            "contrasts": [asdict(item) for item in self.contrasts],
            "cross_model_claim_status": self.cross_model_claim_status,
            "episode_log_set_sha256": self.episode_log_set_sha256,
            "interpretation": self.interpretation,
            "primary_claim_status": self.primary_claim_status,
            "schedule_sha256": self.schedule_sha256,
            "schema": "edgeloopbench.intercode-v0.7-effectiveness-analysis.v4",
            "total_active_wall_time_ns": self.total_active_wall_time_ns,
            "total_automatic_followups": self.total_automatic_followups,
            "total_feedback_followups": self.total_feedback_followups,
            "total_human_prompts": self.total_human_prompts,
            "total_independent_sample_prompts": (
                self.total_independent_sample_prompts
            ),
            "total_initial_prompts": self.total_initial_prompts,
            "total_logical_completion_tokens": (
                self.total_logical_completion_tokens
            ),
            "total_logical_prompt_tokens": self.total_logical_prompt_tokens,
            "total_environment_actions": self.total_environment_actions,
            "total_replayed_environment_actions": (
                self.total_replayed_environment_actions
            ),
            "total_physical_environment_actions": (
                self.total_physical_environment_actions
            ),
            "total_model_prompts": self.total_model_prompts,
            "total_unresolved_handoffs": self.total_unresolved_handoffs,
            "verified_episode_count": self.verified_episode_count,
        }


def analyze_v07_effectiveness(
    evidence: VerifiedCampaignEvidence,
) -> V07EffectivenessAnalysis:
    """Analyze only the exact complete publication-evidence type."""

    if type(evidence) is not VerifiedCampaignEvidence:
        raise ValueError("v0.7 analysis requires VerifiedCampaignEvidence")
    rows = evidence.matrix.episodes
    if evidence.verified_episode_count != CAMPAIGN_EPISODE_COUNT or len(
        rows
    ) != CAMPAIGN_EPISODE_COUNT:
        raise ValueError("v0.7 verified campaign evidence is incomplete")
    _validate_matrix(rows)

    arm_summaries = tuple(
        _summarize_arm(
            model_id,
            arm,
            tuple(
                row
                for row in rows
                if row.episode.model_id == model_id and row.episode.arm == arm
            ),
        )
        for model_id in CAMPAIGN_MODELS
        for arm in CAMPAIGN_ARMS
    )
    contrasts: list[V07ContrastSummary] = []
    for model_id in CAMPAIGN_MODELS:
        model_rows = tuple(row for row in rows if row.episode.model_id == model_id)
        for baseline, candidate in _CONTRAST_ARMS:
            if (baseline, candidate) == _CONTRAST_ARMS[0]:
                role = "primary" if model_id == CAMPAIGN_MODELS[0] else "replication"
            else:
                role = "mechanism"
            contrasts.append(
                _summarize_contrast(
                    model_id=model_id,
                    role=role,
                    baseline_arm=baseline,
                    candidate_arm=candidate,
                    rows=model_rows,
                )
            )

    primary = next(item for item in contrasts if item.role == "primary")
    replication = next(item for item in contrasts if item.role == "replication")
    if primary.positive_result:
        primary_claim = "supported"
    elif primary.decision_classification == "negative_harm_signal":
        primary_claim = "negative_harm_signal"
    else:
        primary_claim = "not_supported"
    if primary.positive_result and replication.point_estimate_pp > 0:
        cross_model = (
            "strongly_replicated"
            if replication.bootstrap_ci_low_pp > 0
            else "directionally_replicated"
        )
    else:
        cross_model = "not_supported"
    if primary.positive_result:
        interpretation = "supported_agent_effectiveness_only"
    elif primary.decision_classification == "negative_harm_signal":
        interpretation = "evidence_frozen_engineered_package_harmful_vs_raw"
    elif primary.decision_classification == "positive_below_practical_threshold":
        interpretation = "positive_below_practical_threshold"
    else:
        interpretation = "inconclusive_not_equivalence"

    values: dict[str, object] = {
        "analysis_revision": V07_ANALYSIS_REVISION,
        "bootstrap_seed": V07_BOOTSTRAP_SEED,
        "bootstrap_repetitions": V07_BOOTSTRAP_REPETITIONS,
        "campaign_log_sha256": evidence.campaign_log_sha256,
        "schedule_sha256": evidence.schedule_sha256,
        "episode_log_set_sha256": evidence.episode_log_set_sha256,
        "verified_episode_count": evidence.verified_episode_count,
        "conclusion_scope": "agent_effectiveness_only",
        "primary_claim_status": primary_claim,
        "cross_model_claim_status": cross_model,
        "interpretation": interpretation,
        "total_model_prompts": sum(row.result.model_calls for row in rows),
        "total_initial_prompts": sum(row.result.initial_prompts for row in rows),
        "total_independent_sample_prompts": sum(
            row.result.independent_sample_prompts for row in rows
        ),
        "total_feedback_followups": sum(
            row.result.feedback_followups for row in rows
        ),
        "total_automatic_followups": sum(
            row.result.independent_sample_prompts + row.result.feedback_followups
            for row in rows
        ),
        "total_logical_prompt_tokens": sum(
            row.result.logical_prompt_tokens for row in rows
        ),
        "total_logical_completion_tokens": sum(
            row.result.logical_completion_tokens for row in rows
        ),
        "total_environment_actions": sum(
            row.result.environment_actions for row in rows
        ),
        "total_replayed_environment_actions": sum(
            row.result.replayed_environment_actions for row in rows
        ),
        "total_physical_environment_actions": sum(
            row.result.environment_actions + row.result.replayed_environment_actions
            for row in rows
        ),
        "total_human_prompts": sum(row.result.human_prompts for row in rows),
        "total_unresolved_handoffs": sum(
            not row.result.strict_success for row in rows
        ),
        "total_active_wall_time_ns": sum(row.active_wall_time_ns for row in rows),
        "arm_summaries": arm_summaries,
        "contrasts": tuple(contrasts),
    }
    core = _analysis_record(values)
    analysis_sha256 = "sha256:" + hashlib.sha256(_canonical_json(core)).hexdigest()
    return V07EffectivenessAnalysis(
        **values,  # type: ignore[arg-type]
        analysis_sha256=analysis_sha256,
    )


def _validate_matrix(rows: tuple[CampaignEpisodeResult, ...]) -> None:
    seen: set[tuple[str, str, str, int]] = set()
    for row in rows:
        if type(row) is not CampaignEpisodeResult:
            raise ValueError("v0.7 evidence matrix row type is invalid")
        key = (
            row.episode.model_id,
            row.episode.task_id,
            row.episode.arm,
            row.episode.seed,
        )
        if key in seen:
            raise ValueError("v0.7 evidence matrix repeats an episode")
        seen.add(key)
        if row.result.run_status == "infrastructure_error":
            raise ValueError("v0.7 evidence matrix contains an invalid episode")
        if row.result.official_success or row.result.human_prompts:
            raise ValueError("v0.7 evidence matrix violates frozen accounting")


def _summarize_arm(
    model_id: str,
    arm: str,
    rows: tuple[CampaignEpisodeResult, ...],
) -> V07ArmSummary:
    if len(rows) != 30:
        raise ValueError("v0.7 arm evidence must contain exactly 30 tasks")
    logical_tokens = lambda row: (
        row.result.logical_prompt_tokens + row.result.logical_completion_tokens
    )
    return V07ArmSummary(
        model_id=model_id,
        arm=arm,
        task_count=len(rows),
        strict_successes=sum(row.result.strict_success for row in rows),
        weighted_strict_success_rate=_weighted_mean(
            rows, lambda row: float(row.result.strict_success)
        ),
        unresolved_handoffs=sum(not row.result.strict_success for row in rows),
        total_model_prompts=sum(row.result.model_calls for row in rows),
        total_initial_prompts=sum(row.result.initial_prompts for row in rows),
        total_independent_sample_prompts=sum(
            row.result.independent_sample_prompts for row in rows
        ),
        total_feedback_followups=sum(row.result.feedback_followups for row in rows),
        total_logical_prompt_tokens=sum(
            row.result.logical_prompt_tokens for row in rows
        ),
        total_logical_completion_tokens=sum(
            row.result.logical_completion_tokens for row in rows
        ),
        total_environment_actions=sum(
            row.result.environment_actions for row in rows
        ),
        total_replayed_environment_actions=sum(
            row.result.replayed_environment_actions for row in rows
        ),
        total_physical_environment_actions=sum(
            row.result.environment_actions + row.result.replayed_environment_actions
            for row in rows
        ),
        total_evaluator_calls=sum(row.result.evaluator_calls for row in rows),
        total_checkpoint_creates=sum(
            row.result.checkpoint_creates for row in rows
        ),
        total_checkpoint_restores=sum(
            row.result.checkpoint_restores for row in rows
        ),
        total_safety_recoveries=sum(
            row.result.safety_recoveries for row in rows
        ),
        total_parser_failures=sum(row.result.parser_failures for row in rows),
        total_human_prompts=sum(row.result.human_prompts for row in rows),
        total_active_wall_time_ns=sum(row.active_wall_time_ns for row in rows),
        weighted_mean_model_prompts=_weighted_mean(
            rows, lambda row: float(row.result.model_calls)
        ),
        weighted_mean_logical_tokens=_weighted_mean(
            rows, lambda row: float(logical_tokens(row))
        ),
        weighted_mean_environment_actions=_weighted_mean(
            rows,
            lambda row: float(row.result.environment_actions),
        ),
        weighted_mean_replayed_environment_actions=_weighted_mean(
            rows,
            lambda row: float(row.result.replayed_environment_actions),
        ),
        weighted_mean_physical_environment_actions=_weighted_mean(
            rows,
            lambda row: float(
                row.result.environment_actions
                + row.result.replayed_environment_actions
            ),
        ),
        weighted_mean_active_wall_seconds=_weighted_mean(
            rows, lambda row: row.active_wall_time_ns / 1_000_000_000
        ),
    )


def _summarize_contrast(
    *,
    model_id: str,
    role: str,
    baseline_arm: str,
    candidate_arm: str,
    rows: tuple[CampaignEpisodeResult, ...],
) -> V07ContrastSummary:
    baseline = {
        row.episode.task_id: row for row in rows if row.episode.arm == baseline_arm
    }
    candidate = {
        row.episode.task_id: row for row in rows if row.episode.arm == candidate_arm
    }
    if set(baseline) != set(candidate) or len(baseline) != 30:
        raise ValueError("v0.7 contrast lacks 30 complete paired tasks")
    task_ids = tuple(sorted(baseline))
    deltas = {
        task_id: int(candidate[task_id].result.strict_success)
        - int(baseline[task_id].result.strict_success)
        for task_id in task_ids
    }
    point = 100 * _weighted_task_values(deltas)
    ci_low, ci_high = _stratified_bootstrap(deltas)
    rescued = sum(delta == 1 for delta in deltas.values())
    regressed = sum(delta == -1 for delta in deltas.values())
    p_value = _exact_mcnemar(rescued, regressed)
    positive_pattern = bool(
        point >= V07_PRACTICAL_THRESHOLD_PP
        and ci_low > 0.0
        and rescued > regressed
        and p_value < 0.05
    )
    negative_pattern = bool(
        point <= -V07_PRACTICAL_THRESHOLD_PP
        and ci_high < 0.0
        and regressed > rescued
        and p_value < 0.05
    )
    if positive_pattern:
        classification = "positive_threshold_met"
    elif negative_pattern:
        classification = "negative_harm_signal"
    elif 0.0 < point < V07_PRACTICAL_THRESHOLD_PP and ci_low > 0.0:
        classification = "positive_below_practical_threshold"
    else:
        classification = "inconclusive_not_equivalence"
    inference_scope = {
        "primary": "confirmatory_primary",
        "replication": "unadjusted_replication",
        "mechanism": "unadjusted_descriptive",
    }[role]
    prompt_deltas = {
        task_id: candidate[task_id].result.model_calls
        - baseline[task_id].result.model_calls
        for task_id in task_ids
    }
    token_deltas = {
        task_id: (
            candidate[task_id].result.logical_prompt_tokens
            + candidate[task_id].result.logical_completion_tokens
            - baseline[task_id].result.logical_prompt_tokens
            - baseline[task_id].result.logical_completion_tokens
        )
        for task_id in task_ids
    }
    physical_environment_action_deltas = {
        task_id: (
            candidate[task_id].result.environment_actions
            + candidate[task_id].result.replayed_environment_actions
            - baseline[task_id].result.environment_actions
            - baseline[task_id].result.replayed_environment_actions
        )
        for task_id in task_ids
    }
    wall_deltas = {
        task_id: (
            candidate[task_id].active_wall_time_ns
            - baseline[task_id].active_wall_time_ns
        )
        / 1_000_000_000
        for task_id in task_ids
    }
    return V07ContrastSummary(
        model_id=model_id,
        role=role,
        inference_scope=inference_scope,
        baseline_arm=baseline_arm,
        candidate_arm=candidate_arm,
        paired_task_count=len(task_ids),
        point_estimate_pp=point,
        bootstrap_ci_low_pp=ci_low,
        bootstrap_ci_high_pp=ci_high,
        rescued=rescued,
        regressed=regressed,
        unchanged=len(task_ids) - rescued - regressed,
        net_rescues=rescued - regressed,
        exact_mcnemar_p_value=p_value,
        weighted_model_prompt_delta=_weighted_task_values(prompt_deltas),
        weighted_logical_token_delta=_weighted_task_values(token_deltas),
        weighted_physical_environment_action_delta=_weighted_task_values(
            physical_environment_action_deltas
        ),
        weighted_active_wall_seconds_delta=_weighted_task_values(wall_deltas),
        unresolved_handoff_delta=-_weighted_task_values(deltas),
        avoided_unresolved_handoffs=rescued,
        decision_classification=classification,
        positive_result=positive_pattern and role in {"primary", "replication"},
    )


def _stratum(task_id: str) -> str:
    value = task_id.split("-", 2)[1]
    if value not in V07_STRATUM_POPULATION:
        raise ValueError("v0.7 task ID has an unknown stratum")
    return value


def _weighted_mean(
    rows: tuple[CampaignEpisodeResult, ...],
    value: Callable[[CampaignEpisodeResult], float],
) -> float:
    by_stratum = {
        stratum: tuple(row for row in rows if _stratum(row.episode.task_id) == stratum)
        for stratum in V07_STRATA
    }
    if any(not items for items in by_stratum.values()):
        raise ValueError("v0.7 weighted estimate lacks a sampled stratum")
    return math.fsum(
        V07_STRATUM_POPULATION[stratum]
        / V07_POPULATION_SIZE
        * math.fsum(value(row) for row in by_stratum[stratum])
        / len(by_stratum[stratum])
        for stratum in V07_STRATA
    )


def _weighted_task_values(values: Mapping[str, int | float]) -> float:
    grouped = {
        stratum: tuple(
            float(value)
            for task_id, value in values.items()
            if _stratum(task_id) == stratum
        )
        for stratum in V07_STRATA
    }
    if any(not items for items in grouped.values()):
        raise ValueError("v0.7 weighted task values lack a sampled stratum")
    return math.fsum(
        V07_STRATUM_POPULATION[stratum]
        / V07_POPULATION_SIZE
        * math.fsum(grouped[stratum])
        / len(grouped[stratum])
        for stratum in V07_STRATA
    )


def _stratified_bootstrap(
    deltas: Mapping[str, int],
) -> tuple[float, float]:
    grouped = {
        stratum: tuple(
            float(delta)
            for task_id, delta in sorted(deltas.items())
            if _stratum(task_id) == stratum
        )
        for stratum in V07_STRATA
    }
    if any(not values for values in grouped.values()):
        raise ValueError("v0.7 bootstrap lacks a sampled stratum")
    generator = random.Random(V07_BOOTSTRAP_SEED)
    estimates: list[float] = []
    for _ in range(V07_BOOTSTRAP_REPETITIONS):
        estimate = math.fsum(
            V07_STRATUM_POPULATION[stratum]
            / V07_POPULATION_SIZE
            * math.fsum(
                values[generator.randrange(len(values))]
                for _ in range(len(values))
            )
            / len(values)
            for stratum, values in grouped.items()
        )
        estimates.append(100 * estimate)
    estimates.sort()
    return (
        estimates[int(0.025 * V07_BOOTSTRAP_REPETITIONS)],
        estimates[int(0.975 * V07_BOOTSTRAP_REPETITIONS) - 1],
    )


def _exact_mcnemar(rescued: int, regressed: int) -> float:
    discordant = rescued + regressed
    if discordant == 0:
        return 1.0
    smaller = min(rescued, regressed)
    tail = math.fsum(
        math.comb(discordant, index) for index in range(smaller + 1)
    )
    return min(1.0, 2.0 * tail / (2**discordant))


def _analysis_record(values: Mapping[str, object]) -> dict[str, object]:
    return {
        **{
            key: value
            for key, value in values.items()
            if key not in {"arm_summaries", "contrasts"}
        },
        "arm_summaries": [asdict(item) for item in values["arm_summaries"]],  # type: ignore[arg-type]
        "contrasts": [asdict(item) for item in values["contrasts"]],  # type: ignore[arg-type]
        "schema": "edgeloopbench.intercode-v0.7-effectiveness-analysis.v4",
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
