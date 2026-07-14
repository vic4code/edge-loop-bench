# ADR 004: Start with a small transparent controller

Status: Accepted
Date: 2026-07-14

## Context

General agent frameworks can add hidden retries, prompt transformations, summarization, telemetry, or stopping behavior. Those conveniences complicate logical token accounting and causal comparison.

## Decision

The first runnable experiment will use a small Python controller and five allowlisted tools: `list_files`, `read_file`, `search`, `apply_patch`, and `run_public_tests`. Strategy state machines and budget transitions will be explicit and append-only.

## Consequences

Initial implementation takes more deliberate work and supports fewer integrations. The resulting experiment is auditable and existing agent CLIs can later be added as separately labeled system adapters.
