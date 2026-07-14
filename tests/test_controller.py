from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.config import LogicalBudget
from edgeloopbench.controller import ModelOutput, run_strategy
from edgeloopbench.tasks import prepare_task


class StrategyControllerTests(unittest.TestCase):
    project_root = Path(__file__).parents[1]
    task_root = project_root / "tasks/micro/python-localized-001"
    fixed_source = '''"""One-based pagination helpers."""


def clamp_page(page: int, total_pages: int) -> int:
    """Return *page* constrained to the inclusive valid page range."""

    if total_pages <= 0:
        raise ValueError("total_pages must be positive")
    return max(1, min(page, total_pages))
'''

    def budget(self, model_calls: int) -> LogicalBudget:
        return LogicalBudget(
            prompt_tokens=4000, completion_tokens=1000,
            model_calls=model_calls, tool_calls=10,
            public_test_runs=3, per_call_context_tokens=4096,
        )

    def test_direct_records_one_successful_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            events = Path(directory) / "events.jsonl"
            task = prepare_task(self.task_root, worktree)

            def model(prompt: str, seed: int, max_output_tokens: int) -> ModelOutput:
                self.assertNotIn("evaluator", prompt.lower())
                return ModelOutput(
                    text=json.dumps({"edits": [{"path": "src/pagination.py", "content": self.fixed_source}]}),
                    thinking="", prompt_tokens=600, completion_tokens=120,
                    total_duration_ns=7_000_000_000,
                )

            result = run_strategy(
                "direct", worktree, task, model, self.budget(1), seed=11,
                event_log=events, evaluate=lambda _root, _task: True,
            )

            self.assertTrue(result.objective_success)
            self.assertEqual((result.model_calls, result.public_test_runs), (1, 1))
            self.assertEqual((result.prompt_tokens, result.completion_tokens), (600, 120))
            records = [json.loads(line) for line in events.read_text().splitlines()]
            self.assertEqual([record["sequence"] for record in records], list(range(1, len(records) + 1)))

    def test_bounded_retry_uses_feedback_after_invalid_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)
            prompts: list[str] = []
            outputs = [
                ModelOutput("not json", "", 500, 20, 1_000_000_000),
                ModelOutput(
                    json.dumps({"edits": [{"path": "src/pagination.py", "content": self.fixed_source}]}),
                    "", 700, 120, 2_000_000_000,
                ),
            ]

            def model(prompt: str, seed: int, max_output_tokens: int) -> ModelOutput:
                prompts.append(prompt)
                return outputs.pop(0)

            result = run_strategy(
                "bounded_retry", worktree, task, model, self.budget(2), seed=29,
                event_log=Path(directory) / "events.jsonl",
                evaluate=lambda _root, _task: True,
            )

            self.assertTrue(result.objective_success)
            self.assertEqual(result.model_calls, 2)
            self.assertEqual(result.prompt_tokens, 1200)
            self.assertEqual(result.tool_calls, 3)
            self.assertIn("not valid JSON", prompts[1])

    def test_direct_reports_logical_token_budget_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)
            budget = self.budget(1)
            budget = LogicalBudget(
                prompt_tokens=100, completion_tokens=budget.completion_tokens,
                model_calls=1, tool_calls=budget.tool_calls,
                public_test_runs=budget.public_test_runs,
                per_call_context_tokens=budget.per_call_context_tokens,
            )

            result = run_strategy(
                "direct", worktree, task,
                lambda _prompt, _seed, _limit: ModelOutput("{}", "", 600, 1, 1),
                budget, seed=11, event_log=Path(directory) / "events.jsonl",
                evaluate=lambda _root, _task: True,
            )

            self.assertEqual(result.run_status, "budget_exhausted")
            self.assertEqual(result.prompt_tokens, 600)


if __name__ == "__main__":
    unittest.main()
