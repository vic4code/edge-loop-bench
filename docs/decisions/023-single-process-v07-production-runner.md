# ADR 023: Run v0.7 through one fail-closed single-process authority

- Status: accepted
- Date: 2026-07-17

## Context

The image builder, local-model attestor, calibration executor, formal campaign
driver, and final evidence verifier were individually executable, but an
operator still had to assemble them in the right order. In particular,
calibration ends with Phi resident while the formal schedule starts with Qwen.
Treating the first formal callback's `previous_model_id=None` as proof of an
empty Ollama server could skip the required Phi unload.

A reboot also destroys the live managed-runtime and single-boot host authority.
Reusing a half-written artifact directory after reboot would make a clean rerun
look like a continuation even though the required live capabilities cannot be
reconstructed.

## Decision

Add `intercode_v07_model_phase.py`, revision
`intercode-v0.7-model-phase-manager-v1`. One builder-issued manager retains the
actual resident `V07ModelRuntime` across calibration and formal execution. It
admits only Qwen then Phi for calibration and Qwen then Phi for formal scoring.
The formal callback's previous-model argument is checked against formal phase
history, while the actual residency transition uses the retained post-
calibration Phi runtime. Every attempted transition is appended to the
intervention journal before the managed unload/load call, and any transition or
host-session failure terminally invalidates the manager.

Add `intercode_v07_production.py`, revision
`intercode-v0.7-production-runner-v2`, as the outer local control surface. Its
default CLI mode is read-only preflight; mutation requires explicit
`--execute`. Before creating a run directory or launching Ollama, it requires:

- macOS VM pressure exactly `1`;
- system-wide free memory at least 25 percent; and
- at least 32 GiB free on the declared Docker-data filesystem.

Preflight accepts a canonical future tokenizer-helper location so the host gate
can be evaluated before provisioning that CPU-intensive dependency. Execution
then requires both the executable helper and its provenance record before it
creates the artifact tree or launches a runtime. This ordering prevents a
missing tokenizer from hiding the host-safety decision while preserving a
mutation-free failure boundary.

The executor then requires a clean committed source inventory and a fresh,
absent artifact root. In one process it declares the intervention journal,
launches and owns Ollama, attests the Docker daemon, builds and reopens four
images, seals image provenance, attests only Qwen3.5 4B and Phi-4 Mini 3.8B,
qualifies 30 tasks plus calibration gold, builds the outcome-free manifest,
runs eight calibration rows, applies the planning gate, prepares the bound
study, runs all 240 model-major formal episodes, seals interventions, verifies
the aggregate evidence, and runs the frozen analysis.

There is deliberately no production resume flag. An interrupted directory is
retained as raw evidence. A later attempt uses a fresh directory, new managed
runtime, new source revalidation, new intervention-journal instance, and new
formal ledger. The low-level ledgers retain their crash-detection and pending-
intent semantics; the outer runner does not convert them into cross-boot
capabilities.

Derived JSON records are written exclusively as owner-only mode `0600` files.
Controller, envelope, calibration, qualification, image-build, intervention,
and campaign journals remain the raw append-only sources. The path-free
production result can be published; its local artifact path is operational and
is excluded from the canonical record.

## Consequences

- No 9B or 12B model is admitted by the production path.
- Calibration and formal execution share the exact same runtime, Docker,
  tokenizer, host, and residency authorities.
- A pressure warning is an infrastructure refusal before model loading, not an
  effectiveness observation.
- Internally consistent evidence is still not a cryptographic signature against
  a malicious local operator controlling the process and raw filesystem.
