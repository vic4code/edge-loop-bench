from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256

from edgeloopbench import intercode_campaign_evidence as evidence_module
from edgeloopbench.interactive_controller import InteractiveResult
from edgeloopbench.intercode_campaign_evidence import VerifiedCampaignEvidence
from edgeloopbench.intercode_campaign_ledger import (
    CAMPAIGN_MODELS,
    CAMPAIGN_TASK_IDS,
    CampaignEpisode,
    CampaignEpisodeResult,
    CampaignMatrix,
    CampaignSpec,
)
from edgeloopbench.intercode_host_safety import HostSafetySample
from edgeloopbench.intercode_v07_analysis import (
    analyze_v07_effectiveness,
)


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def host_sample(index: int, *, after: bool) -> HostSafetySample:
    monotonic = index * 10_000_000 + (2_000_000 if after else 0)
    return HostSafetySample(
        captured_unix_ns=1_800_000_000_000_000_000 + monotonic,
        captured_monotonic_ns=monotonic,
        boot_time_unix_microseconds=1_700_000_000_000_000,
        on_ac_power=True,
        low_power_mode_enabled=False,
        vm_pressure_level=1,
        free_memory_percent=50,
        swap_used_bytes=0,
        thermal_warning=False,
        performance_warning=False,
        disk_free_bytes=64 << 30,
        resident_models=(),
        running_container_ids=(),
    )


def result_for(episode: CampaignEpisode, strict_success: bool) -> InteractiveResult:
    calls = 1 if episode.arm == "direct" else 4
    independent = calls - 1 if episode.arm == "independent_verified_sampling" else 0
    feedback = calls - 1 if episode.arm in {"raw_feedback_loop", "engineered_loop"} else 0
    return InteractiveResult(
        run_status="completed" if episode.arm == "direct" else "budget_exhausted",
        official_success=False,
        strict_success=strict_success,
        stop_reason=(
            "direct_complete" if episode.arm == "direct" else "attempt_budget_exhausted"
        ),
        attempts=calls,
        model_calls=calls,
        logical_prompt_tokens=100 * calls,
        logical_completion_tokens=10 * calls,
        environment_actions=calls,
        replayed_environment_actions=0,
        evaluator_calls=calls + 1,
        checkpoint_creates=calls,
        checkpoint_restores=0,
        safety_recoveries=0,
        parser_failures=0,
        initial_prompts=1,
        independent_sample_prompts=independent,
        feedback_followups=feedback,
        human_prompts=0,
    )


def verified_evidence(
    success: Callable[[CampaignEpisode], bool],
) -> VerifiedCampaignEvidence:
    spec = CampaignSpec(CAMPAIGN_TASK_IDS).bind(digest("study-binding"))
    rows = tuple(
        CampaignEpisodeResult(
            episode=episode,
            result=result_for(episode, bool(success(episode))),
            execution_authority_sha256=spec.study_binding_sha256,
            controller_log_sha256=digest(f"log-{episode.episode_index}"),
            active_wall_time_ns=1_000_000_000,
            before_host_sample=host_sample(episode.episode_index, after=False),
            after_host_sample=host_sample(episode.episode_index, after=True),
        )
        for episode in spec.episodes
    )
    return VerifiedCampaignEvidence(
        CampaignMatrix(rows),
        digest("campaign-log"),
        spec.study_binding_sha256,
        spec.schedule_sha256,
        digest("episode-log-set"),
        tuple(
            (model_id, digest(f"tokenizer-{model_id}"))
            for model_id in CAMPAIGN_MODELS
        ),
        240,
        _authority=evidence_module._VERIFICATION_AUTHORITY,
    )


