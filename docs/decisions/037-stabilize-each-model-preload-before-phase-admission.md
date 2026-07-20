# ADR 037: Stabilize each model preload before phase admission

- Status: Accepted before scoring
- Date: 2026-07-21
- Scope: v0.7 calibration and confirmatory model-residency transitions
- Complements: ADR 036 pre-image admission

## Context

Attempt 15 exercised the bounded pre-image cooldown from ADR 036. Its
preflight was allowed at VM pressure level `1` and 50% free memory. The
external operational trace identified a Docker Desktop Resource Saver
cold-wake during the handoff. Independently, the sealed admission journal's
first sample observed pressure level `1` and exactly the two pre-inventoried
non-benchmark containers. After the external exact-ID steward stopped them,
all 20 follow-on samples contained no running container and no resident model,
but remained at pressure level `2` with 40% to 44% free memory. The 600-second
admission gate timed out, sealed, and stopped before image planning, model
loading, calibration, or a model prompt.

That stop is serving- and host-admission evidence, not agent-effectiveness
evidence. It also exposed a later boundary in the then-current phase manager:
after loading a target model, the manager collected only one immediate host
sample and used it as the phase baseline. A model preload can itself create a
short pressure and swap transition. Accepting or rejecting the phase from one
instantaneous post-load sample would make phase admission timing-sensitive;
operator-managed retries would discard that transition rather than account for
it.

## Decision

Advance these instrument revisions before attempt 16:

- runtime factory:
  `intercode-v0.7-production-runtime-factory-v4-issued-residency-receipts`;
- host policy:
  `intercode-v0.7-host-safety-policy-v2-model-preload-stabilization`;
- model-phase manager:
  `intercode-v0.7-model-phase-manager-v2-model-preload-stabilization`;
- production runner:
  `intercode-v0.7-production-runner-v7-model-preload-stabilization`; and
- preload journal:
  `intercode-v0.7-model-preload-admission-journal-v1`.

Every calibration and confirmatory model-residency transition receives one
fresh journal and the same frozen gate. The manager derives, rather than
accepts from its caller, these four fixed fresh paths under the private records
directory:

- `model-preload-admission/calibration-01.jsonl`;
- `model-preload-admission/calibration-02.jsonl`;
- `model-preload-admission/confirmatory-01.jsonl`; and
- `model-preload-admission/confirmatory-02.jsonl`.

Before the residency mutation, the runner captures one fully allowed
transition baseline under the exact current-resource expectation: no resident
model for the first transition, or exactly the active model for a
model-to-model transition, and no running container. The baseline must pass the
complete admission policy immediately, including VM pressure level `1`, at
least 25% free memory, stable pinned runtime and Docker identity, AC power
present, Low Power Mode disabled, and the pinned disk, thermal, performance,
and resource checks. A denied or ambiguous transition baseline stops before
model mutation; it is not waitable.

The synchronous gate invokes the residency transition exactly once. Its
callback must return the exact builder-sealed, issuer-registered residency
receipt; a field-identical copied object carries no authority. Before any
post-load collection, the gate validates the receipt's canonical path-free
record against the expected runtime receipt identity and the previous and
target frozen model IDs, manifest digests, and artifact digests. A transition
exception or receipt mismatch appends a stopped terminal, seals, and returns no
phase authority.

After the operational target-model load completes, and before any benchmark
model prompt, the runner starts a bounded read-only stabilization. A denied
post-load sample is waitable only when the policy's complete reason set is the
single reason `VM_PRESSURE` and the raw VM-pressure dispatch flag is exactly
`2`. Raw levels `0`, `3`, and `4`, any second reason, less than 25% free memory,
an unexpected or missing resident model, any running container, runtime or
Docker identity drift, telemetry or liveness failure, boot change, power,
disk, thermal, performance, ordering, or journal ambiguity stops immediately.
Every waitable pressure sample resets the clean-sample streak.

Acceptance requires two consecutive fully allowed post-load samples that:

1. are at least 30 seconds apart and finish within the 600-second bounded
   stabilization;
2. both have raw VM pressure level `1`, at least 25% free memory, exactly the
   target resident model, and no running container;
3. preserve the transition baseline's boot and pinned runtime/Docker identity;
4. follow no post-load sample that increased swap by more than 1 GiB from the
   pre-transition baseline; and
5. increase swap by no more than 64 MiB from the first accepted sample to the
   second.

The mode-`0600`, identity-bound journal is created at a fresh pathname and
hash-chains `model_preload_admission_declared`, the pre-transition
`model_preload_transition_baseline`, one `model_preload_admission_started`
event containing the validated path-free residency receipt and stabilization
clock, every complete `model_preload_admission_sample` and derived decision,
and one `model_preload_admission_completed` or
`model_preload_admission_stopped` terminal. A sample that exceeds the 1-GiB
transition growth cap is an immediate hard stop; the gate does not wait for a
second sample. The terminal is followed by `journal_sealed`, then reopened and
replay-verified. Only after a completed journal re-derives the accepted pair
may its second accepted sample be installed directly as the phase baseline.
The runner must not collect an unrecorded replacement sample between preload
admission and the first episode. A stop or timeout invalidates the model phase
and authorizes zero benchmark model prompts; a crash leaves an unsealed
diagnostic journal and authorizes no resume or prompt reissue.

## Alternatives considered

### Keep the immediate one-sample phase admission

Rejected because a transient model-load state can determine admission from
collection timing rather than a stable, replayable boundary.

### Admit pressure level 2 after model load

Rejected because it would weaken the phase baseline. Level `2` is only a
bounded wait observation; accepted baselines still require level `1`.

### Retry the complete model transition externally

Rejected because repeated unload/load attempts would be operator-selected,
unbounded serving interventions and would discard the failed transition's raw
evidence.

### Add a fixed sleep and then take one sample

Rejected because elapsed time does not prove resource identity, pressure,
free-memory, or swap stability.

## Consequences

- Attempt 16 cannot reach calibration or confirmatory prompts unless every
  model transition establishes the same sealed, replayable phase boundary.
- Model load and cooling remain serving operations. Their samples, elapsed
  time, and swap changes are not strict-success observations and cannot create
  a loop-engineering uplift claim.
- The accepted post-load sample becomes the exact phase baseline used by the
  existing per-episode and phase-growth checks, avoiding an unaccounted
  sampling gap.
- Task, model, arm, prompt, controller, evaluator, budget, schedule, and
  statistical decision rules are unchanged.
- This amendment was frozen with zero calibration episodes, zero confirmatory
  episodes, zero model prompts, and no agent-effectiveness outcome from
  attempt 15.
- Uplift claim: **not permitted**
