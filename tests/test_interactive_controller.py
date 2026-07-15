from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from edgeloopbench.interactive_controller import (
    InteractiveBudget,
    InteractiveModelOutput,
    InteractiveModelRequest,
    InteractiveTask,
    run_interactive_strategy,
)
from edgeloopbench.interactive_environment import (
    ActionExecution,
    ActionPolicyFailureKind,
    AttemptEvaluation,
    AttemptEvaluationKind,
    EnvironmentCheckpoint,
    StrictEvaluation,
    TerminalFinalization,
    TerminalSelection,
)
from edgeloopbench.model_adapter import (
    PHI4_MINI_RAW_PROFILE,
    PreparedPrompt,
    TranscriptMessage,
)


STRATEGIES = (
    "direct",
    "independent_verified_sampling",
    "raw_feedback_loop",
    "engineered_loop",
)


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


class FakeEnvironment:
    """Small stateful environment whose opaque checkpoints stay in the factory."""

    def __init__(self, owner: FakeEnvironmentFactory, identifier: int) -> None:
        self.owner = owner
        self.identifier = identifier
        self.actions: list[str] = []
        self.restore_calls: list[EnvironmentCheckpoint] = []
        self.state_digest = digest("initial")
        self.closed = False

    def execute(self, action: str) -> ActionExecution:
        self.actions.append(action)
        self.owner.timeline.append(("execute", self.identifier, action))
        observation, state_digest, admissible = self.owner.effects.get(
            action,
            (f"output for {action}", digest(action), True),
        )
        previous_state_digest = self.state_digest
        if not admissible:
            state_digest = previous_state_digest
        self.state_digest = state_digest
        return ActionExecution(
            observation=observation,
            exit_code=0 if admissible else None,
            state_sha256=state_digest,
            output_sha256=digest(observation),
            admissible=admissible,
            state_changed=state_digest != previous_state_digest,
            policy_failure=(
                None if admissible else ActionPolicyFailureKind.TIMEOUT
            ),
            safety_recovery_performed=not admissible,
            safety_recovery_evidence_sha256=(
                None if admissible else digest(f"recovery-{self.identifier}-{action}")
            ),
        )

    def checkpoint(self) -> EnvironmentCheckpoint:
        number = len(self.owner.checkpoints) + 1
        checkpoint = EnvironmentCheckpoint(
            reference_sha256=digest(f"env-{self.identifier}-checkpoint-{number}"),
            state_sha256=self.state_digest,
        )
        self.owner.checkpoints[checkpoint.reference_sha256] = {
            "action": self.actions[-1],
            "state_digest": self.state_digest,
        }
        self.owner.timeline.append(
            ("checkpoint", self.identifier, checkpoint.reference_sha256)
        )
        return checkpoint

    def restore(self, checkpoint: EnvironmentCheckpoint) -> None:
        self.restore_calls.append(checkpoint)
        self.state_digest = checkpoint.state_sha256
        self.owner.timeline.append(
            ("restore", self.identifier, checkpoint.reference_sha256)
        )

    def close(self) -> None:
        self.closed = True
        self.owner.timeline.append(("close", self.identifier, ""))


class FakeEnvironmentFactory:
    def __init__(
        self,
        *,
        effects: dict[str, tuple[str, str, bool]] | None = None,
        rewards: dict[str, float] | None = None,
    ) -> None:
        self.effects = effects or {}
        self.rewards = rewards or {}
        self._private_output = "HIDDEN ATTEMPT EVALUATOR OUTPUT"
        self.environments: list[FakeEnvironment] = []
        self.checkpoints: dict[str, dict[str, str]] = {}
        self.timeline: list[tuple[str, int, str]] = []
        self.evaluator_calls: list[EnvironmentCheckpoint] = []
        self.terminal_calls: list[tuple[TerminalSelection, int]] = []
        self.terminal_close_observations: list[bool] = []

    def create(self) -> FakeEnvironment:
        environment = FakeEnvironment(self, len(self.environments) + 1)
        self.environments.append(environment)
        return environment

    def evaluate(self, checkpoint: EnvironmentCheckpoint) -> AttemptEvaluation:
        self.evaluator_calls.append(checkpoint)
        action = self.checkpoints[checkpoint.reference_sha256]["action"]
        reward = self.rewards.get(action, 0.0)
        return AttemptEvaluation(
            reward=reward,
            official_success=reward == 1.0,
        )

    def finalize(
        self,
        selection: TerminalSelection,
        strict_evaluate: Callable[[EnvironmentCheckpoint], StrictEvaluation] | None,
        evaluator_call_limit: int,
    ) -> TerminalFinalization:
        self.terminal_calls.append((selection, evaluator_call_limit))
        self.terminal_close_observations.append(
            all(environment.closed for environment in self.environments)
        )
        if strict_evaluate is None:
            return TerminalFinalization(None, 0, 0)
        assert selection.checkpoint is not None
        strict = strict_evaluate(selection.checkpoint)
        return TerminalFinalization(strict, 1, 0)


