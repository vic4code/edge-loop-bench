from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest import mock

from edgeloopbench import intercode_campaign_evidence as campaign_evidence_module
from edgeloopbench import intercode_v07_execution_evidence as execution_evidence_module
from edgeloopbench.interactive_controller import InteractiveResult
from edgeloopbench.intercode_campaign_evidence import VerifiedCampaignEvidence
from edgeloopbench.intercode_campaign_ledger import (
    CAMPAIGN_MODELS,
    CAMPAIGN_TASK_IDS,
    CampaignEpisode,
    CampaignEpisodeExecution,
    CampaignEpisodeResult,
    CampaignMatrix,
    CampaignSpec,
    write_episode_execution_envelope,
)
from edgeloopbench.intercode_host_safety import HostSafetySample
from edgeloopbench.intercode_v07_execution_evidence import (
    V07ExecutionEvidenceError,
    VerifiedV07ExecutionEnvelopeSet,
    verify_v07_execution_envelope_set,
)


STUDY_BINDING_SHA256 = "sha256:" + "7" * 64


def _digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def _result(episode: CampaignEpisode, *, strict_success: bool = False) -> InteractiveResult:
    calls = 1 if episode.arm == "direct" else 4
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
        evaluator_calls=calls + 1,
        checkpoint_creates=calls,
        checkpoint_restores=0,
        safety_recoveries=0,
        parser_failures=0,
        initial_prompts=1,
        independent_sample_prompts=(
            calls - 1 if episode.arm == "independent_verified_sampling" else 0
        ),
        feedback_followups=(
            calls - 1
            if episode.arm in {"raw_feedback_loop", "engineered_loop"}
            else 0
        ),
        human_prompts=0,
    )


def _sample(index: int, *, after: bool) -> HostSafetySample:
    monotonic = index * 10_000 + (2_000 if after else 0)
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


def _execution(episode: CampaignEpisode) -> CampaignEpisodeExecution:
    return CampaignEpisodeExecution(
        result=_result(episode),
        execution_authority_sha256=STUDY_BINDING_SHA256,
        controller_log_sha256=_digest(f"controller-{episode.episode_index}"),
        active_wall_time_ns=1_000,
        before_host_sample=_sample(episode.episode_index, after=False),
        after_host_sample=_sample(episode.episode_index, after=True),
    )


def _campaign_evidence(
    spec: CampaignSpec,
    executions: tuple[CampaignEpisodeExecution, ...],
) -> VerifiedCampaignEvidence:
    rows = tuple(
        CampaignEpisodeResult(
            episode=episode,
            result=execution.result,
            execution_authority_sha256=execution.execution_authority_sha256,
            controller_log_sha256=execution.controller_log_sha256,
            active_wall_time_ns=execution.active_wall_time_ns,
            before_host_sample=execution.before_host_sample,
            after_host_sample=execution.after_host_sample,
        )
        for episode, execution in zip(spec.episodes, executions, strict=True)
    )
    return VerifiedCampaignEvidence(
        CampaignMatrix(rows),
        _digest("campaign"),
        spec.study_binding_sha256,
        spec.schedule_sha256,
        _digest("controllers"),
        tuple(
            (model_id, _digest(f"tokenizer-{model_id}"))
            for model_id in CAMPAIGN_MODELS
        ),
        len(rows),
        _authority=campaign_evidence_module._VERIFICATION_AUTHORITY,
    )


