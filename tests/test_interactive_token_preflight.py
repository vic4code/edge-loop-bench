from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.interactive_controller import (
    InteractiveBudget,
    InteractiveTask,
    run_interactive_strategy,
)
from edgeloopbench.interactive_environment import (
    ActionExecution,
    AttemptEvaluation,
    EnvironmentCheckpoint,
    StrictEvaluation,
    TerminalFinalization,
    TerminalSelection,
)
from edgeloopbench.model_adapter import (
    InteractiveModelOutput,
    PHI4_MINI_RAW_PROFILE,
    PreparedPrompt,
    RenderedPromptByteLimitExceeded,
    TranscriptMessage,
)


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def finalize_terminal(
    selection: TerminalSelection,
    strict_evaluate: object,
    evaluator_call_limit: int,
) -> TerminalFinalization:
    if strict_evaluate is None:
        return TerminalFinalization(None, 0, 0)
    if evaluator_call_limit < 1 or selection.checkpoint is None:
        raise AssertionError("strict terminal call lacks budget or checkpoint")
    result = strict_evaluate(selection.checkpoint)  # type: ignore[operator]
    return TerminalFinalization(result, 1, 0)


class FixedPromptPreparer:
    def __init__(self, counts: list[int]) -> None:
        self.counts = counts
        self.messages: list[tuple[TranscriptMessage, ...]] = []

    def __call__(self, messages: tuple[TranscriptMessage, ...]) -> PreparedPrompt:
        self.messages.append(messages)
        count = self.counts[len(self.messages) - 1]
        rendered = PHI4_MINI_RAW_PROFILE.render(messages)
        return PreparedPrompt(
            rendered_prompt=rendered,
            prompt_tokens=count,
            prompt_sha256=digest(rendered),
            token_ids_sha256=digest(f"tokens-{count}"),
            renderer_profile_sha256=PHI4_MINI_RAW_PROFILE.sha256,
            tokenizer_artifact_sha256=digest("tokenizer"),
            model_artifact_sha256=PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
        )


class TinyEnvironment:
    def __init__(self) -> None:
        self.action = ""

    def execute(self, action: str) -> ActionExecution:
        self.action = action
        return ActionExecution("ok", 0, digest(action), digest("ok"), True, True)

    def checkpoint(self) -> EnvironmentCheckpoint:
        return EnvironmentCheckpoint(digest("checkpoint"), digest(self.action))

    def restore(self, _checkpoint: EnvironmentCheckpoint) -> None:
        raise AssertionError("restore not expected")

    def close(self) -> None:
        pass


class OversizePromptPreparer:
    def __call__(
        self, _messages: tuple[TranscriptMessage, ...]
    ) -> PreparedPrompt:
        raise RenderedPromptByteLimitExceeded(
            observed_bytes=65_537,
            limit_bytes=65_536,
            prompt_sha256=digest("oversize"),
            renderer_profile_sha256=PHI4_MINI_RAW_PROFILE.sha256,
        )


