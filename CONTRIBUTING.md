# Contributing

Contributions are welcome after the v0.1 protocol boundary is understood.

## Local checks

EdgeLoopBench requires Python 3.11 or newer. The current scaffold has no runtime dependencies.

```bash
make check
```

## Change expectations

- Add or update tests for logic changes.
- Update the relevant specification, protocol, or ADR when experimental semantics change.
- Keep example results synthetic and clearly labeled.
- Do not commit model weights, raw private prompts, credentials, or large trace files.
- Cite primary documentation for runtime-specific behavior.
- State whether a performance claim is measured here, reported upstream, or inferred.

## Adding benchmark tasks

A task must be offline, deterministic, license-compatible, resettable to a clean Git commit, and independently validated with a gold patch. Hidden tests must live outside the model-visible worktree. Generated mutation tasks and reconstructed real bugs are reported as separate strata.

## Adding model or runtime results

Include exact model revision or digest, quantization, tokenizer and template, runtime version or commit, server flags, hardware manifest, macOS build, run order, and raw append-only events. A result without this provenance is exploratory and cannot enter a comparison table.
