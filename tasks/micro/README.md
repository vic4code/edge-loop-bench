# Micro task suite plan

The v0.1 scaffold names six task slots but does not yet include executable agent worktrees. This prevents placeholder tasks from being mistaken for a benchmark.

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

Each future task directory must include a machine-readable manifest, initial repository archive or generator, public tests, dependency lockfile, license/provenance file, and a gold patch. Hidden tests live in an evaluator-owned directory outside the agent-visible worktree.

The task must be:

- offline and deterministic;
- resettable to an identical Git commit;
- solvable by the validated gold patch;
- protected against edits to evaluator and test-control paths;
- small enough that the prompt does not consume most of the lowest budget tier;
- classified as generated mutation, reconstructed real bug, or verifier adversarial.

Do not pool those source strata in published analysis.