class InteractiveTokenPreflightTests(unittest.TestCase):
    task = InteractiveTask("bash-fs1-001", "Print the working directory.")

    def budget(
        self,
        *,
        attempts: int = 2,
        prompt_tokens: int = 100,
        completion_tokens: int = 20,
        per_call_context_tokens: int = 64,
    ) -> InteractiveBudget:
        return InteractiveBudget(
            attempts=attempts,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model_calls=attempts,
            environment_actions=attempts,
            evaluator_calls=attempts + 1,
            checkpoint_creates=attempts,
            checkpoint_restores=attempts,
            safety_recoveries=attempts,
            per_call_context_tokens=per_call_context_tokens,
            max_output_tokens=16,
        )

    def run_case(
        self,
        directory: str,
        *,
        strategy: str,
        preparer: FixedPromptPreparer,
        model: object,
        budget: InteractiveBudget,
        strict_calls: list[EnvironmentCheckpoint],
    ):
        environments: list[TinyEnvironment] = []

        def create() -> TinyEnvironment:
            environment = TinyEnvironment()
            environments.append(environment)
            return environment

        def strict(checkpoint: EnvironmentCheckpoint) -> StrictEvaluation:
            strict_calls.append(checkpoint)
            return StrictEvaluation(False, digest("strict"))

        result = run_interactive_strategy(
            strategy=strategy,
            task=self.task,
            model=model,  # type: ignore[arg-type]
            prompt_preparer=preparer,
            environment_factory=create,
            attempt_evaluate=lambda _checkpoint: AttemptEvaluation(0.0, False),
            strict_evaluate=strict,
            terminal_finalize=finalize_terminal,
            budget=budget,
            replicate_seed=11,
            event_log=Path(directory) / "events.jsonl",
        )
        return result, environments

    def test_prompt_that_would_cross_b_star_is_never_sent(self) -> None:
        model_calls: list[object] = []

        def model(request: object) -> InteractiveModelOutput:
            model_calls.append(request)
            raise AssertionError("model must not be called")

        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "events.jsonl"
            preparer = FixedPromptPreparer([21])
            strict_calls: list[EnvironmentCheckpoint] = []
            result, environments = self.run_case(
                directory,
                strategy="direct",
                preparer=preparer,
                model=model,
                budget=self.budget(prompt_tokens=20),
                strict_calls=strict_calls,
            )
            records = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(model_calls, [])
        self.assertEqual(environments, [])
        self.assertEqual(strict_calls, [])
        self.assertEqual(result.run_status, "budget_exhausted")
        self.assertEqual(result.stop_reason, "logical_prompt_token_budget_exhausted")
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(result.logical_prompt_tokens, 0)
        self.assertEqual(result.initial_prompts, 0)
        self.assertEqual(result.independent_sample_prompts, 0)
        self.assertEqual(result.feedback_followups, 0)
        self.assertEqual(
            [record["type"] for record in records],
            [
                "controller_started",
                "model_preflighted",
                "model_request_rejected",
                "terminal_finalization_requested",
                "terminal_finalized",
                "controller_stopped",
                "journal_sealed",
            ],
        )
        self.assertEqual(
            records[0]["controller_revision"],
            "interactive-controller-v4-v07-preregistered-topology",
        )
        self.assertEqual(records[2]["reason"], "prompt_budget")
        self.assertEqual(records[2]["prompt_tokens"], 21)
        self.assertEqual(records[2]["remaining_prompt_tokens"], 20)

    def test_rendered_prompt_byte_ceiling_is_a_predeclared_budget_stop(self) -> None:
        model_calls: list[object] = []

        def model(request: object) -> InteractiveModelOutput:
            model_calls.append(request)
            raise AssertionError("model must not be called")

        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "events.jsonl"
            strict_calls: list[EnvironmentCheckpoint] = []
            result, environments = self.run_case(
                directory,
                strategy="direct",
                preparer=OversizePromptPreparer(),  # type: ignore[arg-type]
                model=model,
                budget=self.budget(),
                strict_calls=strict_calls,
            )
            records = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(model_calls, [])
        self.assertEqual(environments, [])
        self.assertEqual(strict_calls, [])
        self.assertEqual(result.run_status, "budget_exhausted")
        self.assertEqual(result.stop_reason, "rendered_prompt_byte_budget_exhausted")
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(result.logical_prompt_tokens, 0)
        self.assertEqual(
            [record["type"] for record in records],
            [
                "controller_started",
                "model_request_rejected",
                "terminal_finalization_requested",
                "terminal_finalized",
                "controller_stopped",
                "journal_sealed",
            ],
        )
        self.assertEqual(records[1]["reason"], "rendered_prompt_byte_budget")
        self.assertEqual(records[1]["observed_prompt_bytes"], 65_537)
        self.assertEqual(records[1]["prompt_byte_limit"], 65_536)

    def test_prompt_exactly_equal_to_remaining_b_star_is_sent(self) -> None:
        requests: list[object] = []

        def model(request: object) -> InteractiveModelOutput:
            requests.append(request)
            return InteractiveModelOutput('{"command":"pwd"}', 20, 1, 10)

        with tempfile.TemporaryDirectory() as directory:
            preparer = FixedPromptPreparer([20])
            strict_calls: list[EnvironmentCheckpoint] = []
            result, environments = self.run_case(
                directory,
                strategy="direct",
                preparer=preparer,
                model=model,
                budget=self.budget(prompt_tokens=20),
                strict_calls=strict_calls,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(len(environments), 1)
        self.assertEqual(result.run_status, "completed")
        self.assertEqual(result.logical_prompt_tokens, 20)

    def test_blocked_second_preflight_does_not_count_as_a_sent_prompt(self) -> None:
        for strategy, prompt_field in (
            ("independent_verified_sampling", "independent_sample_prompts"),
            ("raw_feedback_loop", "feedback_followups"),
        ):
            with self.subTest(strategy=strategy):
                model_calls: list[object] = []

                def model(request: object) -> InteractiveModelOutput:
                    model_calls.append(request)
                    return InteractiveModelOutput('{"command":"wrong"}', 10, 2, 10)

                with tempfile.TemporaryDirectory() as directory:
                    preparer = FixedPromptPreparer([10, 91])
                    result, _environments = self.run_case(
                        directory,
                        strategy=strategy,
                        preparer=preparer,
                        model=model,
                        budget=self.budget(prompt_tokens=100),
                        strict_calls=[],
                    )

                self.assertEqual(len(model_calls), 1)
                self.assertEqual(result.model_calls, 1)
                self.assertEqual(result.initial_prompts, 1)
                self.assertEqual(getattr(result, prompt_field), 0)
                self.assertEqual(
                    result.stop_reason,
                    "logical_prompt_token_budget_exhausted",
                )

    def test_prompt_telemetry_mismatch_is_infrastructure_invalid(self) -> None:
        requests: list[object] = []

        def model(request: object) -> InteractiveModelOutput:
            requests.append(request)
            return InteractiveModelOutput('{"command":"must-not-run"}', 13, 1, 10)

        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "events.jsonl"
            preparer = FixedPromptPreparer([12])
            strict_calls: list[EnvironmentCheckpoint] = []
            result, environments = self.run_case(
                directory,
                strategy="direct",
                preparer=preparer,
                model=model,
                budget=self.budget(),
                strict_calls=strict_calls,
            )
            published = event_log.read_text(encoding="utf-8")

        self.assertEqual(len(requests), 1)
        self.assertEqual(environments, [])
        self.assertEqual(strict_calls, [])
        self.assertEqual(result.run_status, "infrastructure_error")
        self.assertEqual(result.stop_reason, "prompt_token_telemetry_mismatch")
        self.assertEqual(result.environment_actions, 0)
        self.assertEqual(result.logical_prompt_tokens, 12)
        self.assertIn('"type":"infrastructure_invalid"', published)
        self.assertIn('"preflight_prompt_tokens":12', published)
        self.assertIn('"telemetry_prompt_tokens":13', published)

    def test_output_cap_is_clamped_to_remaining_per_call_context(self) -> None:
        output_caps: list[int] = []

        def model(request: object) -> InteractiveModelOutput:
            cap = getattr(request, "max_output_tokens")
            output_caps.append(cap)
            return InteractiveModelOutput('{"command":"pwd"}', 20, cap, 10)

        with tempfile.TemporaryDirectory() as directory:
            preparer = FixedPromptPreparer([20])
            result, _environments = self.run_case(
                directory,
                strategy="direct",
                preparer=preparer,
                model=model,
                budget=self.budget(per_call_context_tokens=24),
                strict_calls=[],
            )

        self.assertEqual(output_caps, [4])
        self.assertEqual(result.run_status, "completed")

    def test_feedback_loop_builds_alternating_typed_roles(self) -> None:
        outputs = [
            InteractiveModelOutput('{"command":"wrong"}', 10, 2, 10),
            InteractiveModelOutput('{"command":"finish"}', 20, 2, 10),
        ]

        def model(_request: object) -> InteractiveModelOutput:
            return outputs.pop(0)

        evaluations = iter(
            [AttemptEvaluation(0.25, False), AttemptEvaluation(1.0, True)]
        )
        with tempfile.TemporaryDirectory() as directory:
            preparer = FixedPromptPreparer([10, 20])
            result = run_interactive_strategy(
                strategy="raw_feedback_loop",
                task=self.task,
                model=model,  # type: ignore[arg-type]
                prompt_preparer=preparer,
                environment_factory=TinyEnvironment,
                attempt_evaluate=lambda _checkpoint: next(evaluations),
                strict_evaluate=lambda _checkpoint: StrictEvaluation(
                    True, digest("strict")
                ),
                terminal_finalize=finalize_terminal,
                budget=self.budget(),
                replicate_seed=11,
                event_log=Path(directory) / "events.jsonl",
            )

        self.assertTrue(result.official_success)
        self.assertEqual(
            [message.role for message in preparer.messages[1]],
            ["user", "assistant", "user"],
        )
        self.assertEqual(
            preparer.messages[1][1].content,
            '{"command":"wrong"}',
        )
        self.assertEqual(
            preparer.messages[1][2].content,
            "Output: ok\nExit status: 0\nReward: 0.25",
        )


if __name__ == "__main__":
    unittest.main()
