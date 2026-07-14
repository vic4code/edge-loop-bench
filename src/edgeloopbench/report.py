"""Deterministic, self-contained HTML reporting for agent effectiveness."""

from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .config import ExperimentPlan
from .results import RunRecord, SummaryReport


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
:root{color-scheme:light dark;--bg:#f6f7f4;--panel:#fff;--text:#172019;--muted:#657068;--line:#dfe4df;--green:#62d84e;--green-dark:#268b35;--amber:#e0b23f;--red:#d95d55}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}header{height:58px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100% - 1180px)/2));background:color-mix(in srgb,var(--bg) 92%,transparent)}.brand{color:var(--text);text-decoration:none;font-weight:500;letter-spacing:-.04em}.brand span{color:var(--green-dark)}.meta,.direction,.chart-title span,footer,.empty span{color:var(--muted)}main{max-width:1180px;margin:auto;padding:58px 24px 80px}.eyebrow{font-size:11px;font-weight:500;letter-spacing:.14em;color:var(--green-dark)}h1,h2,h3,strong{font-weight:500}h1{font-size:46px;letter-spacing:-.045em;line-height:1.05;max-width:750px;margin:10px 0 16px}h2{font-size:28px;letter-spacing:-.03em;margin:5px 0}h3{font-size:16px;margin:0}.lede{font-size:17px;color:var(--muted);max-width:720px;margin:0 0 70px}section{border-top:1px solid var(--line);padding:34px 0 48px}.section-head,.chart-title{display:flex;align-items:flex-end;justify-content:space-between;gap:18px}.chart-title{margin:30px 0 13px;align-items:baseline}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:var(--panel)}th,td{padding:12px 14px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:500}th:first-child,td:first-child{text-align:left}td strong{display:block}small{display:block;color:var(--muted)}.score{min-width:240px;position:relative}.score span{position:relative;z-index:1;font-variant-numeric:tabular-nums}.score i{position:absolute;left:12px;bottom:7px;height:3px;background:var(--green);display:block}.split{display:grid;grid-template-columns:1.08fr .92fr;gap:36px}.scatter{width:100%;background:var(--panel);display:block}.scatter line{stroke:var(--line);stroke-width:1}.scatter .grid{stroke-dasharray:3 5}.scatter text{fill:var(--text);font-size:11px}.scatter .axis{fill:var(--muted)}.point{fill:var(--green-dark);stroke:var(--panel);stroke-width:2}.point.bounded_retry{fill:var(--green)}.point.maker_verifier{fill:var(--amber)}.positive{color:var(--green-dark)}.negative{color:var(--red)}.heat-title{margin-top:42px}.heatmap .heat{text-align:center;font-weight:500;min-width:110px}.heat.pass{background:color-mix(in srgb,var(--green) 26%,var(--panel))}.heat.mixed{background:color-mix(in srgb,var(--amber) 24%,var(--panel))}.heat.fail{background:color-mix(in srgb,var(--red) 15%,var(--panel))}.empty-cell{color:var(--muted)}.serving{margin-top:12px}.empty{border:1px dashed var(--line);padding:22px;margin-top:25px;display:grid;gap:5px}.empty strong{font-size:16px}footer{border-top:1px solid var(--line);padding-top:20px;font-size:12px;overflow-wrap:anywhere}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}@media(max-width:760px){header{padding:0 16px}.meta{display:none}main{padding:38px 16px 60px}h1{font-size:35px}.lede{margin-bottom:48px}.split{grid-template-columns:1fr}.section-head,.chart-title{align-items:flex-start;flex-direction:column;gap:4px}.table-wrap{overflow:visible}th,td{padding:10px 8px;white-space:normal}.leaderboard th:nth-child(3),.leaderboard td:nth-child(3),.leaderboard th:nth-child(5),.leaderboard td:nth-child(5),.leaderboard th:nth-child(6),.leaderboard td:nth-child(6){display:none}.leaderboard .score{min-width:120px}.paired th:nth-child(4),.paired td:nth-child(4),.paired th:nth-child(5),.paired td:nth-child(5){display:none}.heatmap{table-layout:fixed}.heatmap th,.heatmap td{font-size:10px;overflow-wrap:anywhere;padding:8px 4px}.heatmap th:first-child{width:38%}.heatmap .heat{min-width:0}}@media(prefers-color-scheme:dark){:root{--bg:#101310;--panel:#171b18;--text:#eef4ef;--muted:#a4aea6;--line:#303731;--green:#74e45f;--green-dark:#64cf55;--amber:#e5bd55;--red:#eb7770}}
"""
