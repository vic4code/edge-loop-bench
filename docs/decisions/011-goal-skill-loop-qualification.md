# ADR-011: Qualify goal-and-skill loops before confirmatory scaling

## Status

Accepted

Post-run audit: the v0.4 controller remains accepted as a deterministic
goal-and-skill qualification arm, but it is not an implementation of Claude
Code `/goal`. A faithful fresh-evaluator topology is proposed separately in
`docs/experiment-design-v0.5-fresh-evaluator-goal.md`.

## Date

2026-07-15

## Context

Earlier EdgeLoopBench studies tested bounded retry and model-based verifier
topologies. The v0.4 design combined published goal, verification-skill,
attempt-cap, and pilot-first ideas into a deterministic test-driven controller.
The later topology audit established that official Claude Code `/goal` instead
uses a fresh small evaluator after each complete main-agent turn.

The repository needs an executable interpretation of that guidance without
claiming to reproduce Claude Code internals or exposing hidden evaluation to
the agent. The comparison must also distinguish additional test-time compute
from genuine improvement in final objective success.

## Decision

Add `goal_skill_loop` as a qualification controller with:

- public-test pass as the deterministic, agent-visible goal;
- the same frozen five-part verification skill on every Maker call;
- sanitized public failure evidence between attempts;
- at most five Maker attempts;
- immediate exit on public-test pass;
- hidden evaluation only after the episode, with no hidden feedback returned;
- the same episode-level logical budget ceiling as Direct and Bounded Retry;
- actual token, tool, test, and wall-time accounting for every arm.

Run it first on a fresh eight-task offline pilot. Compare it with one-call
Direct and three-attempt Bounded Retry within each pinned local model. Treat
positive and negative paired transitions separately; equal aggregate success
can hide one rescue and one regression.

## Alternatives considered

### Reuse the v0.3 model checker

Rejected for this experiment. It would conflate goal-based looping with the
same-model checker topology already tested in v0.3.

### Use hidden evaluation as the stop condition

Rejected. It would leak benchmark-only information into the controller and
invalidate the evaluation boundary.

### Give every strategy five mandatory calls

Rejected. Real loop controllers exit early. Forcing calls after success would
measure artificial compute consumption rather than the tested topology.

### Move directly to a 30-task confirmatory suite

Rejected. The official guidance recommends piloting, and a small local pilot
can reject an unproductive topology without spending cloud API tokens or a
large amount of laptop compute.

## Consequences

- A negative result is actionable: the controller does not qualify for a
  larger confirmatory claim on the tested model and task distribution.
- Loop cost remains visible as test-time scaling rather than being hidden by
  prefix-cache or early-exit behavior.
- Public-test passes that fail hidden evaluation remain a known limitation;
  the loop cannot act on evidence it is correctly forbidden to see.
- Results apply to the pinned controller, tasks, budgets, models, and runtime;
  they are not a universal judgment on loop engineering.
