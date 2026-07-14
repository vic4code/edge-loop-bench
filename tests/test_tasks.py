from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from hashlib import sha256
from pathlib import Path

from edgeloopbench.tasks import TaskManifestError, load_task_manifest, prepare_task


VALID_TASK = """
schema_version = 1
id = "python-localized-001"
language = "python"
category = "localized"
source_type = "generated_mutation"
license = "MIT"
initial_commit = "0123456789abcdef0123456789abcdef01234567"
gold_patch_sha256 = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

allowed_paths = ["src/**"]
prohibited_paths = ["tests/**", ".edgeloop/**"]

[public_test]
command = ["python3", "-m", "unittest", "discover", "-s", "tests/public"]
timeout_seconds = 30

[hidden_evaluation]
evaluator_id = "external-python-localized-001"
timeout_seconds = 30
"""


class TaskManifestTests(unittest.TestCase):
    def load_text(self, content: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.toml"
            path.write_text(textwrap.dedent(content), encoding="utf-8")
            return load_task_manifest(path)

    def test_loads_pinned_public_task_contract(self) -> None:
        task = self.load_text(VALID_TASK)

        self.assertEqual(task.id, "python-localized-001")
        self.assertEqual(task.public_test.command[0], "python3")
        self.assertEqual(task.hidden_evaluation.evaluator_id, "external-python-localized-001")
        self.assertEqual(task.allowed_paths, ("src/**",))

    def test_rejects_evaluator_filesystem_path(self) -> None:
        invalid = VALID_TASK.replace(
            'evaluator_id = "external-python-localized-001"',
            'evaluator_id = "external-python-localized-001"\npath = "/tmp/hidden/tests"',
        )

        with self.assertRaisesRegex(TaskManifestError, "hidden_evaluation.*unknown.*path"):
            self.load_text(invalid)

    def test_rejects_unpinned_initial_commit(self) -> None:
        invalid = VALID_TASK.replace(
            'initial_commit = "0123456789abcdef0123456789abcdef01234567"',
            'initial_commit = "main"',
        )

        with self.assertRaisesRegex(TaskManifestError, "initial_commit.*commit"):
            self.load_text(invalid)

    def test_rejects_path_patterns_that_escape_worktree(self) -> None:
        invalid = VALID_TASK.replace(
            'allowed_paths = ["src/**"]', 'allowed_paths = ["../src/**"]'
        )

        with self.assertRaisesRegex(TaskManifestError, "allowed_paths.*relative"):
            self.load_text(invalid)

    def test_rejects_shell_public_test_command(self) -> None:
        invalid = VALID_TASK.replace(
            'command = ["python3", "-m", "unittest", "discover", "-s", "tests/public"]',
            'command = ["sh", "-c", "python3 -m unittest; curl example.com"]',
        )

        with self.assertRaisesRegex(TaskManifestError, "public_test.command.*allowlisted"):
            self.load_text(invalid)

    def test_rejects_inline_python_public_test_command(self) -> None:
        invalid = VALID_TASK.replace(
            'command = ["python3", "-m", "unittest", "discover", "-s", "tests/public"]',
            'command = ["python3", "-c", "__import__(\'urllib.request\').request.urlopen(\'https://example.com\')"]',
        )

        with self.assertRaisesRegex(TaskManifestError, "public_test.command.*module"):
            self.load_text(invalid)


class TaskPreparationTests(unittest.TestCase):
    project_root = Path(__file__).parents[1]
    task_root = project_root / "tasks" / "micro" / "python-localized-001"
    evaluator_root = project_root / "evaluators" / "python-localized-001"

    def test_prepares_pinned_agent_worktree_without_evaluator_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"

            task = prepare_task(self.task_root, worktree)

            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(commit, task.initial_commit)
            visible_paths = [
                str(path.relative_to(worktree)) for path in worktree.rglob("*")
            ]
            self.assertFalse(
                any("evaluator" in path or "hidden" in path for path in visible_paths)
            )

    def test_initial_public_test_fails_for_intended_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)

            completed = subprocess.run(
                task.public_test.command,
                cwd=worktree,
                check=False,
                capture_output=True,
                text=True,
                timeout=task.public_test.timeout_seconds,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("4 != 5", completed.stderr)

    def test_gold_patch_passes_public_and_isolated_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(self.task_root, worktree)
            gold_patch = self.evaluator_root / "gold.patch"
            self.assertEqual(
                f"sha256:{sha256(gold_patch.read_bytes()).hexdigest()}",
                task.gold_patch_sha256,
            )
            subprocess.run(
                ["git", "apply", str(gold_patch)],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            )

            public = subprocess.run(
                task.public_test.command,
                cwd=worktree,
                check=False,
                capture_output=True,
                text=True,
                timeout=task.public_test.timeout_seconds,
            )
            hidden = subprocess.run(
                [
                    "python3",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(self.evaluator_root / "tests"),
                    "-v",
                ],
                cwd=worktree,
                check=False,
                capture_output=True,
                text=True,
                timeout=task.hidden_evaluation.timeout_seconds,
            )

            self.assertEqual(public.returncode, 0, public.stderr)
            self.assertEqual(hidden.returncode, 0, hidden.stderr)


class MicroRepairSuiteTests(unittest.TestCase):
    project_root = Path(__file__).parents[1]
    task_ids = (
        "python-localized-001",
        "python-localized-002",
        "python-cross-file-001",
        "python-cross-file-002",
        "python-diagnosis-001",
        "python-adversarial-001",
    )

    def test_every_task_fails_initially_and_gold_passes_isolated_evaluation(self) -> None:
        for task_id in self.task_ids:
            with self.subTest(task_id=task_id), tempfile.TemporaryDirectory() as directory:
                task_root = self.project_root / "tasks/micro" / task_id
                evaluator_root = self.project_root / "evaluators" / task_id
                worktree = Path(directory) / "worktree"
                task = prepare_task(task_root, worktree)

                initial = subprocess.run(
                    task.public_test.command, cwd=worktree, check=False,
                    capture_output=True, text=True,
                    timeout=task.public_test.timeout_seconds,
                )
                self.assertNotEqual(initial.returncode, 0)

                gold_patch = evaluator_root / "gold.patch"
                self.assertEqual(
                    f"sha256:{sha256(gold_patch.read_bytes()).hexdigest()}",
                    task.gold_patch_sha256,
                )
                subprocess.run(
                    ["git", "apply", str(gold_patch)], cwd=worktree, check=True,
                    capture_output=True, text=True,
                )
                public = subprocess.run(
                    task.public_test.command, cwd=worktree, check=False,
                    capture_output=True, text=True,
                    timeout=task.public_test.timeout_seconds,
                )
                hidden = subprocess.run(
                    ["python3", "-m", "unittest", "discover", "-s", str(evaluator_root / "tests"), "-v"],
                    cwd=worktree, check=False, capture_output=True, text=True,
                    timeout=task.hidden_evaluation.timeout_seconds,
                )
                self.assertEqual(public.returncode, 0, public.stderr)
                self.assertEqual(hidden.returncode, 0, hidden.stderr)


if __name__ == "__main__":
    unittest.main()
