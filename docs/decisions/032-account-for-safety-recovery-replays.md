# ADR 032: Account for safety-recovery replays

- Status: Accepted before scoring
- Date: 2026-07-20
- Supersedes: the Engineered-only replay-accounting scope in ADR 029

## Context

A second adversarial review of the pre-scoring instrument found another
deterministic replay path. After a model-caused policy failure, the trusted
Docker attempt boundary destroys the contaminated container, creates a fresh
one, and replays every previously admitted action so the loop can continue
from the same logical state. ADR 029 counted explicit Engineered checkpoint
restores but did not count these safety-recovery executions.

The smallest counterexample was a Raw loop with one admitted action followed
by a timeout. The model issued two actions, while Docker physically executed
three: the two issued actions plus one deterministic recovery replay. The old
aggregate reported two physical actions. No tokenizer request, model load,
calibration episode, formal episode, or model-dependent outcome existed when
the omission was found.

## Decision

Keep `environment_actions` as the count of model-issued actions. Define
`replayed_environment_actions` as the sum of both deterministic replay paths:

1. actions replayed while restoring an explicit Engineered checkpoint; and
2. previously admitted actions replayed while recovering from a model-caused
   action-policy failure.

The trusted attempt boundary returns the exact safety-recovery replay count in
the typed `ActionExecution`. The replay environment requires that count to
equal its private admitted-action history. The controller records the count on
the append-only `safety_recovery_completed` event before authorizing another
model request. Campaign and calibration verifiers reconstruct the current
replay depth from admissible actions, explicit restores, arm topology, and
event order; a missing, late, negative, or mismatched recovery count fails
closed.

Direct and independent-sampling episodes cannot report recovery replays,
because they do not carry admitted state across model calls. Raw and
Engineered loops may report them. Within one attempt, control flow can perform
either a safety recovery or a post-evaluation checkpoint restore, not both.
For `K = 4` model-issued actions, the shared natural upper bound therefore
remains `K * (K - 1) / 2 = 6` deterministic replay actions. Physical actions
remain model-issued plus all replayed actions; the bound is enforced online
and again by sealed-evidence verification.

The changed semantics advance the controller, episode journal, campaign
ledger, campaign evidence, calibration journal and evidence, execution set,
study evidence, analysis, production runner, manifest schedule, and dependent
test identities. Evidence from the earlier checkpoint-only schemas cannot be
mixed with the corrected run.

## Consequences

- Raw-versus-Engineered tool-cost comparisons include every physical Bash
  execution instead of charging recovery replay only to one arm.
- Logical prompt tokens, model calls, and model-issued actions remain directly
  comparable across arms; deterministic replay overhead is reported
  separately.
- Safety recovery remains model-caused policy evidence, while a failed or
  unverifiable replay remains infrastructure-invalid.
- This is an outcome-independent instrument correction, not treatment tuning.
