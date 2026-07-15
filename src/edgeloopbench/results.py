"""Append-only result loading and deterministic descriptive summaries."""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import MAX_PLANNED_RUNS, ExperimentPlan


STRATEGY_ORDER = {"direct": 0, "bounded_retry": 1, "maker_verifier": 2}
RUN_STATUSES = frozenset(
    {"completed", "budget_exhausted", "timeout", "infrastructure_error", "invalid"}
)
INVALID_STATUSES = frozenset({"infrastructure_error", "invalid"})
MAX_RESULT_FILE_BYTES = 128 * 1024 * 1024
MAX_RESULT_LINE_CHARACTERS = 2 * 1024 * 1024
MAX_RESULT_RECORDS = MAX_PLANNED_RUNS
SHA256_REFERENCE_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$", re.IGNORECASE)


class ResultError(ValueError):
    """Raised when raw result records are malformed or ambiguous."""


@dataclass(frozen=True)
class RunRecord:
    experiment_id: str
    task_id: str
    strategy: str
    budget_tier: str
    seed: int
    manifest_sha256: str | None
    run_status: str
    objective_success: bool
    prompt_tokens: int
    completion_tokens: int
    model_calls: int
    tool_calls: int
    public_test_runs: int
    max_call_context_tokens: int | None
    wall_seconds: float
    energy_joules: float | None = None
    failure_reason: str | None = None
    verifier_verdict: str | None = None
    verifier_protocol_error: bool = False
    fallback_used: bool = False
    candidate_a_success: bool | None = None
    candidate_b_success: bool | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def key(self) -> tuple[str, str, str, str, int]:
        return (
            self.experiment_id,
            self.task_id,
            self.strategy,
            self.budget_tier,
            self.seed,
        )

    @property
    def pair_key(self) -> tuple[str, str, str, int]:
        return (self.experiment_id, self.task_id, self.budget_tier, self.seed)

    @property
    def is_valid(self) -> bool:
        return self.run_status not in INVALID_STATUSES


@dataclass(frozen=True)
class ArmSummary:
    experiment_id: str
    strategy: str
    budget_tier: str
    expected_runs: int | None
    observed_runs: int
    run_count: int
    invalid_runs: int
    missing_runs: int | None
    budget_exhausted_runs: int
    timeout_runs: int
    successes: int
    success_rate: float | None
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    mean_total_tokens: float | None
    success_per_1k_tokens: float | None
    mean_wall_seconds: float | None
    energy_observations: int
    mean_energy_joules: float | None


@dataclass(frozen=True)
class PairSummary:
    experiment_id: str
    budget_tier: str
    baseline_strategy: str
    candidate_strategy: str
    pair_count: int
    success_delta_pp: float
    mean_total_token_delta: float
    mean_wall_delta_seconds: float


@dataclass(frozen=True)
class PlanCoverage:
    expected_runs: int
    observed_runs: int
    missing_runs: int
    invalid_runs: int


@dataclass(frozen=True)
class ManifestBinding:
    experiment_id: str
    manifest_sha256: str | None


@dataclass(frozen=True)
class SummaryReport:
    manifest_bindings: tuple[ManifestBinding, ...]
    arms: tuple[ArmSummary, ...]
    pairs: tuple[PairSummary, ...]
    coverage: PlanCoverage | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "manifest_bindings": [asdict(item) for item in self.manifest_bindings],
            "arms": [asdict(item) for item in self.arms],
            "pairs": [asdict(item) for item in self.pairs],
        }
        if self.coverage is not None:
            payload["coverage"] = asdict(self.coverage)
        return payload


