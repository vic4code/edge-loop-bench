# ADR 015: Seal v0.7 production authorization from verified provenance

- Status: Accepted; implementation required before measured execution
- Date: 2026-07-16

## Context

The v0.7 pre-calibration manifest pins the causal design, local model
identities, selected-task qualification, and request budgets. Its executable
code hashes were previously caller-provided SHA-256-shaped strings, however,
and the episode runner still accepts independently supplied task, model, gold,
Docker, and host-hook objects. Those surfaces can each be internally valid
while originating from different experiments.

A local reboot creates a second provenance risk. A resumed process must prove
that it is executing the same committed code and the same admitted
qualification/calibration package before it is allowed to open another
confirmatory intent. Absolute checkout paths, private evaluator material, and
measured calibration outcomes must not enter the public authorization record.

## Decision

Introduce two builder-sealed, fail-closed boundaries before production
composition is allowed.

1. A verified source inventory is derived only from a clean committed Git
   `HEAD`. It reads every tracked entry as a regular non-symlink file, checks
   its bytes against the committed Git object identity, and computes a
   domain-separated SHA-256 inventory. Its public record contains only Git
   object identities, aggregate counts, and the inventory root; it never
   contains a repository or file path. Revalidation repeats the clean-HEAD and
   byte-inventory proof and requires exact equality.
2. The v0.7 manifest derives prompt, controller, progress, host-safety, and
   whole-source code identities from that verified inventory. Callers cannot
   inject those hashes. The manifest also freezes the exact Docker resource
   limits, per-action limits, v0.7 host-safety threshold subset, deterministic
   run-ID policy, intervention-journal revision, and v0.7 phase caps.
3. A campaign authorization can be built only from that exact pre-calibration
   manifest, the selected-sample qualification evidence it already binds, one
   verified calibration evidence package, both admitted evaluator-sealed model
   dispositions, and an allowed planning gate. Its canonical record binds
   manifest, qualification, calibration, code, runtime, execution, and
   planning roots without retaining task outcomes or local paths.
4. Before any new confirmatory intent, authorization revalidation must
   re-inventory the repository and require exact manifest and provenance
   equality. The campaign ledger exposes a fail-closed pre-intent callback
   inside its lock: for a new campaign it runs before even the declaration is
   durable, and on resume it runs before each new intent. It is deliberately
   skipped while reconciling an already-durable pending intent from an exact
   pre-existing envelope. This authorization does not itself expose an
   arbitrary task, gold, model, or environment factory; production composition
   consumes the sealed authorization through that bounded callback.

The v0.7 host-safety record reuses only the v0.6 threshold semantics: AC power,
Low Power Mode, VM pressure, memory, disk, swap, thermal/performance warnings,
sampling, and cooldown. It does not inherit v0.6 block-requeue or request-count
semantics. v0.7 independently freezes 60 qualification replay actions, 26
calibration prompts, and 780 confirmatory prompts.

## Alternatives considered

### Continue accepting caller-provided code hashes

Rejected. Shape validation proves only that a value resembles a digest, not
that it identifies the bytes executing the campaign.

### Bind only the Git commit ID

Rejected. A dirty worktree, staged replacement, tracked symlink, or changed
file can execute bytes that differ from the commit. The byte inventory makes
the executable checkout itself part of admission.

### Store absolute paths in the manifest

Rejected. Paths leak host-specific information and make otherwise identical
checkouts produce different public evidence. The trusted verifier receives a
local root only while building or revalidating; no root is serialized.

### Reuse the complete v0.6 `HostSafetyPins`

Rejected. Its 400/304/3,800 request and block-requeue values describe the v0.6
campaign, not v0.7. Carrying them into v0.7 would create contradictory
execution semantics.

## Consequences

- Measured execution cannot start from the current dirty development checkout;
  the implementation must first be committed and the worktree clean.
- A code, manifest, qualification, calibration, runtime, or execution-pin
  mismatch prevents authorization construction or revalidation.
- Public provenance stays path-free and outcome-free while a private verifier
  can still prove exact tracked bytes.
- Updating any tracked source file intentionally invalidates the prior
  authorization and requires a new manifest and calibration package.
- The authorization is necessary but not sufficient for execution; a later
  production factory must use it to construct trusted task, gold, Docker,
  Ollama, journal, and host-safety boundaries without dependency injection.
