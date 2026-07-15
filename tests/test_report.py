from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from edgeloopbench.config import load_experiment
from edgeloopbench.report import (
    ComparisonError,
    _controller_flow,
    _study_snapshot,
    _task_suite,
    render_model_comparison,
    render_report,
)
from edgeloopbench.results import load_results, summarize, validate_results_for_plan


class StaticReportTests(unittest.TestCase):
    project_root = Path(__file__).parents[1]

    def test_v02_report_explains_read_only_loop_and_exact_confirmatory_tasks(self) -> None:
        plan = load_experiment(
            self.project_root / "configs/experiments/v0.2/confirmatory-qwen35-4b.toml"
        )

        flow = _controller_flow(plan)
        tasks = _task_suite(plan)

        self.assertIn("Read-only verifier", flow)
        self.assertIn("Candidate A", flow)
        self.assertIn("APPROVE / REJECT / ESCALATE", flow)
        self.assertNotIn("Tested implementation is review-and-revise", flow)
        self.assertIn("ConfirmatoryRepair-30", tasks)
        self.assertEqual(tasks.count('<article class="task-card">'), 30)
        self.assertIn("clamp(value, low, high)", tasks)
        self.assertIn("30 paired observations per arm", tasks)

    def test_v03_report_explains_evidence_gate_and_fresh_task_suite(self) -> None:
        plan = load_experiment(
            self.project_root / "configs/experiments/v0.3/calibration-qwen35-4b.toml"
        )

        flow = _controller_flow(plan)
        tasks = _task_suite(plan)

        self.assertIn("Evidence-Gated Loop", flow)
        self.assertIn("five-item checklist", flow)
        self.assertIn("re-check", flow)
        self.assertIn("restore A", flow)
        self.assertNotIn("review-and-revise", flow)
        self.assertIn("TopologyCalibration-6", tasks)
        self.assertEqual(tasks.count('<article class="task-card">'), 6)
        self.assertIn("shipping_quote", tasks)
        self.assertIn("Verifier adversarial", tasks)

    def test_v04_report_names_goal_loop_and_fresh_pilot_tasks(self) -> None:
        plan = load_experiment(
            self.project_root / "configs/experiments/v0.4/pilot-qwen35-4b.toml"
        )

        flow = _controller_flow(plan)
        tasks = _task_suite(plan)
        snapshot = _study_snapshot(((plan, None, ()),))

        self.assertIn("Goal Skill Loop", flow)
        self.assertIn("at most five", flow)
        self.assertIn("OfficialLoopPilot-8", tasks)
        self.assertIn("Ceiling page count", tasks)
        self.assertIn("Falsy repository values", tasks)
        self.assertEqual(tasks.count('<article class="task-card">'), 8)
        self.assertIn("OfficialLoopPilot-8", snapshot)
        self.assertIn("1 seed × 1 budget tier", snapshot)

    def test_readme_contains_every_v04_episode_record(self) -> None:
        readme = (self.project_root / "README.md").read_text(encoding="utf-8")
        comparison = json.loads(
            (
                self.project_root
                / "results/OPEN-ME/current/comparison.json"
            ).read_text(encoding="utf-8")
        )

        expected_rows: list[str] = []
        for experiment in comparison["experiments"]:
            for record in experiment["records"]:
                objective = "PASS" if record["objective_success"] else "FAIL"
                total_tokens = record["prompt_tokens"] + record["completion_tokens"]
                reason = record["failure_reason"] or "success"
                expected_rows.append(
                    f"| `{record['task_id']}` | `{record['strategy']}` | "
                    f"{objective} | {record['prompt_tokens']:,} | "
                    f"{record['completion_tokens']:,} | {total_tokens:,} | "
                    f"{record['wall_seconds']:.3f} | {record['model_calls']} | "
                    f"{record['tool_calls']} | {record['public_test_runs']} | "
                    f"{record['max_call_context_tokens']:,} | `{reason}` |"
                )

        self.assertEqual(len(expected_rows), 48)
        self.assertEqual(readme.count("| `v04-"), 48)
        for row in expected_rows:
            self.assertIn(row, readme)

        records = [
            record
            for experiment in comparison["experiments"]
            for record in experiment["records"]
        ]
        for strategy, label in (
            ("direct", "Direct"),
            ("bounded_retry", "Bounded Retry"),
            ("goal_skill_loop", "Goal Skill Loop"),
        ):
            arm = [record for record in records if record["strategy"] == strategy]
            prompts = sum(record["model_calls"] for record in arm)
            follow_ups = prompts - len(arm)
            converged = sum(
                record["objective_success"] and record["model_calls"] > 1
                for record in arm
            )
            unresolved = sum(not record["objective_success"] for record in arm)
            self.assertIn(
                f"| {label} | {prompts} | {follow_ups} | {converged} | "
                f"{unresolved}/{len(arm)} |",
                readme,
            )
        self.assertIn("not be presented as a reproduction", readme)
        self.assertIn("fresh small evaluator", readme)

    def test_renders_agent_visible_microrepair_task_catalog(self) -> None:
        plan = load_experiment(
            self.project_root / "configs/experiments/smoke.toml"
        )

        html = _task_suite(plan)

        self.assertIn("MicroRepair-6 task catalog", html)
        self.assertEqual(html.count('<article class="task-card">'), 6)
        self.assertIn("Pagination upper bound", html)
        self.assertIn("Comma-separated tags", html)
        self.assertIn("Inventory reservation state", html)
        self.assertIn("Generated mutation", html)
        self.assertIn("Verifier adversarial", html)
        self.assertIn("Agent-visible repair contract", html)
        self.assertIn("Public tests", html)
        self.assertIn("Hidden evaluation", html)

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
        self.assertIn("Task-clustered 95% CI", html)
        self.assertIn("Exact paired p", html)
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
        self.assertIn(".comparison-leaderboard th:nth-child(2)", html)
        self.assertIn(".transitions{table-layout:fixed}", html)
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

    def test_cross_model_report_supports_manifest_declared_goal_skill_loop(self) -> None:
        base_plan = load_experiment(
            self.project_root / "examples/results/sample-plan.toml"
        )
        base_records = load_results(
            self.project_root / "examples/results/sample-runs.jsonl"
        )
        first_sha = "c" * 64
        plan = replace(
            base_plan,
            id="goal-loop-first",
            strategies=("direct", "bounded_retry", "goal_skill_loop"),
            manifest_sha256=first_sha,
        )
        records = tuple(
            replace(
                record,
                experiment_id=plan.id,
                strategy=(
                    "goal_skill_loop"
                    if record.strategy == "maker_verifier"
                    else record.strategy
                ),
                manifest_sha256="sha256:" + first_sha,
            )
            for record in base_records
        )
        report = summarize(records, plan, validate_results_for_plan(records, plan))
        second_sha = "d" * 64
        second_plan = replace(
            plan,
            id="goal-loop-second",
            model=replace(
                plan.model,
                id="phi4-mini:latest",
                revision="sha256:" + "e" * 64,
                artifact_sha256="sha256:" + "e" * 64,
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
        second_report = summarize(
            second_records,
            second_plan,
            validate_results_for_plan(second_records, second_plan),
        )

        with tempfile.TemporaryDirectory() as directory:
            index = render_model_comparison(
                (
                    (plan, report, records),
                    (second_plan, second_report, second_records),
                ),
                directory,
            )
            html = index.read_text(encoding="utf-8")

        self.assertIn("Goal Skill Loop", html)
        self.assertIn("at most five", html)
        self.assertNotIn("Current Maker–Verifier", html)


if __name__ == "__main__":
    unittest.main()
