from __future__ import annotations

import json
import pickle
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path

from edgeloopbench.interactive_controller import (
    InteractiveBudget,
    InteractiveTask,
    run_interactive_strategy,
)
from edgeloopbench.interactive_environment import (
    ACTION_POLICY_OBSERVATIONS,
    ActionExecution,
    ActionPolicyFailureKind,
    AttemptEvaluationKind,
    EnvironmentCheckpoint,
    StrictEvaluation,
    TerminalSelection,
)
from edgeloopbench.intercode_evaluator import CanonicalStateSnapshot, StateEntry
from edgeloopbench.intercode_replay_environment import (
    V07_STRICT_REPLAY_EVALUATOR_SHA256,
    CandidateMaterial,
    EpisodeCheckpointRegistry,
    ReplayEnvironment,
    ReplayInfrastructureError,
    finalize_v07_terminal,
    make_candidate_progress_evaluator,
    make_strict_evaluator,
)
from edgeloopbench.model_adapter import (
    PHI4_MINI_RAW_PROFILE,
    InteractiveModelOutput,
    InteractiveModelRequest,
    PreparedPrompt,
    TranscriptMessage,
)


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def candidate(
    label: str,
    *,
    exit_code: int = 0,
    stdout: str = "done\n",
    stderr: str = "",
    state_changed: bool = True,
) -> CandidateMaterial:
    entries = ()
    if state_changed:
        entries = (
            StateEntry(
                path=f"workspace/{label}.txt",
                kind="file",
                mode=0o644,
                uid=0,
                gid=0,
                content_sha256=digest(f"content-{label}"),
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
        )
    return CandidateMaterial(
        state=CanonicalStateSnapshot(entries),
        collector_state_sha256=digest(f"state-{label}"),
        exit_code=exit_code,
        normalized_stdout=stdout,
        normalized_stderr=stderr,
        agent_observation=f"observation-{label}",
        state_changed=state_changed,
    )


def policy_failure(state_sha256: str) -> ActionExecution:
    kind = ActionPolicyFailureKind.TIMEOUT
    observation = ACTION_POLICY_OBSERVATIONS[kind]
    return ActionExecution(
        observation=observation,
        exit_code=None,
        state_sha256=state_sha256,
        output_sha256=digest(observation),
        admissible=False,
        state_changed=False,
        policy_failure=kind,
        safety_recovery_performed=True,
        safety_recovery_evidence_sha256=digest("recovered"),
    )


class FakeBoundary:
    def __init__(
        self,
        effects: dict[str, CandidateMaterial | ActionExecution],
    ) -> None:
        self.effects = effects
        self.actions: list[str] = []
        self.close_calls = 0

    def execute(self, action: str) -> CandidateMaterial | ActionExecution:
        self.actions.append(action)
        return self.effects[action]

    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls > 1:
            raise AssertionError("boundary was closed more than once")


class FakeBoundaryFactory:
    def __init__(
        self,
        *plans: dict[str, CandidateMaterial | ActionExecution],
        fallback: dict[str, CandidateMaterial | ActionExecution] | None = None,
    ) -> None:
        self.plans = list(plans)
        self.fallback = fallback
        self.boundaries: list[FakeBoundary] = []

    def __call__(self) -> FakeBoundary:
        index = len(self.boundaries)
        if index < len(self.plans):
            effects = self.plans[index]
        elif self.fallback is not None:
            effects = self.fallback
        else:
            raise AssertionError("unplanned fresh boundary")
        boundary = FakeBoundary(effects)
        self.boundaries.append(boundary)
        return boundary


class CandidateMaterialTests(unittest.TestCase):
    def test_private_material_is_frozen_redacted_and_not_serializable(self) -> None:
        material = candidate("secret-path", stdout="SECRET STDOUT")

        rendered = repr(material)

        self.assertEqual(rendered, "<CandidateMaterial redacted>")
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("secret-path", rendered)
        with self.assertRaises(FrozenInstanceError):
            material.exit_code = 9  # type: ignore[misc]
        with self.assertRaisesRegex(TypeError, "cannot be serialized"):
            pickle.dumps(material)

    def test_agent_observation_uses_the_same_normalized_text_policy(self) -> None:
        baseline = candidate("observation-policy")
        for observation in (
            "line one\r\nline two",
            "unsafe\x00text",
            "unsafe\u2028text",
        ):
            with self.subTest(observation=repr(observation)), self.assertRaisesRegex(
                ValueError, "agent observation"
            ):
                CandidateMaterial(
                    state=baseline.state,
                    collector_state_sha256=baseline.collector_state_sha256,
                    exit_code=0,
                    normalized_stdout="",
                    normalized_stderr="",
                    agent_observation=observation,
                    state_changed=False,
                )


class ReplayEnvironmentTests(unittest.TestCase):
    def test_checkpoint_references_are_deterministic_unique_and_digest_only(self) -> None:
        material = candidate("same")

        def run_once() -> tuple[EnvironmentCheckpoint, EnvironmentCheckpoint]:
            registry = EpisodeCheckpointRegistry()
            first_factory = FakeBoundaryFactory({"same": material})
            second_factory = FakeBoundaryFactory({"same": material})
            first = ReplayEnvironment(registry, first_factory)
            second = ReplayEnvironment(registry, second_factory)
            first.execute("same")
            second.execute("same")
            first_checkpoint = first.checkpoint()
            second_checkpoint = second.checkpoint()
            self.assertIs(registry.candidate_material(first_checkpoint), material)
            self.assertIs(registry.candidate_material(second_checkpoint), material)
            first.close()
            second.close()
            return first_checkpoint, second_checkpoint

        first_run = run_once()
        second_run = run_once()

        self.assertEqual(first_run, second_run)
        self.assertNotEqual(first_run[0].reference_sha256, first_run[1].reference_sha256)
        self.assertEqual(
            first_run[0].state_sha256,
            material.collector_state_sha256,
        )
        self.assertNotIn("same", repr(first_run[0]))

    def test_policy_failure_is_typed_and_does_not_create_a_checkpoint(self) -> None:
        failure = policy_failure(digest("initial"))
        factory = FakeBoundaryFactory({"hang": failure})
        environment = ReplayEnvironment(EpisodeCheckpointRegistry(), factory)

        execution = environment.execute("hang")

        self.assertIs(execution, failure)
        with self.assertRaisesRegex(ReplayInfrastructureError, "no admissible action"):
            environment.checkpoint()
        environment.close()
        environment.close()
        self.assertEqual(factory.boundaries[0].close_calls, 1)

    def test_restore_replays_recorded_history_on_a_fresh_boundary(self) -> None:
        first = candidate("first")
        second = candidate("second")
        plan = {"first": first, "second": second}
        factory = FakeBoundaryFactory(plan, plan)
        environment = ReplayEnvironment(EpisodeCheckpointRegistry(), factory)
        environment.execute("first")
        first_checkpoint = environment.checkpoint()
        environment.execute("second")
        environment.checkpoint()

        environment.restore(first_checkpoint)
        self.assertEqual(environment.checkpoint(), first_checkpoint)
        environment.close()

        self.assertEqual(len(factory.boundaries), 2)
        self.assertEqual(factory.boundaries[0].actions, ["first", "second"])
        self.assertEqual(factory.boundaries[1].actions, ["first"])
        self.assertEqual([item.close_calls for item in factory.boundaries], [1, 1])

    def test_restore_mismatch_is_a_redacted_infrastructure_error(self) -> None:
        recorded = candidate("good", stdout="recorded\n")
        mismatch = candidate("good", stdout="SECRET REPLAY MISMATCH\n")
        factory = FakeBoundaryFactory(
            {"good": recorded},
            {"good": mismatch},
        )
        environment = ReplayEnvironment(EpisodeCheckpointRegistry(), factory)
        environment.execute("good")
        checkpoint = environment.checkpoint()

        with self.assertRaises(ReplayInfrastructureError) as raised:
            environment.restore(checkpoint)
        environment.close()

        self.assertEqual(
            str(raised.exception),
            "checkpoint replay did not reproduce recorded material",
        )
        self.assertNotIn("SECRET", str(raised.exception))
        self.assertEqual([item.close_calls for item in factory.boundaries], [1, 1])


class ReplayEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gold = candidate("gold")
        self.registry = EpisodeCheckpointRegistry()
        self.factory = FakeBoundaryFactory({"finish": self.gold})
        self.environment = ReplayEnvironment(self.registry, self.factory)
        self.environment.execute("finish")
        self.checkpoint = self.environment.checkpoint()

    def tearDown(self) -> None:
        self.environment.close()

    def test_candidate_progress_is_gold_free_and_capped_at_point_eight(self) -> None:
        evaluate = make_candidate_progress_evaluator(self.registry)

        result = evaluate(self.checkpoint)

        self.assertEqual(result.reward, 0.8)
        self.assertFalse(result.official_success)
        self.assertEqual(result.evaluation_kind, AttemptEvaluationKind.EVALUATOR_DERIVED)

    def test_strict_endpoint_compares_state_exit_and_both_streams(self) -> None:
        exact = make_strict_evaluator(self.registry, self.gold)(self.checkpoint)
        wrong_exit = make_strict_evaluator(
            self.registry,
            candidate("gold", exit_code=7),
        )(self.checkpoint)
        wrong_stderr = make_strict_evaluator(
            self.registry,
            candidate("gold", stderr="unexpected\n"),
        )(self.checkpoint)

        self.assertTrue(exact.strict_success)
        self.assertFalse(wrong_exit.strict_success)
        self.assertFalse(wrong_stderr.strict_success)
        self.assertEqual(exact.evaluator_sha256, V07_STRICT_REPLAY_EVALUATOR_SHA256)

    def test_terminal_finalizer_calls_strict_once_only_for_authorized_selection(self) -> None:
        strict_calls: list[EnvironmentCheckpoint] = []

        def strict(checkpoint: EnvironmentCheckpoint) -> StrictEvaluation:
            strict_calls.append(checkpoint)
            return StrictEvaluation(True, V07_STRICT_REPLAY_EVALUATOR_SHA256)

        selected = TerminalSelection(
            checkpoint=self.checkpoint,
            selected_attempt=1,
            evaluation_kind=AttemptEvaluationKind.EVALUATOR_DERIVED,
            official_success=False,
        )
        authorized = finalize_v07_terminal(selected, strict, 1)
        empty = finalize_v07_terminal(
            TerminalSelection(None, None, None, False),
            strict,
            1,
        )
        aborted = finalize_v07_terminal(
            TerminalSelection(
                checkpoint=self.checkpoint,
                selected_attempt=1,
                evaluation_kind=AttemptEvaluationKind.EVALUATOR_DERIVED,
                official_success=False,
                aborted=True,
            ),
            strict,
            1,
        )

        self.assertEqual(strict_calls, [self.checkpoint])
        self.assertEqual(authorized.strict_evaluator_calls, 1)
        self.assertTrue(authorized.strict_evaluation.strict_success)  # type: ignore[union-attr]
        self.assertEqual(empty.strict_evaluator_calls, 0)
        self.assertEqual(aborted.strict_evaluator_calls, 0)


class ReplayControllerEndToEndTests(unittest.TestCase):
    task = InteractiveTask("bash-fs1-000", "Create the requested file.")

    def _run(
        self,
        directory: str,
        *,
        strategy: str,
        actions: list[str],
    ) -> tuple[object, FakeBoundaryFactory]:
        good = candidate("good")
        partial = candidate(
            "partial",
            exit_code=2,
            stdout="",
            stderr="",
            state_changed=False,
        )
        effects = {"good": good, "partial": partial}
        boundary_factory = FakeBoundaryFactory(fallback=effects)
        registry = EpisodeCheckpointRegistry()
        outputs = [
            InteractiveModelOutput(
                text=json.dumps({"command": action}),
                prompt_tokens=10,
                completion_tokens=5,
                total_duration_ns=1,
            )
            for action in actions
        ]
        requests: list[InteractiveModelRequest] = []

        def prepare(messages: tuple[TranscriptMessage, ...]) -> PreparedPrompt:
            rendered = PHI4_MINI_RAW_PROFILE.render(messages)
            return PreparedPrompt(
                rendered_prompt=rendered,
                prompt_tokens=10,
                prompt_sha256=digest(rendered),
                token_ids_sha256=digest(f"tokens-{len(requests)}"),
                renderer_profile_sha256=PHI4_MINI_RAW_PROFILE.sha256,
                tokenizer_artifact_sha256=digest("tokenizer"),
                model_artifact_sha256=PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
            )

        def model(request: InteractiveModelRequest) -> InteractiveModelOutput:
            requests.append(request)
            return outputs.pop(0)

        attempts = len(actions)
        result = run_interactive_strategy(
            strategy=strategy,
            task=self.task,
            model=model,
            prompt_preparer=prepare,
            environment_factory=lambda: ReplayEnvironment(
                registry,
                boundary_factory,
            ),
            attempt_evaluate=make_candidate_progress_evaluator(registry),
            strict_evaluate=make_strict_evaluator(registry, good),
            terminal_finalize=finalize_v07_terminal,
            budget=InteractiveBudget(
                attempts=attempts,
                prompt_tokens=20_000,
                completion_tokens=2_000,
                model_calls=attempts,
                environment_actions=attempts,
                evaluator_calls=attempts + 1,
                checkpoint_creates=attempts,
                checkpoint_restores=attempts,
                safety_recoveries=attempts,
                per_call_context_tokens=8_192,
                max_output_tokens=256,
            ),
            replicate_seed=11,
            event_log=Path(directory) / f"{strategy}.jsonl",
        )
        self.assertEqual(len(requests), attempts)
        return result, boundary_factory

    def test_all_four_controller_arms_use_the_replay_boundary_end_to_end(self) -> None:
        cases = {
            "direct": ["good"],
            "independent_verified_sampling": ["partial", "good"],
            "raw_feedback_loop": ["partial", "good"],
            "engineered_loop": ["good", "partial"],
        }

        with tempfile.TemporaryDirectory() as directory:
            for strategy, actions in cases.items():
                with self.subTest(strategy=strategy):
                    result, factory = self._run(
                        directory,
                        strategy=strategy,
                        actions=actions,
                    )
                    self.assertTrue(result.strict_success)
                    self.assertFalse(result.official_success)
                    self.assertEqual(result.model_calls, len(actions))
                    self.assertTrue(factory.boundaries)
                    self.assertEqual(
                        [boundary.close_calls for boundary in factory.boundaries],
                        [1] * len(factory.boundaries),
                    )

    def test_engineered_restore_mismatch_aborts_without_a_strict_call(self) -> None:
        good = candidate("good", stdout="recorded\n")
        partial = candidate(
            "partial",
            exit_code=2,
            stdout="",
            stderr="",
            state_changed=False,
        )
        mismatch = candidate("good", stdout="different\n")
        boundary_factory = FakeBoundaryFactory(
            {"good": good, "partial": partial},
            {"good": mismatch, "partial": partial},
        )
        registry = EpisodeCheckpointRegistry()
        strict_calls: list[EnvironmentCheckpoint] = []

        def strict(checkpoint: EnvironmentCheckpoint) -> StrictEvaluation:
            strict_calls.append(checkpoint)
            return StrictEvaluation(True, V07_STRICT_REPLAY_EVALUATOR_SHA256)

        outputs = [
            InteractiveModelOutput(
                json.dumps({"command": action}),
                10,
                5,
                1,
            )
            for action in ("good", "partial")
        ]

        def prepare(messages: tuple[TranscriptMessage, ...]) -> PreparedPrompt:
            rendered = PHI4_MINI_RAW_PROFILE.render(messages)
            return PreparedPrompt(
                rendered,
                10,
                digest(rendered),
                digest(f"tokens-{len(outputs)}"),
                PHI4_MINI_RAW_PROFILE.sha256,
                digest("tokenizer"),
                PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
            )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ReplayInfrastructureError,
                "checkpoint replay did not reproduce recorded material",
            ):
                run_interactive_strategy(
                    strategy="engineered_loop",
                    task=self.task,
                    model=lambda _request: outputs.pop(0),
                    prompt_preparer=prepare,
                    environment_factory=lambda: ReplayEnvironment(
                        registry,
                        boundary_factory,
                    ),
                    attempt_evaluate=make_candidate_progress_evaluator(registry),
                    strict_evaluate=strict,
                    terminal_finalize=finalize_v07_terminal,
                    budget=InteractiveBudget(
                        attempts=2,
                        prompt_tokens=20_000,
                        completion_tokens=2_000,
                        model_calls=2,
                        environment_actions=2,
                        evaluator_calls=3,
                        checkpoint_creates=2,
                        checkpoint_restores=2,
                        safety_recoveries=2,
                        per_call_context_tokens=8_192,
                        max_output_tokens=256,
                    ),
                    replicate_seed=11,
                    event_log=Path(directory) / "mismatch.jsonl",
                )

        self.assertEqual(strict_calls, [])
        self.assertEqual(
            [boundary.close_calls for boundary in boundary_factory.boundaries],
            [1, 1],
        )


if __name__ == "__main__":
    unittest.main()
