# EdgeLoopBench v0.1 specification

Status: **accepted for scaffold implementation**
Last updated: 2026-07-14

## 1. Objective

Build an open, reproducible research harness that can determine when structured agent loops improve objective coding-task success enough to justify their additional inference cost on Apple Silicon.

The v0.1 repository must make the research plan executable before it attempts autonomous code editing. It must define fair comparisons, capture reproducibility metadata, validate experiment manifests, summarize synthetic or real result events, and document viable local-serving paths for a MacBook Air.

## 2. Primary research question

> Under the same logical inference and tool budget, do structured agent loops solve more coding tasks than a single agent episode?

A separate systems question is:

> How efficiently can Apple Silicon serve the request patterns produced by those loops?

The first question concerns agent strategy. The second concerns deployment. Results must remain separate until an explicitly labeled deployment experiment combines them.

## 3. Hypotheses

- **H1:** Bounded retry improves hidden-test success over a single episode at medium budgets when public-test feedback is diagnostic.
- **H2:** Maker-verifier reduces false-success and unsafe-patch rates relative to direct and bounded-retry strategies.
- **H3:** Maker-verifier underperforms at very tight budgets because verification consumes capacity, but can overtake bounded retry at medium or large budgets.
- **H4:** Loop gains are larger for localized deterministic repairs than for ambiguous broad multi-file changes.
- **H5:** A serving backend changes latency, memory, and energy, but should not materially change within-model effectiveness when rendered requests and stopping rules are identical.
- **H6:** Verifier value depends on rejection precision, not rejection frequency.

The two preregistered medium-budget contrasts are bounded retry minus direct and maker-verifier minus bounded retry.

## 4. v0.1 deliverables

### Documentation

- a research protocol and metric contract;
- a model and hardware feasibility matrix;
- setup and optimization notes for Ollama, vLLM-Metal, and MLX-LM;
- architecture decisions that preserve causal interpretability;
- a phased roadmap and explicit completion criteria;
- primary-source links with retrieval dates.

### Machine-readable scaffold

- TOML experiment manifests;
- validation of schema, strategy arms, shared budgets, seeds, and track-specific requirements;
- append-only JSONL result records;
- summary output by strategy and budget;
- paired within-task strategy deltas;
- synthetic examples and standard-library tests.

### Commands

```text
edgeloop validate <experiment.toml> [--json]
edgeloop summarize <runs.jsonl> --manifest <experiment.toml> [--allow-incomplete] [--json]
edgeloop compare --experiment <experiment.toml> <runs.jsonl> [--experiment ...] --output <directory> [--json]
edgeloop doctor [--json]
```

`compare` requires at least two complete, manifest-bound effectiveness
experiments. It varies only the pinned model artifact and rejects differences in
tasks, strategies, seeds, budgets, generation, controller, edit schema, or
backend configuration. Weight quantization and effective context must also
match. Loop deltas are paired against `direct` within each model; agent
effectiveness and serving efficiency remain separate.

`doctor` collects non-privileged host facts and reports runtime executables. It must not install software, download weights, change memory limits, or start servers.

## 5. Proposed repository structure

```text
edge-loop-bench/
  configs/
    experiments/
    runtimes/
  docs/
    decisions/
    serving/
  examples/results/
  results/
  src/edgeloopbench/
  tasks/micro/
  tests/
```

The runnable controller adds adapters, strategies, tool implementations,
schemas, isolated task worktrees, and report modules without changing the v0.1
causal boundary. Its accepted requirements are defined in
[`runnable-experiment-spec.md`](runnable-experiment-spec.md).

## 6. Data contracts

### Experiment manifest

Required fields:

- schema version and stable experiment identifier;
- track: `effectiveness`, `serving`, or `deployment`;
- model identifier, immutable revision or draft placeholder, artifact SHA-256, weight quantization, and context limit;
- backend name, immutable version or draft placeholder, artifact SHA-256, exact command, and environment variables;
- ordered strategies and random seeds;
- task identifiers;
- shared logical budget for effectiveness experiments;
- fixed request shapes for serving experiments.

