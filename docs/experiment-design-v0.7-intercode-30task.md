# EdgeLoopBench v0.7 InterCode-Bash 30-task compromise study

- Status: **preregistered; no measured output exists**
- Design date: **2026-07-16**
- Source: **InterCode-Bash at `c3e46d827cfc9d4c704ec078f7abf9f41e3191d8`**
- Confirmatory sample: **30 tasks, strata `9/8/9/4`**
- Models: **Qwen3.5 4B and Phi-4 Mini 3.8B, one resident at a time**
- Decoding replicate: **seed `11` only**
- Attempt cap: **`K = 4`**

This is a bounded local pilot, not an InterCode leaderboard reproduction. It
uses pinned InterCode-Bash tasks and environments, but its frozen strict
evaluator and gold-free online progress signal are EdgeLoopBench adaptations.
v0.7 does not report an official InterCode reward. Any future exact reward
adapter requires a new preregistration and cannot be inserted into this study.

## 1. Question and scope

Under the same per-episode logical-token, model-call, and model-issued-action
ceilings, does a frozen engineered feedback loop improve final strict success
over a raw feedback loop, and are either better than additional independent
samples or one Direct call? Deterministic checkpoint replay executions are
recorded separately rather than misreported as model-issued actions.

The four arms are:

1. `direct`;
2. `independent_verified_sampling`;
3. `raw_feedback_loop`;
4. `engineered_loop`.

The primary treatment contrast is Engineered minus Raw on Qwen3.5 4B. Phi-4
Mini is a prespecified replication. Raw minus Independent and Independent minus
Direct are mechanism contrasts: they distinguish stateful feedback from extra
test-time samples and extra samples from one call.

Agent effectiveness and serving efficiency remain separate. Strict success is
the effectiveness endpoint. Logical tokens, prompts, actions, evaluator calls,
wall time, and host telemetry are costs or safety observations, never folded
into correctness.

### Relation to the Claude Code loop taxonomy

Anthropic's [Getting started with loops](https://claude.com/blog/getting-started-with-loops)
defines a loop as repeated work until a stop condition is met, and separates
turn-based, goal-based, time-based, and proactive topologies. This benchmark
tests only the **within-task goal-loop mechanism**: hand off one verifiable
task once, permit bounded autonomous iterations, and stop at success or a turn
cap. It does not test scheduled triggers, event ingestion, cloud routines,
parallel-agent workflows, or unattended deployment.

The `engineered_loop` arm is therefore an EdgeLoopBench adaptation, not an
exact reproduction of Claude Code `/goal`. The official product may use an
evaluator model when the agent tries to stop. Here, online control receives
only a deterministic, gold-free candidate signal after each action, while the
private deterministic strict evaluator is consulted only for terminal scoring.
This separation prevents the goal answer from leaking into the loop. Results
can support a claim about this frozen bounded topology, not about every form of
"loop engineering" or the Claude Code product.

## 2. Frozen source and 30-task sample

The source corpus is the four official NL2Bash files already pinned in v0.6.
Their 200 rows have filesystem-stratum counts `60/53/60/27` and source-corpus
SHA-256
`b71d029f20453f96a2872b9c1a79d716f48443009acbbf916d63d0d09efc5391`.
The gold-free static-exclusion artifact is
`docs/audits/intercode-bash-static-exclusions-v1.json`, SHA-256
`ab8e1121971ff22426afa3394bb5469bae2ec7d3c6c45e323ecfe55237feb35e`.
It excludes 20 clock-dependent or unsupported-metadata rows, leaving the
eligible static-clean frame `55/45/56/24` (`N = 180`).

Within each stratum, rank eligible task IDs by the ascending lowercase SHA-256
of these exact bytes:

```text
UTF8("edgeloopbench-v0.7-intercode-30task-v1")
+ NUL
+ ASCII(source-corpus SHA-256 without a "sha256:" prefix)
+ NUL
+ ASCII(task_id)
```

Take the first `9/8/9/4` IDs. Ties, if any, break by ascending task ID. The
ordered manifest is stratum order fs1 through fs4 and hash-rank order within a
stratum; it is encoded as one task ID plus LF per row. Its SHA-256 is
`da5355df187c85b248469c6238c4f4c61dbfcca34c290e4163b55292d287fc60`.

