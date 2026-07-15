# ADR 012: Use qualified InterCode-Bash for causal loop evaluation

- Status: Accepted for implementation; measured scoring gated
- Date: 2026-07-15
- Pre-calibration amendment: 2026-07-15

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
Reproduce the upstream scalar from a dedicated trusted change-record surface:
parsed `(path, status)` units and identities of the exact hash-command outputs.
Do not reuse strict metadata equality for the scalar; upstream `M`/`D` omissions
and untracked-directory weighting remain visible weaknesses. Qualification
excludes gold rows whose upstream observation cannot be reproduced safely.
Candidate-side unsupported observations are frozen zero-score model failures
inside the denominator, never infrastructure exclusions; safely tokenized
nonstandard status pairs remain in the upstream-compatible diff set.

Use Qwen3.5 4B as the primary local model and calibrate Phi-4-mini as a possible
replication. Pin model revision, runtime, tokenizer/chat template, weight
quantization, and KV-cache quantization separately. Use two paired replicate
schedules, cumulative logical-token budgets, append-only events, fresh
containers, block-balanced ordering, and hard host-safety gates.

Qualify all 200 source rows, but do not attempt performance scoring on every
qualified row on this 16 GB host. Before model output, draw a fixed 50-task
stratified hash-randomized sample (`15/13/15/7` across fs1..fs4) from the qualified
manifest with a precommitted hash ranking. Run both calibration-qualified models
on that same sample, seeds `[11,29]`, and `K = 6`. Qwen remains the sole primary
estimand; Phi is replication. Weight the primary contrast back to actual
qualified stratum sizes and bootstrap task clusters within strata. Keep the
five-point practical threshold, but explicitly treat the study as underpowered
for reliably proving effects near that boundary.

Cap the complete two-model workflow at 4,460 generation requests: 3,800
confirmatory, 304 calibration/pilot, 128 fixed host-load requests, and 228 for
at most 12 one-time block requeues. Evaluate every final selected checkpoint
strictly, while limiting every-checkpoint post-hoc trajectories to a nested
12-task diagnostic subset. This preserves the primary endpoint and makes the
local experiment finite.

Qualification is provenance-gated rather than DTO-gated. A trusted collector
must derive task identity from a source-owned private capability and record
each of the 400 `(task_id, replay_index)` units in its own pin-bound,
hash-chained attempt journal. Sealed completed units survive a reboot; an
interrupted pre-result attempt may be exactly reconciled and replaced once,
while a durable result may be retained only after cleanup is reconciled. The
aggregator requires one sealed completed generation for every unit and seals a
canonical private index. Public manifests are redacted projections with only
an aggregate evidence root and recovery count; replay-level evidence remains
private. A committed, gold-free source-audit artifact binds the complete
frozen static-exclusion reason map.

Before calibration, replace the flattened feedback draft with a strictly
alternating typed `user`/`assistant` transcript. Use restricted deterministic
renderers for only the message roles exercised by this benchmark; reject
system, tool, image, and malformed sequences. Send the completed bytes through
Ollama raw generation with thinking and runtime truncation disabled. This gives
the loop real multi-turn semantics while keeping candidate 1 byte-identical
across arms.

Adapt the pinned Bash environment's controller-owned `self.workdir` and
`simplify_path` behavior through a frozen safe wrapper. Only the literal grammar
`^cd ([A-Za-z0-9._/-]+)$` can change the persistent directory, and only after a
zero exit status. Reject expansions, options, quoting, multiple operands, and
compound `cd` actions as typed model-policy failures instead of reproducing the
upstream string interpolation ambiguity.

Make logical prompt-budget enforcement a preflight boundary. Build
`llama-tokenize` from Ollama v0.31.1's pinned llama.cpp `b9840` source with the
same Ollama compatibility patches, load the same model artifact in vocab-only
mode, and count the fully rendered prompt before HTTP. The model request may
proceed only when the cumulative count is within `B*`; the output cap also must
fit the per-call context. Backend telemetry must equal preflight exactly.
Mismatch or a backend limit violation invalidates the episode as
infrastructure, so it cannot be counted as either loop failure or success.
Add a separately frozen rendered-prompt byte ceiling before tokenization; a
crossing is a budget stop with no request, while the bounded tokenizer LRU is a
physical optimization only and never discounts logical tokens. Derive the
Ollama model tag from the exact rendering profile, and pin the runtime binary,
HTTP timeout, keep-alive, action schema, and every effective v0.31.1 option so
no Modelfile or server default silently changes a measured request.

