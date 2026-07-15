# ADR 012: Use qualified InterCode-Bash for causal loop evaluation

- Status: Accepted for implementation; measured scoring gated
- Date: 2026-07-15

## Context

The v0.2 and v0.3 confirmatory suites established that additional attempts can
rescue some small synthetic Python repairs, but they did not establish a broad
loop-engineering advantage. The v0.4 eight-task goal-skill pilot was too small
and its topology was not a faithful implementation of Claude Code `/goal`.

The next study needs an external, loop-native benchmark and a sampling control.
SWE-bench is well known but its official Docker execution envelope is unsafe on
this 16 GB host. InterCode was designed around action, execution observation,
and iterative correction; its Bash source population is a plausible local
candidate pending Docker and sustained-load qualification.

The official Claude loop guide supplies design principles rather than an
experimental topology: repeat until an explicit stop condition, make checks
quantitative, bound token usage, pilot before scaling, and use fresh review when
appropriate. The community Loop Engineering repository adds useful systems
patterns such as isolated worktrees, durable state, budgets, and human gates.
Neither source publishes a controlled uplift estimate, and neither makes
EdgeLoop's rollback packet an official Claude strategy.

Due diligence found several boundaries that prevent a direct unqualified run:

- the paper's 117-task Python set is not a reproducible declared split;
- the Bash Dockerfiles use mutable base tags and only build one filesystem by
  default;
- some Bash tasks require external networking;
- reused containers can retain changes outside Git's reset surface;
- evaluator `info` contains gold-derived details that must not enter prompts;
- the upstream Bash reward has weak file-equivalence behavior.

## Decision

Add a new interactive execution path without changing the legacy MicroRepair
runner. Base v0.6 on the four pinned InterCode NL2Bash files and define the
scored population through repeatable gold replay in fresh offline containers.
Exclude Python from v0.6 and never call the study an InterCode-317 benchmark.

Implement four matched arms:

1. Direct;
2. Independent Verified Sampling;
3. Raw Feedback Loop, adapted from InterCode Try Again;
4. Engineered Loop with a deterministic evidence packet, checkpoint rollback,
   and no-progress guard.

Candidate 1 must be byte-identical across all arms. The benchmark-native scalar
reward is a declared verifier treatment, not ordinary public-test output.
Gold, detailed reward components, evaluator output, and evaluator paths remain
withheld. Report both benchmark-compatible success and a separately frozen
strict final objective; use strict success for EdgeLoop correctness claims.

Use Qwen3.5 4B as the primary local model and calibrate Phi-4-mini as a possible
replication. Pin model revision, runtime, tokenizer/chat template, weight
quantization, and KV-cache quantization separately. Use three paired replicate
schedules, cumulative logical-token budgets, append-only events, fresh
containers, block-balanced ordering, and hard host-safety gates.

## Alternatives considered

### Run only official Single Turn versus Try Again

Rejected as the sole design. It would show that more scored turns help but could
not distinguish extra verified samples from stateful feedback or additional
loop engineering.

### Treat independent pass@K as an autonomous controller

Rejected. Any-checkpoint strict success is an oracle diagnostic unless a
deployable selector exists. The implemented sampling arm is explicitly
evaluator-guided, and its post-hoc strict pass@K remains separately labeled.

### Include the first 117 Python rows

Rejected. The upstream repository contains 974 rows, no 117-task selection
rule, and inconsistent result-artifact coverage. Selecting the first rows would
invent a benchmark split and include MBPP prompting examples.

### Run all 200 Bash rows without qualification

Rejected. Network-dependent and nondeterministic rows violate repository
invariants and would turn infrastructure behavior into model failures.

### Retrofit the existing controller

Rejected. The existing runner sends a complete repository snapshot and applies
full-file edits outside the model. An additive interactive environment boundary
preserves the semantics and reproducibility of v0.1 through v0.4 results.

## Consequences

- A completed study would add external loop-native evidence beyond the
  synthetic repair pilots, while remaining limited to qualified interactive
  Bash tasks with an attempt-level verifier.
- A positive Raw result can establish a stateful interaction-package advantage
  beyond verified sampling; it cannot isolate scalar feedback from persistent
  state and transcript. Only Engineered minus Raw evaluates the new loop package.
- More model calls are visible as test-time compute, never hidden as free retry.
- The upstream reward can stop an episode, but only the strict final endpoint
  supports EdgeLoop correctness claims.
- Measured scoring cannot start until task, image, evaluator, prompt, model,
  budget, schedule, and host-safety identities are committed.
