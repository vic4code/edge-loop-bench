from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.config import load_experiment
from edgeloopbench.report import render_report
from edgeloopbench.results import load_results, summarize, validate_results_for_plan


class StaticReportTests(unittest.TestCase):
    project_root = Path(__file__).parents[1]

    def test_renders_self_contained_effectiveness_and_separate_serving_panels(self) -> None:
        plan = load_experiment(self.project_root / "examples/results/sample-plan.toml")
        records = load_results(self.project_root / "examples/results/sample-runs.jsonl")
        coverage = validate_results_for_plan(records, plan)
        report = summarize(records, plan, coverage)

        with tempfile.TemporaryDirectory() as directory:
            index_path = render_report(report, plan, records, directory)
            html = index_path.read_text(encoding="utf-8")
            data = json.loads((Path(directory) / "report.json").read_text())

        self.assertIn("Agent effectiveness", html)
        self.assertIn("Serving efficiency", html)
        self.assertIn("Verified success vs. logical tokens", html)
        self.assertIn("Task × strategy", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script src=", html)
        self.assertEqual(data["summary"]["coverage"]["observed_runs"], 6)
        self.assertEqual(len(data["records"]), 6)


if __name__ == "__main__":
    unittest.main()
