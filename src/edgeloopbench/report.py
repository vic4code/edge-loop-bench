"""Deterministic, self-contained HTML reporting for agent effectiveness."""

from __future__ import annotations

import html
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path

from .config import ExperimentPlan
from .results import ArmSummary, RunRecord, SummaryReport


class ComparisonError(ValueError):
    """Raised when experiments cannot support a causal model comparison."""


def render_report(
    report: SummaryReport,
    plan: ExperimentPlan,
    records: Iterable[RunRecord],
    output_directory: str | Path,
) -> Path:
    """Write deterministic standalone HTML and its machine-readable payload."""

    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    items = tuple(records)
    payload = {
        "schema_version": 1,
        "plan": plan.summary(),
        "summary": report.to_dict(),
        "records": [asdict(item) for item in items],
    }
    (output / "report.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    document = _document(report, plan, items)
    index = output / "index.html"
    index.write_text(document, encoding="utf-8")
    return index


def render_model_comparison(
    experiments: Iterable[
        tuple[ExperimentPlan, SummaryReport, Iterable[RunRecord]]
    ],
    output_directory: str | Path,
) -> Path:
    """Render compatible complete experiments as one paired loop comparison."""

    items = tuple(
        (plan, report, tuple(records))
        for plan, report, records in experiments
    )
    _validate_comparison(items)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "experiments": [
            {
                "plan": plan.summary(),
                "summary": report.to_dict(),
                "records": [asdict(record) for record in records],
                "transitions": _transition_rows(plan, records),
            }
            for plan, report, records in items
        ],
    }
    (output / "comparison.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    index = output / "index.html"
    index.write_text(_comparison_document(items), encoding="utf-8")
    return index


def _validate_comparison(
    items: tuple[
        tuple[ExperimentPlan, SummaryReport, tuple[RunRecord, ...]], ...
    ],
) -> None:
    if len(items) < 2:
        raise ComparisonError("cross-model comparison requires at least two experiments")
    baseline = items[0][0]
    if baseline.track != "effectiveness":
        raise ComparisonError("cross-model comparison requires effectiveness plans")
    if "direct" not in baseline.strategies:
        raise ComparisonError("cross-model comparison requires a direct baseline")
    experiment_ids: set[str] = set()
    model_artifacts: set[str] = set()
    for plan, report, records in items:
        if plan.id in experiment_ids:
            raise ComparisonError(f"duplicate experiment id {plan.id!r}")
        if plan.model.artifact_sha256 in model_artifacts:
            raise ComparisonError(
                f"duplicate model artifact {plan.model.artifact_sha256!r}"
            )
        experiment_ids.add(plan.id)
        model_artifacts.add(plan.model.artifact_sha256)
        if report.coverage is None or report.coverage.missing_runs:
            raise ComparisonError(f"experiment {plan.id!r} is incomplete")
        if report.coverage.invalid_runs:
            raise ComparisonError(f"experiment {plan.id!r} contains invalid runs")
        if len(records) != report.coverage.observed_runs:
            raise ComparisonError(f"experiment {plan.id!r} record count is inconsistent")
        if _comparison_signature(plan) != _comparison_signature(baseline):
            raise ComparisonError(
                f"experiment {plan.id!r} differs outside the pinned model artifact"
            )


def _comparison_signature(plan: ExperimentPlan) -> tuple[object, ...]:
    return (
        plan.track,
        plan.tasks,
        plan.strategies,
        plan.seeds,
        tuple((name, budget) for name, budget in (plan.budgets or {}).items()),
        plan.generation,
        plan.backend,
        plan.model.weight_quantization,
        plan.model.context_limit_tokens,
    )


def _transition_rows(
    plan: ExperimentPlan, records: tuple[RunRecord, ...]
) -> list[dict[str, object]]:
    by_key = {
        (record.task_id, record.budget_tier, record.seed, record.strategy): record
        for record in records
        if record.is_valid
    }
    rows: list[dict[str, object]] = []
    for budget in (plan.budgets or {}):
        for strategy in plan.strategies:
            if strategy == "direct":
                continue
            rescued = regressed = unchanged = 0
            for task in plan.tasks:
                for seed in plan.seeds:
                    direct = by_key[(task, budget, seed, "direct")]
                    candidate = by_key[(task, budget, seed, strategy)]
                    if not direct.objective_success and candidate.objective_success:
                        rescued += 1
                    elif direct.objective_success and not candidate.objective_success:
                        regressed += 1
                    else:
                        unchanged += 1
            rows.append(
                {
                    "budget_tier": budget,
                    "strategy": strategy,
                    "rescued": rescued,
                    "regressed": regressed,
                    "unchanged": unchanged,
                }
            )
    return rows


def _comparison_document(
    items: tuple[
        tuple[ExperimentPlan, SummaryReport, tuple[RunRecord, ...]], ...
    ],
) -> str:
    total_runs = sum(len(records) for _, _, records in items)
    models = " · ".join(_e(plan.model.id) for plan, _, _ in items)
    leaderboard_rows: list[str] = []
    transition_rows: list[str] = []
    for plan, report, records in items:
        direct_by_budget = {
            arm.budget_tier: arm
            for arm in report.arms
            if arm.strategy == "direct"
        }
        for arm in report.arms:
            rate = 100 * (arm.success_rate or 0)
            direct = direct_by_budget[arm.budget_tier]
            direct_rate = 100 * (direct.success_rate or 0)
            leaderboard_rows.append(
                f"<tr><td><strong>{_e(plan.model.id)}</strong></td>"
                f"<td>{_e(arm.budget_tier)}</td><td>{_e(arm.strategy)}</td>"
                f'<td class="score"><span>{rate:.1f}%</span><i style="width:{rate:.2f}%"></i></td>'
                f"<td>{rate-direct_rate:+.1f} pp</td>"
                f"<td>{_number(arm.mean_total_tokens, ' tok')}</td>"
                f"<td>{_number(arm.mean_wall_seconds, ' s', 1)}</td></tr>"
            )
        for row in _transition_rows(plan, records):
            transition_rows.append(
                f"<tr><td><strong>{_e(plan.model.id)}</strong></td>"
                f"<td>{_e(row['budget_tier'])}</td><td>{_e(row['strategy'])}</td>"
                f"<td class=\"positive\">{row['rescued']}</td>"
                f"<td class=\"negative\">{row['regressed']}</td>"
                f"<td>{row['unchanged']}</td></tr>"
            )
    metric_cards = _comparison_metric_cards(items)
    uplift = _baseline_uplift(items)
    study_snapshot = _study_snapshot(items)
    conclusion = _comparison_conclusion(items)
    controller_flow = _controller_flow()
    task_suite = _task_suite(items[0][0])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title>EdgeLoopBench model comparison</title><style>{_CSS}{_COMPARISON_CSS}</style></head>
<body><header><a class="brand" href="#top">EDGE<span>LOOP</span>BENCH</a><div class="meta">{models} · {total_runs} runs</div></header>
<main id="top"><div class="eyebrow">PAIRED MODEL ANALYSIS</div><h1>Loop effect by model</h1>
<p class="lede">Objective repair success under identical tasks, seeds, logical budgets, controller, and Ollama runtime.</p>
{study_snapshot}{conclusion}<section><div class="section-head"><div><div class="eyebrow">AGENT TRACK</div><h2>Success and cost</h2></div><div class="direction">Primary charts use the medium budget tier</div></div>
{metric_cards}
{uplift}
<div class="chart-title heat-title"><h3>Complete results</h3><span>Loop delta is relative to Direct within model</span></div>
<div class="table-wrap"><table class="leaderboard comparison-leaderboard"><thead><tr><th>Model</th><th>Budget</th><th>Strategy</th><th>Success</th><th>vs Direct</th><th>Mean logical tokens</th><th>Mean wall</th></tr></thead><tbody>{''.join(leaderboard_rows)}</tbody></table></div>
<div class="chart-title heat-title"><h3>Paired outcome transitions</h3><span>Same task, budget, and seed</span></div>
<div class="table-wrap"><table class="transitions"><thead><tr><th>Model</th><th>Budget</th><th>Loop</th><th>Rescued</th><th>Regressed</th><th>Unchanged</th></tr></thead><tbody>{''.join(transition_rows)}</tbody></table></div>
</section>{controller_flow}{task_suite}<section class="serving"><div class="section-head"><div><div class="eyebrow">SERVING TRACK</div><h2>Serving efficiency is reported separately</h2></div></div>
<div class="empty"><strong>No composite score</strong><span>GPU throughput, memory, and energy ablations must not be presented as agent-effectiveness gains.</span></div></section>
<footer>Complete manifest-bound experiments · model artifact is the only varying factor</footer></main></body></html>"""


def _study_snapshot(
    items: tuple[
        tuple[ExperimentPlan, SummaryReport, tuple[RunRecord, ...]], ...
    ],
) -> str:
    plan = items[0][0]
    task_count = len(plan.tasks)
    seed_count = len(plan.seeds)
    budget_count = len(plan.budgets or {})
    strategy_count = len(plan.strategies)
    episode_count = sum(len(records) for _, _, records in items)
    dataset = "MicroRepair-6" if set(plan.tasks) == set(_TASK_SUMMARIES) else "Configured repair suite"
    facts = (
        ("Dataset", dataset),
        ("Workload", f"{task_count} offline Python repair tasks"),
        ("Pairing", f"{seed_count} seeds × {budget_count} budget tiers"),
        ("Arms", f"{len(items)} models × {strategy_count} strategies"),
        ("Observed", f"{episode_count} complete episodes"),
    )
    return (
        '<section class="snapshot-section"><div class="section-head"><div><div class="eyebrow">EXPERIMENT SCOPE</div>'
        '<h2>Study snapshot</h2></div><div class="direction">What this result actually contains</div></div>'
        '<div class="snapshot-grid">'
        + "".join(
            f'<div class="snapshot-fact"><span>{_e(label)}</span><strong>{_e(value)}</strong></div>'
            for label, value in facts
        )
        + '</div></section>'
    )


def _comparison_conclusion(
    items: tuple[
        tuple[ExperimentPlan, SummaryReport, tuple[RunRecord, ...]], ...
    ],
) -> str:
    budgets = tuple((items[0][0].budgets or {}).keys())
    budget = "medium" if "medium" in budgets else budgets[0]
    direct_candidates: list[tuple[float, str]] = []
    loop_candidates: list[tuple[float, str, str, int, int]] = []
    maker_rescued = maker_regressed = maker_pairs = 0

    for plan, report, records in items:
        arms = {
            arm.strategy: arm
            for arm in report.arms
            if arm.budget_tier == budget
        }
        direct_rate = 100 * (arms["direct"].success_rate or 0)
        model = _short_model_label(plan.model.id)
        direct_candidates.append((direct_rate, model))
        transitions = {
            str(row["strategy"]): row
            for row in _transition_rows(plan, records)
            if row["budget_tier"] == budget
        }
        for strategy, label in (
            ("bounded_retry", "Bounded Retry"),
            ("maker_verifier", "Maker–Verifier"),
        ):
            delta = 100 * (arms[strategy].success_rate or 0) - direct_rate
            row = transitions[strategy]
            rescued = int(row["rescued"])
            regressed = int(row["regressed"])
            loop_candidates.append((delta, model, label, rescued, regressed))
            if strategy == "maker_verifier":
                maker_rescued += rescued
                maker_regressed += regressed
                maker_pairs += rescued + regressed + int(row["unchanged"])

    direct_rate, direct_model = max(direct_candidates)
    delta, loop_model, loop_label, rescued, regressed = max(loop_candidates)
    if delta > 0:
        loop_finding = (
            f"{loop_model} {loop_label} produced the largest measured uplift: "
            f"{delta:+.1f} percentage points, with {rescued} rescued and {regressed} regressed paired outcomes."
        )
    else:
        loop_finding = "No tested loop improved verified success over its own Direct baseline in this budget tier."

    return f'''<section class="conclusion-section"><div class="section-head"><div><div class="eyebrow">BOTTOM LINE</div>
<h2>What the evidence supports</h2></div><div class="direction">Medium-budget paired comparison</div></div>
<div class="conclusion-lead"><strong>{_e(loop_finding)}</strong><span>The strongest Direct baseline was {_e(direct_model)} at {direct_rate:.1f}% verified success.</span></div>
<div class="conclusion-grid">
<article><span>Measured finding</span><h3>Loops can help, do nothing, or regress</h3><p>Judge each loop against Direct within the same model. More calls are useful only when rescued outcomes exceed regressions at an acceptable cost.</p></article>
<article class="warning"><span>Design limitation</span><h3>Current Maker–Verifier is not the target verifier</h3><p>It rescued {maker_rescued} and regressed {maker_regressed} of {maker_pairs} paired outcomes here. It is a second edit call, not a read-only judge, so this is not evidence that verifier loops generally fail.</p></article>
<article><span>Inference boundary</span><h3>Qualification, not a general coding leaderboard</h3><p>The result applies to this small deterministic repair suite. It does not establish broad model quality or serving efficiency.</p></article>
</div></section>'''


def _baseline_uplift(
    items: tuple[
        tuple[ExperimentPlan, SummaryReport, tuple[RunRecord, ...]], ...
    ],
) -> str:
    budgets = tuple((items[0][0].budgets or {}).keys())
    budget = "medium" if "medium" in budgets else budgets[0]
    cards: list[str] = []
    for plan, report, _records in items:
        arms = {
            arm.strategy: arm
            for arm in report.arms
            if arm.budget_tier == budget
        }
        direct = arms["direct"]
        direct_rate = 100 * (direct.success_rate or 0)
        rows: list[str] = []
        for strategy, label in (
            ("bounded_retry", "Bounded Retry"),
            ("maker_verifier", "Maker–Verifier"),
        ):
            arm = arms[strategy]
            delta = 100 * (arm.success_rate or 0) - direct_rate
            delta_class = "positive" if delta > 0 else "negative" if delta < 0 else "neutral"
            rows.append(
                f'<div class="uplift-row"><strong>{_e(label)}</strong>'
                f'<span><small>Success Δ</small><b class="{delta_class}">{delta:+.1f} pp</b></span>'
                f'<span><small>Token cost</small><b>{_ratio(arm.mean_total_tokens, direct.mean_total_tokens)}</b></span>'
                f'<span><small>Wall time</small><b>{_ratio(arm.mean_wall_seconds, direct.mean_wall_seconds)}</b></span></div>'
            )
        cards.append(
            f'<article class="uplift-card"><div class="uplift-model"><h4>{_e(_short_model_label(plan.model.id))}</h4>'
            f'<span>Direct baseline</span><strong>{direct_rate:.1f}%</strong></div>'
            f'{"".join(rows)}</article>'
        )
    return (
        '<div class="chart-title uplift-title"><h3>Baseline → loop uplift</h3>'
        '<span>Paired success difference; cost is a multiplier of Direct</span></div>'
        f'<div class="uplift-grid">{"".join(cards)}</div>'
    )


def _ratio(candidate: float | None, baseline: float | None) -> str:
    if candidate is None or baseline is None or baseline == 0:
        return "n/a"
    return f"{candidate / baseline:.2f}×"


def _controller_flow() -> str:
    lanes = (
        (
            "Direct baseline",
            "One attempt; no controller retry",
            (
                "Reset clean task worktree",
                "One model call returns replacement-edit JSON",
                "Validate paths and apply candidate",
                "Run public tests once",
                "If public tests pass, run isolated hidden evaluation",
            ),
        ),
        (
            "Bounded Retry",
            "Repair loop within shared logical caps",
            (
                "Build prompt from the current worktree",
                "Model returns replacement-edit JSON",
                "Validate, apply, and run public tests",
                "On rejection or failure, add sanitized feedback",
                "Repeat until pass or call/token/test budget ends",
                "If public tests pass, run isolated hidden evaluation",
            ),
        ),
        (
            "Maker–Verifier",
            "Tested implementation is review-and-revise",
            (
                "Maker returns replacement-edit JSON",
                "Validate, apply, and run public tests",
                "Request a second model call as verifier",
                "Review and revise: verifier may replace source files",
                "Apply revision and run public tests again",
                "If public tests pass, run isolated hidden evaluation",
            ),
        ),
    )
    rendered = []
    for title, subtitle, nodes in lanes:
        rendered.append(
            f'<article class="flow-lane"><div class="flow-head"><h3>{_e(title)}</h3><span>{_e(subtitle)}</span></div>'
            f'<ol>{"".join(f"<li>{_e(node)}</li>" for node in nodes)}</ol></article>'
        )
    return (
        '<section class="flow-section"><div class="section-head"><div><div class="eyebrow">LOOP DESIGN</div>'
        '<h2>Controller flow</h2></div><div class="direction">The nodes below match the tested controller</div></div>'
        f'<div class="flow-grid">{"".join(rendered)}</div>'
        '<div class="boundary-note"><strong>Evaluation boundary</strong><span>Hidden evaluator feedback is never returned to any model. '
        'Maker–Verifier here is not an independent read-only APPROVE/REJECT judge; it is a second review-and-revise edit call.</span></div></section>'
    )


_TASK_SUMMARIES = {
    "python-localized-001": (
        "Localized", "Generated mutation", "Pagination upper bound",
        "Clamp a one-based page to 1…total_pages and reject non-positive totals.",
        "Single-file boundary conditions and error handling.",
    ),
    "python-localized-002": (
        "Localized", "Generated mutation", "Comma-separated tags",
        "Trim tags, discard empty entries, preserve order, and reject all-empty input.",
        "Single-file parsing and input validation.",
    ),
    "python-cross-file-001": (
        "Cross-file", "Generated mutation", "User-name lookup contract",
        "Return an upper-case stored name or UNKNOWN while preserving the repository found/value contract.",
        "Contract reasoning across repository and service modules.",
    ),
    "python-cross-file-002": (
        "Cross-file", "Generated mutation", "Inventory reservation state",
        "Move units from available to reserved without breaking totals; invalid quantities must not mutate state.",
        "Coordinated state invariants across inventory and audit modules.",
    ),
    "python-diagnosis-001": (
        "Diagnosis", "Generated mutation", "Batch totals",
        "Sum amount values by kind while preserving intentional progress messages and empty-input behavior.",
        "Diagnosis when useful failures are mixed with noisy output.",
    ),
    "python-adversarial-001": (
        "Adversarial", "Verifier adversarial", "Canonical label keys",
        "Normalize every non-empty whitespace run to one hyphen and reject whitespace-only labels.",
        "Resistance to a superficial public-test-only fix.",
    ),
}


def _task_suite(plan: ExperimentPlan) -> str:
    cards = []
    for task_id in plan.tasks:
        category, source, title, contract, capability = _TASK_SUMMARIES.get(
            task_id,
            (
                "Benchmark task", "Declared source", task_id,
                "Configured deterministic repair task.",
                "Objective repair under the declared task contract.",
            ),
        )
        cards.append(
            f'<article class="task-card"><div class="task-tags"><span>{_e(category)}</span><span>{_e(source)}</span></div>'
            f'<h3>{_e(title)}</h3><code>{_e(task_id)}</code>'
            f'<h4>Agent-visible repair contract</h4><p>{_e(contract)}</p>'
            f'<dl><dt>Capability</dt><dd>{_e(capability)}</dd>'
            '<dt>Public tests</dt><dd>Deterministic Python unittest feedback is visible to the loop.</dd>'
            '<dt>Hidden evaluation</dt><dd>Final objective pass/fail only; no feedback returns to the model.</dd></dl></article>'
        )
    observations = len(plan.tasks) * len(plan.seeds)
    dataset_name = "MicroRepair-6 task catalog" if set(plan.tasks) == set(_TASK_SUMMARIES) else "Configured task catalog"
    return (
        '<section class="task-section"><div class="section-head"><div><div class="eyebrow">BENCH DATA</div>'
        f'<h2>What was tested: {_e(dataset_name)}</h2></div>'
        f'<div class="direction">{len(plan.tasks)} offline Python repairs × {len(plan.seeds)} seeds = {observations} paired observations per arm</div></div>'
        f'<div class="task-grid">{"".join(cards)}</div>'
        '<p class="task-boundary"><strong>Dataset boundary.</strong> This is an original, offline harness shakeout suite—not HumanEval, SWE-bench, or a broad coding benchmark. Agents see task source, instructions, and public tests. Hidden tests and gold patches stay outside the worktree.</p></section>'
    )


def _comparison_metric_cards(
    items: tuple[
        tuple[ExperimentPlan, SummaryReport, tuple[RunRecord, ...]], ...
    ],
) -> str:
    available_budgets = tuple((items[0][0].budgets or {}).keys())
    budget = "medium" if "medium" in available_budgets else available_budgets[0]
    series = []
    for plan, report, _records in items:
        arms = {
            arm.strategy: arm
            for arm in report.arms
            if arm.budget_tier == budget
        }
        series.append((plan.model.id, arms))
    cards = (
        _metric_card(
            "success",
            "Verified success",
            "Objective repair success · Higher is better",
            series,
            lambda arm: 100 * (arm.success_rate or 0),
            fixed_maximum=100,
            suffix="%",
        ),
        _metric_card(
            "tokens",
            "Logical token cost",
            "Mean logical tokens per episode · Lower is better",
            series,
            lambda arm: arm.mean_total_tokens or 0,
            suffix=" tok",
        ),
        _metric_card(
            "time",
            "Mean episode time",
            "End-to-end wall seconds · Lower is better",
            series,
            lambda arm: arm.mean_wall_seconds or 0,
            suffix=" s",
            precision=1,
        ),
    )
    return f'<div class="metric-grid">{"".join(cards)}</div>'


def _metric_card(
    metric: str,
    title: str,
    subtitle: str,
    series: list[tuple[str, dict[str, ArmSummary]]],
    value_for: Callable[[ArmSummary], float],
    *,
    suffix: str,
    fixed_maximum: float | None = None,
    precision: int = 0,
) -> str:
    strategies = ("direct", "bounded_retry", "maker_verifier")
    values = [
        float(value_for(arms[strategy]))
        for _model, arms in series
        for strategy in strategies
    ]
    maximum = fixed_maximum or max(values, default=1) * 1.12 or 1
    left, top, baseline, plot_width = 50.0, 30.0, 190.0, 304.0
    plot_height = baseline - top
    group_width = plot_width / len(series)
    bar_width = min(24.0, (group_width - 22.0) / len(strategies))
    bar_gap = 4.0
    grid = []
    for fraction in (0.0, 0.5, 1.0):
        y = baseline - plot_height * fraction
        tick_value = maximum * fraction
        tick = _metric_value(tick_value, suffix, precision)
        grid.append(
            f'<line class="metric-gridline" x1="{left:.1f}" y1="{y:.1f}" '
            f'x2="{left+plot_width:.1f}" y2="{y:.1f}"/>'
            f'<text class="metric-axis" x="{left-5:.1f}" y="{y+3:.1f}" '
            f'text-anchor="end">{_e(tick)}</text>'
        )
    marks = []
    labels = []
    for model_index, (model, arms) in enumerate(series):
        group_left = left + model_index * group_width
        bars_width = len(strategies) * bar_width + (len(strategies) - 1) * bar_gap
        bars_left = group_left + (group_width - bars_width) / 2
        for strategy_index, strategy in enumerate(strategies):
            value = float(value_for(arms[strategy]))
            x = bars_left + strategy_index * (bar_width + bar_gap)
            height = plot_height * value / maximum
            y = baseline - height
            formatted = _metric_value(value, suffix, precision)
            accessible_value = (
                "0% verified success"
                if metric == "success" and value == 0
                else formatted
            )
            accessible = (
                f"{_short_model_label(model)} {strategy}: {accessible_value}"
            )
            if value == 0:
                marks.append(
                    f'<line class="zero-marker" data-strategy="{_e(strategy)}" '
                    f'x1="{x:.1f}" y1="{baseline:.1f}" '
                    f'x2="{x+bar_width:.1f}" y2="{baseline:.1f}">'
                    f'<title>{_e(accessible)}</title></line>'
                )
                value_y = baseline - 7
            else:
                marks.append(
                    f'<rect class="metric-bar bar-{_e(strategy)}" x="{x:.1f}" '
                    f'y="{y:.1f}" width="{bar_width:.1f}" height="{height:.1f}">'
                    f'<title>{_e(accessible)}</title></rect>'
                )
                label_offset = 10 if strategy_index == 1 else 0
                candidate_y = y - 5 - label_offset
                value_y = (
                    top + 10 + label_offset
                    if candidate_y < top + 10
                    else candidate_y
                )
            marks.append(
                f'<text class="metric-value" x="{x+bar_width/2:.1f}" '
                f'y="{value_y:.1f}" text-anchor="middle">{_e(formatted)}</text>'
            )
        labels.append(
            f'<text class="metric-model" x="{group_left+group_width/2:.1f}" '
            f'y="{baseline+18:.1f}" text-anchor="middle">'
            f'{_e(_short_model_label(model))}</text>'
        )
    legend = "".join(
        f'<span><i class="legend-swatch bar-{strategy}"></i>{label}</span>'
        for strategy, label in (
            ("direct", "Direct"),
            ("bounded_retry", "Retry"),
            ("maker_verifier", "Maker–Verifier"),
        )
    )
    return f"""<article class="metric-card metric-{_e(metric)}">
<div class="metric-head"><h3><i aria-hidden="true"></i>{_e(title)}</h3><p>{_e(subtitle)}</p></div>
<svg class="metric-chart" viewBox="0 0 370 220" role="img" aria-label="{_e(title)} by model and loop strategy">
<title>{_e(title)} by model and loop strategy</title><desc>{_e(subtitle)}. Zero values are marked on the baseline.</desc>
{''.join(grid)}{''.join(marks)}{''.join(labels)}</svg><div class="metric-legend">{legend}</div></article>"""


def _metric_value(value: float, suffix: str, precision: int) -> str:
    if suffix == "%":
        return f"{value:.0f}%"
    return f"{value:,.{precision}f}{suffix}"


def _short_model_label(model: str) -> str:
    normalized = model.removesuffix(":latest")
    replacements = {
        "qwen3.5:4b": "Qwen 4B",
        "qwen3.5:9b": "Qwen 9B",
        "phi4-mini": "Phi-4 mini",
        "gemma4:12b-it-q4_K_M": "Gemma 4 12B",
    }
    return replacements.get(normalized, normalized)


def _document(
    report: SummaryReport, plan: ExperimentPlan, records: tuple[RunRecord, ...]
) -> str:
    coverage = report.coverage
    observed = coverage.observed_runs if coverage else len(records)
    expected = coverage.expected_runs if coverage else "—"
    meta = (
        f"{_e(plan.model.id)} · {_e(plan.backend.name)} {_e(plan.backend.version)} · "
        f"{plan.model.context_limit_tokens:,} ctx · {observed}/{expected} runs"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title>EdgeLoopBench analysis</title><style>{_CSS}</style></head>
<body><header><a class="brand" href="#top">EDGE<span>LOOP</span>BENCH</a><div class="meta">{meta}</div></header>
<main id="top"><div class="eyebrow">LOCAL AGENT ANALYSIS</div><h1>Loop engineering, measured end to end</h1>
<p class="lede">Verified repair success is reported against logical tokens and wall time. Serving measurements remain a separate track.</p>
<section><div class="section-head"><div><div class="eyebrow">AGENT TRACK</div><h2>Agent effectiveness</h2></div><div class="direction">Verified success · higher is better</div></div>
{_leaderboard(report)}
<div class="split"><div>{_scatter(report)}</div><div>{_paired(report)}</div></div>
{_heatmap(plan, records)}
</section>
<section class="serving"><div class="section-head"><div><div class="eyebrow">SERVING TRACK</div><h2>Serving efficiency</h2></div><div class="direction">Latency, throughput, memory, energy</div></div>
<div class="empty"><strong>Not combined with agent scores</strong><span>This effectiveness report contains no serving-efficiency composite. Generate a serving report from fixed request shapes before comparing Ollama, MLX-LM, or vLLM-Metal.</span></div>
</section>
<footer>Manifest <code>sha256:{_e(plan.manifest_sha256 or 'unbound')}</code> · {_e(plan.model.weight_quantization)} weights · thinking={str(plan.generation.thinking).lower() if plan.generation else 'n/a'}</footer>
</main></body></html>"""


def _leaderboard(report: SummaryReport) -> str:
    rows = []
    for arm in sorted(
        report.arms,
        key=lambda item: (
            -(item.success_rate if item.success_rate is not None else -1),
            item.mean_total_tokens or float("inf"),
        ),
    ):
        rate = 100 * arm.success_rate if arm.success_rate is not None else 0
        rows.append(
            f"<tr><td><strong>{_e(arm.strategy)}</strong><small>{_e(arm.budget_tier)}</small></td>"
            f'<td class="score"><span>{rate:.1f}%</span><i style="width:{rate:.2f}%"></i></td>'
            f"<td>{arm.successes}/{arm.run_count}</td>"
            f"<td>{_number(arm.mean_total_tokens, ' tok')}</td>"
            f"<td>{_number(arm.mean_wall_seconds, ' s', 1)}</td>"
            f"<td>{_number(arm.success_per_1k_tokens, '', 3)}</td></tr>"
        )
    return """<div class="chart-title"><h3>Verified-success leaderboard</h3><span>Observed outcomes, no composite score</span></div>
<div class="table-wrap"><table class="leaderboard"><thead><tr><th>Strategy</th><th>Success</th><th>Passed</th><th>Mean logical tokens</th><th>Mean wall time</th><th>Success / 1K tok</th></tr></thead><tbody>""" + "".join(rows) + "</tbody></table></div>"


def _scatter(report: SummaryReport) -> str:
    arms = [arm for arm in report.arms if arm.mean_total_tokens is not None and arm.success_rate is not None]
    maximum = max((arm.mean_total_tokens or 0 for arm in arms), default=1) * 1.12 or 1
    marks = []
    for arm in arms:
        x = 54 + 620 * (arm.mean_total_tokens or 0) / maximum
        y = 226 - 176 * (arm.success_rate or 0)
        marks.append(
            f'<circle class="point {_e(arm.strategy)}" cx="{x:.1f}" cy="{y:.1f}" r="7"><title>{_e(arm.strategy)}: {100*(arm.success_rate or 0):.1f}%, {arm.mean_total_tokens:.0f} tokens</title></circle>'
            f'<text x="{x+11:.1f}" y="{y+4:.1f}">{_e(arm.strategy)}</text>'
        )
    return f"""<div class="chart-title"><h3>Verified success vs. logical tokens</h3><span>Upper-left is more attractive</span></div>
<svg class="scatter" viewBox="0 0 720 270" role="img" aria-label="Verified success versus mean logical tokens">
<line x1="54" y1="50" x2="54" y2="226"/><line x1="54" y1="226" x2="674" y2="226"/>
<line class="grid" x1="54" y1="138" x2="674" y2="138"/><text class="axis" x="18" y="54">100%</text><text class="axis" x="27" y="142">50%</text><text class="axis" x="35" y="230">0%</text>
<text class="axis" x="54" y="250">0</text><text class="axis" x="600" y="250">mean logical tokens →</text>{''.join(marks)}</svg>"""


def _paired(report: SummaryReport) -> str:
    rows = []
    for pair in report.pairs:
        delta_class = "positive" if pair.success_delta_pp > 0 else "negative" if pair.success_delta_pp < 0 else ""
        rows.append(
            f"<tr><td>{_e(pair.candidate_strategy)} <small>vs {_e(pair.baseline_strategy)}</small></td>"
            f'<td class="{delta_class}">{pair.success_delta_pp:+.1f} pp</td>'
            f"<td>{pair.mean_total_token_delta:+.0f}</td><td>{pair.mean_wall_delta_seconds:+.1f} s</td><td>{pair.pair_count}</td></tr>"
        )
    return """<div class="chart-title"><h3>Paired loop deltas</h3><span>Candidate minus baseline</span></div>
<div class="table-wrap"><table class="paired"><thead><tr><th>Comparison</th><th>Success</th><th>Tokens</th><th>Wall</th><th>n</th></tr></thead><tbody>""" + "".join(rows) + "</tbody></table></div>"


def _heatmap(plan: ExperimentPlan, records: tuple[RunRecord, ...]) -> str:
    cells: dict[tuple[str, str], list[RunRecord]] = {}
    for record in records:
        cells.setdefault((record.task_id, record.strategy), []).append(record)
    header = "".join(f"<th>{_e(strategy)}</th>" for strategy in plan.strategies)
    rows = []
    for task in plan.tasks:
        values = []
        for strategy in plan.strategies:
            group = cells.get((task, strategy), [])
            passed = sum(item.objective_success for item in group)
            rate = passed / len(group) if group else None
            css = "empty-cell" if rate is None else "pass" if rate == 1 else "fail" if rate == 0 else "mixed"
            label = "—" if rate is None else f"{100*rate:.0f}%"
            values.append(f'<td class="heat {css}" aria-label="{_e(task)} {_e(strategy)} {label}">{label}</td>')
        rows.append(f"<tr><th>{_e(task)}</th>{''.join(values)}</tr>")
    return f"""<div class="chart-title heat-title"><h3>Task × strategy</h3><span>Objective success rate across declared seeds and budgets</span></div>
<div class="table-wrap"><table class="heatmap"><thead><tr><th>Task</th>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""


def _number(value: float | None, suffix: str, precision: int = 0) -> str:
    return "—" if value is None else f"{value:,.{precision}f}{suffix}"


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


_COMPARISON_CSS = """
.snapshot-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));margin-top:25px;border:1px solid var(--line);background:var(--panel)}.snapshot-fact{min-width:0;padding:15px;border-right:1px solid var(--line)}.snapshot-fact:last-child{border-right:0}.snapshot-fact span,.conclusion-grid article>span{display:block;color:var(--muted);font-size:10px;font-weight:500;letter-spacing:.08em;text-transform:uppercase}.snapshot-fact strong{display:block;margin-top:6px;font-size:15px}.conclusion-section{padding-top:40px}.conclusion-lead{display:grid;grid-template-columns:1.3fr .7fr;gap:28px;align-items:end;margin-top:25px;padding:22px 0;border-top:4px solid var(--text);border-bottom:1px solid var(--line)}.conclusion-lead strong{font:500 25px/1.25 ui-serif,Georgia,serif}.conclusion-lead span{color:var(--muted)}.conclusion-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line)}.conclusion-grid article{background:var(--panel);padding:18px}.conclusion-grid article.warning{box-shadow:inset 0 4px var(--bar-maker)}.conclusion-grid h3{margin-top:8px;font-size:16px}.conclusion-grid p{margin:8px 0 0;color:var(--muted);font-size:12px}.uplift-title{margin-top:38px}.uplift-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.uplift-card{border:1px solid var(--line);border-radius:8px;background:var(--panel);overflow:hidden}.uplift-model{display:grid;grid-template-columns:1fr auto;gap:1px 12px;padding:15px 16px;border-bottom:1px solid var(--line)}.uplift-model h4{grid-row:1/3;margin:0;align-self:center;font:500 18px/1.2 ui-serif,Georgia,serif}.uplift-model span{color:var(--muted);font-size:11px}.uplift-model>strong{font-size:20px;text-align:right}.uplift-row{display:grid;grid-template-columns:1.2fr repeat(3,1fr);align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid var(--line)}.uplift-row:last-child{border-bottom:0}.uplift-row>strong{font-size:12px}.uplift-row span{display:grid;text-align:right}.uplift-row small{font-size:10px}.uplift-row b{font-weight:500;font-variant-numeric:tabular-nums}.neutral{color:var(--muted)}.flow-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:28px}.flow-lane{min-width:0}.flow-head{min-height:72px;padding:15px 16px;background:var(--bar-direct);color:var(--panel);border-radius:8px 8px 0 0}.flow-lane:nth-child(2) .flow-head{background:var(--bar-retry)}.flow-lane:nth-child(3) .flow-head{background:var(--bar-maker)}.flow-head h3{font:500 20px/1.2 ui-serif,Georgia,serif}.flow-head span{display:block;margin-top:5px;font-size:11px}.flow-lane ol{list-style:none;margin:0;padding:14px;border:1px solid var(--line);border-top:0;border-radius:0 0 8px 8px;background:var(--panel)}.flow-lane li{position:relative;padding:10px 11px;border:1px solid var(--line);background:var(--bg);font-size:12px}.flow-lane li:not(:last-child){margin-bottom:20px}.flow-lane li:not(:last-child)::after{content:"↓";position:absolute;left:50%;bottom:-20px;transform:translateX(-50%);color:var(--muted)}.boundary-note{display:grid;grid-template-columns:auto 1fr;gap:16px;margin-top:18px;padding:15px 16px;border-left:4px solid var(--metric-success);background:var(--panel)}.boundary-note span,.task-boundary{color:var(--muted)}.task-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;margin-top:28px;background:var(--line);border:1px solid var(--line)}.task-card{min-width:0;padding:16px;background:var(--panel)}.task-tags{display:flex;gap:5px;flex-wrap:wrap}.task-tags span{padding:3px 6px;border:1px solid var(--line);color:var(--green-dark);font-size:9px;font-weight:500;letter-spacing:.06em;text-transform:uppercase}.task-card h3{margin-top:10px;font:500 18px/1.2 ui-serif,Georgia,serif}.task-card code{display:block;margin-top:4px;color:var(--muted);font-size:10px;overflow-wrap:anywhere}.task-card h4{margin:14px 0 0;font-size:11px}.task-card p{margin:5px 0 0;color:var(--muted);font-size:12px}.task-card dl{display:grid;grid-template-columns:auto 1fr;gap:5px 10px;margin:14px 0 0;padding-top:12px;border-top:1px solid var(--line);font-size:10px}.task-card dt{font-weight:500}.task-card dd{margin:0;color:var(--muted)}.task-boundary{margin:14px 0 0;font-size:12px}.task-boundary strong{color:var(--text)}.flow-head,.flow-lane:nth-child(2) .flow-head,.flow-lane:nth-child(3) .flow-head{background:var(--panel);color:var(--text);border:1px solid var(--line);border-top:5px solid var(--bar-direct);padding-top:11px}.flow-lane:nth-child(2) .flow-head{border-top-color:var(--bar-retry)}.flow-lane:nth-child(3) .flow-head{border-top-color:var(--bar-maker)}.flow-head span{color:var(--muted)}@media(max-width:980px){.snapshot-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.snapshot-fact{border-bottom:1px solid var(--line)}.snapshot-fact:nth-child(even){border-right:0}.snapshot-fact:last-child{border-bottom:0}.conclusion-lead{grid-template-columns:1fr}.conclusion-grid,.uplift-grid,.flow-grid{grid-template-columns:1fr}.flow-head{min-height:0}.task-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){.snapshot-grid{grid-template-columns:1fr}.snapshot-fact{border-right:0}.conclusion-lead strong{font-size:21px}.uplift-row{grid-template-columns:1fr repeat(3,minmax(0,1fr));padding:11px 10px}.uplift-row>strong{font-size:11px}.uplift-row small{font-size:9px}.boundary-note{grid-template-columns:1fr;gap:4px}.task-grid{grid-template-columns:1fr}.task-card dl{grid-template-columns:1fr;gap:2px}.task-card dd:not(:last-child){margin-bottom:5px}}
.uplift-row small{font-size:10px}
"""


_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f4;--panel:#fff;--text:#172019;--muted:#657068;--line:#dfe4df;--green:#62d84e;--green-dark:#268b35;--amber:#e0b23f;--red:#d95d55;--metric-success:#8247e5;--metric-tokens:#f0c800;--metric-time:#f0773c;--bar-direct:#202321;--bar-retry:#36a852;--bar-maker:#ef6d9a}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}header{height:58px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100% - 1180px)/2));background:color-mix(in srgb,var(--bg) 92%,transparent)}.brand{color:var(--text);text-decoration:none;font-weight:500;letter-spacing:-.04em}.brand span{color:var(--green-dark)}.meta,.direction,.chart-title span,footer,.empty span{color:var(--muted)}main{max-width:1180px;margin:auto;padding:58px 24px 80px}.eyebrow{font-size:11px;font-weight:500;letter-spacing:.14em;color:var(--green-dark)}h1,h2,h3,strong{font-weight:500}h1{font-family:ui-serif,Georgia,serif;font-size:54px;letter-spacing:-.045em;line-height:1.02;max-width:750px;margin:10px 0 16px}h2{font-size:28px;letter-spacing:-.03em;margin:5px 0}h3{font-size:16px;margin:0}.lede{font-size:17px;color:var(--muted);max-width:720px;margin:0 0 64px}section{border-top:1px solid var(--line);padding:34px 0 48px}.section-head,.chart-title{display:flex;align-items:flex-end;justify-content:space-between;gap:18px}.chart-title{margin:30px 0 13px;align-items:baseline}.metric-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:28px}.metric-card{min-width:0;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:16px}.metric-head h3{display:flex;align-items:center;gap:9px;font-family:ui-serif,Georgia,serif;font-size:22px}.metric-head h3 i{display:block;width:14px;height:14px;flex:0 0 14px}.metric-success .metric-head h3 i{background:var(--metric-success)}.metric-tokens .metric-head h3 i{background:var(--metric-tokens)}.metric-time .metric-head h3 i{background:var(--metric-time)}.metric-head p{min-height:38px;margin:8px 0 0;color:var(--muted);font-size:12px}.metric-chart{display:block;width:100%;height:auto}.metric-gridline{stroke:var(--line);stroke-width:1;stroke-dasharray:2 4}.metric-axis,.metric-model,.metric-value{fill:var(--text);font-size:10px}.metric-axis{fill:var(--muted)}.metric-value{font-weight:500}.metric-bar{shape-rendering:crispEdges}.bar-direct{fill:var(--bar-direct);background:var(--bar-direct)}.bar-bounded_retry{fill:var(--bar-retry);background:var(--bar-retry)}.bar-maker_verifier{fill:var(--bar-maker);background:var(--bar-maker)}.zero-marker{stroke-width:5;stroke-linecap:square}.zero-marker[data-strategy="direct"]{stroke:var(--bar-direct)}.zero-marker[data-strategy="bounded_retry"]{stroke:var(--bar-retry)}.zero-marker[data-strategy="maker_verifier"]{stroke:var(--bar-maker)}.metric-legend{display:flex;justify-content:center;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:11px}.metric-legend span{display:flex;align-items:center;gap:5px}.legend-swatch{display:block;width:9px;height:9px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:var(--panel)}th,td{padding:12px 14px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:500}th:first-child,td:first-child{text-align:left}td strong{display:block}small{display:block;color:var(--muted)}.score{min-width:240px;position:relative}.score span{position:relative;z-index:1;font-variant-numeric:tabular-nums}.score i{position:absolute;left:12px;bottom:7px;height:3px;background:var(--green);display:block}.split{display:grid;grid-template-columns:1.08fr .92fr;gap:36px}.scatter{width:100%;background:var(--panel);display:block}.scatter line{stroke:var(--line);stroke-width:1}.scatter .grid{stroke-dasharray:3 5}.scatter text{fill:var(--text);font-size:11px}.scatter .axis{fill:var(--muted)}.point{fill:var(--green-dark);stroke:var(--panel);stroke-width:2}.point.bounded_retry{fill:var(--green)}.point.maker_verifier{fill:var(--amber)}.positive{color:var(--green-dark)}.negative{color:var(--red)}.heat-title{margin-top:42px}.heatmap .heat{text-align:center;font-weight:500;min-width:110px}.heat.pass{background:color-mix(in srgb,var(--green) 26%,var(--panel))}.heat.mixed{background:color-mix(in srgb,var(--amber) 24%,var(--panel))}.heat.fail{background:color-mix(in srgb,var(--red) 15%,var(--panel))}.empty-cell{color:var(--muted)}.serving{margin-top:12px}.empty{border:1px dashed var(--line);padding:22px;margin-top:25px;display:grid;gap:5px}.empty strong{font-size:16px}footer{border-top:1px solid var(--line);padding-top:20px;font-size:12px;overflow-wrap:anywhere}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}@media(max-width:980px){.metric-grid{grid-template-columns:1fr}.metric-head p{min-height:0}}@media(max-width:760px){header{padding:0 16px}.meta{display:none}main{padding:38px 16px 60px}h1{font-size:40px}.lede{margin-bottom:48px}.split{grid-template-columns:1fr}.section-head,.chart-title{align-items:flex-start;flex-direction:column;gap:4px}.metric-card{padding:12px}.table-wrap{overflow:visible}th,td{padding:10px 8px;white-space:normal}.leaderboard th:nth-child(3),.leaderboard td:nth-child(3),.leaderboard th:nth-child(5),.leaderboard td:nth-child(5),.leaderboard th:nth-child(6),.leaderboard td:nth-child(6){display:none}.leaderboard .score{min-width:120px}.comparison-leaderboard th:nth-child(3),.comparison-leaderboard td:nth-child(3){display:table-cell}.comparison-leaderboard th:nth-child(5),.comparison-leaderboard td:nth-child(5),.comparison-leaderboard th:nth-child(6),.comparison-leaderboard td:nth-child(6),.comparison-leaderboard th:nth-child(7),.comparison-leaderboard td:nth-child(7){display:none}.comparison-leaderboard .score{min-width:80px}.transitions th:nth-child(2),.transitions td:nth-child(2),.transitions th:nth-child(6),.transitions td:nth-child(6){display:none}.paired th:nth-child(4),.paired td:nth-child(4),.paired th:nth-child(5),.paired td:nth-child(5){display:none}.heatmap{table-layout:fixed}.heatmap th,.heatmap td{font-size:10px;overflow-wrap:anywhere;padding:8px 4px}.heatmap th:first-child{width:38%}.heatmap .heat{min-width:0}}@media(prefers-color-scheme:dark){:root{--bg:#101310;--panel:#171b18;--text:#eef4ef;--muted:#a4aea6;--line:#303731;--green:#74e45f;--green-dark:#64cf55;--amber:#e5bd55;--red:#eb7770;--bar-direct:#f1f3f1;--bar-retry:#6bd47d;--bar-maker:#f58bb0}}
"""
