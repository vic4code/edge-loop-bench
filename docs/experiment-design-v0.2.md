# EdgeLoopBench v0.2 experiment design

- Status: **proposed; implementation and confirmatory runs have not started**
- Design date: **2026-07-14**
- Target host: **M3 MacBook Pro (Mac15,3), 16 GB unified memory, 10-core GPU**

## 1. Executive summary

EdgeLoopBench asks one narrow causal question:

> For a pinned local model facing the same repair task, does a transparent
> controller loop improve objectively verified success enough to justify its
> extra logical tokens and time?

The primary comparison is **within model**, not between parameter counts. Each
loop is paired with a Direct run on the same task, budget, and decoding seed.
Model-to-model tables are descriptive capability comparisons; resource-tier
comparisons are deployment results.

Version 0.2 replaces the qualification controller's edit-capable second
"verifier" call with a genuine read-only verdict step. The verifier may return
`APPROVE`, `REJECT`, or `ESCALATE`, but cannot edit files. A rejection may cause
one fresh maker revision. Every public-test-passing candidate is checkpointed,
so an invalid verifier response or a broken revision cannot erase the last
usable candidate.

The existing MicroRepair-6 results remain **qualification evidence** for the old
controller. They must not be relabeled or pooled with v0.2 results.

## 2. Research questions

### RQ1 — Bounded retry effectiveness

At a fixed task-level logical budget, does deterministic public-test feedback
allow Bounded Retry to solve more tasks than Direct?

### RQ2 — Maker–Verifier effectiveness

Does a fresh read-only verifier followed by at most one maker revision improve
final objective success relative to Direct and Bounded Retry?

### RQ3 — Failure mechanism

When a loop changes an outcome, is the transition a rescue, a regression, a
format recovery, a public-test recovery, or a verifier-guided semantic repair?

### RQ4 — Model dependence

Does the loop effect vary with the model's Direct baseline, especially when a
stronger model approaches a task-suite ceiling?

### RQ5 — Edge deployment envelope

After effectiveness is frozen, which model and controller policy maximizes
verified tasks under a fixed wall-time, energy, or unified-memory envelope?
This is a separate deployment question, not an agent-effectiveness claim.

## 3. Preregistered hypotheses

- **H1:** Bounded Retry has positive medium-budget success uplift over Direct
  when the first attempt fails a diagnostic public test.
- **H2:** Read-only Maker–Verifier has positive medium-budget net rescue over
  Direct while preserving a public-test-passing fallback candidate.
- **H3:** Verifier-guided revision has positive net rescue
  (`rescued - regressed`) on verifier-adversarial tasks.
- **H4:** Loop uplift is smaller when Direct success is near 100%, because fewer
  recoverable failures remain.
- **H5:** Additional calls without positive net rescue are a cost increase, not
  a performance improvement.

The confirmatory medium-budget contrasts are:

1. `bounded_retry - direct`;
2. `maker_verifier - direct`.

`maker_verifier - bounded_retry` is a prespecified secondary contrast. Report
all three; apply Holm correction to the two confirmatory tests if p-values are
shown.

The previous edit-capable review-and-revise controller is not a confirmatory
arm. A frozen four-arm qualification ablation may compare it with v0.2, but the
result is labeled controller-version evidence and is not pooled with the
three-arm confirmatory benchmark.

## 4. Experimental units and pairing

The primary sampling unit is the **task**. A task-strategy run is paired with
the other strategies by:

- model artifact;
- task ID and frozen initial commit;
- budget tier;
- decoding seed, when stochastic decoding is used;
- controller, prompt, edit-schema, runtime, and serving configuration.

Seeds are repeated measurements within a task, not independent tasks. Bootstrap
and confidence-interval procedures must resample tasks while retaining every
seed belonging to the sampled task.

### Deterministic primary run

The primary v0.2 experiment uses temperature `0.0` and one pinned decoding seed.
Repeating different seed values at temperature zero may be useful for detecting
backend nondeterminism, but those repeats must not increase the inferential
sample size.

### Stochastic robustness run

A separately labeled sensitivity experiment may use a frozen non-zero
temperature and at least three paired seeds. Its sampling parameters require a
new manifest and cannot be pooled with the deterministic primary run.

The 2026-07-14 MicroRepair-6 qualification used two seeds at temperature zero.
Its descriptive table contains 12 run-level observations per arm, but its
inferential task count is six.

## 5. Task data

### 5.1 Qualification suite

MicroRepair-6 is an original offline harness shakeout suite:

| Category | Count | Intended capability |
| --- | ---: | --- |
| Localized | 2 | Boundary conditions, parsing, and input validation |
| Cross-file | 2 | Contract reasoning and coordinated state invariants |
| Diagnosis | 1 | Extracting useful failure signals from noisy output |
| Adversarial | 1 | Avoiding a superficial public-test-only repair |

