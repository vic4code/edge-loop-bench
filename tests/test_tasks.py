from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from edgeloopbench.tasks import TaskManifestError, load_task_manifest


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


if __name__ == "__main__":
    unittest.main()