| Stratum | Eligible | Quota | Selected task IDs in rank order |
| --- | ---: | ---: | --- |
| fs1 | 55 | 9 | `bash-fs1-032`, `bash-fs1-008`, `bash-fs1-023`, `bash-fs1-013`, `bash-fs1-048`, `bash-fs1-051`, `bash-fs1-054`, `bash-fs1-057`, `bash-fs1-055` |
| fs2 | 45 | 8 | `bash-fs2-044`, `bash-fs2-028`, `bash-fs2-046`, `bash-fs2-035`, `bash-fs2-024`, `bash-fs2-004`, `bash-fs2-009`, `bash-fs2-034` |
| fs3 | 56 | 9 | `bash-fs3-013`, `bash-fs3-054`, `bash-fs3-025`, `bash-fs3-005`, `bash-fs3-037`, `bash-fs3-036`, `bash-fs3-050`, `bash-fs3-052`, `bash-fs3-006` |
| fs4 | 24 | 4 | `bash-fs4-020`, `bash-fs4-000`, `bash-fs4-010`, `bash-fs4-024` |

Before any model request, each selected task must pass two fresh gold replays
in a pinned native `linux/arm64`, `--network none` container. Both replays must
agree on the frozen exit policy, normalized output, and declared observable
state. No failed row is replaced. Any failure stops v0.7 before calibration
rather than changing the sample.

## 3. Evaluator and model-visible evidence

Every admissible candidate checkpoint is captured immediately by the pinned,
root-owned trusted state collector before its agent container is released. The
terminal evaluator compares that sealed private material with one clean,
model-inaccessible gold replay from the scoped original image. Engineered
rollback recreates a clean agent container, replays the selected action prefix,
and requires the replayed state and output to equal the captured checkpoint;
any mismatch is infrastructure-invalid. The frozen strict predicate is:

```text
strict_success =
    exit_policy_equal
    AND normalized_stdout_equal
    AND normalized_stderr_equal
    AND observable_state_equal
```

Normalization, observable-state collection, comparison order, byte ceilings,
and evaluator source hashes must be committed before calibration. Gold
commands, expected output, state diffs, evaluator paths, and strict outcomes
never enter a model request or an online controller decision. A missing final
checkpoint is strict failure; an evaluator or isolation fault is
infrastructure-invalid, not model failure.

Online attempts expose only bounded normalized stdout/stderr, exit status,
admissibility, state-change identity, and a preregistered gold-free progress
score:

```text
progress = 0.20 * parsed_single_action
         + 0.20 * action_admissible
         + 0.20 * (exit_status == 0)
         + 0.20 * (state_changed OR normalized_output_nonempty)
```

Each component is Boolean and candidate-only. This heuristic may be poorly
aligned with correctness; that is a declared limitation, not an official
reward. It is used identically wherever a controller needs an online ranking.
The deliberate `0.8` maximum keeps the ranking signal disjoint from the
controller's exact-`1.0` success stop: candidate-only progress can never claim
task completion or terminate an arm as correct. Strict evaluation occurs only
after the episode has durably selected a final checkpoint.

## 4. Four arms and matched budgets

For a given `(model, task, seed=11)`, candidate 1 has identical prompt bytes,
sampling parameters, initial environment, parser, and seed across all arms.
Strategy text begins only after a first failure or continuation.

- **Direct:** one fresh context and environment, one attempt, select its
  admissible checkpoint if present.
- **Independent Verified Sampling:** four fresh contexts and environments with
  no prior action, output, score, or transcript. “Verified” means only that the
  same candidate-surface policy and progress function ran; it does not mean
  strict or official success. Select the maximum-progress checkpoint, breaking
  ties by earliest attempt.
- **Raw Feedback Loop:** one persistent environment and transcript for up to
  four attempts. Append only the previous command's bounded output, exit
  status, and progress score. There is no diagnosis, rollback, deduplication,
  or no-progress instruction. Select the latest admissible checkpoint.
- **Engineered Loop:** the same persistent state and visible evidence as Raw,
  plus a deterministic packet with remaining budget, repeated-action and
  repeated-state counts, progress delta, and a frozen instruction to form a
  new failure hypothesis. A lower-progress candidate restores the best prior
  checkpoint; ties retain the latest checkpoint. Three repeated no-progress
  signatures stop early. Select the latest maximum-progress checkpoint.

`K = 4` is the compromise cap. It provides three opportunities for autonomous
correction and supports attempt-1/2/4 diagnostics while reducing the
confirmatory ceiling from 1,140 prompts at `K = 6` to 780. It is not the
published InterCode Try Again cap and cannot support claims about longer loops.

Per `(model, task)` the maximum is `1 + 4 + 4 + 4 = 13` model prompts. Across
30 tasks and two models, confirmatory execution is capped at **780** prompts:

