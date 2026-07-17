from __future__ import annotations

import dataclasses
import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.interactive_controller import InteractiveBudget
from edgeloopbench.intercode_campaign_ledger import (
    CAMPAIGN_TASK_IDS,
    CampaignEpisode,
    CampaignEpisodeExecution,
    CampaignSpec,
    load_episode_execution_envelope,
)
from edgeloopbench.intercode_evaluator import CanonicalStateSnapshot, StateEntry
from edgeloopbench.intercode_host_safety import HostSafetySample
from edgeloopbench.intercode_replay_environment import CandidateMaterial
from edgeloopbench.intercode_source import PublicBashTask
from edgeloopbench.intercode_v07_docker_qualification import (
    _issue_v07_trusted_gold_material,
)
from edgeloopbench.intercode_v07_runner import (
    V07EpisodeRun,
    run_v07_calibration_episode,
    run_v07_episode,
)
from edgeloopbench.journal import inspect_journal
from edgeloopbench.model_adapter import (
    ExactPromptPreparer,
    OllamaGenerationConfig,
    OllamaRawModel,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
    RestrictedRawRenderingProfile,
    TokenCount,
)


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


EXECUTION_AUTHORITY_SHA256 = digest("v07-runner-execution-authority")


def candidate(
    label: str,
    *,
    stdout: str = "ok\n",
    observation: str = "ok\n",
) -> CandidateMaterial:
    entry = StateEntry(
        path=f"workspace/{label}.txt",
        kind="file",
        mode=0o644,
        uid=0,
        gid=0,
        content_sha256=digest(f"content-{label}"),
        symlink_target=None,
        hardlink_group_sha256=None,
    )
    return CandidateMaterial(
        state=CanonicalStateSnapshot((entry,)),
        collector_state_sha256=digest(f"state-{label}"),
        exit_code=0,
        normalized_stdout=stdout,
        normalized_stderr="",
        agent_observation=observation,
        state_changed=True,
    )


def trusted_gold(
    material: CandidateMaterial,
    *,
    task_id: str = CAMPAIGN_TASK_IDS[0],
):
    return _issue_v07_trusted_gold_material(
        task_id=task_id,
        source_capability_sha256=digest(f"capability-{task_id}"),
        image_id=digest("image-fs1"),
        evaluator_sha256=digest("evaluator"),
        state_normalization_sha256=digest("normalizer"),
        replay_receipt_sha256=digest(f"receipt-{task_id}"),
        material=material,
    )


class FixedTokenCounter:
    def __init__(self, profile: RestrictedRawRenderingProfile) -> None:
        self.profile = profile
        self.prompts: list[str] = []

    def count(self, prompt: str) -> TokenCount:
        self.prompts.append(prompt)
        return TokenCount(
            count=12,
            token_ids_sha256=digest(f"tokens-{len(self.prompts)}"),
            tokenizer_artifact_sha256=digest("tokenizer"),
            model_artifact_sha256=self.profile.model_artifact_sha256,
        )


