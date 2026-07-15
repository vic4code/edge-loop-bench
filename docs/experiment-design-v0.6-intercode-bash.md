# EdgeLoopBench v0.6 InterCode-Bash causal loop study

- Status: **approved for implementation; measured scoring blocked by qualification gates**
- Design date: **2026-07-15**
- External benchmark source: **InterCode-Bash / NL2Bash**
- Primary local model: **Qwen3.5 4B**
- Gated replication model: **Phi-4-mini**

## 1. Research question

Under a fixed logical-token and action budget, and when a benchmark-native
evaluator can score every attempt, how much objective success comes from:

1. additional independent samples;
2. stateful execution feedback; and
3. a frozen engineered loop layered on the same feedback?

The primary loop-engineering contrast is `engineered_loop - raw_feedback_loop`.
The sampling and raw-feedback arms explain the mechanism; they prevent extra
test-time compute from being mislabeled as loop design.

Claude's official loop guidance defines loops as repeated work until a stop
condition and recommends explicit success criteria, quantitative verification,
bounded usage, independent review, and pilot-first rollout. v0.6 operationalizes
that narrow control-system idea: the user supplies the task once, automatic
follow-ups are counted, the verifier supplies the stop signal, and the
controller has a fixed turn/token cap. The guidance is not itself an evaluation
topology and reports no performance uplift, so it does not define an
"official" benchmark arm. Rollback, checkpoint selection, and the no-progress
guard below are EdgeLoop-owned treatment components.

This is an interactive shell-command study of a turn/goal-conditioned loop. It
is not a SWE-bench score, a repository-level software-engineering result, a
reproduction of Claude Code `/goal`, a test of time-based or proactive loops, or
evidence about unobserved human prompt counts.

## 2. Why InterCode and why Bash only

InterCode was published at NeurIPS 2023 specifically to evaluate interactive
coding with execution feedback. Its Single Turn and Try Again comparison is a
closer fit to this question than the repository's small synthetic repair suites.

The source boundary is the official GitHub repository at commit
`c3e46d827cfc9d4c704ec078f7abf9f41e3191d8`. The four committed NL2Bash files
contain 200 rows across four filesystem strata:

| Source file | Rows | SHA-256 |
| --- | ---: | --- |
| `nl2bash_fs_1.json` | 60 | `60f88e1aacc7ebba535093f9890c5c33203f4e5f32958e0e94fbe90ec4f01c82` |
| `nl2bash_fs_2.json` | 53 | `8f4ce24e535fab782fda607e37db2ae1d6c5f99993c638d1ac0a7e0b542f633e` |
| `nl2bash_fs_3.json` | 60 | `a2d4ec8bc7ad69a4e2fb3eb84033994cf65ee9cfb355e3e63099df67a339b2e1` |
| `nl2bash_fs_4.json` | 27 | `ce41b89450f87765a02a51df259ca0c1762e8249185c022adb089147e2c16200` |

The paper's Python count cannot be reconstructed as a canonical split. It
reports 117 MBPP tasks, while the pinned data file contains all 974 tasks and no
split field. Published GPT-3.5 and GPT-4 result artifacts contain different row
sets. Python is therefore excluded from v0.6 rather than silently inventing an
"InterCode-317" suite.

The separate 24-row `test_queries.json` file has SHA-256
`d24a7a1eb61c2621c48a42f942d08f6aa02066630ab49c2a07de2530a226e0aa`.
Its queries and `(query, gold)` pairs have zero exact overlap with the 200-row
population. It is used only for integration and model calibration and is never
pooled with confirmatory results.

InterCode code is MIT-licensed. The NL2Bash dataset is separately MIT-licensed;
the surrounding NL2Bash source repository is GPL-3.0. Attribution and the
upstream license texts must ship with any vendored benchmark material.

## 3. Offline qualification defines the scored population

The upstream 200 rows are the source population, not automatically the scored
population. Some rows require DNS or external networking, some setup scripts
are not fail-fast, and the upstream Docker tags are mutable. EdgeLoopBench does
not add network-dependent tasks.

Before any model sees a confirmatory task, the qualification command must:

1. assign stable IDs `bash-fs{1..4}-{zero-based-row:03d}`;
2. build four native `linux/arm64` images from the pinned setup scripts and
   immutable base-image digests;
3. run setup under a fail-fast wrapper and verify a frozen required-fixture
   inventory; every documented setup correction or exception is source-hashed;
