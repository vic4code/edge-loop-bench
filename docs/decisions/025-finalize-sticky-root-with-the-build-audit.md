# ADR 025: Finalize sticky-root mode in the audit layer

- Status: superseded in part by ADR 028
- Date: 2026-07-20

## Context

After the fs1 fixture omission was corrected, the real image build reached the
final writable-surface audit and failed closed. A diagnostic image stopped
immediately before that audit showed `/` as UID 0 mode `0755`, although an
earlier Dockerfile layer had run `chmod 1777 /`.

The root of an overlay-backed build or container is a mount point. Relying on
metadata set several filesystem layers earlier does not prove the mode seen by
a later build step or by the exported runtime image. The existing audit
correctly rejected the mismatch; its `01777` policy must not be weakened.

## Decision

In the final filesystem-mutating Dockerfile instruction, set `/` to mode
`01777` and immediately run the writable-surface audit in the same `RUN`.
`USER`, `ENV`, and `WORKDIR` remain config-only instructions after that layer.
The final built image must be inspected by starting an isolated diagnostic
container and proving UID 0 mode `01777` before production is retried.

ADR 028 records that Docker's exported runtime mount resets the root mode to
`0755`. The same-layer build audit remains required, but runtime sticky-root
initialization and attestation now provide the exported-container invariant.

Update the Dockerfile source hash and all transitive build pins together. Add
a regression test that requires sticky-root finalization in the audit block,
after the earlier ownership/setup block and before the collector invocation.

## Consequences

- The audit remains strict and observes the intended root mode itself.
- Numeric UID/GID 65532 can create its own upstream-compatible top-level
  outputs, while sticky-directory rules still protect root-owned baseline
  paths.
- The change does not alter task selection, prompts, controller topology,
  budgets, or evaluators.
- Diagnostic images are non-scoring and must be removed after validation.
