from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path

from edgeloopbench import intercode_campaign_ledger as ledger
from edgeloopbench.interactive_controller import (
    INTERACTIVE_CONTROLLER_REVISION,
    InteractiveResult,
)
from edgeloopbench.intercode_campaign_evidence import (
    CampaignEvidenceError,
    VerifiedCampaignEvidence,
    verify_campaign_evidence,
)
from edgeloopbench.intercode_campaign_ledger import (
    CAMPAIGN_TASK_IDS,
    CampaignEpisode,
    CampaignSpec,
)
from edgeloopbench.intercode_host_safety import (
    DockerDaemonIdentity,
    HostSafetySample,
    ResidentModel,
)
from edgeloopbench.intercode_replay_environment import (
    V07_STRICT_REPLAY_EVALUATOR_SHA256,
)
from edgeloopbench.journal import GENESIS_EVENT_SHA256, canonical_event_bytes
from edgeloopbench.model_adapter import (
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
)


STUDY_BINDING_SHA256 = "sha256:" + "7" * 64
DOCKER_DAEMON_IDENTITY = DockerDaemonIdentity(
    binary_sha256="sha256:" + sha256(b"docker-binary").hexdigest(),
    endpoint_sha256="sha256:" + sha256(b"docker-endpoint").hexdigest(),
    client_version="28.3.3",
    server_version="28.3.3",
)
TOKENIZER_ARTIFACT_BY_MODEL = {
    model_id: "sha256:" + sha256(b"pinned tokenizer").hexdigest()
    for model_id in (QWEN35_RAW_PROFILE.model, PHI4_MINI_RAW_PROFILE.model)
}


EpisodeEventMutator = Callable[
    [CampaignEpisode, list[dict[str, object]]],
    list[dict[str, object]],
]
ResultMutator = Callable[[CampaignEpisode, InteractiveResult], InteractiveResult]
RootMutator = Callable[[CampaignEpisode, str], str]
HostSampleMutator = Callable[
    [CampaignEpisode, HostSafetySample, HostSafetySample],
    tuple[HostSafetySample, HostSafetySample],
]


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def _profile_for(model_id: str):  # type: ignore[no-untyped-def]
    if model_id == QWEN35_RAW_PROFILE.model:
        return QWEN35_RAW_PROFILE
    if model_id == PHI4_MINI_RAW_PROFILE.model:
        return PHI4_MINI_RAW_PROFILE
    raise AssertionError("unexpected campaign model")


def _identity(episode: CampaignEpisode) -> dict[str, object]:
    return {
        "task_id": episode.task_id,
        "strategy": episode.arm,
        "replicate_seed": episode.seed,
        "execution_authority_sha256": STUDY_BINDING_SHA256,
    }


