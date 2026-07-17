from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from dataclasses import fields
from hashlib import sha256
from pathlib import Path

from edgeloopbench.interactive_controller import (
    INTERACTIVE_CONTROLLER_REVISION,
    InteractiveResult,
    candidate_seed,
)
from edgeloopbench.intercode_campaign_ledger import CAMPAIGN_MODELS
from edgeloopbench.intercode_host_safety import (
    DockerDaemonIdentity,
    HostSafetySample,
    ResidentModel,
)
from edgeloopbench.intercode_replay_environment import (
    V07_STRICT_REPLAY_EVALUATOR_SHA256,
)
from edgeloopbench.intercode_source import load_intercode_source
from edgeloopbench.intercode_v07_calibration import (
    V07_CALIBRATION_ARMS,
    V07_CALIBRATION_DESIGN_SHA256,
    V07_CALIBRATION_JOURNAL_SCHEMA,
    V07_CALIBRATION_TASK_IDS,
    V07CalibrationDisposition,
    VerifiedV07CalibrationEvidence,
    build_v07_calibration_design,
    canonical_v07_calibration_design_record,
    evaluate_v07_calibration,
    evaluate_v07_planning_gate,
    verify_v07_calibration_evidence,
)
from edgeloopbench.journal import append_journal_event, inspect_journal, seal_journal
from edgeloopbench.model_adapter import PHI4_MINI_RAW_PROFILE, QWEN35_RAW_PROFILE


MANIFEST_SHA256 = "sha256:" + "a" * 64
CALIBRATION_CAMPAIGN_SHA256 = "sha256:" + "c" * 64
CALIBRATION_JOURNAL_SCHEMA = V07_CALIBRATION_JOURNAL_SCHEMA


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + sha256(payload).hexdigest()


def profile_for(model_id: str):  # type: ignore[no-untyped-def]
    return QWEN35_RAW_PROFILE if model_id == QWEN35_RAW_PROFILE.model else PHI4_MINI_RAW_PROFILE


def result_record(result: InteractiveResult) -> dict[str, object]:
    return {field.name: getattr(result, field.name) for field in fields(result)}


def host_sample(
    model_id: str,
    *,
    monotonic_ns: int,
    vm_pressure_level: int = 1,
) -> HostSafetySample:
    daemon = DockerDaemonIdentity(
        binary_sha256=digest("docker-binary"),
        endpoint_sha256=digest("docker-endpoint"),
        client_version="28.3.2",
        server_version="28.3.2",
    )
    profile = profile_for(model_id)
    return HostSafetySample(
        captured_unix_ns=1_700_000_000_000_000_000 + monotonic_ns,
        captured_monotonic_ns=monotonic_ns,
        boot_time_unix_microseconds=1_700_000_000_000_000,
        on_ac_power=True,
        low_power_mode_enabled=False,
        vm_pressure_level=vm_pressure_level,
        free_memory_percent=50,
        swap_used_bytes=1 << 30,
        thermal_warning=False,
        performance_warning=False,
        disk_free_bytes=40 << 30,
        resident_models=(
            ResidentModel(model_id, profile.model_manifest_sha256.removeprefix("sha256:")),
        ),
        running_container_ids=(),
        docker_daemon=daemon,
    )


