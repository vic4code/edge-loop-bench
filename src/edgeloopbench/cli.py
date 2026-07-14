"""Command-line interface for EdgeLoopBench planning and analysis."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .config import ValidationError, load_experiment
from .doctor import collect_host_info
from .experiment import (
    ExperimentError,
    build_isolated_evaluator,
    build_ollama_model,
    execute_plan,
)
from .ollama import OllamaError
from .results import (
    ResultError,
    load_results,
    render_text,
    summarize,
    validate_results_for_plan,
)
from .report import ComparisonError, render_model_comparison, render_report
from .runner import run_public_tests
from .tasks import TaskManifestError, load_task_manifest, prepare_task


class ArgumentParsingError(ValueError):
    """Raised instead of exiting when command-line syntax is invalid."""


class EdgeLoopArgumentParser(argparse.ArgumentParser):
    """Argument parser whose errors can use the CLI's JSON envelope."""

    def error(self, message: str) -> None:
        raise ArgumentParsingError(message)


def build_parser() -> EdgeLoopArgumentParser:
    parser = EdgeLoopArgumentParser(
        prog="edgeloop",
        description="Validate EdgeLoopBench plans and summarize append-only results.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate", help="validate an experiment TOML manifest"
    )
    validate.add_argument("manifest")
    validate.add_argument("--json", action="store_true", dest="as_json")

    summary = subparsers.add_parser(
        "summarize", help="summarize a run-result JSONL file"
    )
    summary.add_argument("results")
    summary.add_argument(
        "--manifest", required=True, help="declared experiment TOML manifest"
    )
    summary.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="report partial coverage instead of rejecting missing declared runs",
    )
    summary.add_argument("--json", action="store_true", dest="as_json")

    report = subparsers.add_parser(
        "report", help="render a self-contained effectiveness report"
    )
    report.add_argument("results")
    report.add_argument("--manifest", required=True)
    report.add_argument("--output", required=True)
    report.add_argument(
        "--allow-incomplete", action="store_true",
        help="render partial coverage instead of rejecting missing declared runs",
    )
    report.add_argument("--json", action="store_true", dest="as_json")

    compare = subparsers.add_parser(
        "compare", help="render a paired cross-model loop comparison"
    )
    compare.add_argument(
        "--experiment",
        action="append",
        nargs=2,
        required=True,
        metavar=("MANIFEST", "RESULTS"),
    )
    compare.add_argument("--output", required=True)
    compare.add_argument("--json", action="store_true", dest="as_json")

    run = subparsers.add_parser("run", help="execute missing agent runs from a manifest")
    run.add_argument("manifest")
    run.add_argument("--results", required=True)
    run.add_argument("--events")
    run.add_argument("--task-catalog", default="tasks/micro")
    run.add_argument("--evaluator-catalog", default="evaluators")
    run.add_argument("--work-root", default="results/work")
    run.add_argument("--max-runs", type=int)
    run.add_argument("--json", action="store_true", dest="as_json")

    doctor = subparsers.add_parser(
        "doctor", help="inspect the local host without changing it"
    )
    doctor.add_argument("--json", action="store_true", dest="as_json")

    task = subparsers.add_parser("task", help="prepare and test public task worktrees")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_prepare = task_commands.add_parser("prepare", help="prepare a pinned task worktree")
    task_prepare.add_argument("task_id")
    task_prepare.add_argument("--work-root", required=True)
    task_prepare.add_argument("--catalog-root", default="tasks/micro")
    task_prepare.add_argument("--json", action="store_true", dest="as_json")
    task_test = task_commands.add_parser("public-test", help="run a worktree's public tests")
    task_test.add_argument("worktree")
    task_test.add_argument("--catalog-root", default="tasks/micro")
    task_test.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw_arguments
    parser = build_parser()
    try:
        arguments = parser.parse_args(raw_arguments)
    except ArgumentParsingError as error:
        if json_requested:
            print(
                json.dumps({"error": str(error), "exit_code": 2}, sort_keys=True),
                file=sys.stderr,
            )
        else:
            parser.print_usage(sys.stderr)
            print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 2
    try:
        if arguments.command == "validate":
            plan = load_experiment(arguments.manifest)
            payload = plan.summary()
            if arguments.as_json:
                _print_json(payload)
            else:
                status = "draft" if plan.draft else "publishable"
                print(
                    f"Valid {plan.track} plan {plan.id!r} ({status}); "
                    f"{plan.run_count} measured runs planned."
                )
            return 0
        if arguments.command == "summarize":
            plan = load_experiment(arguments.manifest)
            records = load_results(arguments.results)
            coverage = validate_results_for_plan(
                records,
                plan,
                require_complete=not arguments.allow_incomplete,
            )
            report = summarize(records, plan, coverage)
            if arguments.as_json:
                _print_json(report.to_dict())
            else:
                print(render_text(report))
            return 0
        if arguments.command == "report":
            plan = load_experiment(arguments.manifest)
            records = load_results(arguments.results)
            coverage = validate_results_for_plan(
                records, plan, require_complete=not arguments.allow_incomplete
            )
            summary_report = summarize(records, plan, coverage)
            render_report(summary_report, plan, records, arguments.output)
            payload = {"index": "index.html", "data": "report.json"}
            if arguments.as_json:
                _print_json(payload)
            else:
                print(f"Rendered report to {Path(arguments.output) / 'index.html'}")
            return 0
        if arguments.command == "compare":
            experiments = []
            for manifest_path, results_path in arguments.experiment:
                plan = load_experiment(manifest_path)
                records = load_results(results_path)
                coverage = validate_results_for_plan(records, plan)
                summary_report = summarize(records, plan, coverage)
                experiments.append((plan, summary_report, records))
            render_model_comparison(experiments, arguments.output)
            payload = {"index": "index.html", "data": "comparison.json"}
            if arguments.as_json:
                _print_json(payload)
            else:
                print(
                    f"Rendered model comparison to "
                    f"{Path(arguments.output) / 'index.html'}"
                )
            return 0
        if arguments.command == "run":
            plan = load_experiment(arguments.manifest)
            model = build_ollama_model(plan)
            evaluator = build_isolated_evaluator(arguments.evaluator_catalog)
            event_log = arguments.events or str(
                Path(arguments.results).with_suffix(".events.jsonl")
            )
            execution = execute_plan(
                plan,
                arguments.task_catalog,
                arguments.work_root,
                event_log,
                arguments.results,
                model=model,
                evaluate=evaluator,
                max_runs=arguments.max_runs,
            )
            payload = asdict(execution)
            if arguments.as_json:
                _print_json(payload)
            else:
                print(
                    f"Executed {execution.executed_runs} runs; "
                    f"skipped {execution.skipped_runs} existing runs."
                )
            return 0
        if arguments.command == "doctor":
            payload = collect_host_info()
            if arguments.as_json:
                _print_json(payload)
            else:
                print(_render_doctor(payload))
            return 0
        if arguments.command == "task" and arguments.task_command == "prepare":
            task_root = Path(arguments.catalog_root) / arguments.task_id
            task_manifest = prepare_task(task_root, arguments.work_root)
            payload = {
                "task_id": task_manifest.id,
                "initial_commit": task_manifest.initial_commit,
            }
            if arguments.as_json:
                _print_json(payload)
            else:
                print(f"Prepared {task_manifest.id} at commit {task_manifest.initial_commit}.")
            return 0
        if arguments.command == "task" and arguments.task_command == "public-test":
            worktree = Path(arguments.worktree)
            task_manifest = _task_for_worktree(worktree, Path(arguments.catalog_root))
            result = run_public_tests(worktree, task_manifest)
            payload = {
                "task_id": task_manifest.id,
                "passed": result.passed,
                "returncode": result.returncode,
                "output": result.output,
            }
            if arguments.as_json:
                _print_json(payload)
            else:
                print(result.output, end="" if result.output.endswith("\n") else "\n")
            return 0 if result.passed else 1
    except (
        ValidationError,
        ResultError,
        TaskManifestError,
        OllamaError,
        ExperimentError,
        ComparisonError,
    ) as error:
        if getattr(arguments, "as_json", False):
            print(
                json.dumps({"error": str(error), "exit_code": 2}, sort_keys=True),
                file=sys.stderr,
            )
        else:
            print(f"error: {error}", file=sys.stderr)
        return 2
    parser.error(f"unknown command: {arguments.command}")
    return 2


def _task_for_worktree(worktree: Path, catalog_root: Path):
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TaskManifestError("worktree does not have a readable Git commit") from error
    commit = completed.stdout.strip()
    matches = []
    for manifest_path in sorted(catalog_root.glob("*/task.toml")):
        manifest = load_task_manifest(manifest_path)
        if manifest.initial_commit == commit:
            matches.append(manifest)
    if len(matches) != 1:
        raise TaskManifestError(
            f"worktree commit does not identify exactly one task in {catalog_root}"
        )
    return matches[0]


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _render_doctor(payload: dict[str, object]) -> str:
    lines = [
        f"Platform: {payload['platform']} {payload['platform_release']} ({payload['machine']})",
        f"Python: {payload['python_version']} at {payload['python_executable']}",
        f"Chip: {payload.get('chip') or 'unavailable'}",
        f"Unified/host memory bytes: {payload.get('memory_bytes') or 'unavailable'}",
        "Runtime executables:",
    ]
    runtimes = payload["runtimes"]
    assert isinstance(runtimes, dict)
    for name, raw in sorted(runtimes.items()):
        assert isinstance(raw, dict)
        lines.append(f"- {name}: {raw.get('path') or 'not found'}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
