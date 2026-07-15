from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from edgeloopbench.config import load_experiment
from edgeloopbench.controller import ModelOutput, ModelRequest
from edgeloopbench.experiment import (
    build_isolated_evaluator,
    build_run_schedule,
    execute_plan,
)
from edgeloopbench.results import load_results
from edgeloopbench.runner import apply_candidate_edits, run_public_tests
from edgeloopbench.tasks import prepare_task


class ExperimentExecutionTests(unittest.TestCase):
    project_root = Path(__file__).parents[1]
    fixed_source = '''"""One-based pagination helpers."""


def clamp_page(page: int, total_pages: int) -> int:
    """Return *page* constrained to the inclusive valid page range."""

    if total_pages <= 0:
        raise ValueError("total_pages must be positive")
    return max(1, min(page, total_pages))
'''

    def test_manifest_bound_schedule_randomizes_tasks_and_strategies_reproducibly(self) -> None:
        plan = load_experiment(
            self.project_root / "configs/experiments/v0.2/confirmatory-qwen35-4b.toml"
        )

        first = build_run_schedule(plan)
        second = build_run_schedule(plan)

        self.assertEqual(first, second)
        self.assertEqual(len(first), plan.run_count)
        self.assertEqual(
            {item.task_id for item in first},
            set(plan.tasks),
        )
        strategy_orders = {}
        for item in first:
            strategy_orders.setdefault(item.task_id, []).append(item.strategy)
        self.assertTrue(any(order[0] != "direct" for order in strategy_orders.values()))
        self.assertTrue(all(set(order) == set(plan.strategies) for order in strategy_orders.values()))

    def test_isolated_evaluator_returns_only_the_objective_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "superficial"
            task = prepare_task(
                self.project_root / "tasks/micro/python-adversarial-001", worktree
            )
            evaluate = build_isolated_evaluator(self.project_root / "evaluators")
            source_path = worktree / "src/keys.py"
            superficial = source_path.read_text().replace(
                'normalized.replace(" ", "-")',
                'normalized.replace(" ", "-").replace("--", "-")',
            )
            apply_candidate_edits(
                worktree, task,
                json.dumps({"edits": [{"path": "src/keys.py", "content": superficial}]}),
            )
            self.assertTrue(run_public_tests(worktree, task).passed)
            superficial_outcome = evaluate(worktree, task)

            repaired_worktree = Path(directory) / "repaired"
            repaired_task = prepare_task(
                self.project_root / "tasks/micro/python-adversarial-001",
                repaired_worktree,
            )
            subprocess.run(
                ["git", "apply", str(self.project_root / "evaluators/python-adversarial-001/gold.patch")],
                cwd=repaired_worktree, check=True, capture_output=True, text=True,
            )
            repaired_outcome = evaluate(repaired_worktree, repaired_task)

        self.assertFalse(superficial_outcome)
        self.assertTrue(repaired_outcome)

    def test_executes_declared_matrix_and_resumes_from_append_only_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "plan.toml"
            manifest.write_text(textwrap.dedent('''
                schema_version = 1
                id = "execution-test"
                track = "effectiveness"
                draft = true
                tasks = ["python-localized-001"]
                strategies = ["direct", "bounded_retry"]
                seeds = [11]

                [generation]
                thinking = false
                temperature = 0.0
                edit_schema_revision = "full-file-edits-v1"
                controller_revision = "commit-deadbeef"

                [model]
                id = "fake"
                revision = "UNPINNED"
                artifact_sha256 = "UNPINNED"
                weight_quantization = "fake"
                context_limit_tokens = 4096

                [backend]
                name = "ollama"
                version = "UNPINNED"
                artifact_sha256 = "UNPINNED"
                command = ["ollama", "serve"]

                [backend.environment]
                OLLAMA_HOST = "127.0.0.1:11434"

                [budgets.small]
                prompt_tokens = 4000
                completion_tokens = 1000
                model_calls = 2
                tool_calls = 6
                public_test_runs = 2
                per_call_context_tokens = 4096
            '''), encoding="utf-8")
            plan = load_experiment(manifest)
            results = root / "runs.jsonl"
            events = root / "events.jsonl"

            def model(_request: ModelRequest) -> ModelOutput:
                text = json.dumps({"edits": [{
                    "path": "src/pagination.py", "content": self.fixed_source,
                }]})
                return ModelOutput(text, "", 600, 120, 1_000_000_000)

            first = execute_plan(
                plan, self.project_root / "tasks/micro", root / "work",
                events, results, model=model,
                evaluate=lambda _worktree, _task: True,
            )
            second = execute_plan(
                plan, self.project_root / "tasks/micro", root / "work",
                events, results, model=model,
                evaluate=lambda _worktree, _task: True,
            )
            records = load_results(results)

        self.assertEqual((first.executed_runs, first.skipped_runs), (2, 0))
        self.assertEqual((second.executed_runs, second.skipped_runs), (0, 2))
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record.objective_success for record in records))


if __name__ == "__main__":
    unittest.main()
