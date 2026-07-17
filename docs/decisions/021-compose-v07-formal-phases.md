# ADR 021: Compose formal episodes only through a bound model-major phase

- Status: accepted
- Date: 2026-07-16

## Context

The campaign ledger, prepared-study binding, runtime session, host session,
qualified Docker attempt factory, and episode runner were independently typed,
but a generic caller could still pair their valid objects incorrectly. Formal
execution also needs one canonical artifact layout so crash reconciliation and
publication verification cannot search arbitrary paths.

## Decision

Add `intercode_v07_formal_executor.py` as the only production composition for
one model-major formal phase. Its builder accepts exact `V07PreparedStudy`,
manifest, pinned InterCode source, `DockerCli`, `DockerActionExecutor`, and an
already admitted `V07HostSafetySession`. The host session must contain exactly
the one model named by the phase and no running container. Its read-only
policy accessor must also return the exact `V07HostSafetyPins` retained by the
prepared study; matching model residency alone is insufficient.

The Docker CLI exposes a builder-issued, read-only boundary identity. Formal
composition requires the frozen `desktop-linux` context, compares the local
endpoint digest and a fresh no-follow executable attestation with the prepared
host identity, and rejects an action executor unless it retains that same
`DockerCli` object with the same binary path and endpoint. This prevents a
valid host session, lifecycle boundary, and streaming executor from being
assembled across different hosts or Docker endpoints.

For an exact scheduled episode, the executor resolves the public task, asks the
prepared study for the live model and opaque trusted gold, constructs the
qualified attempt factory, issues one host before/after capability, and invokes
the frozen runner with the fixed v0.7 budget. Controller journals use
`controllers/episode-NNNN.jsonl`; execution envelopes use
`envelopes/episode-NNNN.execution.jsonl`. Both directories are owner-owned
mode `0700`; the append-only files remain mode `0600`.

The qualified attempt factory is built and type-checked before an episode host
admission is issued. Once issued, the executor always closes the admission in
a `finally` path. A failure before the before-host sample releases the unused
capability; a failure after that sample terminally invalidates the phase. A
stale artifact target or composition exception therefore cannot leave an
invisible active admission or permit unsafe phase reuse.

The executor passes the prepared study binding as the runner's execution
authority, reopens the sealed envelope before returning its typed execution to
the campaign ledger, and requires both that authority and the host samples to
equal the session-issued evidence. A model mismatch is rejected before
artifact creation. One model switch therefore closes the old phase, accounts
the typed residency operation, admits a new host session, and builds a new
phase executor. Pending-envelope reconciliation remains a no-model ledger
operation.

Production advances use `advance_v07_formal_phase`, which performs no more
than one ledger advance. It composes the model-phase guard ahead of the full
prepared-study source, runtime, and intervention-declaration revalidation,
then supplies the phase's canonical envelope lookup as the pending reconciler.
The phase mismatch therefore fails before the ledger writes a new intent, and
resume never silently reissues a model request for an existing intent.

Revision `intercode-v0.7-formal-phase-executor-v2` adds the terminal
`run_v07_formal_campaign` driver. It inspects only the bound ledger, opens
exactly one Qwen phase followed by exactly one Phi phase, delegates every
episode to `advance_v07_formal_phase`, rejects non-monotonic or model-crossing
progress, and permits at most 241 advances for the 240-row schedule. It cannot
alternate models or skip the append-only ledger.

Calibration now uses `intercode_v07_calibration_runtime.py` to compose the same
live runtime, Docker CLI/action boundary, qualified image/gold authority, host
session, and episode admission. Rows must arrive in exact Qwen-then-Phi order;
each issued admission has an explicit abort callback used even when the runner
fails. This closes the earlier gap where production calibration had a generic
runtime-factory protocol but no exact authority composer.

The actual live residency shared across calibration and formal execution is
owned by ADR 023's model-phase manager. The formal driver's first callback
argument describes formal schedule history; it must not be interpreted as an
empty-server assertion after calibration.

## Consequences

- Calibration retains its separately bound crash-safe executor and now has an
  exact production runtime composer.
- This ADR does not authorize host cleanup, Docker image provenance inference,
  or automatic replay of a pending intent.
- The final publication gate still has to verify all 240 controller journals,
  all 240 envelopes, and the sealed intervention summary against the same
  study binding.
