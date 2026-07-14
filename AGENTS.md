# Agent contribution guide

EdgeLoopBench is an experimental instrument. Correct accounting and reproducibility are more important than feature count.

## Invariants

- Keep agent-effectiveness and serving-efficiency conclusions separate.
- Count logical prompt tokens even when a backend reuses a prefix cache.
- Never expose hidden tests, gold patches, or evaluator paths to an agent.
- Pin model revisions, runtime versions, prompts, and controller behavior for published runs.
- Record weight quantization and KV-cache quantization as different variables.
- Change one serving factor at a time in an optimization ablation.
- Append raw events; derive summaries instead of editing results in place.
- Do not add network-dependent benchmark tasks.

## Development workflow

1. Update the specification or ADR when behavior or experimental semantics change.
2. Add a failing test for logic changes.
3. Implement the smallest complete slice.
4. Run `make check`.
5. Review the diff for leaked paths, unstable timestamps, and unverifiable claims.

Use the Python standard library unless a dependency has a measured and documented benefit. Keep platform-specific collection behind an interface so analysis remains portable.