def write_controller_log(
    path: Path,
    *,
    model_id: str,
    task_id: str,
    arm: str,
    calls: int,
    first_parse_failure: bool,
    strict_success: bool,
    leak: bool,
    corrupt_first_action: bool,
    omit_strict_plan: bool,
    corrupt_progress: bool,
    corrupt_official_success: bool,
    execution_authority_sha256: str,
) -> InteractiveResult:
    identity = {
        "task_id": task_id,
        "strategy": arm,
        "replicate_seed": 11,
        "execution_authority_sha256": execution_authority_sha256,
    }
    profile = profile_for(model_id)

    def record(event_type: str, **values: object) -> None:
        event = {"type": event_type, **identity, **values}
        if leak and event_type == "controller_started":
            event["gold_command"] = "hidden evaluator material"
        append_journal_event(path, event)

    record("controller_started", controller_revision=INTERACTIVE_CONTROLLER_REVISION)
    successful_actions: list[int] = []
    cumulative_prompt_tokens = 0
    for attempt in range(1, calls + 1):
        prompt_sha256 = digest(f"{model_id}-{task_id}-{attempt}-prompt")
        state_sha256 = digest(f"{model_id}-{task_id}-{attempt}-state")
        record(
            "model_preflighted",
            attempt=attempt,
            prompt_sha256=prompt_sha256,
            prompt_tokens=100,
            token_ids_sha256=digest(f"{task_id}-{attempt}-tokens"),
            renderer_profile_sha256=profile.sha256,
            tokenizer_artifact_sha256=digest(f"{model_id}-tokenizer"),
            model_artifact_sha256=profile.model_artifact_sha256,
        )
        cumulative_prompt_tokens += 100
        record(
            "model_requested",
            attempt=attempt,
            prompt_sha256=prompt_sha256,
            logical_model_calls_after=attempt,
            logical_prompt_tokens_after=cumulative_prompt_tokens,
            candidate_seed=candidate_seed(11, attempt),
            context_sha256=digest(f"{task_id}-{arm}-{attempt}-context"),
            max_output_tokens=512,
        )
        record(
            "model_completed",
            attempt=attempt,
            response_sha256=digest(f"{task_id}-{arm}-{attempt}-response"),
            prompt_tokens=100,
            completion_tokens=10,
            total_duration_ns=100_000,
        )
        if attempt == 1 and first_parse_failure:
            record("action_rejected", attempt=attempt, reason="parser_failure")
            continue
        action_sha256 = digest(f"{task_id}-{arm}-{attempt}-action")
        record("action_requested", attempt=attempt, action_sha256=action_sha256)
        record(
            "action_completed",
            attempt=attempt,
            action_sha256=action_sha256,
            output_sha256=digest(f"{task_id}-{attempt}-output"),
            state_sha256=state_sha256,
            exit_code=0,
            admissible=True,
            state_changed=True,
            policy_failure=(
                "timeout" if corrupt_first_action and attempt == 1 else None
            ),
            safety_recovery_performed=False,
        )
        record("checkpoint_create_requested", attempt=attempt)
        record("checkpoint_created", attempt=attempt, state_sha256=state_sha256)
        record("attempt_evaluation_requested", attempt=attempt, state_sha256=state_sha256)
        record(
            "attempt_evaluated",
            attempt=attempt,
            reward=(1.0 if corrupt_progress and attempt == 1 else 0.8),
            official_success=(corrupt_official_success and attempt == 1),
            evaluation_kind="evaluator_derived",
        )
        successful_actions.append(attempt)

    selected_attempt = successful_actions[-1] if successful_actions else None
    if selected_attempt is not None and not omit_strict_plan:
        record(
            "strict_evaluation_planned",
            selected_attempt=selected_attempt,
            state_sha256=digest(f"{model_id}-{task_id}-{selected_attempt}-state"),
        )
    record(
        "terminal_finalization_requested",
        selected_attempt=selected_attempt,
        evaluation_kind="evaluator_derived" if selected_attempt is not None else None,
        aborted=False,
        remaining_evaluator_calls=1 if selected_attempt is not None else 0,
    )
    record(
        "terminal_finalized",
        strict_evaluator_calls=1 if selected_attempt is not None else 0,
        posthoc_evaluator_calls=0,
    )
    if selected_attempt is not None:
        record(
            "strict_evaluation_completed",
            strict_success=strict_success,
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
        )
    stop_reason = (
        "direct_parser_failure"
        if arm == "direct" and first_parse_failure
        else "direct_complete"
        if arm == "direct"
        else "attempt_budget_exhausted"
    )
    record(
        "controller_stopped",
        stop_reason=stop_reason,
        selected_attempt=selected_attempt,
        official_success=False,
    )
    seal_journal(path)
    successful_count = len(successful_actions)
    return InteractiveResult(
        run_status=("completed" if arm == "direct" else "budget_exhausted"),
        official_success=False,
        strict_success=strict_success if selected_attempt is not None else False,
        stop_reason=stop_reason,
        attempts=calls,
        model_calls=calls,
        logical_prompt_tokens=100 * calls,
        logical_completion_tokens=10 * calls,
        environment_actions=successful_count,
        evaluator_calls=successful_count + int(selected_attempt is not None),
        checkpoint_creates=successful_count,
        checkpoint_restores=0,
        safety_recoveries=0,
        parser_failures=int(first_parse_failure),
        initial_prompts=1,
        independent_sample_prompts=(
            calls - 1 if arm == "independent_verified_sampling" else 0
        ),
        feedback_followups=(
            calls - 1 if arm in {"raw_feedback_loop", "engineered_loop"} else 0
        ),
        human_prompts=0,
    )


