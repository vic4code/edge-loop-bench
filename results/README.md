# EdgeLoopBench local results

## Open this

The single current result entry point is:

[`OPEN-ME/index.html`](OPEN-ME/index.html)

It is the results hub. The page lists the current Qwen3.5 4B v0.3 evidence-gated
confirmatory report, the earlier v0.2 report, and every historical v0.1 model report without pooling incompatible
experiments. The current generated report and machine-readable data are under
`OPEN-ME/current/`.

## Directory map

| Path | Purpose |
| --- | --- |
| `OPEN-ME/` | Current canonical human-readable result |
| `v0.3/raw/` | Append-only v0.3 calibration and confirmatory JSONL |
| `v0.2/raw/` | Append-only calibration and confirmatory JSONL |
| `v0.2/report/` | Preserved earlier v0.2 confirmatory HTML |
| `v0.2/evidence/` | Host, power, VM, and Ollama boundary snapshots |
| `archive/v0.1/raw/` | Older qualification JSONL |
| `archive/v0.1/reports/` | Older qualification HTML reports |
| `archive/v0.1/investigations/` | One-off seed and retry diagnostics |
| `scratch/` | Interrupted disposable worktrees; not evidence |

Do not edit JSONL results in place. Regenerate the current report from raw data:

```bash
PYTHONPATH=src python3 -m edgeloopbench report \
  results/v0.3/raw/confirmatory/qwen35-4b-runs.jsonl \
  --manifest configs/experiments/v0.3/confirmatory-qwen35-4b.toml \
  --output results/OPEN-ME/current
```
