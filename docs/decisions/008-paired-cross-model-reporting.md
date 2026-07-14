# 008: Require compatible manifests for cross-model loop reports

## Status

Accepted.

## Decision

A cross-model effectiveness report may combine experiments only when task IDs,
strategies, seeds, budget definitions, generation settings, controller revision,
edit schema, and backend configuration are identical. Experiment IDs and pinned
model artifacts must be distinct. Weight quantization and effective context are
held fixed even though the model artifacts differ. The model is the only
experimental factor allowed to vary.

Each model is summarized independently. Loop effects are paired within model by
task, budget tier, and seed. Reports show objective-success deltas, logical-token
and wall-time deltas, plus counts of rescued failures and regressions relative to
the direct strategy. Missing, invalid, or manifest-mismatched runs are rejected
before reports are combined.

Serving-efficiency measurements remain outside this report. They may be shown in
a separate section but must not be folded into an agent-effectiveness score.

## Consequences

- A model cannot receive a different prompt, retry budget, or controller and
  still appear in the same causal comparison.
- Rescues and regressions remain auditable instead of being hidden by an average.
- Backend or GPU optimization ablations require their own serving manifests.
