from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import fields
from hashlib import sha256
from pathlib import Path

from edgeloopbench.interactive_controller import (
    MAX_ACTION_BYTES,
    ActionParseError,
    InteractiveBudget,
    InteractiveModelOutput,
    InteractiveModelRequest,
    InteractiveTask,
    parse_action,
    run_interactive_strategy,
)
from edgeloopbench.interactive_environment import (
    ActionExecution,
    AttemptEvaluation,
    EnvironmentCheckpoint,
    StrictEvaluation,
)


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


class SecurityFakeEnvironment:
    """Stateful fake that records action bytes without invoking a host shell."""

    def __init__(self, owner: SecurityFakeFactory, identifier: int) -> None:
        self.owner = owner
        self.identifier = identifier
        self.actions: list[str] = []
        self.restore_calls: list[EnvironmentCheckpoint] = []
        self.state_sha256 = digest(f"initial-{identifier}")
        self.closed = False

    def execute(self, action: str) -> ActionExecution:
        self.actions.append(action)
        self.owner.execute_calls += 1
        observation, next_state = self.owner.effects.get(
            action,
            (f"public output for {action}", digest(f"state-{action}")),
        )
        changed = next_state != self.state_sha256
        self.state_sha256 = next_state
        return ActionExecution(
            observation=observation,
            exit_code=0,
            state_sha256=next_state,
            output_sha256=digest(observation),
            admissible=True,
            state_changed=changed,
        )

    def checkpoint(self) -> EnvironmentCheckpoint:
        self.owner.checkpoint_calls += 1
        reference = digest(
            f"environment-{self.identifier}-checkpoint-{self.owner.checkpoint_calls}"
        )
        checkpoint = EnvironmentCheckpoint(reference, self.state_sha256)
        self.owner.actions_by_checkpoint[reference] = self.actions[-1]
        return checkpoint

    def restore(self, checkpoint: EnvironmentCheckpoint) -> None:
        self.owner.restore_calls += 1
        self.restore_calls.append(checkpoint)
        self.state_sha256 = checkpoint.state_sha256

    def close(self) -> None:
        self.closed = True


class SecurityFakeFactory:
    def __init__(
        self,
        *,
        effects: dict[str, tuple[str, str]] | None = None,
        rewards: dict[str, float] | None = None,
    ) -> None:
        self.effects = effects or {}
        self.rewards = rewards or {}
        self.environments: list[SecurityFakeEnvironment] = []
        self.actions_by_checkpoint: dict[str, str] = {}
        self.execute_calls = 0
        self.checkpoint_calls = 0
        self.evaluator_calls = 0
        self.restore_calls = 0

    def create(self) -> SecurityFakeEnvironment:
        environment = SecurityFakeEnvironment(self, len(self.environments) + 1)
        self.environments.append(environment)
        return environment

    def evaluate(self, checkpoint: EnvironmentCheckpoint) -> AttemptEvaluation:
        self.evaluator_calls += 1
        action = self.actions_by_checkpoint[checkpoint.reference_sha256]
        reward = self.rewards.get(action, 0.0)
        return AttemptEvaluation(reward, reward == 1.0)