def _episode_payloads(episode: CampaignEpisode) -> list[dict[str, object]]:
    common = _identity(episode)
    profile = _profile_for(episode.model_id)
    pair = f"{episode.model_id}-{episode.task_id}-{episode.seed}"
    prompt_sha256 = digest(f"prompt-{pair}")
    action_sha256 = digest(f"action-{pair}")
    output_sha256 = digest(f"output-{pair}")
    state_sha256 = digest(f"state-{pair}")
    scope = "attempt-1" if episode.arm == "independent_verified_sampling" else "episode"
    stop_reason = (
        "direct_complete"
        if episode.arm == "direct"
        else "action_pipeline_budget_exhausted"
    )
    return [
        {
            "type": "controller_started",
            **common,
            "controller_revision": INTERACTIVE_CONTROLLER_REVISION,
        },
        {
            "type": "model_preflighted",
            **common,
            "attempt": 1,
            "prompt_sha256": prompt_sha256,
            "prompt_tokens": 101,
            "token_ids_sha256": digest(f"tokens-{pair}"),
            "renderer_profile_sha256": profile.sha256,
            "tokenizer_artifact_sha256": TOKENIZER_ARTIFACT_BY_MODEL[
                episode.model_id
            ],
            "model_artifact_sha256": profile.model_artifact_sha256,
        },
        {
            "type": "model_requested",
            **common,
            "attempt": 1,
            "prompt_sha256": prompt_sha256,
            "logical_model_calls_after": 1,
            "logical_prompt_tokens_after": 101,
            "candidate_seed": episode.seed,
            "context_sha256": digest(f"context-{episode.episode_index}"),
            "max_output_tokens": 128,
        },
        {
            "type": "model_completed",
            **common,
            "attempt": 1,
            "response_sha256": digest(f"response-{pair}"),
            "prompt_tokens": 101,
            "completion_tokens": 17,
            "total_duration_ns": 1_000,
        },
        {
            "type": "environment_create_requested",
            **common,
            "attempt": 1,
            "scope": "attempt" if scope == "attempt-1" else "episode",
        },
        {
            "type": "environment_created",
            **common,
            "attempt": 1,
            "scope": "attempt" if scope == "attempt-1" else "episode",
        },
        {
            "type": "action_requested",
            **common,
            "attempt": 1,
            "action_sha256": action_sha256,
        },
        {
            "type": "action_completed",
            **common,
            "attempt": 1,
            "action_sha256": action_sha256,
            "output_sha256": output_sha256,
            "state_sha256": state_sha256,
            "exit_code": 0,
            "admissible": True,
            "state_changed": True,
            "policy_failure": None,
            "safety_recovery_performed": False,
        },
        {
            "type": "checkpoint_create_requested",
            **common,
            "attempt": 1,
        },
        {
            "type": "checkpoint_created",
            **common,
            "attempt": 1,
            "state_sha256": state_sha256,
        },
        {
            "type": "attempt_evaluation_requested",
            **common,
            "attempt": 1,
            "state_sha256": state_sha256,
        },
        {
            "type": "attempt_evaluated",
            **common,
            "attempt": 1,
            "reward": 0.8,
            "official_success": False,
            "evaluation_kind": "evaluator_derived",
        },
        {"type": "environment_close_requested", **common, "scope": scope},
        {"type": "environment_closed", **common, "scope": scope},
        {
            "type": "strict_evaluation_planned",
            **common,
            "selected_attempt": 1,
            "state_sha256": state_sha256,
        },
        {
            "type": "terminal_finalization_requested",
            **common,
            "selected_attempt": 1,
            "evaluation_kind": "evaluator_derived",
            "aborted": False,
            "remaining_evaluator_calls": 1,
        },
        {
            "type": "terminal_finalized",
            **common,
            "strict_evaluator_calls": 1,
            "posthoc_evaluator_calls": 0,
        },
        {
            "type": "strict_evaluation_completed",
            **common,
            "strict_success": False,
            "evaluator_sha256": V07_STRICT_REPLAY_EVALUATOR_SHA256,
        },
        {
            "type": "controller_stopped",
            **common,
            "stop_reason": stop_reason,
            "selected_attempt": 1,
            "official_success": False,
        },
    ]


def _result_for(episode: CampaignEpisode) -> InteractiveResult:
    stop_reason = (
        "direct_complete"
        if episode.arm == "direct"
        else "action_pipeline_budget_exhausted"
    )
    return InteractiveResult(
        run_status=(
            "completed" if episode.arm == "direct" else "budget_exhausted"
        ),
        official_success=False,
        strict_success=False,
        stop_reason=stop_reason,
        attempts=1,
        model_calls=1,
        logical_prompt_tokens=101,
        logical_completion_tokens=17,
        environment_actions=1,
        evaluator_calls=2,
        checkpoint_creates=1,
        checkpoint_restores=0,
        safety_recoveries=0,
        parser_failures=0,
        initial_prompts=1,
        independent_sample_prompts=0,
        feedback_followups=0,
        human_prompts=0,
    )


def _journal_bytes(payloads: list[dict[str, object]]) -> tuple[bytes, str]:
    previous = GENESIS_EVENT_SHA256
    records: list[dict[str, object]] = []
    for sequence, payload in enumerate(payloads, 1):
        record = {
            **payload,
            "sequence": sequence,
            "previous_event_sha256": previous,
        }
        event_sha256 = sha256(canonical_event_bytes(record)).hexdigest()
        record["event_sha256"] = event_sha256
        records.append(record)
        previous = event_sha256
    seal = {
        "type": "journal_sealed",
        "sealed_event_count": len(records),
        "sequence": len(records) + 1,
        "previous_event_sha256": previous,
    }
    seal["event_sha256"] = sha256(canonical_event_bytes(seal)).hexdigest()
    records.append(seal)
    payload = b"".join(
        json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
        for record in records
    )
    return payload, "sha256:" + str(seal["event_sha256"])


