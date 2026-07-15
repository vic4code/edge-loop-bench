"""Manifest-driven experiment execution with append-only resume semantics."""

from __future__ import annotations

import tempfile
import os
import random
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .config import ExperimentPlan
from .controller import Evaluator, ModelCall, ModelOutput, ModelRequest, RunContext, run_strategy
from .ollama import OllamaClient, OllamaGenerateRequest
from .results import load_results, validate_results_for_plan
from .runner import append_event
from .tasks import TaskManifest, prepare_task


class ExperimentError(ValueError):
    """Raised when a local experiment cannot be executed reproducibly."""


@dataclass(frozen=True)
class ExecutionSummary:
    planned_runs: int
    executed_runs: int
    skipped_runs: int


@dataclass(frozen=True)
class RunSpec:
    budget_tier: str
    task_id: str
    strategy: str
    seed: int


def build_run_schedule(plan: ExperimentPlan) -> tuple[RunSpec, ...]:
    """Build a reproducible task-blocked schedule bound to the manifest digest."""

    if plan.manifest_sha256 is None:
        raise ExperimentError("plan must be loaded from a manifest file")
    blocks = [
        (budget_tier, task_id, seed)
        for budget_tier in (plan.budgets or {})
        for task_id in plan.tasks
        for seed in plan.seeds
    ]
    block_rng = random.Random(int(plan.manifest_sha256[:16], 16))
    block_rng.shuffle(blocks)
    schedule: list[RunSpec] = []
    for budget_tier, task_id, seed in blocks:
        strategies = list(plan.strategies)
        material = (
            f"{plan.manifest_sha256}|{budget_tier}|{task_id}|{seed}"
        ).encode("utf-8")
        strategy_rng = random.Random(
            int.from_bytes(sha256(material).digest()[:8], "big")
        )
        strategy_rng.shuffle(strategies)
        schedule.extend(
            RunSpec(budget_tier, task_id, strategy, seed)
            for strategy in strategies
        )
    return tuple(schedule)


def build_ollama_model(plan: ExperimentPlan) -> ModelCall:
    """Build the pinned Ollama model boundary declared by an agent plan."""

    if plan.backend.name != "ollama" or plan.generation is None:
        raise ExperimentError("this local runner requires an Ollama agent plan")
    if plan.generation.edit_schema_revision != "full-file-edits-v1":
        raise ExperimentError("unsupported edit schema revision")
    host = plan.backend.environment.get("OLLAMA_HOST")
    if host is None:
        raise ExperimentError("Ollama plan is missing OLLAMA_HOST")
    endpoint = host if host.startswith("http://") else f"http://{host}"
    client = OllamaClient(endpoint)

    def model(request: ModelRequest) -> ModelOutput:
        response = client.generate(
            OllamaGenerateRequest(
                model=plan.model.id,
                prompt=request.prompt,
                context_window=plan.model.context_limit_tokens,
                max_output_tokens=request.max_output_tokens,
                thinking=plan.generation.thinking,
                seed=request.seed,
                temperature=plan.generation.temperature,
                response_schema=dict(request.response_schema),
            )
        )
        return ModelOutput(
            response.text,
            response.thinking,
            response.prompt_tokens,
            response.completion_tokens,
            response.total_duration_ns,
        )

    return model


def build_isolated_evaluator(evaluator_catalog: str | Path) -> Evaluator:
    """Build an evaluator that never returns private output to the controller."""

    catalog = Path(evaluator_catalog).resolve()

    def evaluate(worktree: Path, task: TaskManifest) -> bool:
        if task.hidden_evaluation.evaluator_id != f"external-{task.id}":
            raise ExperimentError("task evaluator identifier does not match its task")
        tests = catalog / task.id / "tests"
        if not tests.is_dir():
            raise ExperimentError("isolated evaluator is unavailable")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("GIT_")
        }
        environment.update({"PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C", "TZ": "UTC"})
        completed = subprocess.run(
            ["python3", "-m", "unittest", "discover", "-s", str(tests), "-v"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=task.hidden_evaluation.timeout_seconds,
            env=environment,
        )
        return completed.returncode == 0

    return evaluate


def execute_plan(
    plan: ExperimentPlan,
    task_catalog: str | Path,
    work_root: str | Path,
    event_log: str | Path,
    result_log: str | Path,
    *,
    model: ModelCall,
    evaluate: Evaluator,
    max_runs: int | None = None,
) -> ExecutionSummary:
    """Execute missing declared runs in deterministic order and append results."""

    if plan.track not in {"effectiveness", "deployment"}:
        raise ExperimentError("only agent-track plans can be executed by this runner")
    if plan.manifest_sha256 is None:
        raise ExperimentError("plan must be loaded from a manifest file")
    if max_runs is not None and max_runs <= 0:
        raise ExperimentError("max_runs must be positive")

    result_path = Path(result_log)
    existing = ()
    if result_path.exists() and result_path.stat().st_size:
        existing = load_results(result_path)
        validate_results_for_plan(existing, plan, require_complete=False)
    completed_keys = {record.key for record in existing}
    catalog = Path(task_catalog)
    scratch = Path(work_root)
    scratch.mkdir(parents=True, exist_ok=True)
    manifest_reference = f"sha256:{plan.manifest_sha256}"
    executed = 0
    skipped = 0

    for run in build_run_schedule(plan):
        budget = (plan.budgets or {})[run.budget_tier]
        key = (plan.id, run.task_id, run.strategy, run.budget_tier, run.seed)
        if key in completed_keys:
            skipped += 1
            continue
        if max_runs is not None and executed >= max_runs:
            return ExecutionSummary(plan.run_count, executed, skipped)
        with tempfile.TemporaryDirectory(
            prefix="edgeloop-run-", dir=scratch
        ) as directory:
            worktree = Path(directory) / "worktree"
            task = prepare_task(catalog / run.task_id, worktree)
            result = run_strategy(
                run.strategy,
                worktree,
                task,
                model,
                budget,
                seed=run.seed,
                event_log=event_log,
                evaluate=evaluate,
                context=RunContext(
                    experiment_id=plan.id,
                    budget_tier=run.budget_tier,
                    manifest_sha256=manifest_reference,
                ),
            )
        record: dict[str, object] = {
                        "experiment_id": plan.id,
                        "task_id": run.task_id,
                        "strategy": run.strategy,
                        "budget_tier": run.budget_tier,
                        "seed": run.seed,
                        "manifest_sha256": manifest_reference,
                        "run_status": result.run_status,
                        "objective_success": result.objective_success,
                        "prompt_tokens": result.prompt_tokens,
                        "completion_tokens": result.completion_tokens,
                        "model_calls": result.model_calls,
                        "tool_calls": result.tool_calls,
                        "public_test_runs": result.public_test_runs,
                        "max_call_context_tokens": result.max_call_context_tokens,
                        "wall_seconds": result.wall_seconds,
        }
        if result.failure_reason is not None:
            record["failure_reason"] = result.failure_reason
        if result.verifier_verdict is not None:
            record["verifier_verdict"] = result.verifier_verdict
        if result.verifier_protocol_error:
            record["verifier_protocol_error"] = True
        if result.fallback_used:
            record["fallback_used"] = True
        if result.candidate_a_success is not None:
            record["candidate_a_success"] = result.candidate_a_success
        if result.candidate_b_success is not None:
            record["candidate_b_success"] = result.candidate_b_success
        append_event(result_path, record)
        completed_keys.add(key)
        executed += 1
    return ExecutionSummary(plan.run_count, executed, skipped)