class V07ExecutionEvidenceTests(unittest.TestCase):
    def test_requires_exact_240_envelopes_and_matches_campaign_authority(self) -> None:
        spec = CampaignSpec(CAMPAIGN_TASK_IDS).bind(STUDY_BINDING_SHA256)
        executions = tuple(_execution(episode) for episode in spec.episodes)
        evidence = _campaign_evidence(spec, executions)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            for episode, execution in zip(spec.episodes, executions, strict=True):
                write_episode_execution_envelope(
                    root / f"episode-{episode.episode_index:04d}.execution.jsonl",
                    episode,
                    execution,
                )

            first = verify_v07_execution_envelope_set(root, spec, evidence)
            second = verify_v07_execution_envelope_set(root, spec, evidence)

            self.assertIs(type(first), VerifiedV07ExecutionEnvelopeSet)
            self.assertEqual(first.verified_envelope_count, 240)
            self.assertEqual(first.campaign_log_sha256, evidence.campaign_log_sha256)
            self.assertEqual(first.study_binding_sha256, STUDY_BINDING_SHA256)
            self.assertEqual(first.execution_set_sha256, second.execution_set_sha256)

            other_study = CampaignSpec(CAMPAIGN_TASK_IDS).bind(
                "sha256:" + "8" * 64
            )
            with self.assertRaisesRegex(V07ExecutionEvidenceError, "binding"):
                verify_v07_execution_envelope_set(root, other_study, evidence)

            missing = root / "episode-0240.execution.jsonl"
            moved = root / "saved.execution.jsonl"
            missing.rename(moved)
            with self.assertRaisesRegex(V07ExecutionEvidenceError, "exact 240"):
                verify_v07_execution_envelope_set(root, spec, evidence)
            moved.rename(missing)

            first_path = root / "episode-0001.execution.jsonl"
            original = first_path.read_bytes()
            first_path.unlink()
            write_episode_execution_envelope(
                first_path,
                spec.episodes[0],
                replace(executions[0], result=_result(spec.episodes[0], strict_success=True)),
            )
            with self.assertRaisesRegex(V07ExecutionEvidenceError, "campaign binding"):
                verify_v07_execution_envelope_set(root, spec, evidence)
            first_path.unlink()
            first_path.write_bytes(original)
            first_path.chmod(0o600)

            with self.assertRaisesRegex(ValueError, "VerifiedCampaignEvidence"):
                verify_v07_execution_envelope_set(root, spec, evidence.matrix)  # type: ignore[arg-type]

    def test_rejects_a_symlink_directory(self) -> None:
        spec = CampaignSpec(CAMPAIGN_TASK_IDS).bind(STUDY_BINDING_SHA256)
        executions = tuple(_execution(episode) for episode in spec.episodes)
        evidence = _campaign_evidence(spec, executions)
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real"
            link = Path(directory) / "link"
            real.mkdir(mode=0o700)
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(V07ExecutionEvidenceError, "non-symlink"):
                verify_v07_execution_envelope_set(link, spec, evidence)

    def test_directory_swap_cannot_substitute_one_matching_envelope(self) -> None:
        spec = CampaignSpec(CAMPAIGN_TASK_IDS).bind(STUDY_BINDING_SHA256)
        executions = tuple(_execution(episode) for episode in spec.episodes)
        evidence = _campaign_evidence(spec, executions)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            original_parent = base / "original"
            replacement_parent = base / "replacement"
            original_parent.mkdir()
            replacement_parent.mkdir()
            original = original_parent / "envelopes"
            replacement = replacement_parent / "envelopes"
            original.mkdir(mode=0o700)
            replacement.mkdir(mode=0o700)
            selector = base / "selected"
            selector.symlink_to(original_parent, target_is_directory=True)
            root = selector / "envelopes"
            for episode, execution in zip(spec.episodes, executions, strict=True):
                observed = execution
                if episode.episode_index == 1:
                    observed = replace(
                        execution,
                        result=_result(episode, strict_success=True),
                    )
                write_episode_execution_envelope(
                    original / f"episode-{episode.episode_index:04d}.execution.jsonl",
                    episode,
                    observed,
                )
            write_episode_execution_envelope(
                replacement / "episode-0001.execution.jsonl",
                spec.episodes[0],
                executions[0],
            )

            real_load = (
                execution_evidence_module.load_episode_execution_envelope_at
            )
            swapped = False

            def swap_around_first_read(
                descriptor: int,
                name: str,
                episode: CampaignEpisode,
            ):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    selector.unlink()
                    selector.symlink_to(
                        replacement_parent,
                        target_is_directory=True,
                    )
                    try:
                        return real_load(descriptor, name, episode)
                    finally:
                        selector.unlink()
                        selector.symlink_to(
                            original_parent,
                            target_is_directory=True,
                        )
                return real_load(descriptor, name, episode)

            with mock.patch.object(
                execution_evidence_module,
                "load_episode_execution_envelope_at",
                side_effect=swap_around_first_read,
            ), self.assertRaises(V07ExecutionEvidenceError):
                verify_v07_execution_envelope_set(root, spec, evidence)


if __name__ == "__main__":
    unittest.main()
