# ADR 026: Bind the exact image context and empty fixtures

- Status: Accepted
- Date: 2026-07-20

## Context

Attempt 3 stopped before model loading when the fs1 image build exposed a
missing empty fixture. A follow-up audit found the same semantic regression in
fs2 and fs3: upstream `touch` commands both create empty files and assign
timestamps, while the derived `touch -h` normalization does not create missing
paths. The audit also found that `.dockerignore` admitted
`docker/intercode/**`, so unreviewed files such as local bytecode caches could
enter the daemon context without being covered by the recorded context digest.

No model prompt, candidate action, calibration row, or scored episode had run.

## Decision

Create every upstream empty dated fixture explicitly before timestamp
normalization in fs1, fs2, and fs3. Keep fs4 empty as designed. Bind each
corrected setup script by SHA-256.

Replace the recursive Docker context exception with exact file exceptions for
the reviewed Dockerfiles, non-scoring placeholder, state collector, four setup
scripts, and byte-pinned upstream `docker.gitignore`. Bind `.dockerignore` and
every admitted file in the build-plan identity. Reject any future context
expansion until its bytes, purpose, and test coverage are reviewed together.

## Consequences

- All four filesystem images can be qualified before any model work begins.
- Local caches and unrelated repository files cannot silently affect the
  daemon build context.
- The correction changes instrument identity, so attempt 3 remains immutable
  and production must restart in a fresh artifact root from a clean commit.
- An exported image must still pass the runtime sticky-root inspection from
  ADR 025; static source tests alone are insufficient.