Represent checkpoints as immutable Docker writable-layer images owned by an
episode-scoped private resource ledger. Keep logical state identity separate
from Docker image identity, and keep evaluator results in a distinct immutable
record bound to both the checkpoint reference and logical state digest. A
typed preregistered default is not labeled evaluator-derived. The trusted
sequence is collect, pause, commit without an additional Docker pause, inspect
the tag/source/parent/full identity, issue an issuer-held one-shot attestation,
and let the checkpoint store consume it against a durable plan that already
binds the exact source container. The adapter receives the verified image only
through the store's non-serializable private completion, then releases the
temporary alias durably and unpauses. Restore always creates a new
container from the exact image and re-attests state and runtime; it never
mutates a live container back into place. A separate private semantic journal
is inode- and owner-bound, never recreated on append, and durably binds the
precommitted evaluator/default policies, results, and cleanup barrier. The
checkpoint store survives environment close, revalidates the exact terminal
journal chain and complete owned-resource set while holding the journal lock
before deletion, and allows final/post-hoc strict evaluation to precede
reverse-order exact cleanup and terminal sealing.

Do not resurrect checkpoint-store objects after a crash. The block journal
must anchor each two-file scope bootstrap before creating the resource ledger
and semantic journal, then bind both genesis roots and their original file and
parent identity digests. A byte-identical inode replacement is not a valid
resume. A completed, terminally sealed four-arm block is resumed
by skipping it; a block with four durable arms plus its post-check may finish a
metadata-only completion transition. Every other interruption invalidates the
whole block. A separate private recovery journal first seals a durable
invalidation barrier and anchors the exact old journal roots, then authorizes
only issuer-attested presence-or-absence reconciliation. Old arm results and
source journals are never rewritten or mixed with a new attempt. One fresh
generation with new run/scope identities is allowed; a second interruption
makes the study incomplete. This preserves matched-arm accounting across
reboots without recreating lost opaque permits or evaluator material.

Preserve the pinned upstream environment's initial Git repository as a
root-owned, read-only `/.git` plus `/.gitignore` in the agent image. It contains
only the public initial filesystem state: no gold command, evaluator helper,
private task capability, or evaluator path. This lets the model inspect the
same public Git baseline available upstream while preventing index or baseline
tampering. Official status collection invokes the image-pinned system Git by
absolute path with fixed arguments and optional locks disabled; candidate
`PATH`, aliases, binaries, configuration, and environment cannot participate.
Gold replay still occurs in a separate fresh offline container, and strict
comparison remains evaluator-only. Each evaluation invocation creates a fresh
candidate replica from the exact checkpoint image and a fresh clean-gold
replica from the scoped original image. Their typed, bounded evidence enters a
source-pinned pure comparator in the trusted host controller only after both
replicas have durable cleanup completions. The comparator cannot execute
commands or inspect paths. The earlier non-scoring `Dockerfile.evaluator`
placeholder is therefore not promoted into an evaluator image and is not a
measured-run input.

The local-host safety policy is outcome-independent and frozen before model
calibration: AC power, Low Power Mode off, normal VM pressure, minimum memory
and disk headroom, swap-growth ceilings, thermal-warning rejection, one exact
resident model, no unrelated container, a two-sample cooldown, and boot-time
identity. AC power and Low Power Mode remain guarded during a running phase,
not only at admission. Every running and cooldown sample must retain the
admission boot identity. A breach stops or recovers a whole block; it never
removes only the slow or unsuccessful arm.

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

### Score every qualified row with three seeds and `K = 10`

Rejected before any model output. Even the minimum 160-task qualified suite
would allow 29,760 two-model generation requests before requeues, with a much
larger failure-retry ceiling and thousands of snapshots/evaluation replicas.
A precommitted 50-task stratified hash-randomized sample, two paired seeds, and
`K = 6` retain the four-arm causal contrast while permitting completion on the
declared local host. The cost is wider uncertainty and no claim to reproduce
the ten-attempt or full-population performance setting.

### Retrofit the existing controller

Rejected. The existing runner sends a complete repository snapshot and applies
full-file edits outside the model. An additive interactive environment boundary
preserves the semantics and reproducibility of v0.1 through v0.4 results.

### Use Docker export, CRIU, or in-place Git reset as a checkpoint

Rejected. Export omits image/runtime metadata and cannot represent deleted
base-layer paths safely; CRIU adds a platform-specific process-state boundary
that is unavailable and unnecessary here; Git reset covers only the upstream
repository view and misses arbitrary writable-layer state. A full immutable
Docker commit plus a separate gold-free logical-state digest matches the state
the model could have changed while still allowing independent verification.

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
- Raw rendering improves byte-level reproducibility but narrows external
  validity: the result applies to the two frozen restricted profiles, not every
  behavior of Ollama `/api/chat` or the models' tool/image renderers.