def _write_mode_0600(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _serialize_result(result: InteractiveResult) -> dict[str, object]:
    return {
        field.name: getattr(result, field.name)
        for field in dataclasses.fields(InteractiveResult)
    }


def _host_sample(episode: CampaignEpisode, *, after: bool) -> HostSafetySample:
    monotonic = episode.episode_index * 10_000_000 + (2_000_000 if after else 0)
    profile = _profile_for(episode.model_id)
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
        resident_models=(
            ResidentModel(
                episode.model_id,
                profile.model_manifest_sha256.removeprefix("sha256:"),
            ),
        ),
        running_container_ids=(),
        docker_daemon=DOCKER_DAEMON_IDENTITY,
    )


def _build_campaign(
    root: Path,
    *,
    event_mutator: EpisodeEventMutator | None = None,
    result_mutator: ResultMutator | None = None,
    root_mutator: RootMutator | None = None,
    host_sample_mutator: HostSampleMutator | None = None,
) -> tuple[Path, Path, CampaignSpec]:
    spec = CampaignSpec(task_ids=CAMPAIGN_TASK_IDS).bind(
        STUDY_BINDING_SHA256
    )
    root.mkdir(parents=True, exist_ok=True)
    episode_directory = root / "private-episodes"
    episode_directory.mkdir(mode=0o700)
    results: list[tuple[CampaignEpisode, InteractiveResult, str]] = []
    for episode in spec.episodes:
        payloads = _episode_payloads(episode)
        if event_mutator is not None:
            payloads = event_mutator(episode, payloads)
        log_bytes, log_root = _journal_bytes(payloads)
        _write_mode_0600(
            episode_directory / f"episode-{episode.episode_index:04d}.jsonl",
            log_bytes,
        )
        result = _result_for(episode)
        if result_mutator is not None:
            result = result_mutator(episode, result)
        if root_mutator is not None:
            log_root = root_mutator(episode, log_root)
        results.append((episode, result, log_root))

    campaign_payloads: list[dict[str, object]] = [ledger._declaration_event(spec)]
    for episode, result, log_root in results:
        before_host_sample = _host_sample(episode, after=False)
        after_host_sample = _host_sample(episode, after=True)
        if host_sample_mutator is not None:
            before_host_sample, after_host_sample = host_sample_mutator(
                episode,
                before_host_sample,
                after_host_sample,
            )
        identity = {
            "episode_index": episode.episode_index,
            "model_id": episode.model_id,
            "task_id": episode.task_id,
            "arm": episode.arm,
            "seed": episode.seed,
        }
        campaign_payloads.append({"type": "episode_intent", **identity})
        campaign_payloads.append(
            {
                "type": "episode_completed",
                **identity,
                "result": _serialize_result(result),
                "execution_authority_sha256": spec.study_binding_sha256,
                "controller_log_sha256": log_root,
                "active_wall_time_ns": 1_000_000 + episode.episode_index,
                "before_host_sample": before_host_sample.to_record(),
                "after_host_sample": after_host_sample.to_record(),
            }
        )
    campaign_bytes, _campaign_root = _journal_bytes(campaign_payloads)
    campaign_journal = root / "campaign.jsonl"
    _write_mode_0600(campaign_journal, campaign_bytes)
    return campaign_journal, episode_directory, spec