| Prompt class | Maximum |
| --- | ---: |
| Initial episode prompts | 240 |
| Additional independent-sample prompts | 180 |
| Raw feedback-conditioned follow-ups | 180 |
| Engineered feedback-conditioned follow-ups | 180 |
| **Total** | **780** |

Every non-Direct episode receives the same frozen ceiling; Direct leaves its
unused capacity unused. The exact per-episode ceiling is:

| Resource | Ceiling |
| --- | ---: |
| Attempts / model calls / model-issued environment actions | 4 |
| Replayed environment actions (stateful loop maximum) | 6 |
| Physical environment actions (stateful loop maximum) | 10 |
| Per-call context | 4,096 logical tokens |
| Per-call completion | 512 logical tokens |
| Episode logical prompt tokens | 16,380 |
| Episode logical completion tokens | 2,048 |
| Candidate plus terminal evaluator calls | 5 |
| Checkpoint creates / restores / safety recoveries | 4 each |

The prompt ceiling is `4 * (4096 - 1)` so every admitted call retains room for
at least one completion token. These are logical limits, not allowances to
discount a physically cached prefix.

Every request counts its complete logical prompt and completion tokens even if
the runtime reuses a prefix cache. Parser failures consume a prompt and attempt.
Unused calls remain unused after a policy or no-progress stop. The final
manifest separately pins prompt templates, parser, controller, budgets,
runtime, model artifacts, tokenizer/chat templates, weight quantization, and
KV-cache quantization.

A checkpoint restore or safety recovery reconstructs an admitted prefix in a
fresh environment. Those deterministic Bash executions are not new model
decisions, so they do not consume the four model-issued-action slots. They are
nevertheless physical tool executions: every recovery journals its exact
replay count, the shared episode cap is `K * (K - 1) / 2 = 6`, and reporting
gives model-issued, replayed, and total physical actions separately. Direct and
independent sampling carry no cross-call state and therefore replay zero
actions. The Raw topology has a tighter four-action maximum of four replays;
Engineered can reach six through checkpoint restoration. The frozen
conservative formal envelope is therefore 780 model-issued plus 720 replayed
actions, or 1,500 physical environment actions. Exact topology validation
tightens the reachable replay maximum to 600 (240 Raw plus 360 Engineered), so
the reachable physical maximum is 1,380; both the conservative gate and the
observed totals are retained in evidence.

## 5. Limited calibration and wall-time gate

Calibration uses only the disjoint upstream quickstart tasks
`bash-calibration-000` through `bash-calibration-003`. Per model, assign one
task to each arm in the arm order above. Direct permits one request and each
other arm permits four, for at most **13 calibration prompts per model, 26
total**. The same frozen prompts, parser, controller, runtime, and `K = 4` are
used. Calibration outcomes cannot tune the confirmatory task set, prompts,
progress score, selection rules, budgets, or endpoint.

The calibration executor accepts the builder-sealed pre-calibration manifest,
not a caller-supplied manifest digest, and derives the bound digest from that
typed authority. The trusted replay authority's calibration-campaign SHA-256
is bound into the calibration declaration, every per-episode begun marker,
and the final verifier-issued evidence. Before runtime construction can issue
work for a row, its mode-`0600` begun marker is durably sealed. A marker or
other row artifact without the exact recorded terminal therefore halts resume;
it never authorizes an automatic retry.

Operator-intervention accounting remains a separate live append-only journal.
Because its terminal root cannot exist before execution, calibration accepts
no caller-supplied intervention digest. Any later report must consume the
verifier-issued sealed intervention summary rather than a bare root string.

A model advances only if all four calibration episodes are infrastructure-valid,
at least three of four first responses parse to one bounded Bash action and are
admissible, accounting balances exactly, no evaluator material leaks, and host
safety holds. There is no strict-success floor or ceiling on four tasks. A
failed model is reported as a calibration or model-task-fit exclusion, never a
negative loop result.

Let `C_m` be the summed active wall time of that model's four calibration
episodes. Before confirmatory output is opened, record:

```text
estimated_confirmatory_active_time = 30 * sum_m(C_m)
planning_bound = 1.5 * estimated_confirmatory_active_time
```

The estimate is an operational projection, not a performance result. Do not
start confirmatory execution if the two-model planning bound exceeds 18 active
hours. Confirmatory execution also stops before the next episode at 18 active
hours. Reaching either limit makes the study incomplete; it does not authorize
changing `K`, tasks, arms, or denominators.

