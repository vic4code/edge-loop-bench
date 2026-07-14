# MicroRepair-6 task suite

The v0.1 scaffold names six task slots. Executable task bundles are now being
implemented under the accepted
[`runnable experiment specification`](../../docs/runnable-experiment-spec.md).
Until every qualification gate passes, this remains a harness shakeout suite,
not a published benchmark.

## MVP composition

| ID | Category | Intended challenge |
| --- | --- | --- |
| `python-localized-001` | Localized | Boundary condition with a deterministic failing test |
| `python-localized-002` | Localized | Parsing or error-handling defect |
| `python-cross-file-001` | Cross-file | Contract mismatch across two modules |
| `python-cross-file-002` | Cross-file | State update that requires a coordinated change |
| `python-diagnosis-001` | Diagnosis | Useful signal hidden in noisy public-test output |
| `python-adversarial-001` | Adversarial | Superficial public-test fix fails a hidden edge case |

## Acceptance contract

Each public task directory includes a machine-readable manifest, initial
repository archive or deterministic generator, public tests, dependency
lockfile, and license/provenance file. Gold patches and hidden tests live under
an evaluator-owned root outside the agent-visible worktree and must not leak
through prompts, path names, or logs.

The task must be:

- offline and deterministic;
- resettable to an identical Git commit;
- solvable by the validated gold patch;
- protected against edits to evaluator and test-control paths;
- small enough that the prompt does not consume most of the lowest budget tier;
- classified as generated mutation, reconstructed real bug, or verifier adversarial.

Do not pool those source strata in published analysis.
