# ADR 010: Use a small-model confirmatory profile on 16 GB hosts

- Status: Accepted
- Date: 2026-07-15

## Context

The v0.2 design proposed Qwen3.5 4B as a low-resource control, Qwen3.5 9B as
the mid-tier primary model, and Gemma 4 12B as an alternate-family mid-tier
model. The Qwen3.5 4B confirmatory block completed all 90 declared runs.

The 16 GB M3 host then rebooted during the Qwen3.5 9B block. The reboot alone
does not prove a model-caused failure, but controlled one-run resumes provided
enough host-safety evidence to stop: loading the 6.6 GB Ollama artifact reduced
system-wide free memory to roughly 17–20%, and one completed run left about
3.4 GB of swap allocated after the model was unloaded. Gemma 4 12B has a larger
7.6 GB artifact and was not opened for confirmatory execution.

This amendment was recorded before inspecting the completed Qwen3.5 4B
confirmatory aggregate. Raw Qwen3.5 9B events and results remain append-only.

## Decision

Use a small-model-only confirmatory profile on 16 GB hosts:

1. Qwen3.5 4B is the v0.2 primary effectiveness result.
2. Phi-4-mini remains historical qualification evidence unless a future,
   separately identified v0.2 calibration and manifest are preregistered.
3. The 49/90 Qwen3.5 9B partial block is retained as host-safety evidence but
   excluded from effectiveness aggregates and model comparisons.
4. Gemma 4 12B confirmatory execution is not started on this host.
5. The frozen task suite, controller, budgets, endpoints, paired contrasts,
   and practical-benefit threshold do not change.

The resulting inference is deliberately narrow: it can estimate loop effects
for the pinned Qwen3.5 4B artifact on ConfirmatoryRepair-30. It cannot establish
cross-model generalization or a mid-tier deployment result.

## Alternatives considered

### Continue 9B and 12B in smaller batches

Rejected on this host. Smaller batches limit the duration of pressure but do
not remove the unified-memory cost of loading the artifact, and repeated model
loads increased swap even when only one run was requested.

### Treat the 9B partial block as exploratory effectiveness evidence

Rejected. The block is incomplete, interrupted, and selected by host behavior.
Including it would weaken the preregistered pairing and coverage guarantees.

### Replace 9B immediately with Phi-4-mini

Deferred. A new model needs its own disjoint calibration, pinned artifact,
manifest identity, and complete 90-run block. Existing Phi-4-mini qualification
results use the older controller and cannot be pooled with v0.2.

## Consequences

- The completed Qwen3.5 4B block may be opened and reported after this amendment.
- Agent-effectiveness conclusions remain separate from memory and serving data.
- The 9B and 12B manifests remain reproducibility records, not pending work for
  this 16 GB host.
- Cross-model replication remains future work on a safe host or with another
  preregistered small-model artifact.