## 6. Append-only execution and interruption policy

Execution is sequential: Qwen3.5 4B first, then Phi-4 Mini 3.8B, with only one
model resident. Within each model, process tasks in the frozen manifest order.
Arm position rotates by the four-row Williams order `[D,I,E,R]`, `[I,R,D,E]`,
`[R,E,I,D]`, `[E,D,R,I]`, repeated over tasks. This balances arm position
without making a task-level block the recovery unit.

One mode-`0600`, hash-chained campaign journal declares ordering and appends an
intent before each episode callback. Each `(campaign, model, task, arm, seed)`
also has its own mode-`0600`, hash-chained controller journal and a distinct
mode-`0600`, hash-chained execution envelope. The runner may create and seal
that envelope only after the controller journal is terminally sealed and the
post-episode host sample has been captured. Its single execution record binds
the exact episode identity, complete `InteractiveResult`, controller-journal
root, active wall time, and both host samples; its file and parent creation are
durably synchronized before it is returned.

A terminally sealed valid episode is skipped on resume and never reissued. A
started campaign intent with no terminal campaign event halts v0.7. A caller
may provide an explicit pending reconciler that only locates the already
written execution envelope for that exact intent. The campaign independently
reopens and verifies the envelope; only an exact, sealed, regular,
owner-mode-`0600`, identity-stable envelope may close the intent, without
entering the model executor. A missing, mismatched, symlinked, non-regular,
wrong-mode, changed, corrupt, or unsealed envelope leaves the journal bytes and
pending intent unchanged. There is **no automatic replay**, and the campaign
cannot advance to a never-started episode under this protocol version.

There is no block-level invalidation, replay, or crash recovery, and completed
arms are never deleted to manufacture a balanced block. Cleanup may reconcile
exact owned resources, but it cannot authorize another model request or clear
the campaign's incomplete status. Derived analysis reports valid coverage by
arm and complete-pair coverage for every contrast. Interrupted or
infrastructure-invalid outcomes are never counted as model failures.

An infrastructure-invalid terminal is appended as evidence but is also a hard
campaign stop: every future advance fails closed before another episode intent
or model callback. It is not permissible to finish the remaining schedule
after an infrastructure-invalid row.

The campaign also has a cumulative active-model execution limit of exactly
`64,800,000,000,000 ns` (18 hours), summed from completed episode terminals.
Once the sum reaches the limit, every invocation with no pending intent fails
with the typed active-time-limit error before appending another intent or
entering the model executor. An already pending intent may first be reconciled
from its valid sealed execution envelope; the following invocation then
enforces the cumulative limit. Reconciliation never authorizes another model
request.

The study is “complete” only with 30/30 qualification tasks, both calibration
gates, and **240/240 sealed valid confirmatory episodes**. Any interruption,
missing episode, model exclusion, manifest drift, or invalid evaluator makes
v0.7 incomplete. Descriptive coverage and worst-case missing-outcome bounds may
still be published, but no performance-uplift claim may be made.

## 7. Estimands and inference

For model `m`, task `t`, and arm `a`, let `Y[m,t,a]` be final selected-checkpoint
strict success. With eligible stratum counts `N_h = [55,45,56,24]`, define:

```text
arm_rate[m,a] = sum_h (N_h / 180) * mean_{t in sampled stratum h} Y[m,t,a]

delta[m,baseline,candidate]
  = arm_rate[m,candidate] - arm_rate[m,baseline]
```

The primary estimand is
`100 * delta[Qwen, Raw, Engineered]` percentage points. A rescue is a task with
`Y[Raw]=0, Y[Engineered]=1`; a regression reverses those values. The weighted
point estimate uses the eligible-frame stratum weights, while rescue,
regression, and exact McNemar counts remain unweighted paired-task counts.

The primary 95% interval uses 10,000 task-cluster bootstrap replicates with
PRNG seed `20260716`: resample task IDs with replacement within each stratum,
carry all four arms together, and reapply `N_h/180` weights. The report also
gives the unweighted rescue/regression table and two-sided exact McNemar test
for the Qwen primary pair. With one seed, task is the cluster; decoding-seed
variance is not estimated.

A model-specific positive result requires complete execution, a point estimate
of at least `+5.0` percentage points, a bootstrap lower bound above zero, more
rescues than regressions, and exact McNemar `p < 0.05`. A cross-model uplift
statement additionally requires the same direction on Phi; a strong
cross-model claim requires Phi's interval lower bound above zero as well. The
single Qwen primary test is the only confirmatory route. Phi and every secondary
contrast are replication or descriptive analyses; their intervals and p-values
are labeled unadjusted and cannot substitute for a failed primary result.