It is not HumanEval, SWE-bench, or a general coding leaderboard. One task equals
16.7 percentage points, so it supports controller qualification and failure
analysis, not general claims.

### 5.2 Confirmatory suite

The confirmatory suite contains at least 30 frozen Python repair tasks:

| Category | Minimum count | Examples of tested behavior |
| --- | ---: | --- |
| Localized | 12 | Logic, parsing, bounds, error handling |
| Cross-file | 8 | Interface contracts, state consistency |
| Diagnosis | 6 | Noisy logs, misleading symptoms, localization |
| Adversarial | 4 | Visible-test shortcuts and verifier traps |

Source strata are reported separately:

- generated seeded mutations;
- reconstructed permissively licensed bugs;
- verifier-focused adversarial tasks.

Do not tune prompts, budgets, or stopping rules on confirmatory tasks. Build a
disjoint calibration suite with the same category schema. Freeze all decisions
before opening confirmatory aggregate results.

Every task must be offline, deterministic, pinned to a clean initial commit,
solvable by a validated gold patch, and equipped with public tests plus hidden
evaluation outside the model-visible worktree.

## 6. Strategy contracts

All strategies begin with the **same first maker prompt**, model, decoding
settings, tools, clean task state, and per-call maker output cap. This makes
differences after the first call attributable to controller behavior rather
than an unnoticed generation-limit change.

### 6.1 Direct baseline

```text
clean worktree
      |
      v
identical first maker call
      |
      v
validate edit -> apply -> public tests
      |
      +-- public fail / invalid edit --> final failure
      |
      +-- public pass --> isolated hidden evaluation --> objective outcome
```

Direct makes one model call. It is a one-shot baseline, not a loop. Unused
budget is reported as unused; it is not silently reassigned.

### 6.2 Bounded Retry

```text
clean worktree
      |
      v
identical first maker call
      |
      v
validate edit -> apply -> public tests
      |
      +-- pass --> isolated hidden evaluation --> stop
      |
      +-- fail or rejected edit
              |
              v
       deterministic feedback packet
              |
              v
       fresh maker call on evolving worktree
              |
              +-- repeat until pass or shared cap is exhausted
```

The retry feedback packet contains only:

- original agent-visible task requirements;
- current agent-visible source state or deterministic diff summary;
- failure class (`EDIT_REJECTED`, `PUBLIC_TEST_FAILED`, or budget state);
- sanitized public-test output;
- attempt count and remaining logical budget.

It contains no hidden-test result, gold information, evaluator identifier, or
model-generated summary. Retry stops immediately after public tests pass.

### 6.3 Read-only Maker–Verifier

```text
clean worktree
      |
      v
identical first maker call
      |
      v
validate -> apply -> public tests
      |
      +-- fail --> maker repair path within maker reserve
      |
      +-- pass --> checkpoint Candidate A
                         |
                         v
                  fresh read-only verifier
                         |
          +--------------+---------------+
          |              |               |
       APPROVE        REJECT          ESCALATE
          |              |               |
          |              v               |
          |       fresh maker revision   |
          |              |               |
          |       validate + public test |
          |              |               |
          |     +--------+--------+      |
          |     |                 |      |
          |   pass              fail     |
          |     |                 |      |
          | Candidate B       restore A  |
          +-----+-----------------+------+
                |
                v
        isolated post-episode evaluation
```

Before Candidate A exists, maker repair uses the same deterministic failure
packet defined for Bounded Retry. The verifier is never asked to judge an
invalid edit or a public-test-failing candidate.

The verifier receives the task requirements, Candidate A diff, required source
context, and exact sanitized public-test evidence. It receives neither the
maker conversation nor hidden evaluation. It has no write tool and must return:

```json
{
  "verdict": "APPROVE | REJECT | ESCALATE",
  "findings": [
    {
      "category": "requirement | correctness | edge_case | regression",
      "location": "agent-visible file or symbol",
      "reason": "concise evidence-based explanation"
    }
  ]
}
```

Verdict rules:

- `APPROVE`: no blocking finding; Candidate A becomes final.
- `REJECT`: at least one actionable finding; one fresh maker revision is
  allowed within the remaining maker reserve.
- `ESCALATE`: evidence is insufficient or the task is ambiguous; Candidate A
  remains final in the effectiveness track and the escalation is recorded.
- Invalid verifier JSON is recorded as `VERIFIER_PROTOCOL_ERROR`, treated as
  `ESCALATE`, and cannot modify or delete Candidate A.
