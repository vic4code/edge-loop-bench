from __future__ import annotations

import io
import json
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from edgeloopbench.controller import ModelOutput

from edgeloopbench.cli import main
from edgeloopbench.config import load_experiment


class CliTests(unittest.TestCase):
    def test_validate_json_reports_expected_run_count(self) -> None:
        root = Path(__file__).resolve().parents[1]
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(
                ["validate", str(root / "configs/experiments/smoke.toml"), "--json"]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["track"], "effectiveness")
        self.assertEqual(payload["planned_runs"], 72)

    def test_invalid_manifest_returns_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(
                textwrap.dedent("""
                schema_version = 1
                id = "bad"
                track = "unknown"
                draft = true
            """),
                encoding="utf-8",
            )
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = main(["validate", str(path)])

        self.assertEqual(exit_code, 2)
        self.assertIn("track", stderr.getvalue())

    def test_json_mode_returns_machine_readable_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(
                'schema_version = 1\nid = "bad"\ntrack = "unknown"\ndraft = true\n'
            )
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = main(["validate", str(path), "--json"])

        payload = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["exit_code"], 2)
        self.assertIn("track", payload["error"])

    def test_json_mode_returns_machine_readable_argument_error(self) -> None:
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            exit_code = main(["validate", "--json"])

        payload = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["exit_code"], 2)
        self.assertIn("manifest", payload["error"])

    def test_summarize_json_is_machine_readable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "summarize",
                    str(root / "examples/results/sample-runs.jsonl"),
                    "--manifest",
                    str(root / "examples/results/sample-plan.toml"),
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(payload["arms"]), 3)
        self.assertEqual(len(payload["pairs"]), 3)
        self.assertEqual(payload["coverage"]["missing_runs"], 0)
        self.assertEqual(
            payload["manifest_bindings"][0]["manifest_sha256"],
            "sha256:db82951d377e1b07ea368103ae0d6371d34943fae533ef17133b627bd884894b",
        )

    def test_doctor_json_never_requires_a_server(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(["doctor", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIn("python_version", payload)
        self.assertIn("runtimes", payload)
        self.assertNotIn(str(Path.home()), json.dumps(payload))

    def test_task_prepare_and_public_test_commands(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                prepare_exit = main([
                    "task", "prepare", "python-localized-001",
                    "--work-root", str(worktree),
                    "--catalog-root", str(root / "tasks/micro"),
                    "--json",
                ])
            prepared = json.loads(stdout.getvalue())

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                test_exit = main([
                    "task", "public-test", str(worktree),
                    "--catalog-root", str(root / "tasks/micro"),
                    "--json",
                ])
            tested = json.loads(stdout.getvalue())

        self.assertEqual(prepare_exit, 0)
        self.assertEqual(prepared["task_id"], "python-localized-001")
        self.assertEqual(test_exit, 1)
        self.assertFalse(tested["passed"])
        self.assertNotIn(str(worktree), tested["output"])

    def test_report_command_writes_html_and_json(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "report", str(root / "examples/results/sample-runs.jsonl"),
                    "--manifest", str(root / "examples/results/sample-plan.toml"),
                    "--output", directory,
                    "--json",
                ])

            payload = json.loads(stdout.getvalue())
            index = Path(directory) / "index.html"
            data = Path(directory) / "report.json"

            self.assertEqual(exit_code, 0)
            self.assertTrue(index.is_file())
            self.assertTrue(data.is_file())
            self.assertEqual(payload["index"], "index.html")

    def test_compare_command_writes_cross_model_report(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source_manifest = root / "examples/results/sample-plan.toml"
        source_results = root / "examples/results/sample-runs.jsonl"
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            second_manifest = temporary / "second.toml"
            manifest_text = source_manifest.read_text(encoding="utf-8")
            manifest_text = manifest_text.replace('id = "demo"', 'id = "demo-second"')
            manifest_text = manifest_text.replace(
                'id = "synthetic-model"\nrevision = "UNPINNED"\n'
                'artifact_sha256 = "UNPINNED"',
                'id = "second-model"\nrevision = "second-revision"\n'
                'artifact_sha256 = "second-artifact"',
            )
            second_manifest.write_text(manifest_text, encoding="utf-8")
            second_plan = load_experiment(second_manifest)
            second_results = temporary / "second.jsonl"
            rows = []
            for line in source_results.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                row["experiment_id"] = second_plan.id
                row["manifest_sha256"] = "sha256:" + second_plan.manifest_sha256
                rows.append(json.dumps(row, sort_keys=True))
            second_results.write_text("\n".join(rows) + "\n", encoding="utf-8")
            output = temporary / "comparison"

            exit_code = main([
                "compare",
                "--experiment", str(source_manifest), str(source_results),
                "--experiment", str(second_manifest), str(second_results),
                "--output", str(output),
            ])

            self.assertEqual(exit_code, 0)
            self.assertTrue((output / "index.html").is_file())
            self.assertTrue((output / "comparison.json").is_file())

    def test_run_command_can_execute_a_bounded_manifest_slice(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = '''"""One-based pagination helpers."""


def clamp_page(page: int, total_pages: int) -> int:
    """Return *page* constrained to the inclusive valid page range."""

    if total_pages <= 0:
        raise ValueError("total_pages must be positive")
    return max(1, min(page, total_pages))
'''

        def fake_model(_prompt: str, _seed: int, _limit: int) -> ModelOutput:
            text = json.dumps({"edits": [{"path": "src/pagination.py", "content": source}]})
            return ModelOutput(text, "", 600, 120, 1_000_000_000)

        with tempfile.TemporaryDirectory() as directory, patch(
            "edgeloopbench.cli.build_ollama_model", return_value=fake_model
        ), patch(
            "edgeloopbench.cli.build_isolated_evaluator",
            return_value=lambda _worktree, _task: True,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main([
                    "run", str(root / "configs/experiments/smoke.toml"),
                    "--results", str(Path(directory) / "runs.jsonl"),
                    "--events", str(Path(directory) / "events.jsonl"),
                    "--task-catalog", str(root / "tasks/micro"),
                    "--evaluator-catalog", str(root / "evaluators"),
                    "--work-root", str(Path(directory) / "work"),
                    "--max-runs", "1", "--json",
                ])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["executed_runs"], 1)
        self.assertEqual(payload["planned_runs"], 72)


if __name__ == "__main__":
    unittest.main()