def _mutate_first_event(
    event_type: str,
    change: Callable[[dict[str, object]], None],
) -> EpisodeEventMutator:
    def mutate(
        episode: CampaignEpisode,
        payloads: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        if episode.episode_index == 1:
            target = next(item for item in payloads if item["type"] == event_type)
            change(target)
        return payloads

    return mutate


class InterCodeCampaignEvidenceTests(unittest.TestCase):
    def test_verifies_all_240_logs_and_returns_a_path_free_distinct_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(Path(temporary))

            evidence = verify_campaign_evidence(campaign, episodes, spec)

            self.assertIs(type(evidence), VerifiedCampaignEvidence)
            self.assertEqual(evidence.verified_episode_count, 240)
            self.assertEqual(
                evidence.study_binding_sha256,
                STUDY_BINDING_SHA256,
            )
            self.assertEqual(evidence.schedule_sha256, spec.schedule_sha256)
            self.assertEqual(len(evidence.matrix.episodes), 240)
            self.assertEqual(evidence.total_model_calls, 240)
            self.assertEqual(evidence.total_logical_prompt_tokens, 24_240)
            self.assertEqual(evidence.total_logical_completion_tokens, 4_080)
            self.assertEqual(evidence.total_human_prompts, 0)
            self.assertEqual(evidence.total_active_wall_time_ns, 240_028_920)
            self.assertRegex(evidence.campaign_log_sha256, r"^sha256:[0-9a-f]{64}$")
            serialized = json.dumps(dataclasses.asdict(evidence), sort_keys=True)
            self.assertNotIn(temporary, serialized)
            self.assertRegex(evidence.episode_log_set_sha256, r"^sha256:[0-9a-f]{64}$")

    def test_rejects_raw_journal_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(Path(temporary))
            target = episodes / "episode-0001.jsonl"
            with target.open("ab") as handle:
                handle.write(b'{"type":"tampered"}\n')

            with self.assertRaises(CampaignEvidenceError):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_campaign_to_episode_root_mismatch(self) -> None:
        def wrong_root(episode: CampaignEpisode, observed: str) -> str:
            return digest("wrong-root") if episode.episode_index == 1 else observed

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), root_mutator=wrong_root
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "root"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_wrong_renderer_or_model_profile(self) -> None:
        mutator = _mutate_first_event(
            "model_preflighted",
            lambda event: event.__setitem__(
                "renderer_profile_sha256", digest("wrong-renderer")
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "renderer"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_wrong_strict_evaluator_policy_sha(self) -> None:
        mutator = _mutate_first_event(
            "strict_evaluation_completed",
            lambda event: event.__setitem__(
                "evaluator_sha256", digest("wrong-strict-evaluator")
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "strict evaluator SHA"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_event_and_result_counter_disagreement(self) -> None:
        def mutate_result(
            episode: CampaignEpisode, result: InteractiveResult
        ) -> InteractiveResult:
            if episode.episode_index == 1:
                return dataclasses.replace(result, logical_prompt_tokens=102)
            return result

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), result_mutator=mutate_result
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "counter"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_infrastructure_invalid_event_in_completed_episode(self) -> None:
        def mutator(
            episode: CampaignEpisode,
            payloads: list[dict[str, object]],
        ) -> list[dict[str, object]]:
            if episode.episode_index != 1:
                return payloads
            for position, event in enumerate(payloads):
                if event["type"] == "model_completed":
                    payloads.insert(
                        position + 1,
                        {
                            "type": "infrastructure_invalid",
                            **_identity(episode),
                            "attempt": 1,
                            "reason": "prompt_token_telemetry_mismatch",
                            "preflight_prompt_tokens": 101,
                            "telemetry_prompt_tokens": 102,
                        },
                    )
                    return payloads
            raise AssertionError("fixture lacks model_completed")

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "infrastructure"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_run_status_not_derived_from_stop_reason(self) -> None:
        def mutate_result(
            episode: CampaignEpisode, result: InteractiveResult
        ) -> InteractiveResult:
            if episode.episode_index == 1:
                return dataclasses.replace(result, run_status="budget_exhausted")
            return result

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), result_mutator=mutate_result
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "run status"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_cross_arm_candidate_one_model_mismatch(self) -> None:
        mutator = _mutate_first_event(
            "model_completed",
            lambda event: event.__setitem__(
                "response_sha256", digest("different-first-response")
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "candidate-1"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_cross_arm_candidate_one_completion_token_mismatch(self) -> None:
        mutator = _mutate_first_event(
            "model_completed",
            lambda event: event.__setitem__("completion_tokens", 18),
        )

        def mutate_result(
            episode: CampaignEpisode, result: InteractiveResult
        ) -> InteractiveResult:
            if episode.episode_index == 1:
                return dataclasses.replace(result, logical_completion_tokens=18)
            return result

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary),
                event_mutator=mutator,
                result_mutator=mutate_result,
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "candidate-1"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_cross_arm_candidate_one_progress_mismatch(self) -> None:
        mutator = _mutate_first_event(
            "attempt_evaluated",
            lambda event: event.__setitem__("reward", 0.2),
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "candidate-1"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_cross_arm_candidate_one_tokenization_mismatch(self) -> None:
        mutator = _mutate_first_event(
            "model_preflighted",
            lambda event: event.__setitem__(
                "token_ids_sha256", digest("different-first-token-ids")
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "candidate-1"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_cross_arm_candidate_one_action_mismatch(self) -> None:
        def mutator(
            episode: CampaignEpisode,
            payloads: list[dict[str, object]],
        ) -> list[dict[str, object]]:
            if episode.episode_index == 1:
                replacement = digest("different-first-state")
                for event in payloads:
                    if event["type"] in {
                        "action_completed",
                        "checkpoint_created",
                        "attempt_evaluation_requested",
                        "strict_evaluation_planned",
                    }:
                        event["state_sha256"] = replacement
            return payloads

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "candidate-1"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_independent_sampling_with_shared_environment(self) -> None:
        def mutator(
            episode: CampaignEpisode,
            payloads: list[dict[str, object]],
        ) -> list[dict[str, object]]:
            if (
                episode.model_id == QWEN35_RAW_PROFILE.model
                and episode.task_id == CAMPAIGN_TASK_IDS[0]
                and episode.arm == "independent_verified_sampling"
            ):
                for event in payloads:
                    if event["type"] in {
                        "environment_create_requested",
                        "environment_created",
                    }:
                        event["scope"] = "episode"
                    elif event["type"] in {
                        "environment_close_requested",
                        "environment_closed",
                    }:
                        event["scope"] = "episode"
            return payloads

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "environment|scope"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_raw_command_or_gold_leak_even_with_a_valid_root(self) -> None:
        mutator = _mutate_first_event(
            "model_completed",
            lambda event: event.__setitem__("command", "cat /private/gold"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "forbidden|field"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_canonical_host_sample_that_fails_frozen_policy(self) -> None:
        def unsafe_before(
            episode: CampaignEpisode,
            before: HostSafetySample,
            after: HostSafetySample,
        ) -> tuple[HostSafetySample, HostSafetySample]:
            if episode.episode_index == 1:
                before = dataclasses.replace(before, low_power_mode_enabled=True)
            return before, after

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), host_sample_mutator=unsafe_before
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "host safety"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_campaign_rechain_for_safe_host_change_changes_evidence_root(self) -> None:
        def safe_change(
            episode: CampaignEpisode,
            before: HostSafetySample,
            after: HostSafetySample,
        ) -> tuple[HostSafetySample, HostSafetySample]:
            if episode.episode_index == 1:
                after = dataclasses.replace(after, free_memory_percent=49)
            return before, after

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_campaign, first_episodes, first_spec = _build_campaign(
                root / "first"
            )
            second_campaign, second_episodes, second_spec = _build_campaign(
                root / "second", host_sample_mutator=safe_change
            )

            first = verify_campaign_evidence(
                first_campaign, first_episodes, first_spec
            )
            second = verify_campaign_evidence(
                second_campaign, second_episodes, second_spec
            )

            self.assertNotEqual(first.campaign_log_sha256, second.campaign_log_sha256)
            self.assertNotEqual(
                first.episode_log_set_sha256,
                second.episode_log_set_sha256,
            )

    def test_rejects_episode_request_over_frozen_output_budget(self) -> None:
        mutator = _mutate_first_event(
            "model_requested",
            lambda event: event.__setitem__("max_output_tokens", 513),
        )
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary), event_mutator=mutator
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "output-token budget"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_non_0600_episode_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(Path(temporary))
            (episodes / "episode-0001.jsonl").chmod(0o640)

            with self.assertRaisesRegex(CampaignEvidenceError, "0600"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_non_0600_campaign_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(Path(temporary))
            campaign.chmod(0o640)

            with self.assertRaisesRegex(CampaignEvidenceError, "0600"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_symlink_episode_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(Path(temporary))
            target = episodes / "episode-0001.jsonl"
            target.unlink()
            os.symlink("episode-0002.jsonl", target)

            with self.assertRaisesRegex(CampaignEvidenceError, "regular|symlink"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_symlink_campaign_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(Path(temporary))
            real_campaign = campaign.with_name("real-campaign.jsonl")
            campaign.rename(real_campaign)
            os.symlink(real_campaign.name, campaign)

            with self.assertRaisesRegex(CampaignEvidenceError, "regular|symlink"):
                verify_campaign_evidence(campaign, episodes, spec)


if __name__ == "__main__":
    unittest.main()
