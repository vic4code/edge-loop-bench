from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest import mock

from edgeloopbench.interactive_controller import InteractiveResult
from edgeloopbench.intercode_host_safety import HostSafetySample
from edgeloopbench.intercode_campaign_ledger import (
    CAMPAIGN_ACTIVE_TIME_LIMIT_NS,
    CAMPAIGN_ARMS,
    CAMPAIGN_ATTEMPT_CAP,
    CAMPAIGN_EPISODE_LOG_REVISION,
    CAMPAIGN_MODELS,
    CAMPAIGN_PROGRESS_REVISION,
    CAMPAIGN_SEED,
    CAMPAIGN_SOURCE_CORPUS_SHA256,
    CAMPAIGN_STATIC_AUDIT_SHA256,
    CAMPAIGN_STRICT_EVALUATOR_REVISION,
    CAMPAIGN_TASK_IDS,
    CAMPAIGN_TASK_MANIFEST_SHA256,
    CAMPAIGN_WILLIAMS_ORDER,
    CampaignActiveTimeLimitError,
    CampaignEpisode,
    CampaignEpisodeExecution,
    CampaignError,
    CampaignExecutionEnvelopeError,
    CampaignInfrastructureInvalidError,
    CampaignMatrixError,
    CampaignPendingEpisodeError,
    CampaignSpec,
    advance_campaign,
    inspect_campaign,
    load_episode_execution_envelope,
    load_complete_campaign_matrix,
    write_episode_execution_envelope,
)
from edgeloopbench.journal import inspect_journal


STUDY_BINDING_SHA256 = "sha256:" + "7" * 64


def _bound_spec() -> CampaignSpec:
    return CampaignSpec(task_ids=CAMPAIGN_TASK_IDS).bind(
        STUDY_BINDING_SHA256
    )


def _result_for(
    episode: CampaignEpisode,
    *,
    run_status: str | None = None,
    stop_reason: str | None = None,
    model_calls: int | None = None,
    human_prompts: int = 0,
) -> InteractiveResult:
    calls = (1 if episode.arm == "direct" else 4) if model_calls is None else model_calls
    if episode.arm == "direct":
        independent_prompts = 0
        feedback_prompts = 0
        default_reason = "direct_complete"
    elif episode.arm == "independent_verified_sampling":
        independent_prompts = max(0, calls - 1)
        feedback_prompts = 0
        default_reason = "attempt_budget_exhausted"
    else:
        independent_prompts = 0
        feedback_prompts = max(0, calls - 1)
        default_reason = (
            "no_progress_guard"
            if episode.arm == "engineered_loop"
            else "attempt_budget_exhausted"
        )
    reason = stop_reason or default_reason
    status = run_status or (
        "budget_exhausted" if "budget_exhausted" in reason else "completed"
    )
    infrastructure_invalid = status == "infrastructure_error"
    return InteractiveResult(
        run_status=status,
        official_success=False,
        strict_success=not infrastructure_invalid,
        stop_reason=reason,
        attempts=calls,
        model_calls=calls,
        logical_prompt_tokens=101 * calls,
        logical_completion_tokens=17 * calls,
        environment_actions=calls,
        evaluator_calls=calls + 1,
        checkpoint_creates=calls,
        checkpoint_restores=0,
        safety_recoveries=0,
        parser_failures=0,
        initial_prompts=1,
        independent_sample_prompts=independent_prompts,
        feedback_followups=feedback_prompts,
        human_prompts=human_prompts,
    )


def _controller_log_sha256(episode: CampaignEpisode) -> str:
    payload = repr(episode.identity).encode("ascii")
    return "sha256:" + sha256(payload).hexdigest()


def _host_sample(episode: CampaignEpisode, *, after: bool) -> HostSafetySample:
    monotonic = episode.episode_index * 10_000_000 + (2_000_000 if after else 0)
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


def _execution_for(
    episode: CampaignEpisode, **result_overrides: object
) -> CampaignEpisodeExecution:
    return CampaignEpisodeExecution(
        result=_result_for(episode, **result_overrides),  # type: ignore[arg-type]
        execution_authority_sha256=STUDY_BINDING_SHA256,
        controller_log_sha256=_controller_log_sha256(episode),
        active_wall_time_ns=1_000_000 + episode.episode_index,
        before_host_sample=_host_sample(episode, after=False),
        after_host_sample=_host_sample(episode, after=True),
    )