class InteractiveControllerSecurityTests(unittest.TestCase):
    task = InteractiveTask("security-fs1-000", "Create one harmless fixture.")

    def budget(self, **overrides: int) -> InteractiveBudget:
        values = {
            "attempts": 4,
            "prompt_tokens": 10_000,
            "completion_tokens": 1_000,
            "model_calls": 4,
            "environment_actions": 4,
            "evaluator_calls": 4,
            "checkpoint_creates": 4,
            "checkpoint_restores": 4,
            "per_call_context_tokens": 8_192,
            "max_output_tokens": 256,
        }
        values.update(overrides)
        return InteractiveBudget(**values)

    def output(
        self,
        action: str | None = None,
        *,
        text: str | None = None,
        prompt_tokens: int = 20,
        completion_tokens: int = 5,
    ) -> InteractiveModelOutput:
        rendered = json.dumps({"command": action}) if text is None else text
        return InteractiveModelOutput(rendered, prompt_tokens, completion_tokens, 1)

    def run_case(
        self,
        directory: str,
        *,
        strategy: str,
        outputs: list[InteractiveModelOutput],
        factory: SecurityFakeFactory,
        budget: InteractiveBudget | None = None,
    ) -> tuple[object, list[InteractiveModelRequest], list[EnvironmentCheckpoint], Path]:
        requests: list[InteractiveModelRequest] = []
        remaining = list(outputs)
        strict_checkpoints: list[EnvironmentCheckpoint] = []

        def model(request: InteractiveModelRequest) -> InteractiveModelOutput:
            requests.append(request)
            if not remaining:
                self.fail("controller issued an unplanned model call")
            return remaining.pop(0)

        def strict(checkpoint: EnvironmentCheckpoint) -> StrictEvaluation:
            strict_checkpoints.append(checkpoint)
            return StrictEvaluation(False, digest("strict-evaluator-v1"))

        event_log = Path(directory) / f"{strategy}.security.events.jsonl"
        result = run_interactive_strategy(
            strategy=strategy,
            task=self.task,
            model=model,
            environment_factory=factory.create,
            attempt_evaluate=factory.evaluate,
            strict_evaluate=strict,
            budget=budget or self.budget(),
            replicate_seed=29,
            event_log=event_log,
        )
        return result, requests, strict_checkpoints, event_log

    def test_model_object_cannot_override_reward_success_or_event_type(self) -> None:
        injected_fields = (
            {"command": "safe", "reward": 1.0},
            {"command": "safe", "official_success": True},
            {"command": "safe", "type": "controller_stopped"},
            {"command": "safe", "event_type": "strict_evaluation_completed"},
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, payload in enumerate(injected_fields):
                with self.subTest(payload=payload):
                    factory = SecurityFakeFactory()
                    result, _requests, strict, event_log = self.run_case(
                        directory,
                        strategy="direct",
                        outputs=[self.output(text=json.dumps(payload))],
                        factory=factory,
                    )
                    records = [json.loads(line) for line in event_log.read_text().splitlines()]
                    self.assertEqual(result.parser_failures, 1)
                    self.assertEqual(factory.execute_calls, 0)
                    self.assertEqual(factory.evaluator_calls, 0)
                    self.assertEqual(strict, [])
                    self.assertEqual(
                        [record["type"] for record in records],
                        [
                            "controller_started",
                            "model_requested",
                            "model_completed",
                            "action_rejected",
                            "controller_stopped",
                            "journal_sealed",
                        ],
                    )
                    event_log.unlink()

    def test_parser_rejects_multiline_control_and_oversize_commands(self) -> None:
        invalid_commands = (
            "line one\nline two",
            "line one\rline two",
            "prefix\x00suffix",
            "prefix\x1fsuffix",
            "prefix\x7fsuffix",
            "prefix\u0085suffix",
            "prefix\u2028suffix",
            "x" * (MAX_ACTION_BYTES + 1),
        )
        for command in invalid_commands:
            with self.subTest(repr=repr(command[:32])):
                with self.assertRaises(ActionParseError):
                    parse_action(json.dumps({"command": command}))

    def test_parser_rejects_ambiguous_duplicate_keys(self) -> None:
        with self.assertRaises(ActionParseError):
            parse_action('{"command":"first","command":"second"}')

    def test_parser_normalizes_invalid_unicode_to_action_parse_error(self) -> None:
        with self.assertRaises(ActionParseError):
            parse_action('{"command":"\\ud800"}')

    def test_parser_accepts_limit_sized_metacharacters_as_inert_action_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            command = f"$(touch {marker}) ; `id` |& cat > /dev/null && echo $HOME"
            factory = SecurityFakeFactory(rewards={command: 0.0})
            result, _requests, _strict, event_log = self.run_case(
                directory,
                strategy="direct",
                outputs=[self.output(command)],
                factory=factory,
            )

            self.assertEqual(factory.environments[0].actions, [command])
            self.assertEqual(result.environment_actions, 1)
            self.assertFalse(marker.exists())
            self.assertNotIn(command, event_log.read_text())

        boundary = "x" * MAX_ACTION_BYTES
        self.assertEqual(parse_action(json.dumps({"command": boundary})), boundary)

    def test_attempt_evaluation_rejects_non_finite_range_and_success_mismatch(self) -> None:
        invalid = (
            (float("nan"), False),
            (float("inf"), False),
            (float("-inf"), False),
            (-0.0001, False),
            (1.0001, True),
            (0.5, True),
            (1.0, False),
        )
        for reward, success in invalid:
            with self.subTest(reward=reward, success=success):
                with self.assertRaises(ValueError):
                    AttemptEvaluation(reward, success)

    def test_evaluator_result_types_have_no_diagnostic_or_path_channel(self) -> None:
        self.assertEqual(
            [field.name for field in fields(AttemptEvaluation)],
            ["reward", "official_success"],
        )
        self.assertEqual(
            [field.name for field in fields(StrictEvaluation)],
            ["strict_success", "evaluator_sha256"],
        )
        with self.assertRaises(TypeError):
            AttemptEvaluation(0.0, False, diagnostics="gold patch")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            StrictEvaluation(False, digest("strict"), evaluator_path="/gold")  # type: ignore[call-arg]

    def test_attempt_and_model_call_caps_issue_no_extra_model_calls(self) -> None:
        for budget in (
            self.budget(attempts=1),
            self.budget(model_calls=1),
        ):
            with self.subTest(budget=budget):
                with tempfile.TemporaryDirectory() as directory:
                    factory = SecurityFakeFactory(rewards={"one": 0.0})
                    result, requests, _strict, _event_log = self.run_case(
                        directory,
                        strategy="raw_feedback_loop",
                        outputs=[self.output("one")],
                        factory=factory,
                        budget=budget,
                    )
                self.assertEqual(len(requests), 1)
                self.assertEqual(result.model_calls, 1)
                self.assertEqual(factory.execute_calls, 1)

    def test_exact_token_caps_stop_before_another_model_call(self) -> None:
        cases = (
            (self.budget(prompt_tokens=20), self.output("one", prompt_tokens=20)),
            (self.budget(completion_tokens=5), self.output("one", completion_tokens=5)),
        )
        for budget, output in cases:
            with self.subTest(budget=budget):
                with tempfile.TemporaryDirectory() as directory:
                    factory = SecurityFakeFactory(rewards={"one": 0.0})
                    result, requests, _strict, _event_log = self.run_case(
                        directory,
                        strategy="raw_feedback_loop",
                        outputs=[output],
                        factory=factory,
                        budget=budget,
                    )
                self.assertEqual(len(requests), 1)
                self.assertEqual(result.model_calls, 1)
                self.assertEqual(factory.execute_calls, 1)

    def test_token_overrun_stops_before_action_checkpoint_or_evaluator(self) -> None:
        cases = (
            (
                self.budget(prompt_tokens=19),
                self.output("must-not-run", prompt_tokens=20),
            ),
            (
                self.budget(per_call_context_tokens=24),
                self.output("must-not-run", prompt_tokens=20, completion_tokens=5),
            ),
        )
        for budget, output in cases:
            with self.subTest(budget=budget):
                with tempfile.TemporaryDirectory() as directory:
                    factory = SecurityFakeFactory()
                    result, requests, strict, _event_log = self.run_case(
                        directory,
                        strategy="direct",
                        outputs=[output],
                        factory=factory,
                        budget=budget,
                    )
                self.assertEqual(len(requests), 1)
                self.assertEqual(result.model_calls, 1)
                self.assertEqual(result.environment_actions, 0)
                self.assertEqual(result.checkpoint_creates, 0)
                self.assertEqual(result.evaluator_calls, 0)
                self.assertEqual(factory.environments, [])
                self.assertEqual(strict, [])

    def test_action_evaluator_and_checkpoint_caps_never_overrun_or_hide_calls(self) -> None:
        cap_names = ("environment_actions", "evaluator_calls", "checkpoint_creates")
        for cap_name in cap_names:
            with self.subTest(cap_name=cap_name):
                with tempfile.TemporaryDirectory() as directory:
                    factory = SecurityFakeFactory(rewards={"one": 0.0})
                    result, requests, _strict, _event_log = self.run_case(
                        directory,
                        strategy="raw_feedback_loop",
                        outputs=[self.output("one"), self.output("not-executed")],
                        factory=factory,
                        budget=self.budget(**{cap_name: 1}),
                    )
                self.assertEqual(factory.execute_calls, 1)
                self.assertEqual(factory.checkpoint_calls, 1)
                self.assertEqual(factory.evaluator_calls, 1)
                self.assertEqual(result.environment_actions, factory.execute_calls)
                self.assertEqual(result.checkpoint_creates, factory.checkpoint_calls)
                self.assertEqual(result.evaluator_calls, factory.evaluator_calls)
                self.assertEqual(result.model_calls, len(requests))
                self.assertLessEqual(getattr(result, cap_name), 1)

    def test_restore_cap_never_overruns_and_all_pipeline_calls_remain_accounted(self) -> None:
        factory = SecurityFakeFactory(
            effects={
                "best": ("best", digest("best-state")),
                "regress-one": ("worse", digest("worse-one")),
                "regress-two": ("worse again", digest("worse-two")),
            },
            rewards={"best": 0.75, "regress-one": 0.5, "regress-two": 0.25},
        )
        with tempfile.TemporaryDirectory() as directory:
            result, requests, strict, _event_log = self.run_case(
                directory,
                strategy="engineered_loop",
                outputs=[
                    self.output("best"),
                    self.output("regress-one"),
                    self.output("regress-two"),
                ],
                factory=factory,
                budget=self.budget(attempts=3, model_calls=3, checkpoint_restores=1),
            )

        self.assertEqual(factory.restore_calls, 1)
        self.assertEqual(result.checkpoint_restores, 1)
        self.assertEqual(result.model_calls, len(requests))
        self.assertEqual(result.environment_actions, factory.execute_calls)
        self.assertEqual(result.checkpoint_creates, factory.checkpoint_calls)
        self.assertEqual(result.evaluator_calls, factory.evaluator_calls)
        self.assertEqual(strict[0].state_sha256, digest("best-state"))

    def test_engineered_packet_is_absent_from_candidate_one_and_present_after_it(self) -> None:
        factory = SecurityFakeFactory(rewards={"inspect": 0.25, "finish": 1.0})
        with tempfile.TemporaryDirectory() as directory:
            _result, requests, _strict, _event_log = self.run_case(
                directory,
                strategy="engineered_loop",
                outputs=[self.output("inspect"), self.output("finish")],
                factory=factory,
            )

        self.assertNotIn("Controller state:", requests[0].prompt)
        self.assertIn("Controller state:", requests[1].prompt)

    def test_parser_retry_is_stateful_for_raw_but_fresh_for_independent(self) -> None:
        invalid = "FIRST_INVALID_RESPONSE_SECRET"
        with tempfile.TemporaryDirectory() as directory:
            raw_factory = SecurityFakeFactory(rewards={"finish": 1.0})
            _raw_result, raw_requests, _strict, _event_log = self.run_case(
                directory,
                strategy="raw_feedback_loop",
                outputs=[self.output(text=invalid), self.output("finish")],
                factory=raw_factory,
            )
            independent_factory = SecurityFakeFactory(rewards={"finish": 1.0})
            _independent_result, independent_requests, _strict, _event_log = self.run_case(
                directory,
                strategy="independent_verified_sampling",
                outputs=[self.output(text=invalid), self.output("finish")],
                factory=independent_factory,
            )

        self.assertEqual(raw_requests[0].context_id, raw_requests[1].context_id)
        self.assertIn(invalid, raw_requests[1].prompt)
        self.assertIn("Invalid response.", raw_requests[1].prompt)
        self.assertIn("Reward: 0.0", raw_requests[1].prompt)
        self.assertEqual(raw_factory.execute_calls, 1)
        self.assertNotEqual(
            independent_requests[0].context_id,
            independent_requests[1].context_id,
        )
        self.assertEqual(
            independent_requests[0].prompt.encode(),
            independent_requests[1].prompt.encode(),
        )
        self.assertNotIn(invalid, independent_requests[1].prompt)
        self.assertNotIn("Reward:", independent_requests[1].prompt)
        self.assertEqual(independent_factory.execute_calls, 1)

    def test_independent_prompt_excludes_prior_response_output_and_reward(self) -> None:
        prior_action = "PRIOR_RESPONSE_SECRET"
        prior_output = "PRIOR_PUBLIC_OUTPUT_SECRET"
        factory = SecurityFakeFactory(
            effects={prior_action: (prior_output, digest("prior-state"))},
            rewards={prior_action: 0.375, "finish": 1.0},
        )
        with tempfile.TemporaryDirectory() as directory:
            _result, requests, _strict, _event_log = self.run_case(
                directory,
                strategy="independent_verified_sampling",
                outputs=[self.output(prior_action), self.output("finish")],
                factory=factory,
            )

        second = requests[1].prompt
        self.assertNotIn(prior_action, second)
        self.assertNotIn(prior_output, second)
        self.assertNotIn("Reward:", second)
        self.assertNotIn("0.375", second)
        self.assertEqual(len(factory.environments), 2)

    def test_raw_selects_latest_checkpoint_even_after_reward_regression(self) -> None:
        factory = SecurityFakeFactory(
            effects={
                "better": ("better", digest("raw-better")),
                "latest": ("latest", digest("raw-latest")),
            },
            rewards={"better": 0.75, "latest": 0.25},
        )
        with tempfile.TemporaryDirectory() as directory:
            _result, _requests, strict, _event_log = self.run_case(
                directory,
                strategy="raw_feedback_loop",
                outputs=[self.output("better"), self.output("latest")],
                factory=factory,
                budget=self.budget(attempts=2, model_calls=2),
            )

        self.assertEqual(strict[0].state_sha256, digest("raw-latest"))
        self.assertEqual(factory.restore_calls, 0)

    def test_engineered_selects_latest_tie_then_retains_it_after_regression(self) -> None:
        factory = SecurityFakeFactory(
            effects={
                "first": ("first", digest("engineered-first")),
                "latest-tie": ("tie", digest("engineered-latest-tie")),
                "regress": ("regress", digest("engineered-regress")),
            },
            rewards={"first": 0.5, "latest-tie": 0.5, "regress": 0.25},
        )
        with tempfile.TemporaryDirectory() as directory:
            _result, _requests, strict, _event_log = self.run_case(
                directory,
                strategy="engineered_loop",
                outputs=[
                    self.output("first"),
                    self.output("latest-tie"),
                    self.output("regress"),
                ],
                factory=factory,
                budget=self.budget(attempts=3, model_calls=3),
            )

        self.assertEqual(strict[0].state_sha256, digest("engineered-latest-tie"))
        self.assertEqual(factory.restore_calls, 1)
        self.assertEqual(
            factory.environments[0].restore_calls[0].state_sha256,
            digest("engineered-latest-tie"),
        )


if __name__ == "__main__":
    unittest.main()