def load_results(path: str | Path) -> tuple[RunRecord, ...]:
    """Load result objects from JSONL while rejecting duplicate run identities."""

    source = Path(path)
    records: list[RunRecord] = []
    seen: set[tuple[str, str, str, str, int]] = set()
    try:
        if source.stat().st_size > MAX_RESULT_FILE_BYTES:
            raise ResultError(
                f"{source}: file exceeds the {MAX_RESULT_FILE_BYTES}-byte safety limit"
            )
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                if len(line) > MAX_RESULT_LINE_CHARACTERS:
                    raise ResultError(
                        f"{source}: line {line_number}: exceeds the "
                        f"{MAX_RESULT_LINE_CHARACTERS}-character safety limit"
                    )
                if len(records) >= MAX_RESULT_RECORDS:
                    raise ResultError(
                        f"{source}: exceeds the {MAX_RESULT_RECORDS}-record safety limit"
                    )
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ResultError(
                        f"{source}: line {line_number}: invalid JSON: {error.msg}"
                    ) from error
                except ValueError as error:
                    raise ResultError(
                        f"{source}: line {line_number}: invalid JSON number: {error}"
                    ) from error
                except RecursionError as error:
                    raise ResultError(
                        f"{source}: line {line_number}: JSON nesting is too deep"
                    ) from error
                if not isinstance(raw, Mapping):
                    raise ResultError(
                        f"{source}: line {line_number}: record must be a JSON object"
                    )
                record = _parse_record(raw, f"{source}: line {line_number}")
                if record.key in seen:
                    raise ResultError(
                        f"{source}: line {line_number}: duplicate run key {record.key!r}"
                    )
                seen.add(record.key)
                records.append(record)
    except FileNotFoundError as error:
        raise ResultError(f"result file not found: {source}") from error
    except UnicodeDecodeError as error:
        raise ResultError(f"result file is not valid UTF-8: {source}") from error
    except OSError as error:
        raise ResultError(f"cannot read result file {source}: {error}") from error
    if not records:
        raise ResultError(f"{source}: contains no result records")
    return tuple(records)


def validate_results_for_plan(
    records: Iterable[RunRecord],
    plan: ExperimentPlan,
    *,
    require_complete: bool = True,
) -> PlanCoverage:
    """Reject undeclared run identities and make missing-run handling explicit."""

    if plan.track == "serving":
        raise ResultError(
            "agent result summaries require an effectiveness or deployment manifest"
        )
    if plan.run_count > MAX_RESULT_RECORDS:
        raise ResultError(
            f"manifest planned run count {plan.run_count} exceeds safety limit "
            f"{MAX_RESULT_RECORDS}"
        )
    items = tuple(records)
    if plan.manifest_sha256 is None:
        raise ResultError(
            "experiment plan has no source digest; load it from a TOML file"
        )
    expected_manifest = f"sha256:{plan.manifest_sha256}"
    for item in items:
        if item.manifest_sha256 != expected_manifest:
            raise ResultError(
                f"run {item.key!r} manifest_sha256 {item.manifest_sha256!r} "
                f"does not match {expected_manifest!r}"
            )
    expected = _expected_run_keys(plan)
    observed = {item.key for item in items}
    undeclared = sorted(observed - expected)
    if undeclared:
        raise ResultError(
            f"result key {undeclared[0]!r} is not declared by manifest {plan.id!r}"
        )
    for item in items:
        budget = (plan.budgets or {})[item.budget_tier]
        if item.max_call_context_tokens is None:
            raise ResultError(
                f"run {item.key!r} max_call_context_tokens is required for budget validation"
            )
        total_tokens = item.total_tokens
        if item.model_calls == 0:
            if total_tokens != 0 or item.max_call_context_tokens != 0:
                raise ResultError(
                    f"run {item.key!r} with zero model_calls must report zero token totals "
                    "and zero max_call_context_tokens"
                )
        elif not (
            item.max_call_context_tokens
            <= total_tokens
            <= item.model_calls * item.max_call_context_tokens
        ):
            raise ResultError(
                f"run {item.key!r} token total {total_tokens} is inconsistent with context "
                f"telemetry: {item.model_calls} model calls and "
                f"max_call_context_tokens={item.max_call_context_tokens}"
            )
        counters = {
            "prompt_tokens": (item.prompt_tokens, budget.prompt_tokens),
            "completion_tokens": (item.completion_tokens, budget.completion_tokens),
            "model_calls": (item.model_calls, budget.model_calls),
            "tool_calls": (item.tool_calls, budget.tool_calls),
            "public_test_runs": (item.public_test_runs, budget.public_test_runs),
            "max_call_context_tokens": (
                item.max_call_context_tokens,
                budget.per_call_context_tokens,
            ),
        }
        for name, (actual, limit) in counters.items():
            if actual > limit:
                raise ResultError(
                    f"run {item.key!r} reports {name}={actual}, above declared limit {limit}"
                )
        if plan.track == "deployment":
            physical_budget = plan.physical_budget or {}
            max_wall_seconds = physical_budget.get("max_wall_seconds")
            if max_wall_seconds is not None and item.wall_seconds > max_wall_seconds:
                raise ResultError(
                    f"run {item.key!r} reports wall_seconds={item.wall_seconds:g}, "
                    f"above declared limit {max_wall_seconds:g}"
                )
            max_energy_joules = physical_budget.get("max_energy_joules")
            if max_energy_joules is not None:
                if item.energy_joules is None:
                    raise ResultError(
                        f"run {item.key!r} energy_joules is required by the deployment budget"
                    )
                if item.energy_joules > max_energy_joules:
                    raise ResultError(
                        f"run {item.key!r} reports energy_joules={item.energy_joules:g}, "
                        f"above declared limit {max_energy_joules:g}"
                    )
    missing = expected - observed
    if require_complete and missing:
        raise ResultError(
            f"result set is missing {len(missing)} declared runs from manifest {plan.id!r}; "
            "use --allow-incomplete only for explicitly partial analysis"
        )
    return PlanCoverage(
        expected_runs=len(expected),
        observed_runs=len(observed),
        missing_runs=len(missing),
        invalid_runs=sum(not item.is_valid for item in items),
    )