- A revision that is invalid or fails public tests is recorded and Candidate A
  is restored. A public-passing Candidate B becomes final even if it later
  fails hidden evaluation; that regression is part of the measured result.

The effectiveness policy preserves a usable candidate on escalation. A future
deployment safety gate may instead block escalation, but that requires a
separate deployment manifest and cannot be mixed into this comparison.

## 7. Candidate-level evaluation

Hidden evaluation runs only after the model episode has ended and never returns
feedback to any model. For Maker–Verifier, evaluator-owned copies score:

- Candidate A: the first public-test-passing maker patch;
- Candidate B: the public-test-passing verifier-guided revision, if one exists;
- Final candidate: the controller-selected patch used for the primary endpoint.

This enables the following verifier diagnostics without contaminating the
agent:

- **true rejection:** verifier rejects A and A fails hidden evaluation;
- **false rejection:** verifier rejects A but A passes hidden evaluation;
- **approval failure:** verifier approves A but A fails hidden evaluation;
- **revision rescue:** A fails and B passes;
- **revision regression:** A passes and B fails;
- **revision no-op:** A and B have the same objective outcome;
- **protocol failure:** verifier output cannot satisfy its schema.

Candidate evaluations are telemetry. The preregistered primary endpoint remains
final-candidate objective success.

## 8. Budget design

All arms share one task-level maximum budget vector:

- cumulative rendered prompt tokens;
- cumulative completion tokens;
- model calls;
- tool calls;
- public-test runs;
- maximum context tokens in any call.

Caps are maximums, not spending targets. A strategy that succeeds early should
stop and report unused capacity.

Calibrate budget tiers on disjoint tasks. Use observed successful Direct costs
to propose caps, then verify that each strategy can exercise its intended state
machine without receiving a strategy-specific total budget.

For Maker–Verifier, reserve at most 25% of the total completion-token cap for
the verifier. Maker calls, including one revision, share the remaining 75%.
Unused verifier reserve is not converted into hidden extra calls. Record actual
logical tokens even when the runtime reuses a physical prefix cache.

Freeze a per-maker-call completion cap and use it for the identical first maker
call in Direct, Bounded Retry, and Maker–Verifier. Role reservations constrain
cumulative use; they must not silently change the first call's
`max_output_tokens`. Freeze a separate verifier per-call cap. Before every call,
the controller checks both the role reserve and the shared cumulative budget;
it does not issue a call that cannot fit both limits.

## 9. Models and fair comparisons

### 9.1 Primary loop estimate

Estimate each loop effect within one pinned model artifact. Hold task state,
prompts, decoding, context, weight quantization, KV-cache quantization, runtime,
and controller revision fixed.

### 9.2 Cross-model capability

Compare Direct baselines descriptively on the same suite. Parameter count is
metadata, not a fairness control: architectures, tokenizers, training data, and
active-versus-resident parameters differ.

### 9.3 Resource-tier comparison

For the 16 GB Mac, compare models by deployable resource tier. Qwen3.5 9B and
Gemma 4 12B are more naturally compared as similar Q4 artifact-size candidates
than as equal-parameter models. Report peak unified memory, swap, wall time, and
verified success; label this a deployment comparison.

The initial model ladder is:

- Qwen3.5 4B: low-resource control;
- Qwen3.5 9B: mid-tier coding baseline;
- Gemma 4 12B: mid-tier alternate-family candidate.

Phi-4-mini remains archived qualification evidence unless it first passes a
tool/edit-schema calibration gate. Zero-success arms are reported but do not
identify a loop effect.

## 10. Run order and Mac controls

- Run on AC power with Low Power Mode disabled.
- Use one loaded model and one request at a time.
- Pin Ollama, model blobs, context, Flash Attention, weight quantization, and
  KV-cache quantization.
- Warm the model with declared, unscored requests before measured blocks.
- Block by model and task; randomize strategy order within each task-budget
  block so Direct is not always run under the coolest state.
- Randomize task order within model blocks.
- Record macOS build, chip, unified memory, memory pressure, swap, and thermal
  state at block boundaries.
- Define a cooldown or invalidation rule before running; never cool only after
  observing a bad score.
- Restart an interrupted experiment under a new run attempt identity. Append
  events; never edit an old result line into success.

Wall time is secondary in the effectiveness track. Serving claims require
fixed-prompt serving experiments in which one serving factor changes at a time.

## 11. Endpoints and metrics

### Primary effectiveness endpoint

`objective_success = true` only when the final candidate applies, stays within
allowed paths, passes public tests, and passes isolated hidden evaluation.

### Primary effect sizes

- paired success difference in percentage points;
- rescued task count (`Direct fail -> Loop pass`);
- regressed task count (`Direct pass -> Loop fail`);
- net rescue (`rescued - regressed`).