The model revision and backend version may be explicit placeholders only when the manifest is marked `draft = true`. A publishable plan must pin both.

### Run result

Each JSONL line represents one completed task-strategy-budget-seed run. The v0.1 summary contract requires:

- experiment, task, strategy, budget tier, seed, and source-manifest SHA-256;
- run status and a failure reason for non-completed runs;
- objective success boolean;
- prompt and completion tokens;
- model calls, tool calls, public-test executions, and the maximum context tokens observed in one model call;
- wall-clock seconds;
- energy when required by a deployment budget, otherwise optional energy;
- optional verifier outcomes.

Unknown fields are allowed so raw telemetry can evolve without breaking old summaries. Invalid numeric values and duplicate run keys are rejected. Publishable summaries are bound to the SHA-256 of the exact experiment-manifest bytes: undeclared identities, aggregate counters above a declared budget, per-call context overruns, and deployment wall-time or energy overruns fail. Missing and infrastructure-invalid runs are reported explicitly.

Every summary exposes its manifest binding by experiment. Even exploratory summaries without a supplied plan reject records that reuse one experiment identifier across different manifest digests. Plans are capped at 250,000 measured runs, matching the JSONL loader's record limit, so a valid plan cannot demand an impossible complete result set. Manifest input is capped at 4 MiB before TOML parsing.

## 7. Style and implementation constraints

- Python 3.11+ with standard-library runtime dependencies for v0.1.
- Type hints for public functions and immutable value records where practical.
- Deterministic output ordering.
- Human-readable errors that identify the manifest path or JSONL line.
- JSON output for automation; text output for local inspection.
- No shell execution or HTTP calls in the validation and summary paths.
- No silent defaults for scientifically meaningful fields.

## 8. Tests

The v0.1 test suite must cover:

- valid effectiveness and serving manifests;
- missing, mistyped, or inconsistent fields;
- duplicate strategies, tasks, seeds, and run keys;
- effectiveness plans whose arms do not share one budget vector;
- successful manifest-bound aggregation, status classification, coverage, and paired deltas;
- empty, malformed, non-UTF-8, non-finite, and numerically overflowing result records;
- per-call context and deployment physical-budget enforcement;
- CLI exit codes and machine-readable output;
- host detection that remains safe on non-macOS test environments.

## 9. Scope boundaries

### Included in v0.1

- protocol, documentation, validation, summaries, examples, and CI;
- setup profiles that a researcher can opt into manually;
- hardware and runtime discovery without mutation.

### Deferred

- model downloads and runtime installation;
- unrestricted repository editing;
- task sandboxing and hidden-test execution;
- actual agent strategy execution;
- energy collection requiring elevated privileges;
- a leaderboard or cross-machine ranking;
- automated privileged changes to macOS wired-memory limits.

## 10. Success criteria

The scaffold is complete when:

1. `make check` passes on Python 3.11+ without a running model server.
2. The sample effectiveness plan validates.
3. A malformed or unfair plan fails with a precise error.
4. Synthetic JSONL produces deterministic per-arm summaries and paired deltas.
5. Runtime and model guidance distinguishes official facts, upstream claims, estimates, and future local measurements.
6. A new contributor can identify the first runnable experiment and its 72-run matrix from the documentation alone.
7. The project idea and decisions are saved in the user's Obsidian vault.

## 11. Open questions for the first real run

- The first host is the inventoried M3 MacBook Pro (Mac15,3) with 16 GB unified
  memory and a 10-core GPU.
- Is Gemma 4 E2B or E4B the best first quality target after the Qwen3 4B shakeout?
- Which exact GGUF or MLX revisions are available and license-compatible when the protocol freezes?
- The first paired experiment uses Ollama; vLLM-Metal remains a later serving ablation.
- Can energy be measured reproducibly without changing the workload enough to bias it?

These choices change manifests, not the benchmark's core semantics.
