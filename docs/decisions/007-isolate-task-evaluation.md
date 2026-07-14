# ADR 007: Materialize evaluator assets outside agent worktrees

Status: Accepted
Date: 2026-07-14

## Context

Executable repair tasks need hidden tests and a validated solution for
objective scoring. Keeping those assets anywhere in an agent-visible worktree,
including ignored directories or discoverable symlinks, risks contaminating
the experiment. Printing evaluator paths into agent-visible logs can leak the
same information indirectly.

## Decision

Prepare each agent worktree from a public task bundle containing only source,
public tests, provenance, and the public task manifest. Materialize hidden
tests, gold patches, and evaluator control files under a distinct temporary
root. The controller passes only sanitized public-test output to the agent and
runs hidden evaluation after the agent episode ends.

Task integration tests may use synthetic fixture patches to verify isolation,
but production task gold patches and hidden tests are never included in a
model-facing prompt, tool root, path listing, or log record.

## Consequences

Task packaging and evaluation require an explicit two-root interface and path
sanitization tests. This is more deliberate than a single repository fixture,
but it makes accidental evaluator disclosure testable and keeps objective
success independent from model self-reports.