4. run every gold command twice, each time in a new `--network none` container;
5. require a successful frozen exit-status policy, official reward `1.0`, strict
   gold-versus-gold success, and identical initial-state, normalized stdout/
   stderr, and declared observable-filesystem digests across both replays;
6. exclude network-requiring, nondeterministic, unsupported, setup-invalid, or
   evaluator-invalid rows by a machine-readable reason code;
7. commit the ordered inclusion/exclusion manifest, image digests, source-file
   hashes, evaluator revision, and suite SHA-256 before model scoring begins.

There are no outcome-dependent model exclusions. A row that passes gold replay
but defeats a model is a valid failure. The resulting suite is named
`InterCode-Bash-qualified@c3e46d8`; it is not called the unqualified full 200.
Filesystem-stratum results are mandatory, and any pooled result weights each
qualified task once.

Qualification must retain at least 160 tasks and these per-stratum floors:
48/60 for fs1, 42/53 for fs2, 48/60 for fs3, and 21/27 for fs4. Falling below
any floor aborts v0.6 confirmatory scoring; it does not authorize replacing or
cherry-picking rows. Before model scoring, the frozen qualified count is used to
publish paired-McNemar sensitivity across a declared discordance grid and the
task-cluster bootstrap's attainable precision. Failure to resolve a small true
effect remains an inconclusive outcome, not permission to weaken the endpoint.

Measured execution is sequential. Direct uses one fresh agent container per
episode. Independent uses one fresh agent container per executable attempt.
Raw and Engineered each use one fresh agent container whose state persists only
within that episode. Every attempt-level reward and every strict audit uses a
separate fresh evaluator container that is inaccessible to the model and is
destroyed immediately afterward.

All containers have unique names, no network, no host project mount, no Docker
socket, no persistent writable volume, bounded memory/CPU/PIDs, dropped
capabilities, and `no-new-privileges`. Every remaining agent container is
destroyed after the episode. Git reset inside a reused container is not an
accepted reset.

## 4. Three information channels

The adapter keeps these channels separate:

| Channel | Consumer | Allowed content |
| --- | --- | --- |
| `agent_observation` | model | bounded normalized execution output, or the frozen parser retry string |
| `controller_stop_signal` | controller; model only where declared by the arm | scalar InterCode reward and `official_success` bit |
| `objective_evaluator_output` | final analysis only | withheld evaluator result and integrity diagnostics |

Gold commands, evaluator filesystem paths, reward components, evaluation
stdout, and filesystem-diff details never enter a model request. Upstream
trajectory serialization is not used because it records gold and evaluator
internals.

The paper's Try Again prompt exposes output plus a scalar reward derived from
gold. v0.6 preserves that fact instead of presenting it as ordinary shell
feedback. Therefore any observed benefit is conditional on access to an
equivalent attempt-level verifier. It is not evidence that an unaided loop can
recognize task completion.

The benchmark-compatible endpoint is `official_success = (reward == 1.0)`.
Because the pinned upstream Bash reward has known weak equivalence checks, a
separately frozen strict evaluator also runs after controller stop. Online, it
evaluates only the final selected checkpoint and returns nothing to the model or
controller. After the complete run is sealed, retained checkpoints may be
strictly evaluated for an explicitly oracle-labeled diagnostic. The evaluator
compares each requested checkpoint with a clean gold replay over the qualified
observable surface. `strict_success` on the final checkpoint is the correctness
endpoint for EdgeLoop claims; official success is reported alongside it. A
positive claim requires selected-checkpoint official/strict disagreement in no
more than 1.0% of valid Qwen episodes and an absolute disagreement-rate gap of
no more than 1.0 percentage point between any two arms. Otherwise the controller
verifier is declared misaligned and the result remains descriptive.

## 5. Four causal arms

Let `K` be the frozen maximum attempts. All four arms use the same action
grammar, model, initial task state, cumulative budget ceilings, and candidate
seed schedule.

### A. `direct`

Send the shared initial request once, execute one Bash command, obtain the stop
signal, select that checkpoint, and run final evaluation. Unused budget remains
unused.

### B. `independent_verified_sampling`

For each attempt, create a clean environment and a fresh model context, send
the same initial request bytes, and use the next candidate seed. No earlier
command, output, score, or failure bit enters the next request. The controller
may stop on the first official success because the same attempt-level verifier
is available to all multi-attempt arms. If no attempt succeeds, the last
checkpoint is the predeclared final selection.

This arm is evaluator-guided test-time sampling, not a reward-blind deployable
selector. Post-hoc `any_strict_success@K` is labeled an oracle diagnostic and is
never used during execution.

