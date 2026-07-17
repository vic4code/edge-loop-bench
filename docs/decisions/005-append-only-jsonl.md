# ADR 005: Store append-only events and derive summaries

Status: Accepted
Date: 2026-07-14

## Context

Agent runs contain model requests, tool actions, budget transitions, test results, and evaluator outcomes. Editing aggregate CSV files destroys provenance and can hide invalid or retried runs.

## Decision

Raw run telemetry will be append-only JSONL with stable run identities and hashes linking it to manifests, prompts, tasks, and final diffs. Summary files are deterministic derived artifacts.

For the bounded 30-task interactive campaign, the ledger declares the complete
episode schedule before execution. Each episode intent is durably appended
before the callback may touch a model or environment. A reboot-visible intent
without a terminal result halts the campaign and is never retried
automatically. It remains pending until exact owned-resource reconciliation;
cleanup cannot authorize another model request under the same protocol
version. The campaign matrix binds separate sealed per-episode controller-log
roots but is not itself publication authority. A later evidence gate must
verify those logs and requires a complete matrix with no pending or invalid
episode.

## Consequences

Raw data is more verbose and schemas must tolerate additive fields. Experiments remain auditable, resumable, and re-analyzable as metrics improve.
