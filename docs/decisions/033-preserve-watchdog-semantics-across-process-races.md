# ADR 033: Preserve watchdog semantics across process races

- Status: Accepted before scoring
- Date: 2026-07-20
- Scope: sampled writable-layer watchdog and action-policy attribution

## Context

An adversarial review after the first live storage qualification found three
untested process races in the sampled `SizeRw` watchdog.

First, after both `docker exec` output streams reached EOF, the executor could
wait for the remaining action deadline without reading a newly queued
watchdog signal. A process that closed its streams and continued writing could
therefore evade the intended sampling response until timeout. Second, when
the watchdog killed the local Docker CLI after a proven action start, its
local `-9` return code could be recorded as though it were the model command's
exit code. Third, an action that terminated its own container could make the
next `SizeRw` inspection fail because the container was no longer running;
that race could turn the frozen `container_terminated` policy result into an
infrastructure failure.

The new `writable_layer_overflow` policy result also had not yet been carried
through the typed interactive environment and sealed evidence allowlists. All
four issues were found before any v0.7 model request or scored row.

## Decision

After stream EOF, the executor polls process completion in bounded 50 ms
slices under the original absolute deadline and checks the watchdog signal
between slices. An overflow signal aborts the local execution promptly and
performs the existing exact-label cleanup.

For an action-stage sampled overflow, the public exit code is `null`: the
controller generated the local termination, so `-9` is not model output. A
post-storage overflow occurs after the command completed and retains that
command's attested return code.

If a `SizeRw` probe fails, the watchdog performs the existing exact lifecycle
attestation. An attested exited container produces no probe-failure signal and
is handed to the post-action process audit, which records the frozen
`container_terminated` policy result. A running, malformed, or ambiguous
lifecycle after a failed probe remains infrastructure-invalid. This allowance
does not weaken identity, label, image, or security-profile checks.

`writable_layer_overflow` is added to the frozen action-policy enum, fixed
agent-visible observation map, policy-recovery path, campaign verifier, and
calibration verifier. Regression tests cover post-EOF writes, local-versus-
model exit-code provenance, post-storage completion, and the exited-container
probe race.

## Consequences

- The sampled watchdog remains responsive after output closes without
  replacing the original action deadline.
- Reports do not attribute a controller-generated signal exit to the model.
- Equivalent model-caused container termination has stable classification
  across Docker scheduling races.
- Any unresolved Docker probe or lifecycle ambiguity still fails closed and
  is excluded from effectiveness scoring.
