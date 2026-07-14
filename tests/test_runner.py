from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.runner import (
    CandidatePatchError,
    append_event,
    apply_candidate_edits,
    apply_candidate_patch,
    build_agent_prompt,
    run_public_tests,
)
from edgeloopbench.tasks import prepare_task


class RunnerBoundaryTests(unittest.TestCase):
    project_root = Path(__file__).parents[1]
    task_root = project_root / "tasks" / "micro" / "python-localized-001"

    def test_prompt_contains_public_bundle_without_evaluator_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)

            prompt = build_agent_prompt(worktree, task)

            self.assertIn("pagination upper bound", prompt)
            self.assertIn("total_pages - 1", prompt)
            self.assertNotIn("gold.patch", prompt)
            self.assertNotIn("evaluator", prompt.lower())
            self.assertNotIn("hidden", prompt.lower())

    def test_candidate_patch_can_only_change_allowed_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)
            source_patch = (self.project_root / "evaluators/python-localized-001/gold.patch").read_text()

            apply_candidate_patch(worktree, task, source_patch)
            self.assertIn("min(page, total_pages)", (worktree / "src/pagination.py").read_text())

            test_patch = source_patch.replace("src/pagination.py", "tests/public/test_pagination.py")
            with self.assertRaisesRegex(CandidatePatchError, "allowed"):
                apply_candidate_patch(worktree, task, test_patch)

            mismatched_header = source_patch.replace(
                "+++ b/src/pagination.py", "+++ b/tests/public/test_pagination.py"
            )
            with self.assertRaisesRegex(CandidatePatchError, "header paths"):
                apply_candidate_patch(worktree, task, mismatched_header)

    def test_structured_edits_replace_only_allowed_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)
            source = (worktree / "src/pagination.py").read_text().replace(
                "total_pages - 1", "total_pages"
            )

            apply_candidate_edits(
                worktree,
                task,
                json.dumps({"edits": [{"path": "src/pagination.py", "content": source}]}),
            )

            self.assertIn("min(page, total_pages)", (worktree / "src/pagination.py").read_text())
            with self.assertRaisesRegex(CandidatePatchError, "allowed"):
                apply_candidate_edits(
                    worktree,
                    task,
                    json.dumps({"edits": [{"path": "tests/public/test_pagination.py", "content": ""}]}),
                )

    def test_public_test_output_replaces_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)

            result = run_public_tests(worktree, task)

            self.assertFalse(result.passed)
            self.assertNotIn(str(worktree), result.output)

    def test_append_event_preserves_existing_jsonl_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            append_event(path, {"sequence": 1, "type": "run_started"})
            append_event(path, {"sequence": 2, "type": "model_completed"})

            records = [json.loads(line) for line in path.read_text().splitlines()]

            self.assertEqual([record["sequence"] for record in records], [1, 2])


if __name__ == "__main__":
    unittest.main()