### C. `raw_feedback_loop`

Use one persistent task environment and one continuous model transcript. After
each failed attempt, append only the frozen InterCode-style observation packet:

```text
Output: <bounded command stdout, or No output>
Reward: <scalar reward>
```

There is no rollback, deterministic diagnosis, action deduplication, or
controller summary. Stop on official success or budget exhaustion. This is the
closest v0.6 arm to the published Try Again topology, but the matched first
request and EdgeLoop isolation/evaluator rules make it an adaptation rather
than a leaderboard reproduction.

### D. `engineered_loop`

Use the same persistent state and expose the same output and scalar reward as
Raw. Only after the shared first failure, add a deterministic packet containing:

- attempt and remaining model/action/token budgets;
- last command and bounded stdout;
- admissibility, state-change digest, score, best score, and score delta;
- normalized repeated-action and repeated-state signature counts;
- whether a rollback to the highest-scoring checkpoint occurred;
- one frozen instruction to form a new failure hypothesis and issue a
  meaningfully different command.

Checkpoint selection and rollback use the declared benchmark-native score; no
private reward component is available. A lower score restores the highest-score
checkpoint. Equal scores retain the current state. Two repeated no-progress
signatures force an exploration instruction; a third terminates the episode.
The first official success always stops and is retained.

A checkpoint is the frozen tuple of a full container writable-layer snapshot,
working directory, state digest, attempt index, executed-action bytes, exit and
admissibility status, private full normalized stdout/stderr bytes and SHA-256
digests, bounded agent-observation output, and official score. Its opaque
storage handle, full private output, and host/container paths never enter a
prompt or publishable event. Restore recreates a container from the complete
writable snapshot and restores the working directory; it never rewrites the
transcript or presents stored output as a new execution. After rollback, the
next engineered packet contains the actual regressed bounded action/output plus
separate restored-checkpoint metadata. If the arm stops after no progress, it
selects the highest-score checkpoint and uses that checkpoint's associated full
private action/output/state for strict final evaluation. Official success comes
from the checkpoint's immutable stored score and is not recomputed or fabricated
during restore.

Ties select the latest highest-score checkpoint. The immutable action/output/
score tuple remains attached to that snapshot; restoring filesystem state does
not recreate stdout and is never logged as a model-visible execution. A missing
checkpoint, including an episode containing only parser failures, has
`official_success = false` and `strict_success = false`.

Every model-issued command runs under a post-action process-delta audit. A
background or residual process makes the candidate inadmissible; the controller
terminates the contaminated container and restores from the preceding full
snapshot in a new container. PID, IPC, and other ephemeral namespaces are
recreated, so off-snapshot runtime state cannot survive rollback.

This arm is a package treatment. Without later component ablations, any uplift
cannot be attributed specifically to rollback, diagnosis formatting, or the
no-progress guard.

## 6. First-call identity and stochastic schedule

For a given `(task, model, replicate)`, candidate 1 must be byte-identical across
all arms:

- identical rendered prompt bytes and prompt SHA-256;
- identical sampling seed, temperature, context limit, and output cap;
- identical model/runtime state policy;
- identical initial environment digest and action parser.

Strategy-specific text begins only after candidate 1 fails. Attempts `j > 1`
use one deterministic `candidate_seed(replicate_seed, j)` schedule shared by
Independent, Raw, and Engineered. Confirmatory replicate seeds are
`[11, 29, 47]`. Temperature is nonzero; calibration must verify that the pinned
runtime honors seeds and produces an effective unique-sample count. Repeated
prefixes count again as logical prompt tokens even when the backend reuses a
physical cache.

The common initial prompt, retry templates, parser, stdout normalization and
truncation, candidate-seed function, `K = 10`, and all numeric budgets are
hashed in a pre-calibration gate manifest. `B*` is computed from the frozen
templates, context policy, output cap, and observation cap before any model
calibration output exists; it is not selected from arm outcomes. Calibration
may reject a model or the entire design, but it cannot tune these confirmatory
parameters. No confirmatory model output may be opened until the same identities
are copied into a committed confirmatory manifest.

The final manifest separately pins model ID, immutable model revision and
artifact SHA-256, tokenizer and chat-template revisions, weight quantization,
KV-cache quantization, effective context, runtime version/artifact, and every
runtime flag. Weight and KV-cache quantization are never collapsed into one
field.

## 7. Attempt and parser accounting