The frozen reporting classifier is symmetric enough to avoid calling clear
negative evidence "inconclusive." For the Qwen primary contrast only:

- `positive_threshold_met` means every positive-result condition above holds;
- `negative_harm_signal` means the estimate is at most `-5.0` points, the
  interval upper bound is below zero, regressions exceed rescues, and exact
  McNemar `p < 0.05`;
- `positive_below_practical_threshold` means the estimate and interval are
  above zero but the `+5.0`-point practical threshold is not met; and
- every other non-positive outcome is `inconclusive_not_equivalence`.

The negative label is evidence against this frozen Engineered package relative
to Raw; it is not a claim that all loop engineering is harmful. Replication and
mechanism rows carry the same descriptive direction classifier for readability,
but are explicitly marked `unadjusted` and never become additional
confirmatory decision routes.

Thirty tasks and one seed provide limited power and coarse binary resolution.
A null result or interval crossing zero is inconclusive, not equivalence and
not evidence that loop engineering never works. The deterministic hash sample
also relies on a pseudorandom-ranking/exchangeability assumption; it is not an
external randomness-beacon draw.

## 8. Prompt, handoff, and cost accounting

Record these counters separately:

- first model prompt in each episode;
- additional independent-sample prompt;
- automatic feedback-conditioned follow-up;
- complete logical prompt and completion tokens;
- model-issued actions, deterministic replayed actions, their physical total,
  progress evaluations, strict evaluations, and wall time;
- actual human interventions during the campaign;
- valid final strict failures as unresolved handoffs;
- paired rescues, regressions, net rescues, and avoided unresolved handoffs.

An unresolved handoff is not retroactively converted into a human prompt. No
counterfactual “manual prompts saved” value is reported. The operational claim,
if supported, is limited to observed automatic prompt orchestration and paired
avoided unresolved handoffs.

### Pre-scoring amendment on 2026-07-20

Before any tokenizer request, model load, calibration episode, or confirmatory
episode, an adversarial accounting review found that rollback replay executions
were physically performed but not represented in `InteractiveResult`. The
controller/evidence schemas were amended to add the bounded replay counter and
the cost language above was narrowed from generic equal action ceilings to
equal model-issued-action ceilings. The same review corrected the reporting
classifier so `positive_below_practical_threshold` requires a point estimate
strictly below `+5.0` points; an above-threshold estimate that fails another
positive-result condition is `inconclusive_not_equivalence`. No prompt,
candidate, strict outcome, or model-dependent signal informed either change.

The sealed-evidence verifiers also reconstruct the exact Engineered restore
policy from event order, candidate rewards, checkpoint identities, and replay
depths. A restore must complete before the next prompt preflight, must occur
only on a strict regression, and must target the best prior checkpoint; an
equal reward makes the latest checkpoint the new best. Rechained journals with
duplicate, late, tie, stale-best, wrong-target, or wrong-terminal-selection
events are rejected even when their aggregate counters balance.

A second outcome-independent review found that model-caused policy failures
also recreate a fresh environment and replay the previously admitted prefix.
The typed action result, controller journal, campaign ledger, calibration
evidence, and analysis now include that safety-recovery replay count. Verifiers
reconstruct it from the arm and current admitted depth; Raw and Engineered may
report it, while Direct and independent sampling must report zero. The shared
triangular replay cap remains six because one attempt can take the safety-
recovery branch or the checkpoint-restore branch, never both. This correction
and its superseding scope are recorded in
[ADR 032](decisions/032-account-for-safety-recovery-replays.md).

### Docker storage-policy amendment on 2026-07-20

The first qualification lifecycle stopped at container creation, before any
prompt, model load, candidate, or scored outcome, because the admitted Docker
Desktop `overlay2` backend does not support the requested per-container
`--storage-opt size` quota. Before restarting qualification, the unsupported
flag and hard-quota claim were replaced by the frozen mode
`sampled-size-rw-no-hard-quota-v1`: an attested 16 MiB soft/hard `fsize` ulimit
plus exact-container `SizeRw` sampling before, every 0.25 seconds during, and
after each action, with a 1-second probe timeout and a 256 MiB threshold. Any
probe ambiguity fails closed; a during-action signal terminates execution and
triggers exact-label cleanup. This sampled watchdog can overshoot between
probes and is explicitly **not** a hard aggregate quota.

