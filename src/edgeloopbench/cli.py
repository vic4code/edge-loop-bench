"""Command-line interface for EdgeLoopBench planning and analysis."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .config import ValidationError, load_experiment
from .doctor import collect_host_info
from .results import (
    ResultError,
    load_results,
    render_text,
    summarize,
    validate_results_for_plan,
)


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

    doctor = subparsers.add_parser(
        "doctor", help="inspect the local host without changing it"
    )
    doctor.add_argument("--json", action="store_true", dest="as_json")
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
        if arguments.command == "doctor":
            payload = collect_host_info()
            if arguments.as_json:
                _print_json(payload)
            else:
                print(_render_doctor(payload))
            return 0
    except (ValidationError, ResultError) as error:
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