One generated response consumes one attempt, one model call, and its complete
logical prompt/completion tokens. If parsing fails, it consumes no model-issued
environment action and no evaluator call. The controller records
`official_success = false` and a synthetic parser-default score of `0.0`, tagged
as non-evaluator-derived. Direct stops with no checkpoint. Independent advances
to a fresh context and seed without receiving the failure. Raw receives the
frozen parser retry string plus the tagged zero score; Engineered receives the
same facts in its deterministic packet. The first failed response is still
identical across arms.

An executable command consumes one model-issued environment action and one
attempt-level evaluator call even when its exit status is nonzero. Parser,
timeout, reserved-action, output-truncation, and admissibility rules are frozen
before calibration.

Checkpoint creation and restore are deterministic controller-maintenance
operations, not model-issued environment actions or evaluator calls. Every
executed candidate is checkpointed so selected-policy and sealed post-hoc
audits remain possible; Engineered additionally uses restore online. Creates
and restores have separate counters and manifest ceilings, and their wall/disk
cost is recorded. They do not reduce the shared candidate-attempt ceiling.
Direct simply underuses the shared create ceiling. No inference or model-
generated summary is hidden inside a maintenance operation.

## 8. Calibration and model gates

Run the 24 disjoint quickstart tasks first. They qualify mechanics and
model-task fit; they do not estimate the confirmatory effect.

A model advances only when all frozen gates pass:

- a 96-call seed probe sends each of the 24 calibration tasks' byte-identical
  initial prompt under exactly four declared seeds; at least 87/96 responses
  parse to one bounded Bash action;
- exactly 72 Direct episodes cover all 24 tasks and seeds `[11,29,47]`; strict
  success must be between 8/72 and 57/72 inclusive, avoiding a clear floor or
  ceiling;
- at least 80% of parsed commands from those 72 Direct episodes are admissible;
- at least 12/24 seed-probe tasks produce two or more distinct normalized parsed
  actions, and the sum of within-task unique parsed actions is at least 48/96;
- no evaluator leakage test, reset-isolation test, or accounting invariant
  fails;
- the combined model-plus-Docker thermal qualification passes.

All denominators, seed schedules, normalization rules, and the treatment/block
order are committed in the pre-calibration gate manifest. Infrastructure-
invalid calibration calls abort the gate rather than disappearing from a
denominator. Thresholds cannot change after calibration output is viewed.

Qwen3.5 4B is the primary model. Phi-4-mini receives the same calibration and
advances as a separately pinned replication only if it passes the gates. A
failed gate is reported as a model-task-fit or infrastructure exclusion, not as
evidence that loops do not work.

## 9. Budgets, ordering, and host safety

Every arm receives the same ceiling for cumulative logical prompt tokens,
completion tokens, model calls, model-issued environment actions, evaluator
calls, and per-call context. `K` is 10, matching the published Try Again cap.
Before a call, the controller renders the exact prompt and counts it with the
pinned tokenizer. A call whose prompt would cross `B*` is not issued; the
episode stops on its previously selected checkpoint. The per-call output cap is
clamped to the remaining cumulative completion budget. Any mismatch between
the pinned preflight count and backend telemetry makes the complete four-arm
block infrastructure-invalid. Success curves are evaluated at attempts 1, 2,
4, 8, and `K`, as
permitted by the frozen cap. Token-budget curves are secondary diagnostics;
calls have unequal cost.

Before measured scoring, the host must pass a 30-minute sustained Qwen-plus-
Docker load with append-only pre/post samples for AC status, low-power mode,
memory pressure, swap, thermal state, model residency, and running containers.
No unrelated Docker container may be running. Admission/cooldown thresholds
and the telemetry collector revision must be frozen in the manifest.

Each `(model, task, replicate)` is a four-arm block. Arm positions follow a
precomputed balanced Latin schedule whose complete order is hashed before the
run. If a safety threshold is crossed, the entire block is infrastructure-
invalid and requeued, at most twice; only rerunning the slower arm is forbidden.
Timeout and budget exhaustion are valid model failures. Infrastructure failures
are separate and receive an arm-asymmetry sensitivity analysis. A positive
primary claim requires at least 99.0% valid coverage in every arm and no more
than a 1.0 percentage-point valid-coverage gap between any two arms. Otherwise
only the preregistered worst-case sensitivity is reported.

Execution is append-only and resumable from the first missing block. It never
restarts completed valid blocks after a reboot.

## 10. Endpoints and analysis

