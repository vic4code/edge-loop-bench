# ADR 003: Use paired fixed-budget comparisons

Status: Accepted
Date: 2026-07-14

## Context

A loop can spend more resources until it succeeds, making raw success rate a misleading comparison with a direct episode. Tasks also vary more than repeated seeds on one task.

## Decision

Compare strategies within the same task, budget tier, model, server configuration, and seed. Enforce one shared task-level logical budget vector across arms. Calibrate budget tiers on disjoint tasks and freeze them before evaluation.

## Consequences

Some strategies will leave budget unused and others will exhaust it. This is meaningful behavior. Analysis uses paired deltas and task-level bootstrap resampling rather than treating all runs as independent.
