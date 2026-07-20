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
    candidate_seed,
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
            "replay_depth": 1,
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


def _duplicate_restore_payloads(
    episode: CampaignEpisode,
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Forge a rechained log with two restores on the same model attempt."""

    if not (
        episode.model_id == QWEN35_RAW_PROFILE.model
        and episode.task_id == CAMPAIGN_TASK_IDS[0]
        and episode.arm == "engineered_loop"
    ):
        return payloads
    common = _identity(episode)
    profile = _profile_for(episode.model_id)
    first_state = next(
        event["state_sha256"]
        for event in payloads
        if event["type"] == "checkpoint_created"
    )
    insertion = next(
        index
        for index, event in enumerate(payloads)
        if event["type"] == "environment_close_requested"
    )
    additions: list[dict[str, object]] = []
    cumulative_prompt_tokens = 101
    for attempt in (2, 3):
        prompt_sha256 = digest(f"duplicate-restore-prompt-{attempt}")
        action_sha256 = digest(f"duplicate-restore-action-{attempt}")
        state_sha256 = digest(f"duplicate-restore-state-{attempt}")
        cumulative_prompt_tokens += 101
        additions.extend(
            [
                {
                    "type": "model_preflighted",
                    **common,
                    "attempt": attempt,
                    "prompt_sha256": prompt_sha256,
                    "prompt_tokens": 101,
                    "token_ids_sha256": digest(
                        f"duplicate-restore-tokens-{attempt}"
                    ),
                    "renderer_profile_sha256": profile.sha256,
                    "tokenizer_artifact_sha256": TOKENIZER_ARTIFACT_BY_MODEL[
                        episode.model_id
                    ],
                    "model_artifact_sha256": profile.model_artifact_sha256,
                },
                {
                    "type": "model_requested",
                    **common,
                    "attempt": attempt,
                    "prompt_sha256": prompt_sha256,
                    "logical_model_calls_after": attempt,
                    "logical_prompt_tokens_after": cumulative_prompt_tokens,
                    "candidate_seed": candidate_seed(episode.seed, attempt),
                    "context_sha256": digest(
                        f"duplicate-restore-context-{attempt}"
                    ),
                    "max_output_tokens": 128,
                },
                {
                    "type": "model_completed",
                    **common,
                    "attempt": attempt,
                    "response_sha256": digest(
                        f"duplicate-restore-response-{attempt}"
                    ),
                    "prompt_tokens": 101,
                    "completion_tokens": 17,
                    "total_duration_ns": 1_000,
                },
                {
                    "type": "action_requested",
                    **common,
                    "attempt": attempt,
                    "action_sha256": action_sha256,
                },
                {
                    "type": "action_completed",
                    **common,
                    "attempt": attempt,
                    "action_sha256": action_sha256,
                    "output_sha256": digest(
                        f"duplicate-restore-output-{attempt}"
                    ),
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
                    "attempt": attempt,
                },
                {
                    "type": "checkpoint_created",
                    **common,
                    "attempt": attempt,
                    "state_sha256": state_sha256,
                    "replay_depth": 2,
                },
                {
                    "type": "attempt_evaluation_requested",
                    **common,
                    "attempt": attempt,
                    "state_sha256": state_sha256,
                },
                {
                    "type": "attempt_evaluated",
                    **common,
                    "attempt": attempt,
                    "reward": 0.2,
                    "official_success": False,
                    "evaluation_kind": "evaluator_derived",
                },
                {
                    "type": "checkpoint_restore_requested",
                    **common,
                    "attempt": attempt,
                    "target_attempt": 1,
                    "state_sha256": first_state,
                    "replay_depth": 1,
                },
                {
                    "type": "checkpoint_restored",
                    **common,
                    "attempt": attempt,
                    "target_attempt": 1,
                    "state_sha256": first_state,
                    "replay_depth": 1,
                    "replayed_environment_actions": 1,
                },
            ]
        )
        if attempt == 3:
            additions.extend(additions[-2:])
    return [*payloads[:insertion], *additions, *payloads[insertion:]]


def _raw_policy_recovery_payloads(
    episode: CampaignEpisode,
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Add one typed recovery that replays the first accepted raw action."""

    if not (
        episode.model_id == QWEN35_RAW_PROFILE.model
        and episode.task_id == CAMPAIGN_TASK_IDS[0]
        and episode.arm == "raw_feedback_loop"
    ):
        return payloads
    common = _identity(episode)
    profile = _profile_for(episode.model_id)
    first_state = next(
        event["state_sha256"]
        for event in payloads
        if event["type"] == "checkpoint_created"
    )
    prompt_sha256 = digest("raw-recovery-prompt-2")
    action_sha256 = digest("raw-recovery-action-2")
    additions = [
        {
            "type": "model_preflighted",
            **common,
            "attempt": 2,
            "prompt_sha256": prompt_sha256,
            "prompt_tokens": 101,
            "token_ids_sha256": digest("raw-recovery-tokens-2"),
            "renderer_profile_sha256": profile.sha256,
            "tokenizer_artifact_sha256": TOKENIZER_ARTIFACT_BY_MODEL[
                episode.model_id
            ],
            "model_artifact_sha256": profile.model_artifact_sha256,
        },
        {
            "type": "model_requested",
            **common,
            "attempt": 2,
            "prompt_sha256": prompt_sha256,
            "logical_model_calls_after": 2,
            "logical_prompt_tokens_after": 202,
            "candidate_seed": candidate_seed(episode.seed, 2),
            "context_sha256": digest("raw-recovery-context-2"),
            "max_output_tokens": 128,
        },
        {
            "type": "model_completed",
            **common,
            "attempt": 2,
            "response_sha256": digest("raw-recovery-response-2"),
            "prompt_tokens": 101,
            "completion_tokens": 17,
            "total_duration_ns": 1_000,
        },
        {
            "type": "action_requested",
            **common,
            "attempt": 2,
            "action_sha256": action_sha256,
        },
        {
            "type": "action_completed",
            **common,
            "attempt": 2,
            "action_sha256": action_sha256,
            "output_sha256": digest(
                "Command exceeded the sampled writable-layer safety limit."
            ),
            "state_sha256": first_state,
            "exit_code": None,
            "admissible": False,
            "state_changed": False,
            "policy_failure": "writable_layer_overflow",
            "safety_recovery_performed": True,
        },
        {
            "type": "safety_recovery_completed",
            **common,
            "attempt": 2,
            "state_sha256": first_state,
            "recovery_evidence_sha256": digest("raw-recovery-evidence-2"),
            "replayed_environment_actions": 1,
        },
        {
            "type": "attempt_defaulted",
            **common,
            "attempt": 2,
            "reward": 0.0,
            "official_success": False,
            "evaluation_kind": "action_policy_failure",
            "policy_failure": "writable_layer_overflow",
        },
        {
            "type": "model_preflighted",
            **common,
            "attempt": 3,
            "prompt_sha256": digest("raw-recovery-prompt-3"),
            "prompt_tokens": 101,
            "token_ids_sha256": digest("raw-recovery-tokens-3"),
            "renderer_profile_sha256": profile.sha256,
            "tokenizer_artifact_sha256": TOKENIZER_ARTIFACT_BY_MODEL[
                episode.model_id
            ],
            "model_artifact_sha256": profile.model_artifact_sha256,
        },
        {
            "type": "model_requested",
            **common,
            "attempt": 3,
            "prompt_sha256": digest("raw-recovery-prompt-3"),
            "logical_model_calls_after": 3,
            "logical_prompt_tokens_after": 303,
            "candidate_seed": candidate_seed(episode.seed, 3),
            "context_sha256": digest("raw-recovery-context-3"),
            "max_output_tokens": 128,
        },
        {
            "type": "model_completed",
            **common,
            "attempt": 3,
            "response_sha256": digest("raw-recovery-response-3"),
            "prompt_tokens": 101,
            "completion_tokens": 17,
            "total_duration_ns": 1_000,
        },
        {
            "type": "action_rejected",
            **common,
            "attempt": 3,
            "reason": "parser_failure",
        },
    ]
    insertion = next(
        index
        for index, event in enumerate(payloads)
        if event["type"] == "environment_close_requested"
    )
    return [*payloads[:insertion], *additions, *payloads[insertion:]]


def _late_raw_policy_recovery_payloads(
    episode: CampaignEpisode,
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    payloads = _raw_policy_recovery_payloads(episode, payloads)
    if not (
        episode.model_id == QWEN35_RAW_PROFILE.model
        and episode.task_id == CAMPAIGN_TASK_IDS[0]
        and episode.arm == "raw_feedback_loop"
    ):
        return payloads
    delayed = [
        event
        for event in payloads
        if event["type"] in {"safety_recovery_completed", "attempt_defaulted"}
        and event.get("attempt") == 2
    ]
    retained = [event for event in payloads if event not in delayed]
    insertion = next(
        index + 1
        for index, event in enumerate(retained)
        if event["type"] == "model_preflighted" and event.get("attempt") == 3
    )
    return [*retained[:insertion], *delayed, *retained[insertion:]]


def _late_restore_payloads(
    episode: CampaignEpisode,
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Forge a rechained log that preflights attempt 3 before restore 2."""

    payloads = _duplicate_restore_payloads(episode, payloads)
    if not (
        episode.model_id == QWEN35_RAW_PROFILE.model
        and episode.task_id == CAMPAIGN_TASK_IDS[0]
        and episode.arm == "engineered_loop"
    ):
        return payloads

    without_duplicate = _drop_duplicate_attempt_three_restore(payloads)

    early_prompt = [
        event
        for event in without_duplicate
        if event.get("attempt") == 3
        and event["type"] in {"model_preflighted", "model_requested"}
    ]
    retained = [event for event in without_duplicate if event not in early_prompt]
    insertion = next(
        index + 1
        for index, event in enumerate(retained)
        if event.get("attempt") == 2
        and event["type"] == "checkpoint_restore_requested"
    )
    return [*retained[:insertion], *early_prompt, *retained[insertion:]]


def _drop_duplicate_attempt_three_restore(
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    seen_attempt_three_restores = {
        "checkpoint_restore_requested": 0,
        "checkpoint_restored": 0,
    }
    without_duplicate: list[dict[str, object]] = []
    for event in payloads:
        if event.get("attempt") == 3 and event["type"] in seen_attempt_three_restores:
            event_type = str(event["type"])
            seen_attempt_three_restores[event_type] += 1
            if seen_attempt_three_restores[event_type] > 1:
                continue
        without_duplicate.append(event)

    return without_duplicate


def _wrong_best_restore_payloads(
    episode: CampaignEpisode,
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Forge attempt 3 to restore the regressed attempt 2 instead of best 1."""

    payloads = _duplicate_restore_payloads(episode, payloads)
    if not (
        episode.model_id == QWEN35_RAW_PROFILE.model
        and episode.task_id == CAMPAIGN_TASK_IDS[0]
        and episode.arm == "engineered_loop"
    ):
        return payloads
    payloads = _drop_duplicate_attempt_three_restore(payloads)
    second_state = next(
        event["state_sha256"]
        for event in payloads
        if event["type"] == "checkpoint_created" and event.get("attempt") == 2
    )
    for event in payloads:
        if event.get("attempt") == 3 and event["type"] in {
            "checkpoint_restore_requested",
            "checkpoint_restored",
        }:
            event["target_attempt"] = 2
            event["state_sha256"] = second_state
            event["replay_depth"] = 2
            if event["type"] == "checkpoint_restored":
                event["replayed_environment_actions"] = 2
    return payloads


def _tie_restore_payloads(
    episode: CampaignEpisode,
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Forge a restore on an equal-reward candidate that must become latest best."""

    payloads = _duplicate_restore_payloads(episode, payloads)
    if not (
        episode.model_id == QWEN35_RAW_PROFILE.model
        and episode.task_id == CAMPAIGN_TASK_IDS[0]
        and episode.arm == "engineered_loop"
    ):
        return payloads
    payloads = _drop_duplicate_attempt_three_restore(payloads)
    evaluation = next(
        event
        for event in payloads
        if event["type"] == "attempt_evaluated" and event.get("attempt") == 2
    )
    evaluation["reward"] = 0.8
    return payloads


def _stale_tie_best_payloads(
    episode: CampaignEpisode,
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Forge a later regression that targets best 1 after attempt 2 tied it."""

    payloads = _tie_restore_payloads(episode, payloads)
    if not (
        episode.model_id == QWEN35_RAW_PROFILE.model
        and episode.task_id == CAMPAIGN_TASK_IDS[0]
        and episode.arm == "engineered_loop"
    ):
        return payloads
    payloads = [
        event
        for event in payloads
        if not (
            event.get("attempt") == 2
            and event["type"]
            in {"checkpoint_restore_requested", "checkpoint_restored"}
        )
    ]
    third_checkpoint = next(
        event
        for event in payloads
        if event["type"] == "checkpoint_created" and event.get("attempt") == 3
    )
    third_checkpoint["replay_depth"] = 3
    return payloads


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
        replayed_environment_actions=0,
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
            self.assertEqual(evidence.total_environment_actions, 240)
            self.assertEqual(evidence.total_replayed_environment_actions, 0)
            self.assertEqual(evidence.total_physical_environment_actions, 240)
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

    def test_accepts_raw_policy_recovery_replay_and_writable_overflow(self) -> None:
        def mutate_result(
            episode: CampaignEpisode,
            result: InteractiveResult,
        ) -> InteractiveResult:
            if not (
                episode.model_id == QWEN35_RAW_PROFILE.model
                and episode.task_id == CAMPAIGN_TASK_IDS[0]
                and episode.arm == "raw_feedback_loop"
            ):
                return result
            return dataclasses.replace(
                result,
                attempts=3,
                model_calls=3,
                logical_prompt_tokens=303,
                logical_completion_tokens=51,
                environment_actions=2,
                replayed_environment_actions=1,
                safety_recoveries=1,
                parser_failures=1,
                feedback_followups=2,
            )

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary),
                event_mutator=_raw_policy_recovery_payloads,
                result_mutator=mutate_result,
            )

            evidence = verify_campaign_evidence(campaign, episodes, spec)

        self.assertEqual(evidence.total_environment_actions, 241)
        self.assertEqual(evidence.total_replayed_environment_actions, 1)
        self.assertEqual(evidence.total_physical_environment_actions, 242)

    def test_rejects_policy_recovery_after_the_next_model_preflight(self) -> None:
        def mutate_result(
            episode: CampaignEpisode,
            result: InteractiveResult,
        ) -> InteractiveResult:
            if not (
                episode.model_id == QWEN35_RAW_PROFILE.model
                and episode.task_id == CAMPAIGN_TASK_IDS[0]
                and episode.arm == "raw_feedback_loop"
            ):
                return result
            return dataclasses.replace(
                result,
                attempts=3,
                model_calls=3,
                logical_prompt_tokens=303,
                logical_completion_tokens=51,
                environment_actions=2,
                replayed_environment_actions=1,
                safety_recoveries=1,
                parser_failures=1,
                feedback_followups=2,
            )

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary),
                event_mutator=_late_raw_policy_recovery_payloads,
                result_mutator=mutate_result,
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "recovery"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_duplicate_checkpoint_restores_on_one_attempt(self) -> None:
        def mutate_result(
            episode: CampaignEpisode,
            result: InteractiveResult,
        ) -> InteractiveResult:
            if not (
                episode.model_id == QWEN35_RAW_PROFILE.model
                and episode.task_id == CAMPAIGN_TASK_IDS[0]
                and episode.arm == "engineered_loop"
            ):
                return result
            return dataclasses.replace(
                result,
                attempts=3,
                model_calls=3,
                logical_prompt_tokens=303,
                logical_completion_tokens=51,
                environment_actions=3,
                replayed_environment_actions=3,
                evaluator_calls=4,
                checkpoint_creates=3,
                checkpoint_restores=3,
                feedback_followups=2,
            )

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary),
                event_mutator=_duplicate_restore_payloads,
                result_mutator=mutate_result,
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "restore"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_next_prompt_preflight_before_checkpoint_restore(self) -> None:
        def mutate_result(
            episode: CampaignEpisode,
            result: InteractiveResult,
        ) -> InteractiveResult:
            if not (
                episode.model_id == QWEN35_RAW_PROFILE.model
                and episode.task_id == CAMPAIGN_TASK_IDS[0]
                and episode.arm == "engineered_loop"
            ):
                return result
            return dataclasses.replace(
                result,
                attempts=3,
                model_calls=3,
                logical_prompt_tokens=303,
                logical_completion_tokens=51,
                environment_actions=3,
                replayed_environment_actions=2,
                evaluator_calls=4,
                checkpoint_creates=3,
                checkpoint_restores=2,
                feedback_followups=2,
            )

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary),
                event_mutator=_late_restore_payloads,
                result_mutator=mutate_result,
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "restore"):
                verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_restores_that_differ_from_frozen_best_policy(self) -> None:
        cases = (
            (_wrong_best_restore_payloads, 3, 2),
            (_tie_restore_payloads, 2, 2),
            (_stale_tie_best_payloads, 1, 1),
        )
        for mutator, replayed_actions, restore_count in cases:
            def mutate_result(
                episode: CampaignEpisode,
                result: InteractiveResult,
            ) -> InteractiveResult:
                if not (
                    episode.model_id == QWEN35_RAW_PROFILE.model
                    and episode.task_id == CAMPAIGN_TASK_IDS[0]
                    and episode.arm == "engineered_loop"
                ):
                    return result
                return dataclasses.replace(
                    result,
                    attempts=3,
                    model_calls=3,
                    logical_prompt_tokens=303,
                    logical_completion_tokens=51,
                    environment_actions=3,
                    replayed_environment_actions=replayed_actions,
                    evaluator_calls=4,
                    checkpoint_creates=3,
                    checkpoint_restores=restore_count,
                    feedback_followups=2,
                )

            with self.subTest(mutator=mutator.__name__), tempfile.TemporaryDirectory() as temporary:
                campaign, episodes, spec = _build_campaign(
                    Path(temporary),
                    event_mutator=mutator,
                    result_mutator=mutate_result,
                )

                with self.assertRaisesRegex(CampaignEvidenceError, "restore|best"):
                    verify_campaign_evidence(campaign, episodes, spec)

    def test_rejects_impossible_replay_result_without_prior_model_actions(self) -> None:
        def mutate_result(
            episode: CampaignEpisode,
            result: InteractiveResult,
        ) -> InteractiveResult:
            if episode.arm == "engineered_loop":
                return dataclasses.replace(
                    result,
                    replayed_environment_actions=1,
                    checkpoint_restores=1,
                )
            return result

        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, spec = _build_campaign(
                Path(temporary),
                result_mutator=mutate_result,
            )

            with self.assertRaisesRegex(CampaignEvidenceError, "campaign matrix"):
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