The single primary estimand uses Qwen3.5 4B only. Let `B*` be the one cumulative
logical-prompt-token ceiling frozen after calibration and before confirmatory
scoring. For each qualified task `t` and arm `a`, define

```text
Y[t,a] = mean over seeds [11,29,47] of final selected-checkpoint strict_success
Delta  = mean over qualified tasks of (Y[t,engineered] - Y[t,raw])
```

Each qualified task has equal weight; the three seeds stay inside its cluster.
The primary result is `Delta` at `B*` under the simultaneously frozen
completion-token, call, action, evaluator, maintenance, context, and timeout
ceilings. Prefix selection uses each controller's frozen rule: Direct's sole
checkpoint; Independent's first official success or latest executed checkpoint;
Raw's current checkpoint; and Engineered's latest highest-score checkpoint.
Phi, filesystem strata, official reward, other budgets, and attempt/token curves
are secondary or replication analyses.

Required output includes:

- strict and official success at fixed logical-token budgets;
- success at attempts 1, 2, 4, 8, and `K`;
- final-checkpoint success and post-hoc oracle any-checkpoint success;
- model prompts, automatic feedback-conditioned follow-ups, independent sample
  prompts, environment actions, and evaluator calls;
- logical prompt/completion tokens and wall time;
- time/tokens to first successful checkpoint;
- paired rescues, regressions, and net rescues;
- repeated normalized actions, no-progress cycles, and admissibility errors;
- unresolved handoffs and paired avoided unresolved handoffs;
- infrastructure-invalid rates and extra tokens per net rescue.

Online curves use the controller that was actually run. An early official
success is absorbing at later attempt budgets; if that stop is a strict false
positive, strict success remains false at all later unexecuted points. Direct's
one selected result is carried forward as the no-extra-compute baseline.
Post-hoc strict curves evaluate every checkpoint that was actually executed,
only after the complete run is sealed, and never create unexecuted attempts.
`time/tokens to first strict success` is derived from the earliest executed
checkpoint that passes this post-hoc audit. Oracle any-checkpoint success is
reported separately from the selected-policy curve.

`human_prompt_count` is recorded only if a human actually intervenes. During an
autonomous benchmark it is expected to remain zero. A failed episode is an
unresolved handoff, not an invented human prompt.

Primary hypothesis:

> `engineered_loop` improves strict success over `raw_feedback_loop` by at least
> 5 percentage points at `B*`.

The practical qualification rule requires both a point estimate of at least
`+5.0` percentage points and a task-clustered paired 95% bootstrap interval
whose lower bound is above zero. It also requires more rescues than regressions,
the numeric verifier-alignment and valid-coverage gates above, and a reported
extra-token point estimate and interval per net rescue. Token cost has no
post-hoc pass threshold; it is `not applicable` when net rescues are zero.
Bootstrap resampling is by task and carries all seeds; Bash filesystem strata
are preserved.

Secondary contrasts are Raw minus Independent Verified Sampling and Independent
Verified Sampling minus Direct. Raw minus Independent changes persistent state,
continuous transcript, prior actions/output, and model-visible score jointly;
it identifies the complete stateful interaction package, not scalar feedback
alone. If verified sampling matches Raw, that package is not justified at the
tested budget. If Raw helps but Engineered does not, interaction is useful but
this engineering package is unproven. A confidence interval crossing zero is
inconclusive regardless of point estimate.

Cross-model generalization requires the same direction on every calibration-
qualified model. One-model success is reported only for that pinned model.

Wall time, thermal, memory, swap, and maintenance telemetry are descriptive
host-safety and cost measurements. v0.6 makes no serving-efficiency conclusion.

## 11. Scoring remains blocked until these artifacts exist

- vendored source/attribution and immutable task-source hashes;
- four offline-qualified image digests and a clean-reset proof;
- ordered task inclusion/exclusion manifest and suite SHA-256;
- frozen common prompt, raw packet, engineered packet, parser, and controller
  source hashes;
- strict evaluator tests, including adversarial cases for modified files;
- numeric budgets, `K`, generation parameters, and full block-order hash;
- model, tokenizer, chat-template, runtime, weight-quantization, and separately
  recorded KV-cache-quantization pins, plus seed-diversity evidence;
- 30-minute host qualification and admission/cooldown thresholds;
- a complete `make check` pass and leak-focused diff review.

Until these gates pass, v0.6 may run fake-environment tests, gold replay, reset
qualification, and one-task smoke checks only. No performance uplift is claimed.
