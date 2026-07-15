# Result evidence layout

## Current result

Open [`OPEN-ME/index.html`](OPEN-ME/index.html). It is the canonical,
self-contained v0.4 comparison for the two local models that passed the host
safety gate.

Machine-readable evidence is stored beside it under `OPEN-ME/current/`.

## Evidence tiers

| Tier | Path | Git policy | Purpose |
| --- | --- | --- | --- |
| Current | `OPEN-ME/` | committed | One unambiguous human entry point and derived JSON |
| Published | `published/` | committed | Immutable historical HTML/JSON snapshots |
| Raw | `v0.4/*/raw/` | local only | Append-only model events and run records |
| Runtime | `v0.4/*/work/` | local only | Disposable task worktrees |
| Evidence | `v0.4/*/evidence/` | local only | Guard output and host observations |

Raw events are not committed because they contain complete model output. The
published comparison JSON contains pinned manifest metadata, derived run
records, coverage, arm summaries, and paired transitions without model prose.
SHA-256 digests and line counts for the local raw files are recorded in
[`docs/runs/v04-goal-skill-loop-pilot.md`](../docs/runs/v04-goal-skill-loop-pilot.md).

## Regenerate v0.4

```bash
PYTHONPATH=src python3 -m edgeloopbench compare \
  --experiment configs/experiments/v0.4/pilot-phi4-mini.toml \
    results/v0.4/pilot-phi4-mini/raw/runs.jsonl \
  --experiment configs/experiments/v0.4/pilot-qwen35-4b.toml \
    results/v0.4/pilot-qwen35-4b/raw/runs.jsonl \
  --output results/v0.4/comparison
```

Never edit JSONL evidence in place. Rerunning a manifest appends missing run
identities and skips completed ones.
