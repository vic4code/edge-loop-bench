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
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title>EdgeLoopBench model comparison</title><style>{_CSS}</style></head>
<body><header><a class="brand" href="#top">EDGE<span>LOOP</span>BENCH</a><div class="meta">{models} · {total_runs} runs</div></header>
<main id="top"><div class="eyebrow">PAIRED MODEL ANALYSIS</div><h1>Loop effect by model</h1>
<p class="lede">Objective repair success under identical tasks, seeds, logical budgets, controller, and Ollama runtime.</p>
<section><div class="section-head"><div><div class="eyebrow">AGENT TRACK</div><h2>Success and cost</h2></div><div class="direction">Primary charts use the medium budget tier</div></div>
{metric_cards}
<div class="chart-title heat-title"><h3>Complete results</h3><span>Loop delta is relative to Direct within model</span></div>
<div class="table-wrap"><table class="leaderboard comparison-leaderboard"><thead><tr><th>Model</th><th>Budget</th><th>Strategy</th><th>Success</th><th>vs Direct</th><th>Mean logical tokens</th><th>Mean wall</th></tr></thead><tbody>{''.join(leaderboard_rows)}</tbody></table></div>
<div class="chart-title heat-title"><h3>Paired outcome transitions</h3><span>Same task, budget, and seed</span></div>
<div class="table-wrap"><table class="transitions"><thead><tr><th>Model</th><th>Budget</th><th>Loop</th><th>Rescued</th><th>Regressed</th><th>Unchanged</th></tr></thead><tbody>{''.join(transition_rows)}</tbody></table></div>
</section><section class="serving"><div class="section-head"><div><div class="eyebrow">SERVING TRACK</div><h2>Serving efficiency is reported separately</h2></div></div>
<div class="empty"><strong>No composite score</strong><span>GPU throughput, memory, and energy ablations must not be presented as agent-effectiveness gains.</span></div></section>
<footer>Complete manifest-bound experiments · model artifact is the only varying factor</footer></main></body></html>"""


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


_CSS = """
:root{color-scheme:light dark;--bg:#f6f7f4;--panel:#fff;--text:#172019;--muted:#657068;--line:#dfe4df;--green:#62d84e;--green-dark:#268b35;--amber:#e0b23f;--red:#d95d55;--metric-success:#8247e5;--metric-tokens:#f0c800;--metric-time:#f0773c;--bar-direct:#202321;--bar-retry:#36a852;--bar-maker:#ef6d9a}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}header{height:58px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100% - 1180px)/2));background:color-mix(in srgb,var(--bg) 92%,transparent)}.brand{color:var(--text);text-decoration:none;font-weight:500;letter-spacing:-.04em}.brand span{color:var(--green-dark)}.meta,.direction,.chart-title span,footer,.empty span{color:var(--muted)}main{max-width:1180px;margin:auto;padding:58px 24px 80px}.eyebrow{font-size:11px;font-weight:500;letter-spacing:.14em;color:var(--green-dark)}h1,h2,h3,strong{font-weight:500}h1{font-family:ui-serif,Georgia,serif;font-size:54px;letter-spacing:-.045em;line-height:1.02;max-width:750px;margin:10px 0 16px}h2{font-size:28px;letter-spacing:-.03em;margin:5px 0}h3{font-size:16px;margin:0}.lede{font-size:17px;color:var(--muted);max-width:720px;margin:0 0 64px}section{border-top:1px solid var(--line);padding:34px 0 48px}.section-head,.chart-title{display:flex;align-items:flex-end;justify-content:space-between;gap:18px}.chart-title{margin:30px 0 13px;align-items:baseline}.metric-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin-top:28px}.metric-card{min-width:0;border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:16px}.metric-head h3{display:flex;align-items:center;gap:9px;font-family:ui-serif,Georgia,serif;font-size:22px}.metric-head h3 i{display:block;width:14px;height:14px;flex:0 0 14px}.metric-success .metric-head h3 i{background:var(--metric-success)}.metric-tokens .metric-head h3 i{background:var(--metric-tokens)}.metric-time .metric-head h3 i{background:var(--metric-time)}.metric-head p{min-height:38px;margin:8px 0 0;color:var(--muted);font-size:12px}.metric-chart{display:block;width:100%;height:auto}.metric-gridline{stroke:var(--line);stroke-width:1;stroke-dasharray:2 4}.metric-axis,.metric-model,.metric-value{fill:var(--text);font-size:10px}.metric-axis{fill:var(--muted)}.metric-value{font-weight:500}.metric-bar{shape-rendering:crispEdges}.bar-direct{fill:var(--bar-direct);background:var(--bar-direct)}.bar-bounded_retry{fill:var(--bar-retry);background:var(--bar-retry)}.bar-maker_verifier{fill:var(--bar-maker);background:var(--bar-maker)}.zero-marker{stroke-width:5;stroke-linecap:square}.zero-marker[data-strategy="direct"]{stroke:var(--bar-direct)}.zero-marker[data-strategy="bounded_retry"]{stroke:var(--bar-retry)}.zero-marker[data-strategy="maker_verifier"]{stroke:var(--bar-maker)}.metric-legend{display:flex;justify-content:center;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:11px}.metric-legend span{display:flex;align-items:center;gap:5px}.legend-swatch{display:block;width:9px;height:9px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:var(--panel)}th,td{padding:12px 14px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:500}th:first-child,td:first-child{text-align:left}td strong{display:block}small{display:block;color:var(--muted)}.score{min-width:240px;position:relative}.score span{position:relative;z-index:1;font-variant-numeric:tabular-nums}.score i{position:absolute;left:12px;bottom:7px;height:3px;background:var(--green);display:block}.split{display:grid;grid-template-columns:1.08fr .92fr;gap:36px}.scatter{width:100%;background:var(--panel);display:block}.scatter line{stroke:var(--line);stroke-width:1}.scatter .grid{stroke-dasharray:3 5}.scatter text{fill:var(--text);font-size:11px}.scatter .axis{fill:var(--muted)}.point{fill:var(--green-dark);stroke:var(--panel);stroke-width:2}.point.bounded_retry{fill:var(--green)}.point.maker_verifier{fill:var(--amber)}.positive{color:var(--green-dark)}.negative{color:var(--red)}.heat-title{margin-top:42px}.heatmap .heat{text-align:center;font-weight:500;min-width:110px}.heat.pass{background:color-mix(in srgb,var(--green) 26%,var(--panel))}.heat.mixed{background:color-mix(in srgb,var(--amber) 24%,var(--panel))}.heat.fail{background:color-mix(in srgb,var(--red) 15%,var(--panel))}.empty-cell{color:var(--muted)}.serving{margin-top:12px}.empty{border:1px dashed var(--line);padding:22px;margin-top:25px;display:grid;gap:5px}.empty strong{font-size:16px}footer{border-top:1px solid var(--line);padding-top:20px;font-size:12px;overflow-wrap:anywhere}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}@media(max-width:980px){.metric-grid{grid-template-columns:1fr}.metric-head p{min-height:0}}@media(max-width:760px){header{padding:0 16px}.meta{display:none}main{padding:38px 16px 60px}h1{font-size:40px}.lede{margin-bottom:48px}.split{grid-template-columns:1fr}.section-head,.chart-title{align-items:flex-start;flex-direction:column;gap:4px}.metric-card{padding:12px}.table-wrap{overflow:visible}th,td{padding:10px 8px;white-space:normal}.leaderboard th:nth-child(3),.leaderboard td:nth-child(3),.leaderboard th:nth-child(5),.leaderboard td:nth-child(5),.leaderboard th:nth-child(6),.leaderboard td:nth-child(6){display:none}.leaderboard .score{min-width:120px}.comparison-leaderboard th:nth-child(3),.comparison-leaderboard td:nth-child(3){display:table-cell}.comparison-leaderboard th:nth-child(5),.comparison-leaderboard td:nth-child(5),.comparison-leaderboard th:nth-child(6),.comparison-leaderboard td:nth-child(6),.comparison-leaderboard th:nth-child(7),.comparison-leaderboard td:nth-child(7){display:none}.comparison-leaderboard .score{min-width:80px}.transitions th:nth-child(2),.transitions td:nth-child(2),.transitions th:nth-child(6),.transitions td:nth-child(6){display:none}.paired th:nth-child(4),.paired td:nth-child(4),.paired th:nth-child(5),.paired td:nth-child(5){display:none}.heatmap{table-layout:fixed}.heatmap th,.heatmap td{font-size:10px;overflow-wrap:anywhere;padding:8px 4px}.heatmap th:first-child{width:38%}.heatmap .heat{min-width:0}}@media(prefers-color-scheme:dark){:root{--bg:#101310;--panel:#171b18;--text:#eef4ef;--muted:#a4aea6;--line:#303731;--green:#74e45f;--green-dark:#64cf55;--amber:#e5bd55;--red:#eb7770;--bar-direct:#f1f3f1;--bar-retry:#6bd47d;--bar-maker:#f58bb0}}
"""