def write_evidence_files(
    root: Path,
    *,
    first_parse_failures: frozenset[int] = frozenset(),
    strict_models: frozenset[str] = frozenset(),
    leak_episode: int | None = None,
    unsafe_host_episode: int | None = None,
    result_mismatch_episode: int | None = None,
    corrupt_action_episode: int | None = None,
    omit_strict_plan_episode: int | None = None,
    corrupt_progress_episode: int | None = None,
    corrupt_official_success_episode: int | None = None,
    active_wall_time_ns: int = 1_000_000_000,
    manifest_sha256: str = MANIFEST_SHA256,
    calibration_campaign_sha256: str = CALIBRATION_CAMPAIGN_SHA256,
    seal_calibration: bool = True,
) -> tuple[Path, tuple[Path, ...]]:
    design = build_v07_calibration_design(load_intercode_source())
    controller_paths: list[Path] = []
    episode_rows: list[dict[str, object]] = []
    episode_index = 0
    for model_id in CAMPAIGN_MODELS:
        for task_id, arm, cap in zip(
            design.task_ids,
            design.arms,
            design.request_caps,
            strict=True,
        ):
            episode_index += 1
            path = root / f"controller-{episode_index:03d}.jsonl"
            result = write_controller_log(
                path,
                model_id=model_id,
                task_id=task_id,
                arm=arm,
                calls=cap,
                first_parse_failure=episode_index in first_parse_failures,
                strict_success=model_id in strict_models,
                leak=episode_index == leak_episode,
                corrupt_first_action=episode_index == corrupt_action_episode,
                omit_strict_plan=episode_index == omit_strict_plan_episode,
                corrupt_progress=episode_index == corrupt_progress_episode,
                corrupt_official_success=(
                    episode_index == corrupt_official_success_episode
                ),
                execution_authority_sha256=manifest_sha256,
            )
            controller_paths.append(path)
            before_ns = episode_index * (active_wall_time_ns + 1_000_000)
            before = host_sample(model_id, monotonic_ns=before_ns)
            after = host_sample(
                model_id,
                monotonic_ns=before_ns + active_wall_time_ns,
                vm_pressure_level=(2 if episode_index == unsafe_host_episode else 1),
            )
            serialized_result = result_record(result)
            if episode_index == result_mismatch_episode:
                serialized_result["logical_prompt_tokens"] = 99
            episode_rows.append(
                {
                    "type": "calibration_episode_recorded",
                    "episode_index": episode_index,
                    "model_id": model_id,
                    "task_id": task_id,
                    "arm": arm,
                    "seed": 11,
                    "result": serialized_result,
                    "execution_authority_sha256": manifest_sha256,
                    "controller_log_sha256": (
                        "sha256:" + inspect_journal(path, require_sealed=True).last_event_sha256
                    ),
                    "active_wall_time_ns": active_wall_time_ns,
                    "before_host_sample": before.to_record(),
                    "after_host_sample": after.to_record(),
                }
            )

    journal = root / "calibration.jsonl"
    append_journal_event(
        journal,
        {
            "type": "calibration_declared",
            "schema": CALIBRATION_JOURNAL_SCHEMA,
            "design_sha256": design.design_sha256,
            "schedule_sha256": design.schedule_sha256,
            "precalibration_manifest_sha256": manifest_sha256,
            "calibration_campaign_sha256": calibration_campaign_sha256,
            "models": list(CAMPAIGN_MODELS),
            "episode_count": 8,
        },
    )
    for row in episode_rows:
        append_journal_event(journal, row)
    if seal_calibration:
        seal_journal(journal)
    return journal, tuple(controller_paths)


