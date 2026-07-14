# ADR 006: Bind result rows and summaries to exact manifests

Status: **accepted**

## Context

An experiment identifier is convenient but not immutable. A researcher can edit budgets, model pins, or backend flags while retaining the same identifier, then accidentally pool incompatible rows into one table. File names and directory layout do not prevent that failure.

## Decision

- Hash the exact UTF-8 TOML bytes when an experiment plan is loaded.
- Store that `sha256:` reference on every manifest-bound result row.
- Require the row digest to match the supplied manifest before coverage or aggregation.
- Report manifest bindings in both text and JSON summaries.
- Reject multiple bindings under one experiment identifier, including a mixture of bound and unbound rows.
- Cap a plan at 250,000 measured runs, equal to the result loader's record safety limit.

## Consequences

Whitespace-only manifest edits intentionally create a new binding. This is stricter than hashing normalized TOML, but it makes the provenance rule simple and independently reproducible. Exploratory summaries may remain unbound only when every row for that experiment is consistently unbound; such output is visibly labeled and is not publishable evidence.
