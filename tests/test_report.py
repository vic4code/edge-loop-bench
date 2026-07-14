from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from edgeloopbench.config import load_experiment
from edgeloopbench.report import ComparisonError, render_model_comparison, render_report
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

    def test_renders_paired_cross_model_loop_comparison(self) -> None:
        plan = load_experiment(self.project_root / "examples/results/sample-plan.toml")
        records = load_results(self.project_root / "examples/results/sample-runs.jsonl")
        coverage = validate_results_for_plan(records, plan)
        report = summarize(records, plan, coverage)
        second_sha = "b" * 64
        second_plan = replace(
            plan,
            id="demo-second-model",
            model=replace(
                plan.model,
                id="gemma4:12b-it-q4_K_M",
                revision="sha256:" + "a" * 64,
                artifact_sha256="sha256:" + "a" * 64,
            ),
            manifest_sha256=second_sha,
        )
        second_records = tuple(
            replace(
                record,
                experiment_id=second_plan.id,
                manifest_sha256="sha256:" + second_sha,
            )
            for record in records
        )
        second_coverage = validate_results_for_plan(second_records, second_plan)
        second_report = summarize(second_records, second_plan, second_coverage)
        second_report = replace(
            second_report,
            arms=tuple(
                replace(
                    arm,
                    successes=0,
                    success_rate=0.0,
                    success_per_1k_tokens=0.0,
                )
                for arm in second_report.arms
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            index_path = render_model_comparison(
                (
                    (plan, report, records),
                    (second_plan, second_report, second_records),
                ),
                directory,
            )
            html = index_path.read_text(encoding="utf-8")
            data = json.loads((Path(directory) / "comparison.json").read_text())

        self.assertIn("Loop effect by model", html)
        self.assertIn("Study snapshot", html)
        self.assertIn("What the evidence supports", html)
        self.assertIn("Measured finding", html)
        self.assertIn("Design limitation", html)
        self.assertIn("not evidence that verifier loops generally fail", html)
        self.assertIn("Verified success", html)
        self.assertIn("Logical token cost", html)
        self.assertIn("Mean episode time", html)
        self.assertEqual(html.count('<article class="metric-card '), 3)
        self.assertIn('class="zero-marker"', html)
        self.assertIn("0% verified success", html)
        self.assertIn("Baseline → loop uplift", html)
        self.assertIn("Direct baseline", html)
        self.assertIn("Gemma 4 12B", html)
        self.assertIn("Success Δ", html)
        self.assertIn("Token cost", html)
        self.assertIn("Controller flow", html)
        self.assertIn("Review and revise", html)
        self.assertIn("Hidden evaluator feedback is never returned", html)
        self.assertIn("What was tested", html)
        self.assertEqual(html.count('<article class="task-card">'), 2)
        self.assertIn("task-001", html)
        self.assertIn("Rescued", html)
        self.assertIn("Regressed", html)
        self.assertIn("Serving efficiency is reported separately", html)
        self.assertEqual(len(data["experiments"]), 2)

        incompatible_plan = replace(
            second_plan,
            model=replace(second_plan.model, weight_quantization="Q8_0"),
        )
        with self.assertRaisesRegex(ComparisonError, "differs outside"):
            render_model_comparison(
                (
                    (plan, report, records),
                    (incompatible_plan, second_report, second_records),
                ),
                tempfile.gettempdir(),
            )


if __name__ == "__main__":
    unittest.main()