class FakeTransport:
    def __init__(
        self,
        commands: list[str],
        timeline: list[str],
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.commands = list(commands)
        self.timeline = timeline
        self.failure = failure
        self.payloads: list[bytes] = []

    def __call__(self, payload: bytes) -> bytes:
        self.timeline.append("model")
        self.payloads.append(payload)
        if self.failure is not None:
            raise self.failure
        if not self.commands:
            raise AssertionError("unplanned model request")
        request = json.loads(payload)
        response = {
            "model": request["model"],
            "done": True,
            "done_reason": "stop",
            "response": json.dumps({"command": self.commands.pop(0)}),
            "prompt_eval_count": 12,
            "eval_count": 6,
            "total_duration": 1_000_000,
        }
        return json.dumps(response, separators=(",", ":")).encode("utf-8")


class FakeBoundary:
    def __init__(self, owner: FakeBoundaryFactory) -> None:
        self.owner = owner
        self.closed = False

    def execute(self, action: str) -> CandidateMaterial:
        self.owner.timeline.append("execute")
        self.owner.actions.append(action)
        if not self.owner.materials:
            raise AssertionError("unplanned environment action")
        return self.owner.materials.pop(0)

    def close(self) -> None:
        if self.closed:
            raise AssertionError("boundary closed twice")
        self.closed = True
        self.owner.timeline.append("close")


class FakeBoundaryFactory:
    def __init__(
        self,
        materials: list[CandidateMaterial],
        timeline: list[str],
    ) -> None:
        self.materials = list(materials)
        self.timeline = timeline
        self.actions: list[str] = []
        self.boundaries: list[FakeBoundary] = []

    def __call__(self) -> FakeBoundary:
        self.timeline.append("boundary")
        boundary = FakeBoundary(self)
        self.boundaries.append(boundary)
        return boundary


def host_sample(captured_monotonic_ns: int) -> HostSafetySample:
    return HostSafetySample(
        captured_unix_ns=1_000_000_000 + captured_monotonic_ns,
        captured_monotonic_ns=captured_monotonic_ns,
        boot_time_unix_microseconds=123,
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


class HostHarness:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.clock_values = [200, 800]

    def before(self) -> HostSafetySample:
        self.timeline.append("before")
        return host_sample(100)

    def after(self) -> HostSafetySample:
        self.timeline.append("after")
        return host_sample(900)

    def monotonic_ns(self) -> int:
        self.timeline.append("clock")
        if not self.clock_values:
            raise AssertionError("unplanned monotonic clock call")
        return self.clock_values.pop(0)


def generation(
    profile: RestrictedRawRenderingProfile,
) -> OllamaGenerationConfig:
    return OllamaGenerationConfig(
        profile=profile,
        runtime_version="0.31.1",
        runtime_binary_sha256=digest("ollama-binary"),
        context_tokens=4_096,
        num_batch=128,
        num_gpu=99,
        main_gpu=0,
        use_mmap=True,
        num_thread=8,
        draft_num_predict=0,
        temperature=0.2,
        top_k=40,
        top_p=0.9,
        min_p=0.0,
        typical_p=1.0,
        repeat_last_n=64,
        repeat_penalty=1.1,
        presence_penalty=0.0,
        frequency_penalty=0.0,
        stop=("<stop>",),
        keep_alive_seconds=-1,
        request_timeout_seconds=120.0,
    )


def budget() -> InteractiveBudget:
    return InteractiveBudget(
        attempts=4,
        prompt_tokens=16_380,
        completion_tokens=2_048,
        model_calls=4,
        environment_actions=4,
        evaluator_calls=5,
        checkpoint_creates=4,
        checkpoint_restores=4,
        safety_recoveries=4,
        per_call_context_tokens=4_096,
        max_output_tokens=512,
    )


class InterCodeV07RunnerTests(unittest.TestCase):
    spec = CampaignSpec(CAMPAIGN_TASK_IDS)
    task = PublicBashTask(
        task_id=CAMPAIGN_TASK_IDS[0],
        query="Create workspace/done.txt containing done.",
        stratum="fs1",
    )
    calibration_episode = CampaignEpisode(
        1,
        QWEN35_RAW_PROFILE.model,
        "bash-calibration-000",
        "direct",
        11,
    )
    calibration_task = PublicBashTask(
        task_id="bash-calibration-000",
        query="Count files under the testbed directory.",
        stratum="calibration",
    )

    def runtime(
        self,
        commands: list[str],
        timeline: list[str],
        *,
        profile: RestrictedRawRenderingProfile = QWEN35_RAW_PROFILE,
        failure: BaseException | None = None,
    ) -> tuple[ExactPromptPreparer, OllamaRawModel, FakeTransport]:
        counter = FixedTokenCounter(profile)
        preparer = ExactPromptPreparer(profile, counter)
        transport = FakeTransport(commands, timeline, failure=failure)
        model = OllamaRawModel(generation(profile), transport=transport)
        return preparer, model, transport

    def test_direct_composes_adapter_replay_and_sealed_campaign_execution(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        material = candidate("done")
        factory = FakeBoundaryFactory([material], timeline)
        preparer, model, transport = self.runtime(["touch workspace/done.txt"], timeline)

        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "episode-0001.jsonl"
            execution_envelope = Path(directory) / "episode-0001.execution.jsonl"

            def after_controller_seal() -> HostSafetySample:
                self.assertTrue(
                    inspect_journal(event_log, require_sealed=True).sealed
                )
                self.assertFalse(execution_envelope.exists())
                return host.after()

            execution = run_v07_episode(
                episode=self.spec.episodes[0],
                task=self.task,
                private_gold=trusted_gold(material),
                model=model,
                prompt_preparer=preparer,
                boundary_factory=factory,
                budget=budget(),
                before_episode_admission=host.before,
                after_episode_admission=after_controller_seal,
                execution_authority_sha256=EXECUTION_AUTHORITY_SHA256,
                monotonic_ns=host.monotonic_ns,
                event_log=event_log,
                execution_envelope=execution_envelope,
            )
            inspection = inspect_journal(event_log, require_sealed=True)
            mode = stat.S_IMODE(event_log.stat().st_mode)
            envelope_inspection = inspect_journal(
                execution_envelope,
                require_sealed=True,
            )
            envelope_mode = stat.S_IMODE(execution_envelope.stat().st_mode)
            reopened = load_episode_execution_envelope(
                execution_envelope,
                self.spec.episodes[0],
            )

        self.assertIsInstance(execution, V07EpisodeRun)
        self.assertIsInstance(execution.execution, CampaignEpisodeExecution)
        self.assertTrue(execution.execution.result.strict_success)
        self.assertEqual(execution.execution.result.model_calls, 1)
        self.assertEqual(execution.execution.result.initial_prompts, 1)
        self.assertEqual(execution.execution.result.independent_sample_prompts, 0)
        self.assertEqual(execution.execution.result.feedback_followups, 0)
        self.assertEqual(
            execution.execution.controller_log_sha256,
            "sha256:" + inspection.last_event_sha256,
        )
        self.assertEqual(execution.active_wall_time_ns, 600)
        self.assertEqual(execution.before_host_admission, host_sample(100))
        self.assertEqual(execution.after_host_admission, host_sample(900))
        self.assertEqual(mode, 0o600)
        self.assertTrue(envelope_inspection.sealed)
        self.assertEqual(envelope_mode, 0o600)
        self.assertEqual(reopened, execution.execution)
        self.assertEqual(len(transport.payloads), 1)
        self.assertEqual(timeline[0], "before")
        self.assertEqual(timeline[-1], "after")
        self.assertLess(timeline.index("close"), timeline.index("after"))

    def test_raw_feedback_uses_at_most_four_calls_and_three_followups(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        materials = [candidate(f"step-{index}") for index in range(1, 5)]
        factory = FakeBoundaryFactory(materials, timeline)
        commands = [f"step-{index}" for index in range(1, 5)]
        preparer, model, transport = self.runtime(commands, timeline)
        raw_episode = self.spec.episodes[3]
        self.assertEqual(raw_episode.arm, "raw_feedback_loop")

        with tempfile.TemporaryDirectory() as directory:
            execution_envelope = Path(directory) / "episode-0004.execution.jsonl"
            execution = run_v07_episode(
                episode=raw_episode,
                task=self.task,
                private_gold=trusted_gold(materials[-1]),
                model=model,
                prompt_preparer=preparer,
                boundary_factory=factory,
                budget=budget(),
                before_episode_admission=host.before,
                after_episode_admission=host.after,
                execution_authority_sha256=EXECUTION_AUTHORITY_SHA256,
                monotonic_ns=host.monotonic_ns,
                event_log=Path(directory) / "episode-0004.jsonl",
                execution_envelope=execution_envelope,
            )

        self.assertEqual(execution.execution.result.model_calls, 4)
        self.assertEqual(execution.execution.result.initial_prompts, 1)
        self.assertEqual(execution.execution.result.feedback_followups, 3)
        self.assertEqual(execution.execution.result.independent_sample_prompts, 0)
        self.assertEqual(len(transport.payloads), 4)
        self.assertEqual(factory.actions, commands)

    def test_private_gold_and_evaluator_material_never_enter_prompt_or_journal(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        hidden_gold = candidate(
            "hidden-gold-marker",
            stdout="SECRET_GOLD_OUTPUT\n",
            observation="private/evaluator/path",
        )
        factory = FakeBoundaryFactory([candidate("candidate")], timeline)
        preparer, model, transport = self.runtime(["true"], timeline)

        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "episode.jsonl"
            execution_envelope = Path(directory) / "episode.execution.jsonl"
            run_v07_episode(
                episode=self.spec.episodes[0],
                task=self.task,
                private_gold=trusted_gold(hidden_gold),
                model=model,
                prompt_preparer=preparer,
                boundary_factory=factory,
                budget=budget(),
                before_episode_admission=host.before,
                after_episode_admission=host.after,
                execution_authority_sha256=EXECUTION_AUTHORITY_SHA256,
                monotonic_ns=host.monotonic_ns,
                event_log=event_log,
                execution_envelope=execution_envelope,
            )
            visible = b"\n".join(
                (
                    *transport.payloads,
                    event_log.read_bytes(),
                    execution_envelope.read_bytes(),
                )
            )

        for secret in (
            b"hidden-gold-marker",
            b"SECRET_GOLD_OUTPUT",
            b"private/evaluator/path",
        ):
            self.assertNotIn(secret, visible)

    def test_controller_failure_runs_post_hook_but_never_returns_execution(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        factory = FakeBoundaryFactory([candidate("unused")], timeline)
        preparer, model, _transport = self.runtime(
            ["unused"],
            timeline,
            failure=RuntimeError("simulated model failure"),
        )

        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "episode.jsonl"
            execution_envelope = Path(directory) / "episode.execution.jsonl"
            with self.assertRaisesRegex(RuntimeError, "simulated model failure"):
                run_v07_episode(
                    episode=self.spec.episodes[0],
                    task=self.task,
                    private_gold=trusted_gold(candidate("gold")),
                    model=model,
                    prompt_preparer=preparer,
                    boundary_factory=factory,
                    budget=budget(),
                    before_episode_admission=host.before,
                    after_episode_admission=host.after,
                    execution_authority_sha256=EXECUTION_AUTHORITY_SHA256,
                    monotonic_ns=host.monotonic_ns,
                    event_log=event_log,
                    execution_envelope=execution_envelope,
                )
            inspection = inspect_journal(event_log)

        self.assertEqual(timeline[0], "before")
        self.assertEqual(timeline[-1], "after")
        self.assertFalse(inspection.sealed)
        self.assertFalse(execution_envelope.exists())
        self.assertEqual(factory.boundaries, [])

    def test_post_admission_failure_withholds_sealed_execution(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        material = candidate("done")
        factory = FakeBoundaryFactory([material], timeline)
        preparer, model, _transport = self.runtime(["true"], timeline)

        def refuse_after() -> HostSafetySample:
            timeline.append("after")
            raise RuntimeError("post admission refused")

        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "episode.jsonl"
            execution_envelope = Path(directory) / "episode.execution.jsonl"
            with self.assertRaisesRegex(RuntimeError, "post admission refused"):
                run_v07_episode(
                    episode=self.spec.episodes[0],
                    task=self.task,
                    private_gold=trusted_gold(material),
                    model=model,
                    prompt_preparer=preparer,
                    boundary_factory=factory,
                    budget=budget(),
                    before_episode_admission=host.before,
                    after_episode_admission=refuse_after,
                    execution_authority_sha256=EXECUTION_AUTHORITY_SHA256,
                    monotonic_ns=host.monotonic_ns,
                    event_log=event_log,
                    execution_envelope=execution_envelope,
                )
            inspection = inspect_journal(event_log, require_sealed=True)

        self.assertTrue(inspection.sealed)
        self.assertFalse(execution_envelope.exists())
        self.assertEqual(timeline[-1], "after")

    def test_wrong_model_task_or_expanded_cap_is_rejected_before_admission(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        factory = FakeBoundaryFactory([candidate("unused")], timeline)
        phi_preparer, phi_model, _transport = self.runtime(
            ["true"], timeline, profile=PHI4_MINI_RAW_PROFILE
        )
        invalid_task = dataclasses.replace(self.task, task_id=CAMPAIGN_TASK_IDS[1])
        expanded = dataclasses.replace(budget(), model_calls=5)
        expanded_prompt_budget = dataclasses.replace(
            budget(), prompt_tokens=16_381
        )

        cases = (
            {"model": phi_model, "prompt_preparer": phi_preparer},
            {"task": invalid_task},
            {"budget": expanded},
            {"budget": expanded_prompt_budget},
            {"private_gold": candidate("unsealed-gold")},
        )
        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "must-not-exist.jsonl"
            execution_envelope = Path(directory) / "must-not-exist.execution.jsonl"
            for changes in cases:
                with self.subTest(changes=tuple(changes)):
                    qwen_preparer, qwen_model, _transport = self.runtime(["true"], timeline)
                    arguments = {
                        "episode": self.spec.episodes[0],
                        "task": self.task,
                        "private_gold": trusted_gold(candidate("gold")),
                        "model": qwen_model,
                        "prompt_preparer": qwen_preparer,
                        "boundary_factory": factory,
                        "budget": budget(),
                        "before_episode_admission": host.before,
                        "after_episode_admission": host.after,
                        "execution_authority_sha256": EXECUTION_AUTHORITY_SHA256,
                        "monotonic_ns": host.monotonic_ns,
                        "event_log": event_log,
                        "execution_envelope": execution_envelope,
                    }
                    arguments.update(changes)
                    with self.assertRaises(ValueError):
                        run_v07_episode(**arguments)  # type: ignore[arg-type]
                    self.assertFalse(event_log.exists())
                    self.assertFalse(execution_envelope.exists())

            qwen_preparer, qwen_model, _transport = self.runtime(["true"], timeline)
            with self.assertRaisesRegex(ValueError, "distinct"):
                run_v07_episode(
                    episode=self.spec.episodes[0],
                    task=self.task,
                    private_gold=trusted_gold(candidate("gold")),
                    model=qwen_model,
                    prompt_preparer=qwen_preparer,
                    boundary_factory=factory,
                    budget=budget(),
                    before_episode_admission=host.before,
                    after_episode_admission=host.after,
                    execution_authority_sha256=EXECUTION_AUTHORITY_SHA256,
                    monotonic_ns=host.monotonic_ns,
                    event_log=event_log,
                    execution_envelope=event_log,
                )

        self.assertEqual(timeline, [])

    def test_calibration_entrypoint_runs_only_the_exact_frozen_calibration_row(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        material = candidate("calibration")
        factory = FakeBoundaryFactory([material], timeline)
        preparer, model, _transport = self.runtime(["find testbed"], timeline)

        with tempfile.TemporaryDirectory() as directory:
            controller = Path(directory) / "calibration-001.jsonl"
            envelope = Path(directory) / "calibration-001.execution.jsonl"
            execution = run_v07_calibration_episode(
                episode=self.calibration_episode,
                task=self.calibration_task,
                private_gold=trusted_gold(
                    material,
                    task_id=self.calibration_episode.task_id,
                ),
                model=model,
                prompt_preparer=preparer,
                boundary_factory=factory,
                budget=budget(),
                before_episode_admission=host.before,
                after_episode_admission=host.after,
                execution_authority_sha256=EXECUTION_AUTHORITY_SHA256,
                monotonic_ns=host.monotonic_ns,
                event_log=controller,
                execution_envelope=envelope,
            )
            reopened = load_episode_execution_envelope(
                envelope,
                self.calibration_episode,
            )

        self.assertIs(type(execution), V07EpisodeRun)
        self.assertEqual(execution.execution, reopened)
        self.assertEqual(execution.execution.result.model_calls, 1)

    def test_formal_and_calibration_entrypoints_reject_each_others_rows(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        factory = FakeBoundaryFactory([candidate("unused")], timeline)
        preparer, model, _transport = self.runtime(["true"], timeline)

        with tempfile.TemporaryDirectory() as directory:
            common = {
                "model": model,
                "prompt_preparer": preparer,
                "boundary_factory": factory,
                "budget": budget(),
                "before_episode_admission": host.before,
                "after_episode_admission": host.after,
                "execution_authority_sha256": EXECUTION_AUTHORITY_SHA256,
                "monotonic_ns": host.monotonic_ns,
            }
            with self.assertRaisesRegex(ValueError, "formal schedule"):
                run_v07_episode(
                    episode=self.calibration_episode,
                    task=self.calibration_task,
                    private_gold=trusted_gold(
                        candidate("calibration-gold"),
                        task_id=self.calibration_episode.task_id,
                    ),
                    event_log=Path(directory) / "formal.jsonl",
                    execution_envelope=Path(directory) / "formal.execution.jsonl",
                    **common,
                )
            with self.assertRaisesRegex(ValueError, "calibration schedule"):
                run_v07_calibration_episode(
                    episode=self.spec.episodes[0],
                    task=self.task,
                    private_gold=trusted_gold(candidate("formal-gold")),
                    event_log=Path(directory) / "calibration.jsonl",
                    execution_envelope=Path(directory) / "calibration.execution.jsonl",
                    **common,
                )

        self.assertEqual(timeline, [])

    def test_calibration_rejects_wrong_profile_budget_and_task_bound_gold(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        factory = FakeBoundaryFactory([candidate("unused")], timeline)
        qwen_preparer, qwen_model, _transport = self.runtime(["true"], timeline)
        phi_preparer, phi_model, _transport = self.runtime(
            ["true"], timeline, profile=PHI4_MINI_RAW_PROFILE
        )
        exact_gold = trusted_gold(
            candidate("gold"), task_id=self.calibration_episode.task_id
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {
                "episode": self.calibration_episode,
                "task": self.calibration_task,
                "private_gold": exact_gold,
                "model": qwen_model,
                "prompt_preparer": qwen_preparer,
                "boundary_factory": factory,
                "budget": budget(),
                "before_episode_admission": host.before,
                "after_episode_admission": host.after,
                "execution_authority_sha256": EXECUTION_AUTHORITY_SHA256,
                "monotonic_ns": host.monotonic_ns,
                "event_log": root / "controller.jsonl",
                "execution_envelope": root / "execution.jsonl",
            }
            for changes in (
                {"model": phi_model, "prompt_preparer": phi_preparer},
                {"budget": dataclasses.replace(budget(), max_output_tokens=513)},
                {"private_gold": trusted_gold(candidate("wrong-task"))},
            ):
                arguments = {**base, **changes}
                with self.subTest(changes=tuple(changes)):
                    with self.assertRaises(ValueError):
                        run_v07_calibration_episode(**arguments)
                    self.assertFalse(base["event_log"].exists())
                    self.assertFalse(base["execution_envelope"].exists())

        self.assertEqual(timeline, [])

    def test_calibration_host_samples_must_enclose_active_time(self) -> None:
        timeline: list[str] = []
        factory = FakeBoundaryFactory([candidate("candidate")], timeline)
        preparer, model, _transport = self.runtime(["true"], timeline)
        clock = iter((200, 800)).__next__

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "before episode finish"):
                run_v07_calibration_episode(
                    episode=self.calibration_episode,
                    task=self.calibration_task,
                    private_gold=trusted_gold(
                        candidate("gold"), task_id=self.calibration_episode.task_id
                    ),
                    model=model,
                    prompt_preparer=preparer,
                    boundary_factory=factory,
                    budget=budget(),
                    before_episode_admission=lambda: host_sample(100),
                    after_episode_admission=lambda: host_sample(700),
                    execution_authority_sha256=EXECUTION_AUTHORITY_SHA256,
                    monotonic_ns=clock,
                    event_log=Path(directory) / "controller.jsonl",
                    execution_envelope=Path(directory) / "execution.jsonl",
                )

    def test_runner_refuses_preexisting_controller_or_envelope_before_admission(self) -> None:
        timeline: list[str] = []
        host = HostHarness(timeline)
        factory = FakeBoundaryFactory([candidate("unused")], timeline)
        preparer, model, _transport = self.runtime(["true"], timeline)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for occupied in ("controller", "envelope"):
                controller = root / f"{occupied}.jsonl"
                envelope = root / f"{occupied}.execution.jsonl"
                (controller if occupied == "controller" else envelope).touch(mode=0o600)
                with self.subTest(occupied=occupied):
                    with self.assertRaisesRegex(ValueError, "must not exist"):
                        run_v07_calibration_episode(
                            episode=self.calibration_episode,
                            task=self.calibration_task,
                            private_gold=trusted_gold(
                                candidate(f"{occupied}-gold"),
                                task_id=self.calibration_episode.task_id,
                            ),
                            model=model,
                            prompt_preparer=preparer,
                            boundary_factory=factory,
                            budget=budget(),
                            before_episode_admission=host.before,
                            after_episode_admission=host.after,
                            execution_authority_sha256=(
                                EXECUTION_AUTHORITY_SHA256
                            ),
                            monotonic_ns=host.monotonic_ns,
                            event_log=controller,
                            execution_envelope=envelope,
                        )

        self.assertEqual(timeline, [])


if __name__ == "__main__":
    unittest.main()
