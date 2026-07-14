# ADR 009: Use a read-only verifier and preserve public-passing candidates

- Status: Proposed
- Date: 2026-07-14

## Context

The qualification implementation labeled `maker_verifier` asked a second model
call to return replacement edits. That call could overwrite a public-passing
maker patch. In the 2026-07-14 qualification, this produced systematic
regressions, including revisions that failed public tests. The implementation
therefore measured review-and-revise behavior, not the read-only verifier
defined by the experiment protocol.

A v0.2 controller must distinguish verifier judgment from maker action and must
make every outcome transition auditable without exposing hidden evaluation.

## Decision

Adopt a three-role state transition:

1. a maker creates the first candidate using the same initial prompt as Direct;
2. a fresh read-only verifier returns structured `APPROVE`, `REJECT`, or
   `ESCALATE` findings and has no edit capability;
3. only a `REJECT` verdict may trigger one fresh maker revision.

Checkpoint every public-test-passing candidate. Invalid verifier output,
escalation, rejected edits, and public-test-failing revisions cannot erase the
last public-passing candidate. In the effectiveness track, the controller falls
back to that candidate and records why. A deployment safety policy that blocks
on escalation is a separate experiment.

After the episode ends, evaluator-owned copies may score Candidate A and a
public-passing Candidate B. Those results never return to the model. This allows
the report to distinguish true rejection, false rejection, revision rescue, and
revision regression while retaining final-candidate success as the primary
endpoint.

## Alternatives considered

### Let the verifier edit directly

Rejected because it conflates judgment and repair, prevents verifier-precision
measurement, and can destroy an already usable candidate.

### Discard every verifier-rejected candidate

Rejected for the effectiveness experiment because verifier errors would become
an implicit safety policy and obscure the candidate's objective quality. This
policy may be studied later in the deployment track.

### Use a larger or different verifier model

Deferred. The first v0.2 comparison uses the same pinned artifact for maker and
verifier so the controller is the intended varying factor. Heterogeneous
maker-verifier systems require a separately labeled manifest.

### Allow unlimited verifier-maker cycles

Rejected because loops could spend until success, weakening budget fairness and
auditability. Version 0.2 permits at most one verifier-guided revision.

## Consequences

- The model adapter needs role-specific structured-output contracts.
- The controller needs candidate checkpoints, restore behavior, and explicit
  verdict/fallback events.
- Maker and verifier token reserves must be recorded separately while remaining
  inside one shared task-level budget.
- Qualification tests become more complex but failure attribution becomes
  substantially clearer.
- Old review-and-revise results remain valid historical evidence for that exact
  controller revision, but cannot be pooled with v0.2.

The complete proposed protocol is
[`experiment-design-v0.2.md`](../experiment-design-v0.2.md).
