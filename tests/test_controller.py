from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.config import LogicalBudget
from edgeloopbench.controller import (
    ModelOutput,
    ModelRequest,
    RunContext,
    run_strategy,
)
from edgeloopbench.tasks import prepare_task


class StrategyControllerTests(unittest.TestCase):
    project_root = Path(__file__).parents[1]
    task_root = project_root / "tasks/micro/python-localized-001"
    adversarial_task_root = project_root / "tasks/micro/python-adversarial-001"
    fixed_source = '''"""One-based pagination helpers."""


def clamp_page(page: int, total_pages: int) -> int:
    """Return *page* constrained to the inclusive valid page range."""

    if total_pages <= 0:
        raise ValueError("total_pages must be positive")
    return max(1, min(page, total_pages))
'''

    def context(self) -> RunContext:
        return RunContext(
            experiment_id="test-loop", budget_tier="small",
            manifest_sha256="sha256:" + "a" * 64,
        )

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

            def model(request: ModelRequest) -> ModelOutput:
                self.assertEqual(request.role, "maker")
                self.assertNotIn("evaluator", request.prompt.lower())
                return ModelOutput(
                    text=json.dumps({"edits": [{"path": "src/pagination.py", "content": self.fixed_source}]}),
                    thinking="", prompt_tokens=600, completion_tokens=120,
                    total_duration_ns=7_000_000_000,
                )

            result = run_strategy(
                "direct", worktree, task, model, self.budget(1), seed=11,
                event_log=events, evaluate=lambda _root, _task: True,
                context=self.context(),
            )

            self.assertTrue(result.objective_success)
            self.assertEqual((result.model_calls, result.public_test_runs), (1, 1))
            self.assertEqual((result.prompt_tokens, result.completion_tokens), (600, 120))
            records = [json.loads(line) for line in events.read_text().splitlines()]
            self.assertEqual([record["sequence"] for record in records], list(range(1, len(records) + 1)))
            self.assertTrue(all(record["experiment_id"] == "test-loop" for record in records))
            self.assertTrue(all(record["budget_tier"] == "small" for record in records))

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

            def model(request: ModelRequest) -> ModelOutput:
                prompts.append(request.prompt)
                return outputs.pop(0)

            result = run_strategy(
                "bounded_retry", worktree, task, model, self.budget(2), seed=29,
                event_log=Path(directory) / "events.jsonl",
                evaluate=lambda _root, _task: True,
                context=self.context(),
            )

            self.assertTrue(result.objective_success)
            self.assertEqual(result.model_calls, 2)
            self.assertEqual(result.prompt_tokens, 1200)
            self.assertEqual(result.tool_calls, 3)
            self.assertIn("not valid JSON", prompts[1])

    def test_retry_does_not_call_maker_without_a_remaining_public_test(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)
            calls = 0
            still_broken = (worktree / "src/pagination.py").read_text()

            def model(_request: ModelRequest) -> ModelOutput:
                nonlocal calls
                calls += 1
                return ModelOutput(
                    json.dumps({"edits": [{"path": "src/pagination.py", "content": still_broken}]}),
                    "", 500, 100, 1,
                )

            base = self.budget(3)
            budget = LogicalBudget(
                prompt_tokens=base.prompt_tokens,
                completion_tokens=base.completion_tokens,
                model_calls=base.model_calls,
                tool_calls=base.tool_calls,
                public_test_runs=1,
                per_call_context_tokens=base.per_call_context_tokens,
            )
            result = run_strategy(
                "bounded_retry", worktree, task, model, budget, seed=11,
                event_log=Path(directory) / "events.jsonl",
                evaluate=lambda _root, _task: False,
                context=self.context(),
            )

            self.assertEqual(calls, 1)
            self.assertEqual(result.failure_reason, "public_test_budget_exhausted")

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
                lambda _request: ModelOutput("{}", "", 600, 1, 1),
                budget, seed=11, event_log=Path(directory) / "events.jsonl",
                evaluate=lambda _root, _task: True,
                context=self.context(),
            )

            self.assertEqual(result.run_status, "budget_exhausted")
            self.assertEqual(result.prompt_tokens, 600)

    def test_maker_verifier_uses_read_only_verdict_then_maker_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.adversarial_task_root, worktree)
            prompts: list[str] = []
            superficial = '''"""Canonical keys for human-readable labels."""


def canonical_key(label: str) -> str:
    """Normalize *label* for use as a stable key."""

    normalized = label.strip().lower()
    if not normalized:
        raise ValueError("label must contain text")
    return normalized.replace(" ", "-").replace("--", "-")
'''
            robust = superficial.replace(
                'normalized.replace(" ", "-").replace("--", "-")',
                '"-".join(normalized.split())',
            )
            outputs = [
                ModelOutput(
                    json.dumps({"edits": [{"path": "src/keys.py", "content": superficial}]}),
                    "", 500, 100, 1_000_000_000,
                ),
                ModelOutput(
                    json.dumps({
                        "verdict": "REJECT",
                        "findings": [{
                            "category": "edge_case",
                            "location": "canonical_key",
                            "reason": "Repeated whitespace is not fully collapsed.",
                        }],
                    }),
                    "", 400, 80, 1_000_000_000,
                ),
                ModelOutput(
                    json.dumps({"edits": [{"path": "src/keys.py", "content": robust}]}),
                    "", 550, 110, 1_000_000_000,
                ),
            ]

            def model(request: ModelRequest) -> ModelOutput:
                prompts.append(request)
                return outputs.pop(0)

            result = run_strategy(
                "maker_verifier", worktree, task, model, self.budget(3), seed=11,
                event_log=Path(directory) / "events.jsonl",
                evaluate=lambda root, _task: ".split()" in (root / "src/keys.py").read_text(),
                context=self.context(),
            )

            self.assertTrue(result.objective_success)
            self.assertEqual((result.model_calls, result.public_test_runs), (3, 2))
            self.assertEqual([request.role for request in prompts], ["maker", "verifier", "maker"])
            self.assertEqual(prompts[0].max_output_tokens, 750)
            self.assertEqual(prompts[1].max_output_tokens, 250)
            self.assertNotIn("Return corrected JSON", prompts[1].prompt)
            self.assertNotIn("Each edit must contain", prompts[1].prompt)
            self.assertNotIn("Allowed edit patterns", prompts[1].prompt)
            self.assertEqual(result.verifier_verdict, "REJECT")
            self.assertEqual((result.candidate_a_success, result.candidate_b_success), (False, True))

    def test_maker_verifier_restores_candidate_a_after_invalid_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)
            candidate_a = self.fixed_source
            outputs = [
                ModelOutput(
                    json.dumps({"edits": [{"path": "src/pagination.py", "content": candidate_a}]}),
                    "", 500, 100, 1,
                ),
                ModelOutput(
                    json.dumps({
                        "verdict": "REJECT",
                        "findings": [{
                            "category": "correctness",
                            "location": "clamp_page",
                            "reason": "Review the upper bound.",
                        }],
                    }),
                    "", 300, 50, 1,
                ),
                ModelOutput("not edit json", "", 400, 20, 1),
            ]

            result = run_strategy(
                "maker_verifier", worktree, task, lambda _request: outputs.pop(0),
                self.budget(3), seed=11,
                event_log=Path(directory) / "events.jsonl",
                evaluate=lambda root, _task: root.joinpath("src/pagination.py").read_text() == candidate_a,
                context=self.context(),
            )

            self.assertTrue(result.objective_success)
            self.assertTrue(result.fallback_used)
            self.assertTrue(result.candidate_a_success)
            self.assertIsNone(result.candidate_b_success)
            self.assertEqual((worktree / "src/pagination.py").read_text(), candidate_a)

    def test_invalid_verifier_output_escalates_without_changing_candidate_a(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            events = Path(directory) / "events.jsonl"
            task = prepare_task(self.task_root, worktree)
            outputs = [
                ModelOutput(
                    json.dumps({"edits": [{"path": "src/pagination.py", "content": self.fixed_source}]}),
                    "", 500, 100, 1,
                ),
                ModelOutput(
                    json.dumps({"edits": [{"path": "src/pagination.py", "content": "malicious"}]}),
                    "", 300, 50, 1,
                ),
            ]

            result = run_strategy(
                "maker_verifier", worktree, task, lambda _request: outputs.pop(0),
                self.budget(2), seed=11, event_log=events,
                evaluate=lambda root, _task: root.joinpath("src/pagination.py").read_text() == self.fixed_source,
                context=self.context(),
            )

            self.assertTrue(result.objective_success)
            self.assertEqual(result.verifier_verdict, "ESCALATE")
            self.assertTrue(result.verifier_protocol_error)
            self.assertEqual((worktree / "src/pagination.py").read_text(), self.fixed_source)
            records = [json.loads(line) for line in events.read_text().splitlines()]
            self.assertIn("verifier_protocol_error", [record["type"] for record in records])


if __name__ == "__main__":
    unittest.main()
