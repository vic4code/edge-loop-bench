from __future__ import annotations

import io
import json
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from edgeloopbench.cli import main


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
            "sha256:4f88916713f12e764feca93c62a36ed0004eadc37c6fa16942d7bf63b8f548fd",
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


if __name__ == "__main__":
    unittest.main()
