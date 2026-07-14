# Research plan

## Thesis

Local inference does not make agent loops free. It converts a variable API bill into bounded hardware, time, memory, thermal, and energy budgets. That conversion is scientifically useful: it makes the cost of retries and verification observable and creates a realistic edge-systems optimization problem.

The strongest project is not “run an agent locally.” It is:

> Measure whether structured agent loops improve verified task success at equal logical cost, then optimize the Apple Silicon serving path without changing the reasoning comparison.

## Workstreams

### A. Agent effectiveness

Compare three controller strategies with the same model, tools, task state, prompts, sampling parameters, and global budget:

- one continuous direct episode;
- bounded retry with deterministic feedback packets;
- maker-verifier with a fresh read-only verifier context.

Use objective hidden evaluation. Model self-reports are telemetry, not ground truth.

### B. Serving efficiency

Replay fixed rendered prompts against one pinned backend and checkpoint at a time. Sweep one independent factor per experiment:

- weight quantization;
- KV-cache precision;
- context length;
- warm versus cold prefix cache;
- memory fraction or cache size;
- speculative decoding;
- request concurrency;
- sustained-load thermal behavior.

This workstream develops practical Ollama, vLLM-Metal, MLX, Metal profiling, and unified-memory skills.

### C. Deployment envelope

After A and B are stable, ask a product-style question: under a fixed wall-time or energy budget, which model, backend, and loop policy produces the most verified successes? Report this as a system result, not a pure reasoning result.

## Phases

### Phase 0 — Protocol and feasibility

- Freeze terms, metrics, and causal boundaries.
- Inventory the host and choose feasible model tiers.
- Pin the first runtime, model revision, tokenizer, and prompt template.
- Build six offline Python tasks and a disjoint calibration set.
- Validate that hidden tests never enter the agent worktree.

Exit criterion: every experimental variable has one owner and one recording field.

### Phase 1 — Serving qualification

- Run fixed shapes at 512, 4K, and 16K prompt tokens.
- Generate 128 and 512 tokens at batch size one.
- Use warm-ups plus at least ten measured repetitions.
- Record TTFT, prefill throughput, decode throughput, p50/p95 latency, load time, memory pressure, swap, and thermal state.
- Reject operating points that swap heavily, throttle unpredictably, or fail repeatedly.

Exit criterion: one stable operating point is selected without using agent-task outcomes.

### Phase 2 — 72-run MVP

- Six evaluation tasks.
- Three strategies.
- Two calibrated budgets.
- Two seeds.

Report descriptive paired results and all failures. Do not claim statistical generality.

Exit criterion: raw events reproduce every aggregate with one command.

### Phase 3 — Benchmark v0.1

- At least 30 tasks across four difficulty categories.
- Small, medium, and large budgets.
- At least three paired seeds.
- Preregistered primary contrasts and bootstrap confidence intervals by task.
- Mutation tasks and reconstructed real bugs reported separately.

Exit criterion: the protocol supports defensible comparative claims.

### Phase 4 — Edge optimization

- Compare cache and quantization ablations on the frozen workload.
- Add vLLM-Metal paged KV and speculative decoding experiments.
- Add MLX-LM prompt-cache and rotating-KV experiments.
- Use bounded Metal profiling only for diagnosed bottlenecks.
- Repeat the most useful operating points across memory tiers or chips.

Exit criterion: each recommended optimization has a measured baseline, resource trade-off, and regression guardrail.

## Decision gates

### Model fit gate

A model advances only if it leaves sufficient unified memory for macOS, runtime allocations, KV cache, task tools, and measurement. Advertised context length and active MoE parameters do not prove fit.

### Quality gate

The small control proves the harness. It does not need to be the final research model. A larger model advances only if its calibration gain justifies the reduction in context or experiment throughput.

### Optimization gate

An optimization advances only if it improves a preregistered target without unacceptable success-rate or stability regression. Upstream performance claims are hypotheses until reproduced on the target MacBook Air.

## Publishable outputs

- A reproducible benchmark protocol and controller.
- An Apple Silicon model/runtime feasibility guide.
- Paired evidence on direct, retry, and maker-verifier strategies.
- Serving ablations for context, KV cache, quantization, and prefix reuse.
- A cost model that reports cloud-equivalent API cost separately from local wall time and energy.
- Raw append-only data and scripts that reproduce all tables.

## Risks

- **Weak local models:** loops may amplify repeated mistakes. This is a result, not a failed project.
- **Thermal bias:** randomized blocked run order and cooldown rules are mandatory.
- **Checkpoint mismatch:** cross-runtime studies may differ in format or quantization; label them end-to-end stack comparisons.
- **Contamination:** use novel generated variants and separate real-bug strata.
- **Token-accounting drift:** rendered prompts and tool output must be logged before requests are sent.
- **Scope explosion:** keep the first task language Python and the first controller independent of large agent frameworks.