class InteractiveControllerTests(unittest.TestCase):
    task = InteractiveTask(
        task_id="bash-fs1-000",
        query="Create reports/done.txt containing the word done.",
    )

    def budget(
        self,
        *,
        attempts: int = 4,
        safety_recoveries: int | None = None,
    ) -> InteractiveBudget:
        return InteractiveBudget(
            attempts=attempts,
            prompt_tokens=20_000,
            completion_tokens=2_000,
            model_calls=attempts,
            environment_actions=attempts,
            evaluator_calls=attempts + 1,
            checkpoint_creates=attempts,
            checkpoint_restores=attempts,
            safety_recoveries=(
                attempts if safety_recoveries is None else safety_recoveries
            ),
            per_call_context_tokens=8_192,
            max_output_tokens=256,
        )

    def output(
        self,
        action: str | None = None,
        *,
        text: str | None = None,
        prompt_tokens: int = 100,
        completion_tokens: int = 10,
    ) -> InteractiveModelOutput:
        if text is None:
            text = json.dumps({"command": action})
        return InteractiveModelOutput(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_duration_ns=1_000_000,
        )

    def execute_strategy(
        self,
        directory: str,
        *,
        strategy: str,
        outputs: list[InteractiveModelOutput],
        factory: FakeEnvironmentFactory,
        strict_evaluate: Callable[[EnvironmentCheckpoint], StrictEvaluation] | None = None,
        terminal_finalize: Callable[
            [TerminalSelection, object, int], TerminalFinalization
        ] | None = None,
        attempts: int = 4,
        safety_recoveries: int | None = None,
    ):
        requests: list[InteractiveModelRequest] = []
        planned = list(outputs)

        def prepare(messages: tuple[TranscriptMessage, ...]) -> PreparedPrompt:
            index = len(requests)
            if index >= len(planned):
                self.fail("controller prepared an unplanned model call")
            rendered = PHI4_MINI_RAW_PROFILE.render(messages)
            return PreparedPrompt(
                rendered_prompt=rendered,
                prompt_tokens=planned[index].prompt_tokens,
                prompt_sha256=digest(rendered),
                token_ids_sha256=digest(f"tokens-{index}"),
                renderer_profile_sha256=PHI4_MINI_RAW_PROFILE.sha256,
                tokenizer_artifact_sha256=digest("test-tokenizer"),
                model_artifact_sha256=PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
            )

        def model(request: InteractiveModelRequest) -> InteractiveModelOutput:
            requests.append(request)
            return outputs.pop(0)

        strict = strict_evaluate or (
            lambda _checkpoint: StrictEvaluation(
                strict_success=False,
                evaluator_sha256=digest("strict-false"),
            )
        )
        result = run_interactive_strategy(
            strategy=strategy,
            task=self.task,
            model=model,
            prompt_preparer=prepare,
            environment_factory=factory.create,
            attempt_evaluate=factory.evaluate,
            strict_evaluate=strict,
            terminal_finalize=terminal_finalize or factory.finalize,  # type: ignore[arg-type]
            budget=self.budget(
                attempts=attempts,
                safety_recoveries=safety_recoveries,
            ),
            replicate_seed=11,
            event_log=Path(directory) / f"{strategy}.events.jsonl",
        )
        return result, requests

    def test_first_prompt_bytes_and_seed_are_identical_across_all_arms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_requests: list[InteractiveModelRequest] = []
            for strategy in STRATEGIES:
                factory = FakeEnvironmentFactory(rewards={"finish": 1.0})
                _result, requests = self.execute_strategy(
                    directory,
                    strategy=strategy,
                    outputs=[self.output("finish")],
                    factory=factory,
                )
                first_requests.append(requests[0])

        self.assertEqual(
            {request.prompt.encode("utf-8") for request in first_requests},
            {first_requests[0].prompt.encode("utf-8")},
        )
        self.assertEqual({request.seed for request in first_requests}, {11})

    def test_direct_makes_exactly_one_call_even_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(rewards={"wrong": 0.0})
            result, requests = self.execute_strategy(
                directory,
                strategy="direct",
                outputs=[self.output("wrong"), self.output("unused")],
                factory=factory,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.environment_actions, 1)
        self.assertEqual(result.evaluator_calls, 2)
        self.assertEqual([environment.actions for environment in factory.environments], [["wrong"]])
        selection, _limit = factory.terminal_calls[0]
        self.assertEqual(
            selection.evaluation_kind,
            AttemptEvaluationKind.EVALUATOR_DERIVED,
        )
        self.assertIsNotNone(selection.checkpoint)
        self.assertEqual(factory.terminal_close_observations, [True])

    def test_action_policy_failure_stays_in_denominator_without_checkpoint_or_evaluator(self) -> None:
        frozen_observation = "Command timed out."
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(
                effects={
                    "hang": (frozen_observation, digest("contaminated"), False),
                    "finish": ("done", digest("done"), True),
                },
                rewards={"finish": 1.0},
            )
            result, requests = self.execute_strategy(
                directory,
                strategy="raw_feedback_loop",
                outputs=[self.output("hang"), self.output("finish")],
                factory=factory,
            )
            records = [
                json.loads(line)
                for line in (
                    Path(directory) / "raw_feedback_loop.events.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(result.official_success)
        self.assertEqual(result.environment_actions, 2)
        self.assertEqual(result.evaluator_calls, 2)
        self.assertEqual(result.checkpoint_creates, 1)
        self.assertEqual(result.safety_recoveries, 1)
        self.assertEqual(result.maintenance_operations, 2)
        recoveries = [
            record for record in records
            if record.get("type") == "safety_recovery_completed"
        ]
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0]["attempt"], 1)
        self.assertEqual(recoveries[0]["state_sha256"], digest("initial"))
        self.assertEqual(
            recoveries[0]["recovery_evidence_sha256"],
            digest("recovery-1-hang"),
        )
        self.assertIn(
            f"Output: {frozen_observation}\nReward: 0.0",
            requests[1].prompt,
        )
        self.assertEqual(
            [entry[0] for entry in factory.timeline],
            ["execute", "execute", "checkpoint", "close"],
        )

    def test_direct_policy_failure_has_no_selected_checkpoint_or_strict_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(
                effects={
                    "hang": ("Command timed out.", digest("contaminated"), False),
                }
            )
            strict_calls: list[EnvironmentCheckpoint] = []

            def strict(checkpoint: EnvironmentCheckpoint) -> StrictEvaluation:
                strict_calls.append(checkpoint)
                return StrictEvaluation(True, digest("must-not-run"))

            result, _requests = self.execute_strategy(
                directory,
                strategy="direct",
                outputs=[self.output("hang")],
                factory=factory,
                strict_evaluate=strict,
            )

        self.assertEqual(result.stop_reason, "direct_action_policy_failure")
        self.assertEqual(result.environment_actions, 1)
        self.assertEqual(result.evaluator_calls, 0)
        self.assertEqual(result.checkpoint_creates, 0)
        self.assertEqual(result.safety_recoveries, 1)
        self.assertFalse(result.official_success)
        self.assertFalse(result.strict_success)
        self.assertEqual(strict_calls, [])
        selection, _limit = factory.terminal_calls[0]
        self.assertIsNone(selection.checkpoint)
        self.assertIsNone(selection.evaluation_kind)
        self.assertFalse(selection.aborted)
        self.assertEqual(factory.terminal_close_observations, [True])

    def test_safety_recovery_ceiling_stops_before_a_second_action_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(
                effects={
                    "hang": ("Command timed out.", digest("contaminated"), False),
                }
            )
            result, requests = self.execute_strategy(
                directory,
                strategy="raw_feedback_loop",
                outputs=[self.output("hang"), self.output("hang")],
                factory=factory,
                attempts=2,
                safety_recoveries=1,
            )

        self.assertEqual(result.stop_reason, "action_pipeline_budget_exhausted")
        self.assertEqual(result.safety_recoveries, 1)
        self.assertEqual(result.environment_actions, 1)
        self.assertEqual(result.checkpoint_creates, 0)
        self.assertEqual(len(requests), 2)

    def test_terminal_strict_and_posthoc_calls_enter_the_total_evaluator_count(self) -> None:
        terminal_limits: list[int] = []

        def finalize(
            selection: TerminalSelection,
            strict_evaluate: object,
            evaluator_call_limit: int,
        ) -> TerminalFinalization:
            terminal_limits.append(evaluator_call_limit)
            self.assertIsNotNone(selection.checkpoint)
            strict = strict_evaluate(selection.checkpoint)  # type: ignore[operator]
            return TerminalFinalization(strict, 1, 2)

        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(rewards={"wrong": 0.0})
            result, _requests = self.execute_strategy(
                directory,
                strategy="direct",
                outputs=[self.output("wrong")],
                factory=factory,
                terminal_finalize=finalize,
            )

        self.assertEqual(terminal_limits, [4])
        self.assertEqual(len(factory.evaluator_calls), 1)
        self.assertEqual(result.evaluator_calls, 4)

    def test_independent_policy_failure_advances_to_a_fresh_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(
                effects={
                    "hang": ("Command timed out.", digest("contaminated"), False),
                    "finish": ("done", digest("done"), True),
                },
                rewards={"finish": 1.0},
            )
            result, requests = self.execute_strategy(
                directory,
                strategy="independent_verified_sampling",
                outputs=[self.output("hang"), self.output("finish")],
                factory=factory,
            )

        self.assertTrue(result.official_success)
        self.assertEqual(len(factory.environments), 2)
        self.assertEqual(result.environment_actions, 2)
        self.assertEqual(result.evaluator_calls, 2)
        self.assertEqual(result.checkpoint_creates, 1)
        self.assertEqual(result.safety_recoveries, 1)
        self.assertEqual(requests[0].prompt, requests[1].prompt)

    def test_engineered_repeated_policy_failures_trigger_no_progress_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(
                effects={
                    "hang": ("Command timed out.", digest("contaminated"), False),
                }
            )
            result, requests = self.execute_strategy(
                directory,
                strategy="engineered_loop",
                outputs=[self.output("hang") for _ in range(4)],
                factory=factory,
                attempts=4,
            )

        self.assertEqual(result.stop_reason, "no_progress_guard")
        self.assertEqual(len(requests), 3)
        self.assertEqual(result.environment_actions, 3)
        self.assertEqual(result.evaluator_calls, 0)
        self.assertEqual(result.checkpoint_creates, 0)
        self.assertEqual(result.checkpoint_restores, 0)
        self.assertEqual(result.safety_recoveries, 3)
        self.assertEqual(result.maintenance_operations, 3)
        self.assertFalse(result.official_success)
        self.assertFalse(result.strict_success)
        self.assertIn('"admissible": false', requests[1].prompt)
        self.assertIn('"safety_recovery_performed": true', requests[1].prompt)
        self.assertIn('"restored_state_sha256": "sha256:', requests[1].prompt)

    def test_independent_sampling_uses_fresh_environment_and_context_without_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(
                effects={
                    "wrong": ("diagnostic that must stay hidden", digest("wrong"), True),
                    "finish": ("done", digest("done"), True),
                },
                rewards={"wrong": 0.25, "finish": 1.0},
            )
            result, requests = self.execute_strategy(
                directory,
                strategy="independent_verified_sampling",
                outputs=[self.output("wrong"), self.output("finish")],
                factory=factory,
            )

        self.assertTrue(result.official_success)
        self.assertEqual(len(factory.environments), 2)
        self.assertEqual([environment.actions for environment in factory.environments], [["wrong"], ["finish"]])
        self.assertEqual(requests[0].prompt.encode(), requests[1].prompt.encode())
        self.assertNotEqual(requests[0].context_id, requests[1].context_id)
        self.assertNotEqual(requests[0].seed, requests[1].seed)
        self.assertNotIn("diagnostic that must stay hidden", requests[1].prompt)
        self.assertNotIn("Reward:", requests[1].prompt)
        self.assertNotIn("wrong", requests[1].prompt)

    def test_raw_loop_appends_exact_observation_and_reward_in_one_context(self) -> None:
        observation = "line one\nline two"
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(
                effects={
                    "inspect": (observation, digest("inspected"), True),
                    "finish": ("done", digest("done"), True),
                },
                rewards={"inspect": 0.25, "finish": 1.0},
            )
            result, requests = self.execute_strategy(
                directory,
                strategy="raw_feedback_loop",
                outputs=[self.output("inspect"), self.output("finish")],
                factory=factory,
            )

        self.assertTrue(result.official_success)
        self.assertEqual(len(factory.environments), 1)
        self.assertEqual(requests[0].context_id, requests[1].context_id)
        self.assertIn(f"Output: {observation}\nReward: 0.25", requests[1].prompt)
        self.assertTrue(requests[1].prompt.endswith("<|assistant|>"))
        self.assertNotIn("Controller state:", requests[0].prompt)
        self.assertNotIn("Controller state:", requests[1].prompt)

    def test_engineered_loop_rolls_back_and_stops_after_third_no_progress_signature(self) -> None:
        effects = {
            "advance": ("advanced", digest("stable"), True),
            "regress": ("regressed", digest("worse"), True),
        }
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(
                effects=effects,
                rewards={"advance": 0.5, "regress": 0.25},
            )
            selected: list[EnvironmentCheckpoint] = []

            def strict(checkpoint: EnvironmentCheckpoint) -> StrictEvaluation:
                selected.append(checkpoint)
                return StrictEvaluation(False, digest("strict-false"))

            result, requests = self.execute_strategy(
                directory,
                strategy="engineered_loop",
                outputs=[
                    self.output("advance"),
                    self.output("regress"),
                    self.output("advance"),
                    self.output("advance"),
                    self.output("must-not-run"),
                ],
                factory=factory,
                strict_evaluate=strict,
                attempts=5,
            )

        environment = factory.environments[0]
        self.assertNotIn("Controller state:", requests[0].prompt)
        self.assertIn("Controller state:", requests[1].prompt)
        self.assertIn('"rollback_performed": true', requests[2].prompt)
        self.assertIn("form a new failure hypothesis", requests[3].prompt.lower())
        self.assertEqual(len(environment.restore_calls), 1)
        self.assertEqual(result.stop_reason, "no_progress_guard")
        self.assertEqual(result.model_calls, 4)
        self.assertEqual(result.checkpoint_restores, 1)
        self.assertEqual(selected[0].state_sha256, digest("stable"))

    def test_parser_failure_counts_attempt_and_tokens_but_not_actions_or_evaluators(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory()
            result, requests = self.execute_strategy(
                directory,
                strategy="direct",
                outputs=[self.output(text="not-json", prompt_tokens=123, completion_tokens=7)],
                factory=factory,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.logical_prompt_tokens, 123)
        self.assertEqual(result.logical_completion_tokens, 7)
        self.assertEqual(result.parser_failures, 1)
        self.assertEqual(result.environment_actions, 0)
        self.assertEqual(result.evaluator_calls, 0)
        self.assertEqual(result.maintenance_operations, 0)
        self.assertFalse(result.official_success)
        self.assertFalse(result.strict_success)
        self.assertEqual(factory.environments, [])

    def test_private_evaluator_output_never_enters_prompts_or_events_and_strict_runs_after_close(self) -> None:
        attempt_secret = "DO NOT LEAK GOLD COMMAND OR EVALUATOR PATH"
        strict_secret = "DO NOT LEAK STRICT FILESYSTEM DIFF"
        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "raw_feedback_loop.events.jsonl"
            factory = FakeEnvironmentFactory(
                effects={
                    "inspect": ("public observation", digest("inspect"), True),
                    "finish": ("done", digest("done"), True),
                },
                rewards={"inspect": 0.0, "finish": 1.0},
            )
            factory._private_output = attempt_secret
            strict_calls: list[EnvironmentCheckpoint] = []

            def strict(checkpoint: EnvironmentCheckpoint) -> StrictEvaluation:
                records = [json.loads(line) for line in event_log.read_text().splitlines()]
                self.assertEqual(records[-1]["type"], "terminal_finalization_requested")
                self.assertEqual(len(factory.evaluator_calls), 2)
                self.assertEqual(factory.timeline[-1][0], "close")
                strict_calls.append(checkpoint)
                _private_output = strict_secret
                self.assertEqual(_private_output, strict_secret)
                return StrictEvaluation(True, digest("strict-true"))

            result, requests = self.execute_strategy(
                directory,
                strategy="raw_feedback_loop",
                outputs=[self.output("inspect"), self.output("finish")],
                factory=factory,
                strict_evaluate=strict,
            )
            published = event_log.read_text() + "\n".join(request.prompt for request in requests)

        self.assertTrue(result.strict_success)
        self.assertEqual(len(strict_calls), 1)
        self.assertNotIn(attempt_secret, published)
        self.assertNotIn(strict_secret, published)

    def test_logical_action_evaluator_and_maintenance_counters_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeEnvironmentFactory(
                rewards={"inspect": 0.0, "finish": 1.0},
            )
            result, _requests = self.execute_strategy(
                directory,
                strategy="raw_feedback_loop",
                outputs=[
                    self.output("inspect", prompt_tokens=101, completion_tokens=7),
                    self.output("finish", prompt_tokens=151, completion_tokens=9),
                ],
                factory=factory,
            )

        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(result.logical_prompt_tokens, 252)
        self.assertEqual(result.logical_completion_tokens, 16)
        self.assertEqual(result.environment_actions, 2)
        self.assertEqual(result.evaluator_calls, 3)
        self.assertEqual(result.checkpoint_creates, 2)
        self.assertEqual(result.checkpoint_restores, 0)
        self.assertEqual(result.maintenance_operations, 2)


if __name__ == "__main__":
    unittest.main()