### Cost metrics

- mean and median logical tokens per episode;
- prompt and completion tokens separately;
- model calls and public-test runs;
- mean, p50, and p95 wall time;
- extra logical tokens and seconds per net rescued task.

If net rescue is zero or negative, cost per net rescue is reported as undefined,
not zero or infinity disguised as a score.

### Verifier metrics

- verdict distribution;
- true- and false-rejection counts;
- approval-failure count;
- revision rescues and regressions;
- structured-output compliance;
- fallback and escalation frequency.

## 12. Statistical analysis

MicroRepair-6 remains descriptive. For the confirmatory suite:

1. preserve task-level pairing;
2. report raw numerator/denominator beside every percentage;
3. compute paired percentage-point differences;
4. bootstrap tasks with replacement, retaining all seed repeats for each sampled
   task, to obtain 95% confidence intervals;
5. report an exact paired binary test as a sensitivity analysis;
6. report results by task category and source stratum without pooling them into
   an unexplained composite score;
7. treat a mixed-effects logistic model as secondary only.

With 30 tasks, one transition equals 3.3 percentage points. Classify a point
estimate as a **practical measured benefit** only when all of the following hold:

- positive point estimate;
- at least three more rescues than regressions (at least +10 pp);
- no critical path, evaluator-isolation, or accounting violation.

Call the evidence **statistically resolved** only when the task-clustered 95%
interval also excludes zero in the beneficial direction. A practical benefit
whose interval overlaps zero is promising but inconclusive. Failure to meet the
practical rule is `no measured benefit`; none of these outcomes proves that all
loops are useless.

## 13. Qualification gates

The v0.2 controller may enter confirmatory evaluation only after:

1. fake-model unit tests cover every state transition and budget exit;
2. verifier prompts have no write capability and schema-invalid responses cannot
   alter Candidate A;
3. candidate checkpoint and restore behavior passes integration tests;
4. no prompt, event, error, or HTML artifact contains evaluator paths, hidden
   tests, or gold patches;
5. every task fails initially and its evaluator-owned gold patch passes;
6. the model produces valid maker edit JSON and verifier verdict JSON on a
   disjoint calibration suite;
7. all report aggregates reproduce from append-only events;
8. `make check` passes without network access or a running model server.

Do not inspect confirmatory aggregate scores to tune prompts. If a gate fails,
fix the controller using unit tests or the calibration suite, increment the
controller revision, and start a new manifest-bound experiment.

## 14. Reporting contract

The primary HTML report must show, in this order:

1. experiment snapshot and exact task data;
2. Direct capability baselines;
3. within-model loop uplift;
4. rescued, regressed, and unchanged paired outcomes;
5. logical-token and wall-time costs;
6. exact Direct, Retry, and Maker–Verifier flow diagrams;
7. verifier candidate transitions and verdict diagnostics;
8. conclusion and inference boundary;
9. serving results in a separate section.

Required conclusion grammar:

- say **"helped on this suite"**, not **"is better"**;
- say **"no measured uplift"**, not **"does not work"**;
- identify ceiling effects when Direct is near 100%;
- identify protocol failures separately from model reasoning failures;
- never call a review-and-revise editor a read-only verifier;
- never combine agent success, speed, memory, and energy into one score.

## 15. Reproducibility artifacts

Freeze and publish or retain according to repository policy:

- experiment manifest and its SHA-256;
- model and runtime artifact digests;
- controller revision, prompts, and output schemas;
- task IDs and initial commits;
- randomized run schedule;
- raw append-only model, tool, and evaluator events;
- candidate snapshot hashes and final diffs;
- derived JSON and HTML generation command;
- host and serving configuration;
- invalid, interrupted, fallback, and budget-exhausted runs.

Generated summaries are derived artifacts. Never rewrite raw outcomes to match a
desired conclusion.

## 16. Execution phases

### Phase A — Controller conformance

Implement read-only verifier output, candidate checkpoints, deterministic Retry
feedback packets, candidate-level events, and fake-model state-machine tests.

### Phase B — Disjoint calibration

Calibrate prompts, schema compliance, budget tiers, and model eligibility. Freeze
the controller and create new manifest digests.

### Phase C — MicroRepair-6 v0.2 qualification

Run the frozen controller on six tasks. Use the result only to confirm the
instrument and explain failure mechanisms.

### Phase D — Confirmatory benchmark

Run at least 30 unopened evaluation tasks. Produce task-clustered confidence
intervals and the preregistered contrasts.

### Phase E — Serving and deployment

Replay frozen prompt shapes for serving ablations, then evaluate verified tasks
under explicit wall-time, energy, or memory caps. Keep both tracks distinct from
the causal controller comparison.
