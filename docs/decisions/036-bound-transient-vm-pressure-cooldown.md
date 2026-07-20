# ADR 036: Bound transient VM-pressure cooling before image mutation

- Status: Accepted before scoring
- Date: 2026-07-20
- Scope: v0.7 production image-build admission retry classification
- Supersedes: ADR 035 retry classification only

## Context

Attempts 13 and 14 exercised the prelaunched exact-ID steward added after ADR
035. In both attempts, preflight observed pressure level `1`; the first
admission sample observed only the two configured non-benchmark containers;
and the steward stopped and reconciled exactly those identities. The next
sample, approximately 30 seconds later, observed no running container but VM
pressure level `2`. Under ADR 035 that pressure-only sample was an immediate
hard denial, so both fresh journals sealed before Docker identity, image work,
model loading, calibration, or a model prompt.

The repeated sequence distinguishes a bounded host-cooling transition from an
admissible baseline. Restarting another fresh production process would discard
that distinction and turn the same transition into operator-managed retries.
Changing the admitted pressure threshold would instead weaken the host-safety
contract.

## Decision

Advance the outer runner to
`intercode-v0.7-production-runner-v6-bounded-pressure-cooldown` and the
admission journal to
`intercode-v0.7-image-build-admission-journal-v2`.

A denied admission sample is retryable only when its complete reason set is
one of:

1. `VM_PRESSURE`;
2. `RUNNING_CONTAINERS`; or
3. `VM_PRESSURE` plus `RUNNING_CONTAINERS`.

Whenever `VM_PRESSURE` is present, the raw sample's VM pressure level must be
exactly `2`. Raw levels `0`, `3`, and `4` are hard denials even when no other
reason is present. Whenever `RUNNING_CONTAINERS` is present, the observed set
must be nonempty and must be a subset of the configured pair of full stewarded
container IDs. This permits the external steward to stop an already
inventoried container while the host is also cooling; production still
performs no container mutation. An unknown container, a container without an
exact configured steward set, or any reason outside the three listed above is a
hard denial.

Every retryable denial resets the allowed-sample streak. Admission still
requires two consecutive fully allowed samples with VM pressure level exactly
`1`, no resident model, no running container, and all other frozen host checks
passing. Samples remain 30 seconds apart and the whole stabilization remains
bounded by 600 seconds. Thus pressure level `2` may be observed while waiting,
but can never become the accepted image-build baseline. Timeout and every
telemetry, liveness, boot, identity, ordering, cooldown-pair, or journal
ambiguity remain fail-closed.

The v2 journal declaration pins `retryable_vm_pressure_levels: [2]`, and every
sample records its raw pressure level and derived retry classification. The
journal is sealed and reverified before image planning. Its revision boundary
prevents v1 journals from being interpreted under the amended semantics.

## Consequences

- Transient pressure cooling is accounted for inside one bounded attempt
  rather than converted into unbounded operator restarts.
- The admitted host threshold is unchanged: image mutation still requires two
  clean pressure-level-`1` samples.
- Only warning-level raw pressure `2` is a waitable cooling observation; raw
  levels `0`, `3`, and `4` remain fail-closed.
- External authority remains narrow: only the exact pre-inventoried containers
  can be reconciled, and production never mutates them.
- Task, model, arm, prompt, controller, evaluator, budget, and scoring rules are
  unchanged.
- Attempts 13 and 14 were sealed zero-prompt failures. This amendment was
  therefore frozen without model output or effectiveness outcomes.
- Uplift claim: **not permitted**