def _execution_with_active_time(
    episode: CampaignEpisode,
    active_wall_time_ns: int,
) -> CampaignEpisodeExecution:
    before = _host_sample(episode, after=False)
    after = replace(
        _host_sample(episode, after=True),
        captured_unix_ns=before.captured_unix_ns + active_wall_time_ns,
        captured_monotonic_ns=(
            before.captured_monotonic_ns + active_wall_time_ns
        ),
    )
    return CampaignEpisodeExecution(
        result=_result_for(episode),
        execution_authority_sha256=STUDY_BINDING_SHA256,
        controller_log_sha256=_controller_log_sha256(episode),
        active_wall_time_ns=active_wall_time_ns,
        before_host_sample=before,
        after_host_sample=after,
    )


class InterCodeCampaignLedgerTests(unittest.TestCase):
    def test_declares_the_exact_deterministic_240_episode_schedule(self) -> None:
        tasks = CAMPAIGN_TASK_IDS
        spec = CampaignSpec(task_ids=tasks)

        self.assertEqual(CAMPAIGN_MODELS, ("qwen3.5:4b", "phi4-mini:3.8b"))
        self.assertEqual(
            CAMPAIGN_ARMS,
            (
                "direct",
                "independent_verified_sampling",
                "raw_feedback_loop",
                "engineered_loop",
            ),
        )
        self.assertEqual(CAMPAIGN_SEED, 11)
        self.assertEqual(CAMPAIGN_ATTEMPT_CAP, 4)
        self.assertEqual(
            CAMPAIGN_TASK_MANIFEST_SHA256,
            "da5355df187c85b248469c6238c4f4c61dbfcca34c290e4163b55292d287fc60",
        )
        self.assertEqual(
            CAMPAIGN_WILLIAMS_ORDER,
            (
                (
                    "direct",
                    "independent_verified_sampling",
                    "engineered_loop",
                    "raw_feedback_loop",
                ),
                (
                    "independent_verified_sampling",
                    "raw_feedback_loop",
                    "direct",
                    "engineered_loop",
                ),
                (
                    "raw_feedback_loop",
                    "engineered_loop",
                    "independent_verified_sampling",
                    "direct",
                ),
                (
                    "engineered_loop",
                    "direct",
                    "raw_feedback_loop",
                    "independent_verified_sampling",
                ),
            ),
        )
        self.assertEqual(len(spec.episodes), 240)
        self.assertEqual(
            spec.episodes[0].identity,
            (1, "qwen3.5:4b", tasks[0], "direct", 11),
        )
        self.assertEqual(
            tuple(episode.arm for episode in spec.episodes[:8]),
            CAMPAIGN_WILLIAMS_ORDER[0] + CAMPAIGN_WILLIAMS_ORDER[1],
        )
        self.assertEqual(
            spec.episodes[120].identity,
            (121, "phi4-mini:3.8b", tasks[0], "direct", 11),
        )
        self.assertEqual(
            spec.episodes[-1].identity,
            (240, "phi4-mini:3.8b", tasks[-1], "engineered_loop", 11),
        )

        with self.assertRaisesRegex(ValueError, "exactly 30"):
            CampaignSpec(task_ids=tasks[:-1])
        with self.assertRaisesRegex(ValueError, "unique"):
            CampaignSpec(task_ids=tasks[:-1] + (tasks[0],))
        with self.assertRaisesRegex(ValueError, "public identifier"):
            CampaignSpec(task_ids=tasks[:-1] + ("../../private/gold",))
        with self.assertRaisesRegex(ValueError, "frozen 30-task manifest"):
            CampaignSpec(task_ids=tasks[:-1] + ("bash-fs4-026",))

    def test_unbound_schedule_cannot_create_durable_campaign_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "campaign.jsonl"
            spec = CampaignSpec(task_ids=CAMPAIGN_TASK_IDS)
            called = False

            def forbidden(_episode: CampaignEpisode) -> CampaignEpisodeExecution:
                nonlocal called
                called = True
                raise AssertionError("an unbound schedule cannot execute")

            with self.assertRaisesRegex(CampaignError, "study binding"):
                advance_campaign(journal, spec, forbidden)

            self.assertFalse(called)
            self.assertFalse(journal.exists())

    def test_fsyncs_new_journal_parent_and_intent_before_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "campaign.jsonl"
            spec = _bound_spec()
            timeline: list[str] = []

            import edgeloopbench.journal as journal_module

            real_fsync = journal_module.os.fsync

            def observed_fsync(descriptor: int) -> None:
                real_fsync(descriptor)
                kind = (
                    "directory_fsync"
                    if stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    else "file_fsync"
                )
                timeline.append(kind)

            expected_episode = spec.episodes[0]
            expected = _execution_for(expected_episode)

            def execute(episode: CampaignEpisode) -> CampaignEpisodeExecution:
                timeline.append("callback")
                rows = _records(journal)
                self.assertEqual(rows[-1]["type"], "episode_intent")
                self.assertEqual(rows[-1]["episode_index"], 1)
                self.assertEqual(episode, expected_episode)
                return expected

            with mock.patch.object(journal_module.os, "fsync", observed_fsync):
                outcome = advance_campaign(journal, spec, execute)

            callback_index = timeline.index("callback")
            self.assertIn("directory_fsync", timeline[:callback_index])
            self.assertGreaterEqual(timeline[:callback_index].count("file_fsync"), 2)
            self.assertEqual(outcome.action, "episode_completed")
            rows = _records(journal)
            self.assertEqual(
                [row["type"] for row in rows],
                ["campaign_declared", "episode_intent", "episode_completed"],
            )
            declaration = rows[0]
            self.assertEqual(
                declaration["study_binding_sha256"],
                STUDY_BINDING_SHA256,
            )
            self.assertEqual(declaration["attempt_cap"], CAMPAIGN_ATTEMPT_CAP)
            self.assertEqual(
                declaration["source_corpus_sha256"], CAMPAIGN_SOURCE_CORPUS_SHA256
            )
            self.assertEqual(
                declaration["static_audit_sha256"], CAMPAIGN_STATIC_AUDIT_SHA256
            )
            self.assertEqual(
                declaration["progress_revision"], CAMPAIGN_PROGRESS_REVISION
            )
            self.assertEqual(
                declaration["strict_evaluator_revision"],
                CAMPAIGN_STRICT_EVALUATOR_REVISION,
            )
            self.assertEqual(
                declaration["episode_log_revision"], CAMPAIGN_EPISODE_LOG_REVISION
            )
            completed = rows[-1]
            self.assertEqual(
                completed["controller_log_sha256"], expected.controller_log_sha256
            )
            self.assertEqual(completed["active_wall_time_ns"], 1_000_001)
            self.assertEqual(
                completed["before_host_sample"],
                _host_sample(expected_episode, after=False).to_record(),
            )
            self.assertEqual(
                completed["after_host_sample"],
                _host_sample(expected_episode, after=True).to_record(),
            )
            self.assertEqual(
                completed["result"],
                {
                    "attempts": 1,
                    "checkpoint_creates": 1,
                    "checkpoint_restores": 0,
                    "environment_actions": 1,
                    "evaluator_calls": 2,
                    "feedback_followups": 0,
                    "human_prompts": 0,
                    "independent_sample_prompts": 0,
                    "initial_prompts": 1,
                    "logical_completion_tokens": 17,
                    "logical_prompt_tokens": 101,
                    "model_calls": 1,
                    "official_success": False,
                    "parser_failures": 0,
                    "run_status": "completed",
                    "safety_recoveries": 0,
                    "stop_reason": "direct_complete",
                    "strict_success": True,
                },
            )

    def test_revalidates_before_first_durable_campaign_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "campaign.jsonl"
            spec = _bound_spec()
            execute_called = False

            def execute(_episode: CampaignEpisode) -> CampaignEpisodeExecution:
                nonlocal execute_called
                execute_called = True
                raise AssertionError("executor must not run after rejected revalidation")

            def reject(episode: CampaignEpisode) -> None:
                self.assertEqual(episode, spec.episodes[0])
                self.assertFalse(journal.exists())
                raise RuntimeError("live authority changed")

            with self.assertRaisesRegex(RuntimeError, "live authority changed"):
                advance_campaign(
                    journal,
                    spec,
                    execute,
                    before_new_intent=reject,
                )

            self.assertFalse(execute_called)
            self.assertFalse(journal.exists())

    def test_does_not_revalidate_when_reconciling_a_pending_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "campaign.jsonl"
            envelope = root / "episode-001.jsonl"
            spec = _bound_spec()
            pending = spec.episodes[0]
            revalidated: list[CampaignEpisode] = []

            def interrupted(episode: CampaignEpisode) -> CampaignEpisodeExecution:
                self.assertEqual(episode, pending)
                raise RuntimeError("simulated process exit after durable intent")

            with self.assertRaisesRegex(RuntimeError, "simulated process exit"):
                advance_campaign(
                    journal,
                    spec,
                    interrupted,
                    before_new_intent=revalidated.append,
                )
            self.assertEqual(revalidated, [pending])

            write_episode_execution_envelope(
                envelope,
                pending,
                _execution_for(pending),
            )

            def forbidden_revalidation(_episode: CampaignEpisode) -> None:
                raise AssertionError("pending reconciliation is not a new intent")

            reconciled = advance_campaign(
                journal,
                spec,
                lambda _episode: (_ for _ in ()).throw(
                    AssertionError("reconciliation must not execute a model")
                ),
                reconcile_pending=lambda episode: (
                    envelope if episode == pending else None
                ),
                before_new_intent=forbidden_revalidation,
            )

            self.assertEqual(reconciled.action, "episode_reconciled")
            self.assertEqual(reconciled.episode, pending)

    def test_reopen_skips_completed_episode_and_executes_only_the_next_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "campaign.jsonl"
            spec = _bound_spec()
            observed: list[tuple[int, str, str, str, int]] = []

            advance_campaign(
                journal,
                spec,
                lambda episode: observed.append(episode.identity)
                or _execution_for(episode),
            )
            second = advance_campaign(
                journal,
                spec,
                lambda episode: observed.append(episode.identity)
                or _execution_for(episode),
            )

            self.assertEqual(
                observed,
                [spec.episodes[0].identity, spec.episodes[1].identity],
            )
            self.assertEqual(second.progress.completed_episodes, 2)
            self.assertEqual(second.progress.pending_episodes, 0)

    def test_pending_intent_halts_unchanged_until_a_future_exact_reconciler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "campaign.jsonl"
            spec = _bound_spec()

            def interrupted(_episode: CampaignEpisode) -> CampaignEpisodeExecution:
                raise RuntimeError("simulated process interruption")

            with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
                advance_campaign(journal, spec, interrupted)
            before = journal.read_bytes()
            callback_called = False

            def forbidden(episode: CampaignEpisode) -> CampaignEpisodeExecution:
                nonlocal callback_called
                callback_called = True
                return _execution_for(episode)

            for _ in range(2):
                with self.assertRaises(CampaignPendingEpisodeError) as caught:
                    advance_campaign(journal, spec, forbidden)
                self.assertEqual(caught.exception.episode, spec.episodes[0])
                self.assertEqual(journal.read_bytes(), before)
            self.assertFalse(callback_called)
            progress = inspect_campaign(journal, spec)
            self.assertEqual(progress.pending_episodes, 1)
            self.assertEqual(progress.invalid_episodes, 0)
            self.assertEqual(
                [
                    row["episode_index"]
                    for row in _records(journal)
                    if row["type"] == "episode_intent"
                ],
                [1],
            )

            envelope = Path(directory) / "episode-0001.execution.jsonl"
            expected = _execution_for(spec.episodes[0])
            write_episode_execution_envelope(
                envelope,
                spec.episodes[0],
                expected,
            )
            reconciled = advance_campaign(
                journal,
                spec,
                forbidden,
                reconcile_pending=lambda episode: (
                    envelope if episode == spec.episodes[0] else None
                ),
            )
            self.assertEqual(reconciled.action, "episode_reconciled")
            self.assertEqual(reconciled.progress.completed_episodes, 1)
            self.assertFalse(callback_called)

    def test_pending_reconciliation_rejects_another_study_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_spec = _bound_spec()
            second_spec = CampaignSpec(CAMPAIGN_TASK_IDS).bind(
                "sha256:" + "8" * 64
            )
            stale = root / "stale.execution.jsonl"
            write_episode_execution_envelope(
                stale,
                first_spec.episodes[0],
                _execution_for(first_spec.episodes[0]),
            )
            journal = root / "second-campaign.jsonl"
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                advance_campaign(
                    journal,
                    second_spec,
                    lambda _episode: (_ for _ in ()).throw(
                        RuntimeError("interrupted")
                    ),
                )

            with self.assertRaises(CampaignPendingEpisodeError):
                advance_campaign(
                    journal,
                    second_spec,
                    _execution_for,
                    reconcile_pending=lambda _episode: stale,
                )

    def test_active_time_limit_stops_before_new_intent_or_model_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "campaign.jsonl"
            spec = _bound_spec()
            advance_campaign(
                journal,
                spec,
                lambda episode: _execution_with_active_time(
                    episode,
                    CAMPAIGN_ACTIVE_TIME_LIMIT_NS,
                ),
            )
            before = journal.read_bytes()
            callback_called = False

            def forbidden(episode: CampaignEpisode) -> CampaignEpisodeExecution:
                nonlocal callback_called
                callback_called = True
                return _execution_for(episode)

            with self.assertRaises(CampaignActiveTimeLimitError) as caught:
                advance_campaign(journal, spec, forbidden)

            self.assertEqual(
                caught.exception.cumulative_active_wall_time_ns,
                CAMPAIGN_ACTIVE_TIME_LIMIT_NS,
            )
            self.assertEqual(
                caught.exception.limit_ns,
                CAMPAIGN_ACTIVE_TIME_LIMIT_NS,
            )
            self.assertEqual(
                str(caught.exception),
                "campaign cumulative active wall time reached the 18-hour limit",
            )
            self.assertFalse(callback_called)
            self.assertEqual(journal.read_bytes(), before)

    def test_pending_envelope_reconciles_before_active_time_limit_halts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "campaign.jsonl"
            spec = _bound_spec()
            advance_campaign(
                journal,
                spec,
                lambda episode: _execution_with_active_time(
                    episode,
                    CAMPAIGN_ACTIVE_TIME_LIMIT_NS - 1,
                ),
            )
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                advance_campaign(
                    journal,
                    spec,
                    lambda _episode: (_ for _ in ()).throw(
                        RuntimeError("interrupted")
                    ),
                )
            pending = spec.episodes[1]
            envelope = root / "episode-0002.execution.jsonl"
            write_episode_execution_envelope(
                envelope,
                pending,
                _execution_with_active_time(pending, 1),
            )
            reconciled = advance_campaign(
                journal,
                spec,
                _execution_for,
                reconcile_pending=lambda episode: (
                    envelope if episode == pending else None
                ),
            )
            self.assertEqual(reconciled.action, "episode_reconciled")
            self.assertEqual(reconciled.progress.completed_episodes, 2)
            before = journal.read_bytes()
            callback_called = False

            def forbidden(episode: CampaignEpisode) -> CampaignEpisodeExecution:
                nonlocal callback_called
                callback_called = True
                return _execution_for(episode)

            with self.assertRaises(CampaignActiveTimeLimitError):
                advance_campaign(journal, spec, forbidden)
            self.assertFalse(callback_called)
            self.assertEqual(journal.read_bytes(), before)

    def test_pending_reconciler_rejects_missing_invalid_and_unsealed_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "campaign.jsonl"
            spec = _bound_spec()

            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                advance_campaign(
                    journal,
                    spec,
                    lambda _episode: (_ for _ in ()).throw(
                        RuntimeError("interrupted")
                    ),
                )
            before = journal.read_bytes()

            def interrupt_reconciliation(_episode: CampaignEpisode) -> Path:
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                advance_campaign(
                    journal,
                    spec,
                    _execution_for,
                    reconcile_pending=interrupt_reconciliation,
                )
            self.assertEqual(journal.read_bytes(), before)

            valid = root / "valid.execution.jsonl"
            write_episode_execution_envelope(
                valid,
                spec.episodes[0],
                _execution_for(spec.episodes[0]),
            )
            unsealed = root / "unsealed.execution.jsonl"
            unsealed.write_bytes(valid.read_bytes().splitlines(keepends=True)[0])
            os.chmod(unsealed, 0o600)
            invalid = root / "invalid.execution.jsonl"
            invalid.write_bytes(valid.read_bytes().replace(b'"active_wall_time_ns":1000001', b'"active_wall_time_ns":1000002', 1))
            os.chmod(invalid, 0o600)

            for candidate in (root / "missing.jsonl", unsealed, invalid):
                with self.subTest(candidate=candidate.name):
                    with self.assertRaises(CampaignPendingEpisodeError):
                        advance_campaign(
                            journal,
                            spec,
                            _execution_for,
                            reconcile_pending=lambda _episode, path=candidate: path,
                        )
                    self.assertEqual(journal.read_bytes(), before)

    def test_execution_envelope_round_trip_security_and_toctou_defenses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _bound_spec()
            episode = spec.episodes[0]
            expected = _execution_for(episode)
            envelope = root / "episode.execution.jsonl"

            written = write_episode_execution_envelope(envelope, episode, expected)
            self.assertEqual(written, expected)
            self.assertEqual(
                load_episode_execution_envelope(envelope, episode),
                expected,
            )
            self.assertEqual(stat.S_IMODE(envelope.stat().st_mode), 0o600)
            self.assertTrue(inspect_journal(envelope, require_sealed=True).sealed)
            with self.assertRaisesRegex(CampaignExecutionEnvelopeError, "identity"):
                load_episode_execution_envelope(envelope, spec.episodes[1])
            with self.assertRaisesRegex(CampaignExecutionEnvelopeError, "already exists"):
                write_episode_execution_envelope(envelope, episode, expected)

            wrong_mode = root / "wrong-mode.jsonl"
            wrong_mode.write_bytes(envelope.read_bytes())
            os.chmod(wrong_mode, 0o644)
            symlink = root / "symlink.jsonl"
            symlink.symlink_to(envelope)
            directory_path = root / "directory.jsonl"
            directory_path.mkdir()
            for candidate, message in (
                (wrong_mode, "mode 0600"),
                (symlink, "non-symlink"),
                (directory_path, "regular"),
            ):
                with self.subTest(candidate=candidate.name):
                    with self.assertRaisesRegex(CampaignExecutionEnvelopeError, message):
                        load_episode_execution_envelope(candidate, episode)

            import edgeloopbench.intercode_campaign_ledger as ledger_module

            replacement = root / "replacement-target.jsonl"
            real_read = ledger_module.os.read
            swapped = False

            def swap_path_after_read(descriptor: int, count: int) -> bytes:
                nonlocal swapped
                payload = real_read(descriptor, count)
                if not swapped:
                    swapped = True
                    envelope.rename(replacement)
                    envelope.write_bytes(replacement.read_bytes())
                    os.chmod(envelope, 0o600)
                return payload

            with mock.patch.object(ledger_module.os, "read", swap_path_after_read):
                with self.assertRaisesRegex(
                    CampaignExecutionEnvelopeError,
                    "changed",
                ):
                    load_episode_execution_envelope(envelope, episode)

    def test_rejects_free_text_result_before_it_can_leak_into_the_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "campaign.jsonl"
            spec = _bound_spec()
            private_text = "/private/evaluator/gold.patch"

            with self.assertRaisesRegex(CampaignError, "stop_reason"):
                advance_campaign(
                    journal,
                    spec,
                    lambda episode: _execution_for(
                        episode, stop_reason=private_text
                    ),
                )

            self.assertNotIn(private_text.encode(), journal.read_bytes())
            before = journal.read_bytes()
            with self.assertRaises(CampaignPendingEpisodeError):
                advance_campaign(journal, spec, _execution_for)
            self.assertEqual(journal.read_bytes(), before)

    def test_enforces_arm_specific_prompt_and_human_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _bound_spec()
            direct = spec.episodes[0]

            with self.assertRaisesRegex(CampaignError, "Direct"):
                advance_campaign(
                    root / "bad-direct.jsonl",
                    spec,
                    lambda episode: CampaignEpisodeExecution(
                        replace(
                            _result_for(episode, model_calls=2),
                            initial_prompts=2,
                        ),
                        STUDY_BINDING_SHA256,
                        _controller_log_sha256(episode),
                        1,
                        _host_sample(episode, after=False),
                        _host_sample(episode, after=True),
                    ),
                )
            with self.assertRaisesRegex(CampaignError, "human_prompts"):
                advance_campaign(
                    root / "human.jsonl",
                    spec,
                    lambda episode: _execution_for(episode, human_prompts=1),
                )
            with self.assertRaisesRegex(CampaignError, "official_success"):
                advance_campaign(
                    root / "official.jsonl",
                    spec,
                    lambda episode: CampaignEpisodeExecution(
                        replace(_result_for(episode), official_success=True),
                        STUDY_BINDING_SHA256,
                        _controller_log_sha256(episode),
                        1,
                        _host_sample(episode, after=False),
                        _host_sample(episode, after=True),
                    ),
                )

            independent_journal = root / "bad-independent.jsonl"
            advance_campaign(independent_journal, spec, _execution_for)

            def wrong_prompt_class(episode: CampaignEpisode) -> CampaignEpisodeExecution:
                self.assertEqual(episode.arm, "independent_verified_sampling")
                result = replace(
                    _result_for(episode),
                    independent_sample_prompts=0,
                    feedback_followups=3,
                )
                return CampaignEpisodeExecution(
                    result,
                    STUDY_BINDING_SHA256,
                    _controller_log_sha256(episode),
                    1,
                    _host_sample(episode, after=False),
                    _host_sample(episode, after=True),
                )

            with self.assertRaisesRegex(CampaignError, "Independent"):
                advance_campaign(independent_journal, spec, wrong_prompt_class)
            self.assertEqual(direct.arm, "direct")

    def test_seals_only_after_all_240_terminal_and_loads_a_matrix_not_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "campaign.jsonl"
            spec = _bound_spec()
            callback_count = 0

            def execute(episode: CampaignEpisode) -> CampaignEpisodeExecution:
                nonlocal callback_count
                callback_count += 1
                return _execution_for(episode)

            for expected_completed in range(1, 241):
                outcome = advance_campaign(journal, spec, execute)
                self.assertEqual(outcome.progress.completed_episodes, expected_completed)
                self.assertEqual(outcome.progress.sealed, expected_completed == 240)

            self.assertEqual(callback_count, 240)
            self.assertEqual(_records(journal)[-1]["type"], "journal_sealed")
            no_op = advance_campaign(journal, spec, execute)
            self.assertEqual(no_op.action, "campaign_complete")
            self.assertEqual(callback_count, 240)

            matrix = load_complete_campaign_matrix(journal, spec)
            self.assertEqual(len(matrix.episodes), 240)
            self.assertEqual(matrix.total_model_calls, 780)
            self.assertEqual(matrix.total_logical_prompt_tokens, 78_780)
            self.assertEqual(matrix.total_logical_completion_tokens, 13_260)
            self.assertEqual(matrix.total_human_prompts, 0)
            self.assertEqual(
                matrix.total_active_wall_time_ns,
                sum(1_000_000 + episode.episode_index for episode in spec.episodes),
            )
            self.assertIn("not publication authority", matrix.limitation)

    def test_complete_matrix_loader_refuses_incomplete_or_invalid_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = _bound_spec()
            incomplete = root / "incomplete.jsonl"
            advance_campaign(incomplete, spec, _execution_for)

            with self.assertRaisesRegex(CampaignMatrixError, "incomplete"):
                load_complete_campaign_matrix(incomplete, spec)

            invalid = root / "invalid.jsonl"
            first = spec.episodes[0]
            outcome = advance_campaign(
                invalid,
                spec,
                lambda episode: _execution_for(
                    episode,
                    run_status="infrastructure_error",
                    stop_reason="prompt_token_telemetry_mismatch",
                ),
            )
            self.assertEqual(outcome.action, "episode_invalid")
            before = invalid.read_bytes()
            callback_called = False

            def forbidden(_episode: CampaignEpisode) -> CampaignEpisodeExecution:
                nonlocal callback_called
                callback_called = True
                return _execution_for(_episode)

            with self.assertRaises(CampaignInfrastructureInvalidError):
                advance_campaign(invalid, spec, forbidden)
            self.assertFalse(callback_called)
            self.assertEqual(invalid.read_bytes(), before)

            progress = inspect_campaign(invalid, spec)
            self.assertFalse(progress.sealed)
            self.assertEqual(progress.completed_episodes, 0)
            self.assertEqual(progress.invalid_episodes, 1)
            self.assertEqual(first.arm, "direct")
            with self.assertRaisesRegex(CampaignMatrixError, "invalid"):
                load_complete_campaign_matrix(invalid, spec)


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
