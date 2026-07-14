# ADR 001: Separate effectiveness and serving experiments

Status: Accepted
Date: 2026-07-14

## Context

Agent loops change request count, context repetition, tool usage, and stopping behavior. Serving engines change physical latency, caching, throughput, and sometimes numerical execution. A combined benchmark cannot tell whether a gain came from better reasoning structure or a faster backend.

## Decision

Maintain three explicit tracks:

- effectiveness under shared logical budgets;
- serving under fixed rendered request shapes;
- optional deployment experiments under physical budgets.

Primary loop claims may use only the effectiveness track.

## Consequences

The repository produces more than one result table and cannot advertise a single scalar winner initially. In return, strategy and systems conclusions remain interpretable.