def summarize(
    records: Iterable[RunRecord],
    plan: ExperimentPlan | None = None,
    coverage: PlanCoverage | None = None,
) -> SummaryReport:
    """Aggregate per-arm summaries and complete within-task strategy pairs."""

    items = tuple(records)
    if not items:
        raise ResultError("cannot summarize an empty result collection")

    manifest_bindings = _collect_manifest_bindings(items)

    if plan is not None:
        validated_coverage = validate_results_for_plan(
            items, plan, require_complete=False
        )
        if coverage is not None and coverage != validated_coverage:
            raise ResultError(
                "provided plan coverage does not match the result records"
            )
        coverage = validated_coverage

    groups: dict[tuple[str, str, str], list[RunRecord]] = {}
    for item in items:
        groups.setdefault(
            (item.experiment_id, item.strategy, item.budget_tier), []
        ).append(item)
    arm_keys = set(groups)
    if plan is not None:
        arm_keys.update(
            (plan.id, strategy, budget_tier)
            for budget_tier in (plan.budgets or {})
            for strategy in plan.strategies
        )
    arms = tuple(
        _summarize_arm(
            key,
            groups.get(key, []),
            expected_runs=(
                len(plan.tasks) * len(plan.seeds) if plan is not None else None
            ),
        )
        for key in sorted(
            arm_keys, key=lambda key: (key[0], key[2], _strategy_sort_key(key[1]))
        )
    )

    by_experiment_budget: dict[tuple[str, str], list[RunRecord]] = {}
    for item in items:
        by_experiment_budget.setdefault(
            (item.experiment_id, item.budget_tier), []
        ).append(item)
    pairs: list[PairSummary] = []
    for (experiment_id, budget_tier), group in sorted(by_experiment_budget.items()):
        strategies = sorted({item.strategy for item in group}, key=_strategy_sort_key)
        for baseline, candidate in itertools.combinations(strategies, 2):
            pair = _summarize_pair(
                experiment_id, budget_tier, baseline, candidate, group
            )
            if pair is not None:
                pairs.append(pair)
    return SummaryReport(
        manifest_bindings=manifest_bindings,
        arms=arms,
        pairs=tuple(pairs),
        coverage=coverage,
    )


