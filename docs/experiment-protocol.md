# Experiment protocol

This file records the accepted v0.1 protocol. The proposed v0.2 confirmatory
design resolves the qualification controller's edit-capable verifier, repeated
temperature-zero seeds, candidate preservation, and candidate-level verifier
diagnostics. See [`experiment-design-v0.2.md`](experiment-design-v0.2.md). Until
ADR 009 is accepted and implemented, v0.1 results must retain their exact tested
controller label.

## 1. Experimental tracks

### Effectiveness track

The primary comparison measures objective task success under a shared logical budget vector. It isolates the effect of controller strategy.

### Serving track

The serving benchmark measures fixed request shapes and isolates backend or configuration changes. It does not run an agent and makes no reasoning-quality claim.

### Deployment track

An optional later study constrains wall time or energy. Every result is checked against the declared physical cap; an energy-capped run is invalid without an energy measurement. This track intentionally couples serving and strategy effects and must be reported separately.

## 2. Strategies

### Direct

One continuous maker context may inspect files, edit code, and run public tests until it returns `FINAL` or exhausts its budget. There is no controller restart or independent verifier.

### Bounded retry

Up to three fresh maker episodes operate on the same evolving worktree. After each `FINAL`, the controller runs public tests. On failure, the next episode receives a deterministic packet containing:

- original task;
- current diff;
- last public-test result;
- previous action summaries;
- remaining budget.

Controller summaries are deterministic. Any model-generated compression counts against the budget.

### Maker-verifier

A maker proposes a patch. A fresh, read-only verifier receives the task, diff, and public-test evidence but not the maker conversation. It returns structured `APPROVE`, `REJECT`, or `ESCALATE`.

A rejection may trigger another maker revision, with at most two maker-verifier cycles in the MVP. The verifier cannot edit files or inspect hidden tests. Start with a 75/25 maker/verifier completion-token reservation, tune it only on the calibration set, and freeze it before evaluation.

## 3. Shared logical budget

Every strategy receives the same task-level caps:

- cumulative rendered prompt tokens, including repeated prefixes and tool output;
- cumulative completion tokens;
- maximum model calls;
- maximum tool calls;
- maximum public-test executions;
- per-call context limit.

Each result records `max_call_context_tokens`, the largest input-plus-generated context observed in any one call, so the final cap can be audited. Aggregate tokens, call count, and this maximum must be arithmetically consistent. Runs that never issue a model request record zero for all three token fields and the maximum.

Logical input tokens count even when the server gets a physical prefix-cache hit. Controller-triggered public tests count against the test budget. Hidden evaluation happens after the run and never returns information to an agent.

A generous wall timeout only terminates hangs in the effectiveness track. Wall time is not its primary budget.

Calibrate small, medium, and large tiers on a disjoint task set. One defensible starting procedure is median, two times median, and four times median successful direct-episode consumption, rounded to explicit absolute caps. Publish the frozen caps.

## 4. Task suite

Start with six self-contained Python repair tasks, then grow to at least 30:

- 12 localized logic, parsing, boundary, or error-handling defects;
- 8 cross-file contract or state-management defects;
- 6 diagnosis tasks with realistic noisy output;
- 4 adversarial tasks whose superficial fix passes visible tests but fails hidden cases.

Use generated seeded mutations, reconstructed permissively licensed bugs, and verifier-focused adversarial tasks. Report those sources separately.

Every task must have:

- a clean initial Git commit;
- offline deterministic dependencies and a lockfile;
- agent-visible public tests;
- hidden tests stored outside the agent worktree;
- a validated gold patch;
- allowed and prohibited path rules;
- explicit commands and timeouts.

## 5. Primary endpoint

`objective_success = true` only when the final patch:

1. applies cleanly;
2. passes all public and hidden tests;
3. changes no prohibited files;
4. satisfies any task-specific invariant.

The agent's or verifier's declared success is never the primary endpoint.

## 6. Confound controls

- Pin model, revision, quantization, tokenizer, chat template, server, flags, controller, and prompts.
- Reset model-side state and create a fresh task worktree for every run.
- Give each strategy identical allowlisted tools.
- Render and store the exact request sent to each backend.
- Randomize run order in blocks of task, strategy, budget, and seed.
- Use at least three paired seeds for confirmatory claims.
- Run on AC power with low-power mode disabled and no competing workloads.
- Record chip, cores, unified memory, macOS build, thermal state, model checksum, runtime checksum, and flags.
- Warm the server before measurement and define a cooldown rule.
- Treat cache hits as physical savings, never logical token savings.
- Ban edits to controller, evaluator, public-test, and hidden-test paths as appropriate.

## 7. Serving qualification

Use identical rendered prompts rather than semantically similar chat payloads. For each selected operating point:

- prompt lengths: 512, 4K, and 16K tokens;
- generation lengths: 128 and 512 tokens;
- concurrency: one for the baseline;
- fixed seed and sampling configuration;
- at least three warm-ups and ten measured repetitions;
- explicit cold-load and warm-resident phases.

Record TTFT, prefill and decode throughput, end-to-end latency, load time, memory pressure, swap, CPU/GPU residency if observable, and thermal state. Energy remains optional until a reproducible low-distortion collector exists.

## 8. Analysis

The MVP is descriptive. For the larger benchmark:

- preserve task-strategy-budget-seed pairing;
- report success rates and paired percentage-point differences;
- bootstrap by task while retaining all seeds for a sampled task;
- report 95% confidence intervals;
- use a mixed-effects logistic model only as a secondary analysis;
- report budget-exhaustion and invalid-run counts, not just completed successes.

Pre-register medium-budget contrasts:

1. bounded retry minus direct;
2. maker-verifier minus bounded retry.

## 9. Reproducibility manifest

Each run must link to hashes or immutable identifiers for:

- hardware and operating system;
- experiment plan and task state;
- model, tokenizer, template, and quantization;
- runtime binary or source revision and flags;
- controller and strategy prompt;
- raw append-only events;
- final diff and evaluator output.

The validated experiment manifest stores the model and backend artifact SHA-256 values, exact server command, and non-secret environment. The validator rejects obvious secret-bearing environment names and command flags, but this is only a guardrail: review every value and command before publishing. Do not put API tokens, passwords, or credentials in a manifest.

Published summaries are derived artifacts. Raw events are never rewritten to match a desired table.
