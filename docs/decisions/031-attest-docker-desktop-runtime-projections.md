# ADR 031: Attest Docker Desktop runtime projections explicitly

- Status: Accepted before scoring
- Date: 2026-07-20
- Scope: v0.7 Docker lifecycle and process attestation

## Context

The first live end-to-end invocation of the corrected `DockerCli` created a
container with `HostConfig.OomKillDisable=false`, but Docker Desktop 27.3.1
projected that same field as JSON `null` after start. The container retained
the default OOM-killer behavior; no disable request had been supplied. The
same qualification then showed that `docker container top` rejects blank
process-column headings such as `pid=` because the daemon cannot locate its
required PID field.

Both failures occurred in diagnostic-only containers before any tokenizer
request, model load, task action, calibration row, or scoring row.

## Decision

The lifecycle validator now requires the `OomKillDisable` key to exist. A
created container must report the Boolean `false`. A running or exited
container may report only Boolean `false` or JSON `null`; `true`, a missing
key, or any other type fails closed. This is a lifecycle-specific projection
allowance, not permission to disable the OOM killer.

The fixed process audit uses explicit, non-empty headings:

```text
pid=PID,ppid=PPID,stat=STAT,comm=COMMAND
```

The parser requires the exact four-token header followed by exactly one
healthy `tail` process row. It does not accept arbitrary `ps` formats or a
caller-supplied process command.

Regression tests first reproduced both live failures. A fresh diagnostic
create, start, root attestation, sampled `SizeRw` watchdog, bounded action,
post-action process audit, trusted state collection, and exact cleanup then
passed against Docker Desktop 27.3.1.

## Consequences

- Runtime inspection remains fail-closed for a missing or enabled OOM-disable
  field while matching Docker Desktop's observed lifecycle representation.
- The daemon can identify the PID column, and the benchmark still parses only
  one exact process-table shape.
- The implementation and tests are part of the committed source inventory for
  every future manifest. Earlier failed attempts remain append-only evidence
  and cannot be resumed.