def render_text(report: SummaryReport) -> str:
    """Render a compact deterministic terminal report."""

    lines = ["Manifest bindings"]
    for binding in report.manifest_bindings:
        digest = binding.manifest_sha256 or "unbound exploratory data"
        lines.append(f"- {binding.experiment_id}: {digest}")
    lines.append("Agent effectiveness arms")
    for arm in report.arms:
        success = f"{arm.success_rate:.1%}" if arm.success_rate is not None else "n/a"
        mean_tokens = (
            f"{arm.mean_total_tokens:.0f}"
            if arm.mean_total_tokens is not None
            else "n/a"
        )
        mean_wall = (
            f"{arm.mean_wall_seconds:.1f}s"
            if arm.mean_wall_seconds is not None
            else "n/a"
        )
        lines.append(
            f"- {arm.experiment_id}/{arm.budget_tier}/{arm.strategy}: "
            f"{arm.successes}/{arm.run_count} valid success ({success}), "
            f"{arm.observed_runs} observed, {arm.invalid_runs} invalid, "
            f"mean {mean_tokens} tokens, {mean_wall}"
        )
    if report.coverage is not None:
        lines.append(
            "Coverage: "
            f"{report.coverage.observed_runs}/{report.coverage.expected_runs} observed, "
            f"{report.coverage.missing_runs} missing, {report.coverage.invalid_runs} invalid"
        )
    lines.append("Paired strategy deltas (candidate - baseline)")
    if not report.pairs:
        lines.append("- no complete strategy pairs")
    for pair in report.pairs:
        lines.append(
            f"- {pair.experiment_id}/{pair.budget_tier}: {pair.candidate_strategy} vs "
            f"{pair.baseline_strategy}, n={pair.pair_count}, success {pair.success_delta_pp:+.1f} pp, "
            f"tokens {pair.mean_total_token_delta:+.0f}, wall {pair.mean_wall_delta_seconds:+.1f}s"
        )
    return "\n".join(lines)


def _parse_record(raw: Mapping[str, Any], source: str) -> RunRecord:
    success = raw.get("objective_success")
    if not isinstance(success, bool):
        raise ResultError(f"{source}: objective_success must be a boolean")
    run_status = _nonempty_string(raw, "run_status", source)
    if run_status not in RUN_STATUSES:
        raise ResultError(f"{source}: run_status must be one of {sorted(RUN_STATUSES)}")
    if run_status != "completed" and success:
        raise ResultError(f"{source}: {run_status} run cannot claim objective success")
    failure_reason = None
    if "failure_reason" in raw:
        failure_reason = _nonempty_string(raw, "failure_reason", source)
    if run_status != "completed" and failure_reason is None:
        raise ResultError(f"{source}: {run_status} run must include failure_reason")
    manifest_sha256 = None
    if "manifest_sha256" in raw:
        manifest_sha256 = _nonempty_string(raw, "manifest_sha256", source)
        if not SHA256_REFERENCE_PATTERN.fullmatch(manifest_sha256):
            raise ResultError(f"{source}: manifest_sha256 must be a SHA-256 reference")
    verifier_verdict = None
    if "verifier_verdict" in raw:
        verifier_verdict = _nonempty_string(raw, "verifier_verdict", source)
        if verifier_verdict not in {"APPROVE", "REJECT", "ESCALATE"}:
            raise ResultError(f"{source}: verifier_verdict is invalid")
    return RunRecord(
        experiment_id=_nonempty_string(raw, "experiment_id", source),
        task_id=_nonempty_string(raw, "task_id", source),
        strategy=_nonempty_string(raw, "strategy", source),
        budget_tier=_nonempty_string(raw, "budget_tier", source),
        seed=_nonnegative_integer(raw, "seed", source),
        manifest_sha256=manifest_sha256,
        run_status=run_status,
        objective_success=success,
        prompt_tokens=_nonnegative_integer(raw, "prompt_tokens", source),
        completion_tokens=_nonnegative_integer(raw, "completion_tokens", source),
        model_calls=_nonnegative_integer(raw, "model_calls", source),
        tool_calls=_nonnegative_integer(raw, "tool_calls", source),
        public_test_runs=_nonnegative_integer(raw, "public_test_runs", source),
        max_call_context_tokens=(
            _nonnegative_integer(raw, "max_call_context_tokens", source)
            if "max_call_context_tokens" in raw
            else None
        ),
        wall_seconds=_nonnegative_number(raw, "wall_seconds", source),
        energy_joules=(
            _nonnegative_number(raw, "energy_joules", source)
            if "energy_joules" in raw
            else None
        ),
        failure_reason=failure_reason,
        verifier_verdict=verifier_verdict,
        verifier_protocol_error=_optional_boolean(
            raw, "verifier_protocol_error", source, default=False
        ),
        fallback_used=_optional_boolean(raw, "fallback_used", source, default=False),
        candidate_a_success=_optional_boolean(raw, "candidate_a_success", source),
        candidate_b_success=_optional_boolean(raw, "candidate_b_success", source),
    )


