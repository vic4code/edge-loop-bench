# ADR 029: Account for physical replay actions before scoring

- Status: accepted
- Date: 2026-07-20
- Replay scope: superseded by ADR 032; checkpoint-topology rules remain active

## Context

An adversarial pre-scoring review found that Engineered rollback recreates a
fresh environment by physically replaying the selected Bash prefix. The old
episode result counted only model-issued actions and checkpoint restores, so a
tool-cost comparison could understate Engineered execution. The same review
found that an above-`+5pp` estimate failing another inferential condition could
be mislabeled `positive_below_practical_threshold`, contrary to the frozen
reporting rule.

No model request, calibration episode, or confirmatory outcome existed.

## Decision

Keep `environment_actions` as the comparable count of model-issued decisions.
Add `replayed_environment_actions` as a distinct count returned by the trusted
restore boundary and recorded in the append-only controller event. Require an
exact positive count on every restore, permit it only for Engineered, and cap
the episode at `K * (K - 1) / 2 = 6`. Derive
`physical_environment_actions` as issued plus replayed and expose all three in
campaign evidence, study evidence, arm summaries, and contrasts.

Bind each restore request and completion to its exact prior checkpoint attempt,
state digest, and replay depth. The campaign and calibration verifiers rebuild
the controller topology rather than trusting those event fields: a restore
must finish before the next model preflight, may occur only on a strict reward
regression, and must target the frozen best checkpoint. Equal reward replaces
the prior best with the latest checkpoint. Terminal Engineered selection must
therefore equal the reconstructed best. Duplicate, late, stale-best, tie, and
wrong-target restores all fail closed even if an attacker rechains the journal
and balances the aggregate counters.

Require `0 < point_estimate < 5.0` as well as a positive interval lower bound
for `positive_below_practical_threshold`. An estimate at or above `+5.0` that
fails any other positive-result condition is
`inconclusive_not_equivalence`.

The accounting field changes the controller, journal, calibration, campaign,
execution-envelope, study-evidence, analysis, production-runner, manifest, and
schedule identities. Production therefore starts only from a fresh clean
source inventory.

## Consequences

- Equal ceilings now mean equal model calls, logical tokens, and model-issued
  actions; physical replay overhead is visible instead of free.
- Human prompts remain exactly observed rather than inferred, while automated
  orchestration costs include deterministic tool replay.
- The classifier cannot use a below-threshold label for an above-threshold but
  statistically unsupported pattern.
- This is a pre-outcome instrument correction, not treatment tuning.
