# ADR 028: Initialize and attest the runtime sticky root

- Status: accepted; supersedes ADR 025's exported-root assumption
- Date: 2026-07-20

## Context

All four canonical image builds passed the strict writable-surface audit after
two pinned-base distinctions were made explicit: `.dockerenv` is injected only
when Docker starts a container, and the unused world-writable
`/var/lib/pebble/default` directory is hardened to mode `0755` during image
construction.

An isolated exported fs1 container nevertheless observed `/` as UID 0, GID 0,
mode `0755`. Docker's runtime overlay mount does not retain the image build
layer's root-directory mode. The image audit can prove the filesystem contents
and covered writable surface, but it cannot make the runtime mount point
sticky. Without a runtime transition, numeric UID 65532 cannot create the
top-level relative outputs expected by the upstream working directory `/`.

No model was loaded or prompted during this diagnosis.

## Decision

Keep the image build audit strict and keep `Config.User` fixed at
`65532:65532`. Immediately after an exact owned container starts, the Docker
trust boundary performs one fixed, non-shell root operation:

```text
/bin/chmod 1777 /
```

It then independently reads the root metadata with a fixed trusted command,
requires UID 0, GID 0, and mode `01777`, and re-attests the container identity
and lifecycle before returning it. Any command failure, output, metadata, or
identity mismatch fails closed. Every model-issued action continues to execute
explicitly as `65532:65532`; no generic root execution surface is exposed.

The build audit treats only `.dockerenv` as a declared runtime-injected root
name. Runtime state collection continues to require the complete pinned root
baseline, including `.dockerenv`.

## Consequences

- Build and runtime mount invariants are recorded separately instead of
  claiming that an image layer controls Docker's root mount metadata.
- Agent commands regain the upstream `/` working-directory behavior without
  granting capabilities, network access, or root model execution.
- Container start semantics and their tests become part of the frozen runtime
  identity; production must use a fresh artifact root after the source change.