class InterCodeV07AnalysisTests(unittest.TestCase):
    def test_replay_actions_are_distinct_and_included_in_physical_totals(self) -> None:
        evidence = verified_evidence(lambda _episode: False)
        rows = list(evidence.matrix.episodes)
        index = next(
            index
            for index, row in enumerate(rows)
            if row.episode.model_id == "qwen3.5:4b"
            and row.episode.arm == "engineered_loop"
        )
        rows[index] = replace(
            rows[index],
            result=replace(
                rows[index].result,
                replayed_environment_actions=2,
                checkpoint_restores=1,
            ),
        )
        evidence = VerifiedCampaignEvidence(
            CampaignMatrix(tuple(rows)),
            evidence.campaign_log_sha256,
            evidence.study_binding_sha256,
            evidence.schedule_sha256,
            evidence.episode_log_set_sha256,
            evidence.tokenizer_artifacts_by_model,
            evidence.verified_episode_count,
            _authority=evidence_module._VERIFICATION_AUTHORITY,
        )

        analysis = analyze_v07_effectiveness(evidence)
        qwen_engineered = next(
            item
            for item in analysis.arm_summaries
            if item.model_id == "qwen3.5:4b" and item.arm == "engineered_loop"
        )

        self.assertEqual(analysis.total_environment_actions, 780)
        self.assertEqual(analysis.total_replayed_environment_actions, 2)
        self.assertEqual(analysis.total_physical_environment_actions, 782)
        self.assertEqual(qwen_engineered.total_environment_actions, 120)
        self.assertEqual(qwen_engineered.total_replayed_environment_actions, 2)
        self.assertEqual(qwen_engineered.total_physical_environment_actions, 122)
        self.assertAlmostEqual(
            qwen_engineered.weighted_mean_physical_environment_actions,
            4.0 + (55 / 180 / 9 * 2),
        )
        primary = next(item for item in analysis.contrasts if item.role == "primary")
        self.assertAlmostEqual(
            primary.weighted_physical_environment_action_delta,
            55 / 180 / 9 * 2,
        )

    def test_weighted_primary_statistics_and_prompt_handoff_accounting(self) -> None:
        first_phi_fs2 = next(
            task_id for task_id in CAMPAIGN_TASK_IDS if task_id.startswith("bash-fs2-")
        )

        def success(episode: CampaignEpisode) -> bool:
            if episode.arm != "engineered_loop":
                return False
            if episode.model_id == "qwen3.5:4b":
                return episode.task_id.startswith("bash-fs2-")
            return episode.task_id == first_phi_fs2

        analysis = analyze_v07_effectiveness(verified_evidence(success))
        qwen_engineered = next(
            item
            for item in analysis.arm_summaries
            if item.model_id == "qwen3.5:4b" and item.arm == "engineered_loop"
        )
        primary = next(item for item in analysis.contrasts if item.role == "primary")

        self.assertAlmostEqual(qwen_engineered.weighted_strict_success_rate, 0.25)
        self.assertEqual(qwen_engineered.strict_successes, 8)
        self.assertEqual(primary.point_estimate_pp, 25.0)
        self.assertEqual(primary.bootstrap_ci_low_pp, 25.0)
        self.assertEqual(primary.bootstrap_ci_high_pp, 25.0)
        self.assertEqual((primary.rescued, primary.regressed), (8, 0))
        self.assertAlmostEqual(primary.exact_mcnemar_p_value, 0.0078125)
        self.assertTrue(primary.positive_result)
        self.assertEqual(primary.inference_scope, "confirmatory_primary")
        self.assertEqual(primary.decision_classification, "positive_threshold_met")
        self.assertEqual(analysis.primary_claim_status, "supported")
        self.assertEqual(
            analysis.cross_model_claim_status,
            "directionally_replicated",
        )
        self.assertEqual(analysis.total_model_prompts, 780)
        self.assertEqual(analysis.total_initial_prompts, 240)
        self.assertEqual(analysis.total_independent_sample_prompts, 180)
        self.assertEqual(analysis.total_feedback_followups, 360)
        self.assertEqual(analysis.total_automatic_followups, 540)
        self.assertEqual(analysis.total_human_prompts, 0)
        self.assertEqual(analysis.total_unresolved_handoffs, 231)
        self.assertEqual(analysis.total_active_wall_time_ns, 240_000_000_000)
        self.assertEqual(analysis.campaign_log_sha256, digest("campaign-log"))
        self.assertEqual(
            analysis.to_dict()["campaign_log_sha256"], digest("campaign-log")
        )

    def test_null_primary_is_not_mislabeled_as_equivalence_or_uplift(self) -> None:
        fs2 = tuple(
            task_id for task_id in CAMPAIGN_TASK_IDS if task_id.startswith("bash-fs2-")
        )

        def success(episode: CampaignEpisode) -> bool:
            if episode.model_id != "qwen3.5:4b":
                return False
            if episode.arm == "engineered_loop":
                return episode.task_id == fs2[0]
            if episode.arm == "raw_feedback_loop":
                return episode.task_id == fs2[1]
            return False

        first = analyze_v07_effectiveness(verified_evidence(success))
        second = analyze_v07_effectiveness(verified_evidence(success))
        primary = next(item for item in first.contrasts if item.role == "primary")

        self.assertEqual(primary.point_estimate_pp, 0.0)
        self.assertEqual((primary.rescued, primary.regressed), (1, 1))
        self.assertEqual(primary.exact_mcnemar_p_value, 1.0)
        self.assertFalse(primary.positive_result)
        self.assertEqual(first.primary_claim_status, "not_supported")
        self.assertEqual(first.cross_model_claim_status, "not_supported")
        self.assertEqual(first.interpretation, "inconclusive_not_equivalence")
        self.assertEqual(first.analysis_sha256, second.analysis_sha256)

    def test_above_threshold_without_inferential_support_is_not_below_threshold(self) -> None:
        fs2 = tuple(
            task_id for task_id in CAMPAIGN_TASK_IDS if task_id.startswith("bash-fs2-")
        )

        def success(episode: CampaignEpisode) -> bool:
            return bool(
                episode.model_id == "qwen3.5:4b"
                and episode.arm == "engineered_loop"
                and episode.task_id in fs2[:4]
            )

        analysis = analyze_v07_effectiveness(verified_evidence(success))
        primary = next(item for item in analysis.contrasts if item.role == "primary")

        self.assertEqual(primary.point_estimate_pp, 12.5)
        self.assertGreater(primary.bootstrap_ci_low_pp, 0.0)
        self.assertEqual(primary.exact_mcnemar_p_value, 0.125)
        self.assertFalse(primary.positive_result)
        self.assertEqual(
            primary.decision_classification,
            "inconclusive_not_equivalence",
        )

    def test_clear_negative_primary_is_reported_as_harm_not_inconclusive(self) -> None:
        def success(episode: CampaignEpisode) -> bool:
            return (
                episode.model_id == "qwen3.5:4b"
                and episode.arm == "raw_feedback_loop"
                and episode.task_id.startswith("bash-fs2-")
            )

        analysis = analyze_v07_effectiveness(verified_evidence(success))
        primary = next(item for item in analysis.contrasts if item.role == "primary")

        self.assertEqual(primary.point_estimate_pp, -25.0)
        self.assertEqual(primary.bootstrap_ci_high_pp, -25.0)
        self.assertEqual((primary.rescued, primary.regressed), (0, 8))
        self.assertAlmostEqual(primary.exact_mcnemar_p_value, 0.0078125)
        self.assertFalse(primary.positive_result)
        self.assertEqual(primary.decision_classification, "negative_harm_signal")
        self.assertEqual(analysis.primary_claim_status, "negative_harm_signal")
        self.assertEqual(
            analysis.interpretation,
            "evidence_frozen_engineered_package_harmful_vs_raw",
        )

        unadjusted = [item for item in analysis.contrasts if item.role != "primary"]
        self.assertTrue(unadjusted)
        self.assertTrue(
            all(item.inference_scope.startswith("unadjusted_") for item in unadjusted)
        )

    def test_rejects_a_bare_or_forged_campaign_matrix(self) -> None:
        evidence = verified_evidence(lambda _episode: False)
        with self.assertRaisesRegex(ValueError, "VerifiedCampaignEvidence"):
            analyze_v07_effectiveness(evidence.matrix)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