def _summarize_arm(
    key: tuple[str, str, str],
    records: list[RunRecord],
    *,
    expected_runs: int | None,
) -> ArmSummary:
    experiment_id, strategy, budget_tier = key
    valid = [item for item in records if item.is_valid]
    successes = sum(item.objective_success for item in valid)
    prompt_tokens = sum(item.prompt_tokens for item in valid)
    completion_tokens = sum(item.completion_tokens for item in valid)
    total_tokens = prompt_tokens + completion_tokens
    energies = [item.energy_joules for item in valid if item.energy_joules is not None]
    valid_count = len(valid)
    return ArmSummary(
        experiment_id=experiment_id,
        strategy=strategy,
        budget_tier=budget_tier,
        expected_runs=expected_runs,
        observed_runs=len(records),
        run_count=valid_count,
        invalid_runs=len(records) - valid_count,
        missing_runs=(
            expected_runs - len(records) if expected_runs is not None else None
        ),
        budget_exhausted_runs=sum(
            item.run_status == "budget_exhausted" for item in valid
        ),
        timeout_runs=sum(item.run_status == "timeout" for item in valid),
        successes=successes,
        success_rate=(successes / valid_count if valid_count else None),
        total_prompt_tokens=prompt_tokens,
        total_completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        mean_total_tokens=(
            _finite_integer_mean(total_tokens, valid_count, "total_tokens aggregate")
            if valid_count
            else None
        ),
        success_per_1k_tokens=(
            successes * 1000 / total_tokens if total_tokens else None
        ),
        mean_wall_seconds=(
            _finite_float_mean(
                (item.wall_seconds for item in valid),
                valid_count,
                "wall_seconds aggregate",
            )
            if valid_count
            else None
        ),
        energy_observations=len(energies),
        mean_energy_joules=(
            _finite_float_mean(energies, len(energies), "energy_joules aggregate")
            if energies
            else None
        ),
    )