The amended canonical execution record pins the mode, threshold, ulimit,
cadence, and timeout. It advances the pre-calibration manifest schema to
`intercode-v0.7-precalibration-manifest-v2` and the replay authority to
`intercode-v0.7-docker-qualification-authority-v2`; their derived identities
therefore cannot accept or mix evidence from the attempted v1 lifecycle.
Qualification restarts from a fresh source inventory. No prompt, controller,
task, arm, model, budget, evaluator, or outcome rule changed. The rationale and
upstream capability constraints are recorded in
[ADR 030](decisions/030-replace-unsupported-storage-quota-with-sampled-watchdog.md).

A final watchdog review added bounded 50 ms process polling after output EOF,
removed controller-generated `-9` values from action-stage overflow results,
preserved completed-command exit codes for post-storage overflow, and made an
attested exited-container probe race flow to the frozen
`container_terminated` policy result. Probe or lifecycle ambiguity still fails
closed. [ADR 033](decisions/033-preserve-watchdog-semantics-across-process-races.md)
records the outcome-independent change.

### Pre-image admission stabilization on 2026-07-20

Production handoffs repeatedly showed that Docker Desktop can restart two
previously inventoried non-benchmark containers after an external stable-host
window but before the runner's one-shot full admission sample. Polling and a
persistent read-only Docker event stream did not remove that race. Attempts 13
and 14 then showed a second bounded transition: after an exact-ID steward
stopped both containers, the next sample had no running container but
transient VM pressure level `2`. Every cited attempt stopped before image-build
evidence, model loading, calibration, or any model prompt.

The production runner therefore freezes one bounded read-only stabilization
immediately before image planning. Its policy still evaluates
`ExpectedHostResources()` with no resident models and no running containers.
Configuration may name either zero or exactly two full, sorted, unique
stewarded container IDs, but those IDs are not accepted resources. A denial is
waitable only when its reason set is `VM_PRESSURE`, `RUNNING_CONTAINERS`, or
both. If `VM_PRESSURE` is present, the raw pressure level must be exactly `2`;
levels `0`, `3`, and `4` are hard denials. If `RUNNING_CONTAINERS` is present,
the observed nonempty ID set must be a subset of the configured pair.
Production itself never mutates those containers; an external steward may
reconcile only the exact pre-inventoried IDs. Every unknown container, any
other policy reason, telemetry or liveness failure, identity change, and
sample-order or cooldown failure stops immediately. Every waitable denial
resets the clean-sample streak.

This is a bounded cooldown, not a pressure-threshold relaxation. The admitted
baseline still requires two consecutive fully allowed pressure-level-`1`
samples 30 seconds apart and must stabilize within 600 seconds. A fresh `O_EXCL`,
owner-mode-`0600`, identity-bound journal records every raw path-free sample and
derived decision in a hash chain. It is terminally sealed and reverified, and
the accepted sample is re-derived from the sealed evidence before any image
mutation. Runner revision
`intercode-v0.7-production-runner-v6-bounded-pressure-cooldown` and journal
revision `intercode-v0.7-image-build-admission-journal-v2` prevent earlier
attempts from mixing with the amended evidence. The v2 declaration pins
`retryable_vm_pressure_levels: [2]`, so replay does not infer this boundary
from mutable runner behavior.
[ADR 035](decisions/035-stabilize-pre-mutation-host-admission.md) records the
stabilization boundary;
[ADR 036](decisions/036-bound-transient-vm-pressure-cooldown.md) records the
pre-outcome retry amendment.

## 9. Stop gates before model scoring

Stop rather than modify the design if any of these occurs:

- source, audit, sample, image, runtime, model, tokenizer, prompt, controller,
  progress, strict-evaluator, or schedule identity differs from its manifest;
- any selected task fails its two offline gold replays;
- `make check`, isolation, clean-reset, accounting, or leak tests fail;
- calibration or its 18-hour planning gate fails;
- AC power is lost, Low Power Mode is enabled, VM pressure is not normal outside
  the explicitly bounded pre-image level-`2` cooling wait, an unexpected
  model/container appears, or the frozen v0.7 memory/swap/disk/thermal
  threshold is crossed;
- the prompt ceiling, active-time ceiling, or any per-episode budget is crossed;
- an episode is interrupted or any journal/resource identity is ambiguous.

No 9B or 12B model is loaded. No network-dependent task is added. No v0.7 HTML,
README result, performance table, or uplift statement is produced until the
complete study and derived analysis pass the frozen integrity gates.
