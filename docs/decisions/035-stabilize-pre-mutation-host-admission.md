# ADR 035: Stabilize pre-mutation host admission inside production

- Status: Accepted before scoring
- Date: 2026-07-20
- Scope: v0.7 production image-build admission and raw safety evidence

## Context

Production attempts 8 through 10 passed an external stable-host window but
were denied by the runner's one-shot full admission before image build. Docker
Desktop can restart two previously inventoried AgentGPT containers while the
daemon is handed from the external steward to production. A 250-millisecond
polling supervisor and a persistent read-only `docker events` stream did not
close that wake race. Attempt 10 still created no image manifest, loaded no
model, and issued no model prompt.

Treating those containers as expected benchmark resources would weaken the
empty-host contract. Repeatedly restarting the whole production process would
discard the denied raw sample and add an unbounded operational retry. Having
the production runner stop containers would also mix external-resource
mutation into the benchmark authority.

## Decision

Advance the outer runner to
`intercode-v0.7-production-runner-v5-admission-stabilization`. Immediately
before image planning or build, it performs one bounded, read-only admission
stabilization and records every decision.

The configuration accepts either no stewarded container IDs or exactly two.
Every supplied ID must be a full 64-character lowercase hexadecimal Docker ID,
and the tuple must be sorted and unique. These IDs are an allowlist for an
external steward to reconcile an already inventoried wake race; they are not
expected benchmark resources. Policy evaluation always uses an empty
`ExpectedHostResources`, so the admitted baseline still requires no resident
model and no running container.

One denied sample may be retried only when all of the following hold:

1. the policy's sole denial reason is `RUNNING_CONTAINERS`;
2. the observed running-container set is nonempty; and
3. every observed ID belongs to the configured stewarded pair.

With no configured pair, any running container is therefore a hard denial. An
unknown container, a mixed container-and-pressure denial, any other host-policy
reason, runtime-liveness or telemetry failure, boot change, sample-order
failure, or cooldown-pair failure immediately appends a stopped terminal,
seals the journal, and fails production. Production never stops, restarts,
pauses, removes, updates, or changes the restart policy of a container.

After a retryable denial, production waits for the external steward while
continuing read-only collection. Admission requires two consecutive fully
allowed samples separated by the frozen 30-second interval, under the existing
empty-resource cooldown policy and identical boot and Docker identities. The
total stabilization timeout is 600 seconds. These `30 / 2 / 600` values are
runtime pins, not operator-selected arguments.

The runner creates the admission journal at an absent pathname with `O_EXCL`
and `O_NOFOLLOW` under the retained owner-mode-`0700` records directory. The
journal is an owner-owned, mode-`0600`, identity-bound regular file. It appends
the path-free declaration, complete raw host samples, derived policy and retry
decisions, and one completed or stopped terminal through the existing hash
chain. File and parent identity are rechecked around writes and reads. The
terminal journal is sealed, reopened, and reverified; a successful baseline is
re-derived from those sealed records before image planning can begin.

## Consequences

- The runner can observe an external exact-ID reconciliation without granting
  the unrelated containers admission or mutating them itself.
- A single retryable wake race becomes append-only evidence rather than a lost
  one-shot denial; every other unsafe state remains fail-closed.
- Image, tokenizer, task, prompt, controller, evaluator, arm, budget, and
  scoring semantics are unchanged.
- The amendment precedes image qualification, calibration, model loading, and
  every model prompt, so it cannot be informed by effectiveness outcomes.
- A crash leaves a fresh unsealed attempt as diagnostic evidence; the outer
  production runner still has no resume mode.