def _summarize_pair(
    experiment_id: str,
    budget_tier: str,
    baseline: str,
    candidate: str,
    records: list[RunRecord],
) -> PairSummary | None:
    baseline_by_unit = {
        item.pair_key: item
        for item in records
        if item.strategy == baseline and item.is_valid
    }
    candidate_by_unit = {
        item.pair_key: item
        for item in records
        if item.strategy == candidate and item.is_valid
    }
    shared = sorted(baseline_by_unit.keys() & candidate_by_unit.keys())
    if not shared:
        return None
    success_deltas = [
        int(candidate_by_unit[key].objective_success)
        - int(baseline_by_unit[key].objective_success)
        for key in shared
    ]
    token_deltas = [
        candidate_by_unit[key].total_tokens - baseline_by_unit[key].total_tokens
        for key in shared
    ]
    wall_deltas = [
        candidate_by_unit[key].wall_seconds - baseline_by_unit[key].wall_seconds
        for key in shared
    ]
    count = len(shared)
    return PairSummary(
        experiment_id=experiment_id,
        budget_tier=budget_tier,
        baseline_strategy=baseline,
        candidate_strategy=candidate,
        pair_count=count,
        success_delta_pp=100 * sum(success_deltas) / count,
        mean_total_token_delta=_finite_integer_mean(
            sum(token_deltas), count, "token delta aggregate"
        ),
        mean_wall_delta_seconds=_finite_float_mean(
            wall_deltas, count, "wall delta aggregate"
        ),
    )


def _strategy_sort_key(strategy: str) -> tuple[int, str]:
    return (STRATEGY_ORDER.get(strategy, len(STRATEGY_ORDER)), strategy)


def _expected_run_keys(plan: ExperimentPlan) -> set[tuple[str, str, str, str, int]]:
    return {
        (plan.id, task, strategy, budget_tier, seed)
        for task in plan.tasks
        for strategy in plan.strategies
        for budget_tier in (plan.budgets or {})
        for seed in plan.seeds
    }


def _collect_manifest_bindings(
    records: Iterable[RunRecord],
) -> tuple[ManifestBinding, ...]:
    by_experiment: dict[str, set[str | None]] = {}
    for item in records:
        by_experiment.setdefault(item.experiment_id, set()).add(item.manifest_sha256)
    bindings: list[ManifestBinding] = []
    for experiment_id, digests in sorted(by_experiment.items()):
        if len(digests) != 1:
            raise ResultError(
                f"experiment {experiment_id!r} has multiple manifest bindings; "
                "summarize each manifest separately"
            )
        bindings.append(
            ManifestBinding(
                experiment_id=experiment_id,
                manifest_sha256=next(iter(digests)),
            )
        )
    return tuple(bindings)


def _nonempty_string(raw: Mapping[str, Any], key: str, source: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResultError(f"{source}: {key} must be a non-empty string")
    return value.strip()


def _nonnegative_integer(raw: Mapping[str, Any], key: str, source: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResultError(f"{source}: {key} must be an integer")
    if value < 0:
        raise ResultError(f"{source}: {key} must be non-negative")
    return value


def _nonnegative_number(raw: Mapping[str, Any], key: str, source: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResultError(f"{source}: {key} must be a number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ResultError(f"{source}: {key} must be a finite number") from error
    if not math.isfinite(number):
        raise ResultError(f"{source}: {key} must be finite")
    if number < 0:
        raise ResultError(f"{source}: {key} must be non-negative")
    return number


def _optional_boolean(
    raw: Mapping[str, Any],
    key: str,
    source: str,
    *,
    default: bool | None = None,
) -> bool | None:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise ResultError(f"{source}: {key} must be a boolean")
    return value


def _finite_float_mean(values: Iterable[float], count: int, source: str) -> float:
    try:
        total = math.fsum(values)
    except OverflowError as error:
        raise ResultError(f"{source} exceeds the finite range") from error
    if not math.isfinite(total):
        raise ResultError(f"{source} exceeds the finite range")
    result = total / count
    if not math.isfinite(result):
        raise ResultError(f"{source} exceeds the finite range")
    return result


def _finite_integer_mean(total: int, count: int, source: str) -> float:
    try:
        result = total / count
    except OverflowError as error:
        raise ResultError(f"{source} exceeds the finite range") from error
    if not math.isfinite(result):
        raise ResultError(f"{source} exceeds the finite range")
    return result
