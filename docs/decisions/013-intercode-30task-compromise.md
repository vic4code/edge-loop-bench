# ADR 013: Run a 30-task InterCode-Bash compromise study

- Status: Accepted; measured execution gated
- Date: 2026-07-16

## Context

ADR 012 preregistered a strong 50-task, two-seed, `K = 6` InterCode-Bash study.
Its causal controls and trust boundaries remain useful, but its two-model
confirmatory ceiling was 3,800 model prompts before calibration and recovery.
The block-level crash protocol also required substantial implementation and
operational state for a local 16 GB host that had already rebooted under load.

The next experiment must remain representative enough to distinguish extra
sampling, raw execution feedback, and engineered control, while being small
enough to finish sequentially and safely. Reducing only model size is
insufficient; the task, seed, attempt, calibration, and recovery matrix must
also be bounded before any output exists.

## Decision

The official Claude Code taxonomy distinguishes turn-based, goal-based,
time-based, and proactive loops. v0.7 deliberately isolates the bounded
within-task goal-loop mechanism. `engineered_loop` is an adaptation with a
deterministic candidate-only feedback packet and a terminal private scorer; it
is not labeled as an exact `/goal`, `/loop`, or `/schedule` reproduction.

Run v0.7 as a new preregistered pilot rather than rewriting v0.6 or its
historical rationale.

- Use the official InterCode-Bash source at commit `c3e46d8`, its four pinned
  NL2Bash files, and the committed gold-free static-exclusion audit.
- Select 30 static-clean tasks by a frozen within-stratum SHA-256 ranking with
  quotas `9/8/9/4`. Qualify exactly those tasks with two fresh offline gold
  replays before model calls; never replace a failed row.
- Run Qwen3.5 4B and Phi-4 Mini 3.8B, one resident at a time. Load no larger
  model.
- Compare Direct, Independent Verified Sampling, Raw Feedback Loop, and
  Engineered Loop with seed `11` and `K = 4`.
- Freeze the shared non-Direct episode ceiling at a 4,096-token context,
  512-token per-call completion, 16,380 logical prompt tokens, and 2,048
  logical completion tokens; Direct consumes one call and leaves the rest
  unused.
- Cap confirmatory execution at 780 model prompts. Limit calibration to four
  disjoint upstream quickstart tasks per model and 13 prompts per model.
- Admit calibration only from the exact builder-sealed pre-calibration
  manifest and authority-sealed replay result. Bind both the manifest digest
  and replay campaign digest into the calibration declaration, each durable
  begun marker, and verifier-issued evidence. Seal the begun marker before
  runtime construction; an unrecorded row with any artifact is permanently
  pending and is never retried automatically.
- Keep operator interventions in their own live append-only journal. Do not
  pre-bind a caller-supplied intervention root before one exists; downstream
  reporting must consume its verifier-issued sealed summary.
- Use a frozen strict state-plus-normalized-output equality predicate as the
  final correctness endpoint. Online controllers may see only candidate-side
  output and a preregistered gold-free progress heuristic capped at `0.8`, so
  it cannot trigger the controller's exact-`1.0` correctness stop.
- Do not reproduce or report the official InterCode reward in v0.7. Any exact
  reward adapter is a new preregistration. This is a pinned-task and
  pinned-environment adaptation, not a leaderboard run.
- Journal at campaign and episode granularity. After the controller seal and
  post-host sample, durably seal a separate mode-`0600` execution envelope
  binding the exact episode, result, controller root, active time, and host
  evidence. Reuse sealed completed episodes. A pending intent may be closed
  only by independently reverifying that exact pre-existing envelope; the
  reconciler cannot invoke the model executor. Missing or invalid envelopes
  leave the intent pending and are never replayed. Do not implement block-level
  recovery or requeue for v0.7. Publication verification must additionally
  enumerate exactly 240 owner-only envelopes, reopen each one through the
  strict envelope parser, and match every typed payload to the independently
  verified campaign/controller matrix. The ledger alone is not publication
  evidence.
- Treat an infrastructure-invalid terminal as a hard campaign stop. Preserve
  the invalid row, but fail every future advance before another intent or model
  callback rather than collecting the rest of the schedule.
- Freeze a cumulative completed-episode active-time limit of exactly 18 hours.
  Enforce it before every new intent and model callback. A pending intent may
  first reconcile only from its pre-existing sealed envelope; the next advance
  then fails with the specific active-time-limit error if the sum reached the
  cap.
- Treat 240/240 valid confirmatory episodes as a prerequisite for any uplift
  claim. Incomplete execution may yield only coverage and worst-case
  descriptive reporting.

Qwen Engineered minus Raw is the single primary contrast. Phi is replication;
Raw minus Independent and Independent minus Direct are mechanism analyses.
Logical prompt tokens are counted in full even when physically cached, and
human prompts, automatic feedback follow-ups, independent samples, unresolved
handoffs, and paired avoided handoffs remain separate counters.

## Alternatives considered

### Continue the v0.6 matrix unchanged

Rejected for the next local run. It provides better seed coverage but its
request and recovery envelope is disproportionate to the current host and the
goal of obtaining one complete, interpretable pilot.

### Keep 30 tasks but retain `K = 6`

Rejected. It raises the two-model ceiling from 780 to 1,140 prompts. `K = 4`
still exposes three correction opportunities and the attempt-1/2/4 trajectory.

### Drop the independent-sampling or raw-feedback arm

Rejected. Without Independent, extra test-time samples are confounded with
interaction. Without Raw, engineered packet/rollback effects are confounded
with receiving any feedback at all.

### Use the upstream scalar reward without exact reproduction

Rejected. A partly reconstructed gold-derived reward would make the controller
treatment unverifiable and could be mislabeled as official. A frozen terminal
strict predicate plus clearly heuristic gold-free feedback is narrower and
auditable.

### Automatically replay interrupted task blocks

Rejected for v0.7. Block recovery adds opaque-capability reconstruction and
asymmetric replay risk. Episode-level sealed resume preserves completed work;
an interrupted key remains invalid and therefore prevents an uplift claim.

## Consequences

- The maximum confirmatory inference load is explicit and 79% lower than the
  v0.6 two-model ceiling.
- The design retains all three causal mechanism contrasts and an external,
  loop-native task source.
- One seed and 30 tasks cannot estimate decoding variance and are unlikely to
  resolve small effects. Null findings will often be inconclusive.
- The gold-free progress heuristic may be misaligned; Engineered-minus-Raw is
  evidence about the entire frozen package, not rollback or packet formatting
  in isolation.
- An interruption after a sealed execution envelope but before the campaign
  terminal append can be reconciled without another model call. Every earlier
  interruption, invalid envelope, or infrastructure-invalid terminal remains
  visible and makes the study formally incomplete.
- v0.6 remains the fuller future design. v0.7 is the executable local evidence
  step and cannot be pooled with v0.4 or presented as an official benchmark
  score.
