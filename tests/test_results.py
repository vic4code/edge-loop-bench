from __future__ import annotations

import json
import math
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from edgeloopbench.config import ExperimentPlan, load_experiment
from edgeloopbench.results import (
    ResultError,
    load_results,
    summarize,
    validate_results_for_plan,
)


def record(task: str, strategy: str, success: bool, tokens: int) -> dict[str, object]:
    return {
        "experiment_id": "exp",
        "task_id": task,
        "strategy": strategy,
        "budget_tier": "medium",
        "seed": 1,
        "run_status": "completed",
        "objective_success": success,
        "prompt_tokens": tokens - 100,
        "completion_tokens": 100,
        "model_calls": 1,
        "tool_calls": 2,
        "public_test_runs": 1,
        "max_call_context_tokens": 1000,
        "wall_seconds": float(tokens) / 10,
    }


def bind_to_plan(item: dict[str, object], plan: ExperimentPlan) -> None:
    item["manifest_sha256"] = f"sha256:{plan.manifest_sha256}"


class ResultSummaryTests(unittest.TestCase):
    def write_records(self, records: list[dict[str, object]]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "runs.jsonl"
        path.write_text(
            "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
        )
        return path

    def load_deployment_plan(self) -> ExperimentPlan:
        root = Path(__file__).resolve().parents[1]
        source = (root / "examples/results/sample-plan.toml").read_text(
            encoding="utf-8"
        )
        source = source.replace('track = "effectiveness"', 'track = "deployment"')
        source += (
            "\n[physical_budget]\nmax_wall_seconds = 100\nmax_energy_joules = 500\n"
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "deployment.toml"
        path.write_text(source, encoding="utf-8")
        return load_experiment(path)

    def test_summarizes_arms_and_paired_deltas(self) -> None:
        records = [
            record("a", "direct", False, 1000),
            record("a", "bounded_retry", True, 1500),
            record("b", "direct", True, 1200),
            record("b", "bounded_retry", True, 1400),
        ]

        report = summarize(load_results(self.write_records(records)))
        direct = next(arm for arm in report.arms if arm.strategy == "direct")
        pair = report.pairs[0]

        self.assertEqual(direct.run_count, 2)
        self.assertEqual(direct.observed_runs, 2)
        self.assertEqual(direct.invalid_runs, 0)
        self.assertEqual(direct.successes, 1)
        self.assertEqual(direct.success_rate, 0.5)
        self.assertAlmostEqual(direct.success_per_1k_tokens, 1000 / 2200)
        self.assertEqual(pair.baseline_strategy, "direct")
        self.assertEqual(pair.candidate_strategy, "bounded_retry")
        self.assertEqual(pair.pair_count, 2)
        self.assertEqual(pair.success_delta_pp, 50.0)
        self.assertEqual(pair.mean_total_token_delta, 350.0)

    def test_summary_reports_manifest_binding(self) -> None:
        item = record("a", "direct", True, 1000)
        item["manifest_sha256"] = "sha256:" + "a" * 64

        payload = summarize(load_results(self.write_records([item]))).to_dict()

        self.assertEqual(
            payload["manifest_bindings"],
            [
                {
                    "experiment_id": "exp",
                    "manifest_sha256": "sha256:" + "a" * 64,
                }
            ],
        )

    def test_unbound_summary_rejects_mixed_manifest_digests(self) -> None:
        first = record("a", "direct", True, 1000)
        second = record("b", "direct", False, 1000)
        first["manifest_sha256"] = "sha256:" + "a" * 64
        second["manifest_sha256"] = "sha256:" + "b" * 64

        with self.assertRaisesRegex(ResultError, "multiple manifest bindings"):
            summarize(load_results(self.write_records([first, second])))

    def test_reports_only_complete_pairs(self) -> None:
        records = [
            record("a", "direct", False, 1000),
            record("a", "bounded_retry", True, 1500),
            record("b", "direct", True, 1200),
        ]

        pair = summarize(load_results(self.write_records(records))).pairs[0]

        self.assertEqual(pair.pair_count, 1)

    def test_rejects_duplicate_run_key(self) -> None:
        item = record("a", "direct", True, 1000)

        with self.assertRaisesRegex(ResultError, "duplicate run key"):
            load_results(self.write_records([item, item]))

    def test_rejects_non_boolean_success(self) -> None:
        item = record("a", "direct", True, 1000)
        item["objective_success"] = 1

        with self.assertRaisesRegex(ResultError, "objective_success.*boolean"):
            load_results(self.write_records([item]))

    def test_rejects_non_finite_numbers(self) -> None:
        item = record("a", "direct", True, 1000)
        item["wall_seconds"] = math.inf

        with self.assertRaisesRegex(ResultError, "finite"):
            load_results(self.write_records([item]))

    def test_rejects_malformed_json_with_line_number(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "runs.jsonl"
        path.write_text("{not-json}\n", encoding="utf-8")

        with self.assertRaisesRegex(ResultError, "line 1"):
            load_results(path)

    def test_rejects_non_utf8_result_file_with_domain_error(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "runs.jsonl"
        path.write_bytes(b"\xff\n")

        with self.assertRaisesRegex(ResultError, "valid UTF-8"):
            load_results(path)

    def test_rejects_json_integer_above_parser_safety_limit(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "runs.jsonl"
        path.write_text('{"seed":' + "9" * 5000 + "}\n", encoding="utf-8")

        with self.assertRaisesRegex(ResultError, "invalid JSON number"):
            load_results(path)

    def test_rejects_empty_result_file(self) -> None:
        with self.assertRaisesRegex(ResultError, "no result records"):
            load_results(self.write_records([]))

    def test_infrastructure_error_is_reported_but_excluded_from_denominator(
        self,
    ) -> None:
        item = record("a", "direct", False, 1000)
        item["run_status"] = "infrastructure_error"
        item["failure_reason"] = "server process exited"

        arm = summarize(load_results(self.write_records([item]))).arms[0]

        self.assertEqual(arm.observed_runs, 1)
        self.assertEqual(arm.run_count, 0)
        self.assertEqual(arm.invalid_runs, 1)
        self.assertIsNone(arm.success_rate)

    def test_non_completed_status_cannot_claim_success(self) -> None:
        item = record("a", "direct", True, 1000)
        item["run_status"] = "timeout"
        item["failure_reason"] = "task wall timeout"

        with self.assertRaisesRegex(ResultError, "timeout.*cannot.*success"):
            load_results(self.write_records([item]))

    def test_plan_validation_rejects_undeclared_arm(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = load_experiment(root / "examples/results/sample-plan.toml")
        item = record("task-001", "unregistered_loop", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(ResultError, "not declared by manifest"):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_plan_validation_rejects_silent_missing_runs_by_default(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = load_experiment(root / "examples/results/sample-plan.toml")
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(ResultError, "missing 5 declared runs"):
            validate_results_for_plan(records, plan)

    def test_plan_validation_rejects_over_budget_counters(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = load_experiment(root / "examples/results/sample-plan.toml")
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        item["model_calls"] = 7
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(
            ResultError, "model_calls=7.*above declared limit 6"
        ):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_plan_validation_enforces_per_call_context_budget(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = load_experiment(root / "examples/results/sample-plan.toml")
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        item["prompt_tokens"] = 16385
        item["completion_tokens"] = 0
        item["max_call_context_tokens"] = 16385
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(
            ResultError, "max_call_context_tokens=16385.*above declared limit 16384"
        ):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_plan_validation_requires_context_telemetry(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = load_experiment(root / "examples/results/sample-plan.toml")
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        del item["max_call_context_tokens"]
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(ResultError, "max_call_context_tokens.*required"):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_plan_validation_rejects_inconsistent_context_telemetry(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = load_experiment(root / "examples/results/sample-plan.toml")
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        item["prompt_tokens"] = 17000
        item["completion_tokens"] = 100
        item["model_calls"] = 1
        item["max_call_context_tokens"] = 1
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(
            ResultError, "token total.*inconsistent.*context telemetry"
        ):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_plan_validation_rejects_tokens_without_model_call(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = load_experiment(root / "examples/results/sample-plan.toml")
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        item["model_calls"] = 0
        item["max_call_context_tokens"] = 0
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(ResultError, "zero model_calls.*zero token totals"):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_deployment_validation_enforces_wall_budget(self) -> None:
        plan = self.load_deployment_plan()
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        item["wall_seconds"] = 100.1
        item["energy_joules"] = 400.0
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(
            ResultError, "wall_seconds=100.1.*above declared limit 100"
        ):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_deployment_energy_budget_requires_energy_measurement(self) -> None:
        plan = self.load_deployment_plan()
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        item["wall_seconds"] = 90.0
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(ResultError, "energy_joules.*required"):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_deployment_validation_enforces_energy_budget(self) -> None:
        plan = self.load_deployment_plan()
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        item["wall_seconds"] = 90.0
        item["energy_joules"] = 500.1
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(
            ResultError, "energy_joules=500.1.*above declared limit 500"
        ):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_plan_validation_can_report_explicit_partial_coverage(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = load_experiment(root / "examples/results/sample-plan.toml")
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        bind_to_plan(item, plan)

        records = load_results(self.write_records([item]))
        coverage = validate_results_for_plan(records, plan, require_complete=False)

        self.assertEqual(coverage.expected_runs, 6)
        self.assertEqual(coverage.observed_runs, 1)
        self.assertEqual(coverage.missing_runs, 5)

    def test_plan_validation_rejects_wrong_manifest_digest(self) -> None:
        root = Path(__file__).resolve().parents[1]
        plan = load_experiment(root / "examples/results/sample-plan.toml")
        item = record("task-001", "direct", False, 1000)
        item["experiment_id"] = "demo"
        item["seed"] = 11
        item["manifest_sha256"] = "sha256:" + "0" * 64

        records = load_results(self.write_records([item]))
        with self.assertRaisesRegex(ResultError, "manifest_sha256.*does not match"):
            validate_results_for_plan(records, plan, require_complete=False)

    def test_energy_mean_reports_measurement_coverage(self) -> None:
        first = record("a", "direct", True, 1000)
        first["energy_joules"] = 10.0
        second = record("b", "direct", False, 1000)

        arm = summarize(load_results(self.write_records([first, second]))).arms[0]

        self.assertEqual(arm.energy_observations, 1)
        self.assertEqual(arm.mean_energy_joules, 10.0)

    def test_budget_exhaustion_is_a_valid_objective_failure(self) -> None:
        item = record("a", "direct", False, 1000)
        item["run_status"] = "budget_exhausted"
        item["failure_reason"] = "completion token cap"

        arm = summarize(load_results(self.write_records([item]))).arms[0]

        self.assertEqual(arm.run_count, 1)
        self.assertEqual(arm.budget_exhausted_runs, 1)
        self.assertEqual(arm.invalid_runs, 0)

    def test_float_summaries_are_independent_of_jsonl_order(self) -> None:
        records = [record(str(index), "direct", False, 1000) for index in range(4)]
        for item, wall in zip(records, (1e16, 1.0, 1.0, 1.0), strict=True):
            item["wall_seconds"] = wall

        forward = summarize(load_results(self.write_records(records)))
        reverse = summarize(load_results(self.write_records(list(reversed(records)))))

        self.assertEqual(asdict(forward), asdict(reverse))

    def test_rejects_float_aggregate_overflow(self) -> None:
        records = [
            record("a", "direct", False, 1000),
            record("b", "direct", False, 1000),
        ]
        for item in records:
            item["wall_seconds"] = 1e308

        with self.assertRaisesRegex(
            ResultError, "wall_seconds aggregate.*finite range"
        ):
            summarize(load_results(self.write_records(records)))

    def test_rejects_integer_aggregate_overflow(self) -> None:
        item = record("a", "direct", False, 1000)
        item["prompt_tokens"] = 10**400

        with self.assertRaisesRegex(
            ResultError, "total_tokens aggregate.*finite range"
        ):
            summarize(load_results(self.write_records([item])))


if __name__ == "__main__":
    unittest.main()
