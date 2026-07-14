# ADR 005: Store append-only events and derive summaries

Status: Accepted
Date: 2026-07-14

## Context

Agent runs contain model requests, tool actions, budget transitions, test results, and evaluator outcomes. Editing aggregate CSV files destroys provenance and can hide invalid or retried runs.

## Decision

Raw run telemetry will be append-only JSONL with stable run identities and hashes linking it to manifests, prompts, tasks, and final diffs. Summary files are deterministic derived artifacts.

## Consequences

Raw data is more verbose and schemas must tolerate additive fields. Experiments remain auditable, resumable, and re-analyzable as metrics improve.
