from __future__ import annotations

import json
import tempfile
import unittest
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
    AttemptEvaluation,
    EnvironmentCheckpoint,
    StrictEvaluation,
)
from edgeloopbench.journal import JournalIntegrityError, inspect_journal


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


class InjectedFailure(RuntimeError):
    pass


class RecoveryHarness:
    """Tiny deterministic fake with one injectable crash boundary."""

    def __init__(self, event_log: Path, fail_at: str | None) -> None:
        self.event_log = event_log
        self.fail_at = fail_at
        self.outputs = ["best", "worse"]
        self.rewards = {"best": 0.75, "worse": 0.25}
        self.actions_by_checkpoint: dict[str, str] = {}
        self.records_at_failure: list[dict[str, object]] = []
        self.model_calls = 0
        self.factory_calls = 0
        self.execute_calls = 0
        self.checkpoint_calls = 0
        self.evaluator_calls = 0
        self.restore_calls = 0
        self.strict_calls = 0
        self.close_calls = 0

    def model(self, _request: InteractiveModelRequest) -> InteractiveModelOutput:
        self.model_calls += 1
        self._fail("model")
        action = self.outputs.pop(0)
        return InteractiveModelOutput(
            text=json.dumps({"command": action}),
            prompt_tokens=20,
            completion_tokens=5,
            total_duration_ns=1,
        )

    def create(self) -> RecoveryEnvironment:
        self.factory_calls += 1
        self._fail("environment_create")
        return RecoveryEnvironment(self)

    def evaluate(self, checkpoint: EnvironmentCheckpoint) -> AttemptEvaluation:
        self.evaluator_calls += 1
        self._fail("attempt_evaluator")
        action = self.actions_by_checkpoint[checkpoint.reference_sha256]
        reward = self.rewards[action]
        return AttemptEvaluation(reward, reward == 1.0)

    def strict(self, _checkpoint: EnvironmentCheckpoint) -> StrictEvaluation:
        self.strict_calls += 1
        self._fail("strict_evaluator")
        return StrictEvaluation(False, digest("strict-evaluator-v1"))

    def snapshot(self) -> tuple[int, ...]:
        return (
            self.model_calls,
            self.factory_calls,
            self.execute_calls,
            self.checkpoint_calls,
            self.evaluator_calls,
            self.restore_calls,
            self.strict_calls,
            self.close_calls,
        )

    def _fail(self, boundary: str) -> None:
        if self.fail_at != boundary:
            return
        self.records_at_failure = [
            json.loads(line)
            for line in self.event_log.read_text(encoding="utf-8").splitlines()
        ]
        raise InjectedFailure(boundary)


class RecoveryEnvironment:
    def __init__(self, owner: RecoveryHarness) -> None:
        self.owner = owner
        self.state_sha256 = digest("initial")
        self.last_action = ""

    def execute(self, action: str) -> ActionExecution:
        self.owner.execute_calls += 1
        self.owner._fail("action")
        self.last_action = action
        self.state_sha256 = digest(f"state-{action}")
        return ActionExecution(
            observation=f"output for {action}",
            exit_code=0,
            state_sha256=self.state_sha256,
            output_sha256=digest(f"output-{action}"),
            admissible=True,
            state_changed=True,
        )

    def checkpoint(self) -> EnvironmentCheckpoint:
        self.owner.checkpoint_calls += 1
        self.owner._fail("checkpoint_create")
        checkpoint = EnvironmentCheckpoint(
            digest(f"checkpoint-{self.owner.checkpoint_calls}"),
            self.state_sha256,
        )
        self.owner.actions_by_checkpoint[checkpoint.reference_sha256] = self.last_action
        return checkpoint

    def restore(self, checkpoint: EnvironmentCheckpoint) -> None:
        self.owner.restore_calls += 1
        self.owner._fail("restore")
        self.state_sha256 = checkpoint.state_sha256

    def close(self) -> None:
        self.owner.close_calls += 1


class InteractiveRecoveryTests(unittest.TestCase):
    task = InteractiveTask("recovery-fs1-000", "Create one deterministic fixture.")

    def budget(self) -> InteractiveBudget:
        return InteractiveBudget(
            attempts=2,
            prompt_tokens=1_000,
            completion_tokens=100,
            model_calls=2,
            environment_actions=2,
            evaluator_calls=2,
            checkpoint_creates=2,
            checkpoint_restores=2,
            per_call_context_tokens=512,
            max_output_tokens=32,
        )

    def execute_case(self, harness: RecoveryHarness, *, strategy: str) -> object:
        return run_interactive_strategy(
            strategy=strategy,
            task=self.task,
            model=harness.model,
            environment_factory=harness.create,
            attempt_evaluate=harness.evaluate,
            strict_evaluate=harness.strict,
            budget=self.budget(),
            replicate_seed=47,
            event_log=harness.event_log,
        )

    def test_crashes_leave_valid_unsealed_journal_and_forbid_in_place_retry(self) -> None:
        cases = (
            ("model", "direct", "model_requested", "model_completed"),
            (
                "environment_create",
                "direct",
                "environment_create_requested",
                "environment_created",
            ),
            ("action", "direct", "action_requested", "action_completed"),
            (
                "checkpoint_create",
                "direct",
                "checkpoint_create_requested",
                "checkpoint_created",
            ),
            (
                "attempt_evaluator",
                "direct",
                "attempt_evaluation_requested",
                "attempt_evaluated",
            ),
            (
                "restore",
                "engineered_loop",
                "checkpoint_restore_requested",
                "checkpoint_restored",
            ),
            (
                "strict_evaluator",
                "direct",
                "strict_evaluation_planned",
                "strict_evaluation_completed",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            for failure, strategy, intent, completion in cases:
                with self.subTest(failure=failure):
                    event_log = Path(directory) / f"{failure}.events.jsonl"
                    harness = RecoveryHarness(event_log, failure)

                    with self.assertRaisesRegex(InjectedFailure, failure):
                        self.execute_case(harness, strategy=strategy)

                    types_at_failure = [
                        str(record["type"]) for record in harness.records_at_failure
                    ]
                    self.assertIn(intent, types_at_failure)
                    self.assertNotIn(completion, types_at_failure)

                    inspection = inspect_journal(event_log)
                    self.assertGreater(inspection.record_count, 0)
                    self.assertFalse(inspection.sealed)
                    self.assertIsNone(inspection.partial_tail)
                    with self.assertRaisesRegex(JournalIntegrityError, "not sealed"):
                        inspect_journal(event_log, require_sealed=True)

                    side_effects_before_retry = harness.snapshot()
                    with self.assertRaisesRegex(ValueError, "journal must be empty"):
                        self.execute_case(harness, strategy=strategy)
                    self.assertEqual(harness.snapshot(), side_effects_before_retry)

    def test_successful_run_ends_with_valid_terminal_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_log = Path(directory) / "successful.events.jsonl"
            harness = RecoveryHarness(event_log, None)
            harness.outputs = ["best"]
            harness.rewards = {"best": 1.0}

            self.execute_case(harness, strategy="direct")

            inspection = inspect_journal(event_log, require_sealed=True)
            records = [
                json.loads(line)
                for line in event_log.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(inspection.sealed)
            self.assertEqual(records[-1]["type"], "journal_sealed")
            self.assertEqual(records[-1]["sealed_event_count"], len(records) - 1)


if __name__ == "__main__":
    unittest.main()
