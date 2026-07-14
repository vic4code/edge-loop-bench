# Runnable experiment specification

Status: **accepted for implementation**
Last updated: 2026-07-14

## Objective

Add the first Mac-native agent-effectiveness experiment to the v0.1 analysis
scaffold. The experiment must run offline after model installation, expose only
public task material to the agent, preserve append-only evidence, and report
loop gains separately from serving efficiency.

## Initial operating point

- Host: Apple M4 MacBook Air, 16 GB unified memory.
- Runtime: Ollama 0.31.1 bound to `127.0.0.1` with cloud access disabled.
- Control model: the exact local artifact currently published as
  `qwen3.5:4b`; freeze its resolved digest before measured runs.
- Context qualification: start at 4,096 tokens and advance to 8,192 only
  after memory and swap checks pass.
- Concurrency: one model request and one loaded model.
- Weight quantization and KV-cache quantization remain distinct manifest
  fields. The initial KV-cache setting is `q8_0`.
- Ollama's thinking mode is an explicit, pinned request field. The control
  shakeout starts with thinking disabled; a thinking-enabled comparison is a
  separate controller ablation, not an unrecorded backend default.

`qwen3.5:9b` is a later primary-model candidate, not part of the control-model
shakeout. Kimi may be evaluated only through a separately labeled
cloud-assisted adapter on this host; it cannot be pooled with local serving
results.

## MicroRepair-6

The first suite contains six deterministic Python standard-library repairs:

| ID | Category | Defect shape |
| --- | --- | --- |
| `python-localized-001` | localized | boundary condition |
| `python-localized-002` | localized | parsing or error handling |
| `python-cross-file-001` | cross-file | caller/implementation contract |
| `python-cross-file-002` | cross-file | coordinated state update |
| `python-diagnosis-001` | diagnosis | root cause behind noisy public output |
| `python-adversarial-001` | adversarial | superficial public-test fix misses an edge case |

Each task is reset to a pinned initial Git commit and contains only the task
statement, editable source, public tests, license/provenance, and the public
manifest. The evaluator materializes hidden tests and the validated gold patch
in a separate temporary root that is never a descendant of, symlinked into,
named in, or passed to the agent worktree. Agent-visible logs must not contain
evaluator paths or hidden-test names.

The initial six-task suite is a harness qualification dataset. Generated
mutations, reconstructed bugs, and verifier-adversarial tasks are reported as
separate source strata and are not pooled into a confirmatory claim.

## Strategy matrix

Run `direct`, `bounded_retry`, and `maker_verifier` with one identical logical
budget vector for each budget tier. The shakeout matrix is:

```text
6 tasks x 3 strategies x 2 budget tiers x 2 seeds = 72 runs
```

Two seeds are sufficient only for pipeline qualification. Confirmatory claims
require at least three paired seeds, as specified in the experiment protocol.
Every fresh arm starts from the same task commit and resets model-side
conversation state.

## Commands

The implementation will add these commands without changing the existing
validation and summary interfaces:

```text
edgeloop task prepare <task-id> --work-root <path>
edgeloop task public-test <worktree>
edgeloop run <experiment.toml> --results <append-only.jsonl>
edgeloop report <runs.jsonl> --manifest <experiment.toml> --output <directory>
```

Task preparation and public tests require no network. A measured `run` may
talk only to the loopback model endpoint declared in its manifest. `report`
is deterministic and offline.

## Testing strategy

- Unit tests validate task manifests, path policies, budgets, event records,
  and derived report data.
- Integration tests prepare a task in a temporary directory, prove its public
  test fails initially, apply a fixture candidate patch, and prove public and
  isolated evaluation outcomes.
- Tests use a deterministic fake model adapter. `make check` never requires a
  model server, downloaded weights, network, or privileged access.
- A manual local smoke test validates the real Ollama adapter before measured
  runs but is not part of `make check`.
- Candidate edits use a pinned JSON schema containing full-file replacements.
  The runner validates every path against the task allowlist before writing.
  Unified-diff syntax is not scored as a proxy for repair ability.

## Reporting contract

The report uses a clean, high-density visual style inspired by modern model
analysis sites while retaining EdgeLoopBench branding. It produces static,
self-contained HTML plus machine-readable JSON from validated results.

Required views:

1. a study snapshot naming the dataset, task count, seeds, budgets, models, and
   total episode count;
2. a result-derived conclusion that separates measured findings from design
   limitations and does not generalize beyond the declared suite;
3. an agent-visible task catalog with category, source stratum, repair contract,
   and evaluation boundary, without evaluator identifiers or gold details;
4. verified-success leaderboard with observed counts;
5. paired task-level direct-to-loop transitions;
6. verified success versus logical tokens and versus wall time;
7. marginal gain by model call or retry;
8. task-strategy-budget heatmap;
9. a separate serving panel for TTFT, decode rate, latency, memory, and energy.

Charts must label higher/lower-is-better direction, sample size, model digest,
runtime version, context, weight quantization, KV-cache quantization, and local
versus cloud-assisted execution. Agent effectiveness and serving efficiency
must not be collapsed into one score.

When the tested controller differs from the preregistered target design, the
HTML must name the tested behavior beside the conclusion. In particular, a
review-and-revise second edit call must not be described as evidence about an
independent read-only verifier.

## Boundaries

- Always append raw events and derive summaries and charts from them.
- Always count logical prompt tokens even when Ollama reuses a prefix cache.
- Preserve Ollama `thinking` and final-response text as separate raw fields;
  its completion-token count covers the complete generated stream and is not
  reconstructed from either visible string.
- Always keep the model-facing worktree free of evaluator assets and paths.
- Ask before downloading another multi-gigabyte model or changing privileged
  macOS memory settings.
- Never allow a benchmark task to access the network.
- Never call a two-seed shakeout a statistically confirmed improvement.

## Success criteria

1. `make check` passes without Ollama running.
2. All six initial tasks can be prepared and reset byte-for-byte on this Mac.
3. Initial public tests fail for the intended reason and validated gold
   patches pass isolated evaluation.
4. A fake-model 72-run matrix produces append-only records and a deterministic
   report.
5. At least one real Qwen3.5 4B smoke run records the resolved model digest,
   logical tokens, duration, and final patch outcome.
6. The report keeps local serving measurements separate from any Kimi
   cloud-assisted agent result.
