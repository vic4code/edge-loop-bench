# Synthetic result examples

Files in this directory are hand-authored synthetic data for smoke tests and documentation. They are not model measurements and must never appear in research result tables.

`sample-plan.toml` declares the complete six-run matrix represented by `sample-runs.jsonl`. Every row binds to the SHA-256 of the exact plan bytes and reports `max_call_context_tokens`. The summary CLI requires this association so invented arms, over-budget counters, per-call context overruns, and silent omissions cannot enter an apparently valid table.