class InterCodeV07CalibrationTests(unittest.TestCase):
    def test_design_freezes_four_disjoint_tasks_and_thirteen_prompt_cap(self) -> None:
        design = build_v07_calibration_design(load_intercode_source())

        self.assertEqual(design.task_ids, V07_CALIBRATION_TASK_IDS)
        self.assertEqual(design.arms, V07_CALIBRATION_ARMS)
        self.assertEqual(design.request_caps, (1, 4, 4, 4))
        self.assertEqual(design.max_prompts_per_model, 13)
        self.assertEqual(design.max_prompts_two_models, 26)
        self.assertEqual(design.seed, 11)
        encoded = json.dumps(design.to_record(), sort_keys=True)
        for forbidden in ("query", "gold", "/Users/", "evaluator_path"):
            self.assertNotIn(forbidden, encoded)
        self.assertRegex(design.design_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(design.design_sha256, V07_CALIBRATION_DESIGN_SHA256)

    def test_canonical_design_record_is_semantically_complete_and_detached(self) -> None:
        record = canonical_v07_calibration_design_record()

        self.assertEqual(canonical_digest(record), V07_CALIBRATION_DESIGN_SHA256)
        self.assertEqual(record["maximum_prompts_per_model"], 13)
        self.assertEqual(record["maximum_prompts_two_models"], 26)
        self.assertEqual(record["first_response_parse_and_admissibility_minimum"], 3)
        self.assertEqual(record["active_time_limit_seconds"], 18 * 60 * 60)
        assignments = record["assignments_per_model"]
        self.assertIsInstance(assignments, list)
        assignments[0]["task_id"] = "mutated"  # type: ignore[index]
        fresh = canonical_v07_calibration_design_record()
        self.assertEqual(fresh["assignments_per_model"][0]["task_id"], "bash-calibration-000")  # type: ignore[index]

    def test_verifier_opens_exact_journals_and_derives_twenty_six_prompt_evidence(self) -> None:
        design = build_v07_calibration_design(load_intercode_source())
        with tempfile.TemporaryDirectory() as directory:
            journal, controllers = write_evidence_files(
                Path(directory), strict_models=frozenset({CAMPAIGN_MODELS[0]})
            )

            evidence = verify_v07_calibration_evidence(
                design,
                precalibration_manifest_sha256=MANIFEST_SHA256,
                calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                calibration_journal_path=journal,
                controller_log_paths=controllers,
            )

        self.assertIs(type(evidence), VerifiedV07CalibrationEvidence)
        self.assertEqual(evidence.episode_count, 8)
        self.assertEqual(evidence.total_model_prompts, 26)
        self.assertEqual(evidence.precalibration_manifest_sha256, MANIFEST_SHA256)
        self.assertEqual(
            evidence.calibration_campaign_sha256,
            CALIBRATION_CAMPAIGN_SHA256,
        )
        self.assertEqual(len(evidence.controller_log_sha256s), 8)
        qwen = evaluate_v07_calibration(evidence, CAMPAIGN_MODELS[0])
        phi = evaluate_v07_calibration(evidence, CAMPAIGN_MODELS[1])
        self.assertTrue(qwen.admitted)
        self.assertTrue(phi.admitted)
        self.assertEqual(qwen.strict_successes, 4)
        self.assertEqual(phi.strict_successes, 0)
        self.assertEqual((qwen.total_model_prompts, phi.total_model_prompts), (13, 13))

    def test_mechanics_gate_is_derived_from_first_controller_outcome(self) -> None:
        design = build_v07_calibration_design(load_intercode_source())
        with tempfile.TemporaryDirectory() as directory:
            journal, controllers = write_evidence_files(
                Path(directory), first_parse_failures=frozenset({1, 2})
            )
            evidence = verify_v07_calibration_evidence(
                design,
                precalibration_manifest_sha256=MANIFEST_SHA256,
                calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                calibration_journal_path=journal,
                controller_log_paths=controllers,
            )

        qwen = evaluate_v07_calibration(evidence, CAMPAIGN_MODELS[0])
        phi = evaluate_v07_calibration(evidence, CAMPAIGN_MODELS[1])
        self.assertFalse(qwen.admitted)
        self.assertIn("first_action_fit_below_three_of_four", qwen.reasons)
        self.assertEqual(qwen.parsed_and_admissible_first_responses, 2)
        self.assertTrue(phi.admitted)

    def test_verifier_rejects_leak_accounting_host_manifest_and_seal_failures(self) -> None:
        design = build_v07_calibration_design(load_intercode_source())
        cases = (
            ({"leak_episode": 1}, "forbidden"),
            ({"result_mismatch_episode": 1}, "event-derived"),
            ({"unsafe_host_episode": 1}, "host safety"),
            ({"manifest_sha256": digest("wrong-manifest")}, "manifest"),
            (
                {"calibration_campaign_sha256": digest("wrong-campaign")},
                "campaign",
            ),
            ({"seal_calibration": False}, "sealed"),
            ({"corrupt_action_episode": 1}, "admissible"),
            ({"omit_strict_plan_episode": 1}, "strict"),
            ({"corrupt_progress_episode": 1}, "progress"),
            ({"corrupt_official_success_episode": 1}, "official_success"),
        )
        for options, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                journal, controllers = write_evidence_files(Path(directory), **options)
                with self.assertRaisesRegex(ValueError, message):
                    verify_v07_calibration_evidence(
                        design,
                        precalibration_manifest_sha256=MANIFEST_SHA256,
                        calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                        calibration_journal_path=journal,
                        controller_log_paths=controllers,
                    )

    def test_verifier_rejects_non_0600_or_duplicate_controller_logs(self) -> None:
        design = build_v07_calibration_design(load_intercode_source())
        with tempfile.TemporaryDirectory() as directory:
            journal, controllers = write_evidence_files(Path(directory))
            os.chmod(controllers[0], 0o644)
            with self.assertRaisesRegex(ValueError, "0600"):
                verify_v07_calibration_evidence(
                    design,
                    precalibration_manifest_sha256=MANIFEST_SHA256,
                    calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                    calibration_journal_path=journal,
                    controller_log_paths=controllers,
                )
            os.chmod(controllers[0], 0o600)
            duplicate = (controllers[0], controllers[0], *controllers[2:])
            with self.assertRaisesRegex(ValueError, "distinct"):
                verify_v07_calibration_evidence(
                    design,
                    precalibration_manifest_sha256=MANIFEST_SHA256,
                    calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                    calibration_journal_path=journal,
                    controller_log_paths=duplicate,
                )
            alias = Path(directory) / "controller-alias.jsonl"
            alias.symlink_to(controllers[0])
            symlinked = (alias, *controllers[1:])
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                verify_v07_calibration_evidence(
                    design,
                    precalibration_manifest_sha256=MANIFEST_SHA256,
                    calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                    calibration_journal_path=journal,
                    controller_log_paths=symlinked,
                )

    def test_evidence_and_dispositions_cannot_be_publicly_forged_or_replaced(self) -> None:
        design = build_v07_calibration_design(load_intercode_source())
        with self.assertRaises(TypeError):
            VerifiedV07CalibrationEvidence()  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            V07CalibrationDisposition(  # type: ignore[call-arg]
                model_id=CAMPAIGN_MODELS[0],
                admitted=True,
                reasons=(),
                episode_count=4,
                parsed_and_admissible_first_responses=4,
                strict_successes=0,
                total_model_prompts=13,
                active_wall_time_ns=1,
            )
        with tempfile.TemporaryDirectory() as directory:
            journal, controllers = write_evidence_files(Path(directory))
            evidence = verify_v07_calibration_evidence(
                design,
                precalibration_manifest_sha256=MANIFEST_SHA256,
                calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                calibration_journal_path=journal,
                controller_log_paths=controllers,
            )
        disposition = evaluate_v07_calibration(evidence, CAMPAIGN_MODELS[0])
        with self.assertRaises(TypeError):
            dataclasses.replace(disposition, active_wall_time_ns=-1)
        forged = object.__new__(V07CalibrationDisposition)
        for field in dataclasses.fields(disposition):
            object.__setattr__(forged, field.name, getattr(disposition, field.name))
        object.__setattr__(forged, "active_wall_time_ns", -1)
        phi = evaluate_v07_calibration(evidence, CAMPAIGN_MODELS[1])
        with self.assertRaisesRegex(ValueError, "invariants"):
            evaluate_v07_planning_gate((forged, phi))

    def test_planning_gate_uses_only_paired_sealed_dispositions_and_caps_at_18h(self) -> None:
        design = build_v07_calibration_design(load_intercode_source())
        with tempfile.TemporaryDirectory() as directory:
            journal, controllers = write_evidence_files(Path(directory))
            evidence = verify_v07_calibration_evidence(
                design,
                precalibration_manifest_sha256=MANIFEST_SHA256,
                calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                calibration_journal_path=journal,
                controller_log_paths=controllers,
            )
        qwen = evaluate_v07_calibration(evidence, CAMPAIGN_MODELS[0])
        phi = evaluate_v07_calibration(evidence, CAMPAIGN_MODELS[1])
        allowed = evaluate_v07_planning_gate((qwen, phi))
        self.assertEqual(allowed.estimated_confirmatory_active_time_ns, 240_000_000_000)
        self.assertEqual(allowed.planning_bound_ns, 360_000_000_000)
        self.assertTrue(allowed.allowed)

        with tempfile.TemporaryDirectory() as directory:
            journal, controllers = write_evidence_files(
                Path(directory), active_wall_time_ns=200_000_000_000
            )
            slow_evidence = verify_v07_calibration_evidence(
                design,
                precalibration_manifest_sha256=MANIFEST_SHA256,
                calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                calibration_journal_path=journal,
                controller_log_paths=controllers,
            )
        slow = tuple(
            evaluate_v07_calibration(slow_evidence, model_id)
            for model_id in CAMPAIGN_MODELS
        )
        refused = evaluate_v07_planning_gate(slow)
        self.assertFalse(refused.allowed)
        self.assertEqual(refused.reason, "planning_bound_exceeds_18_active_hours")


if __name__ == "__main__":
    unittest.main()
