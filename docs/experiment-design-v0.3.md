# EdgeLoopBench v0.3 evidence-gated loop design

- Status: **approved; implementation complete, calibration safety-gated**
- Design date: **2026-07-15**
- Target host: **16 GB Apple Silicon, small-model-only profile**
- Primary model: **pinned Qwen3.5 4B Q4_K_M artifact**

## 1. Objective

Test whether an evidence-gated maker/checker loop improves final objective repair
success relative to Direct and Bounded Retry, while reporting its full logical
token and wall-time cost.

The design adapts the reusable control principles in
[`cobusgreyling/loop-engineering`](https://github.com/cobusgreyling/loop-engineering):

- separate maker and checker roles;
- isolated verification and a checker that cannot edit;
- bounded attempts and explicit escalation;
- early exit when no further action is justified;
- append-only run history, cost accounting, and a kill switch.

That repository describes long-running coding-agent operations rather than an
inference benchmark topology. Scheduling, PR automation, connectors, and
worktree parallelism are therefore out of scope for this experiment.

## 2. Experimental question and claims boundary

Primary question:

> On a fresh, offline 30-task repair suite, does `evidence_gated_loop` increase
> verified task success over `direct` and `bounded_retry` for the pinned 4B
> model?

This is an agent-effectiveness experiment. It will not turn wall time, memory,
or cache behavior into a serving-efficiency claim. A positive result applies
only to this model, controller revision, task suite, and budget.

The existing v0.2 confirmatory suite is not reused for the primary endpoint.
Its aggregate has already been inspected and informed this design, so rerunning
on it would be a post-hoc ablation rather than a new confirmation.

## 3. Strategy topology

All arms receive the same clean initial worktree, first maker prompt, first-call
completion cap, model artifact, decoding settings, and task-level maximum
budget. Unused budget remains unused.

### Direct

One maker call, edit validation, one public-test run, then isolated evaluation.

### Bounded Retry

Up to three maker attempts. A failed edit or public test produces a
deterministic feedback packet. A public-test pass ends the episode immediately.

### Evidence-gated loop

```text
maker -> validate/apply -> public tests
  | fail and maker attempts remain
  +---------------------------> maker retry
  |
  | pass
  v
checkpoint incumbent A -> fresh read-only checker
  | all checklist items PASS ------------------------> select A
  | any UNKNOWN -------------------------------------> escalate, select A
  | actionable FAIL and maker attempts remain
  v
maker revision -> validate/apply -> public tests
  | fail --------------------------------------------> restore A
  | pass
  v
fresh read-only re-check
  | all checklist items PASS ------------------------> select B
  | otherwise ---------------------------------------> restore A
```

There are at most three maker attempts total, including the initial call and a
checker-guided revision. There are at most two checker calls. No checker output
can write files. Candidate A remains the fallback whenever revision or re-check
does not complete successfully.

## 4. Checker contract

The checker receives only agent-visible requirements, current source/diff, and
sanitized public-test evidence. It receives no maker conversation, hidden test,
gold patch, evaluator path, or hidden-evaluation outcome.

It returns a `checks` object with one fixed key for each frozen checklist item:

1. `requirement_coverage`;
2. `boundary_conditions`;
3. `state_and_side_effects`;
4. `cross_file_contract`;
5. `regression_risk`.

Keys cannot be repeated or reordered into substitute categories. Each value
contains `PASS`, `FAIL`, or `UNKNOWN`, plus an agent-visible location
of 1-96 characters and concise evidence of 1-180 characters. Missing public
tests alone is not grounds for `UNKNOWN`: the checker must inspect the stated
requirements and visible source. The controller derives the verdict rather than trusting a
free-form verdict:

- any `FAIL` with actionable evidence -> `REJECT`;
- otherwise any `UNKNOWN` -> `ESCALATE`;
- all five `PASS` -> `APPROVE`;
- malformed or contradictory output -> protocol error and `ESCALATE`.

This makes the source repository's conservative checker stance executable and
auditable without asking the model to grade itself with an unconstrained label.

## 5. Data and preregistration

Create two new offline, deterministic, non-network suites:

- `TopologyCalibration-6`: six tasks used only for schema, prompt, and budget
  calibration;
- `ConfirmatoryRepair-B-30`: 12 localized, 8 cross-file, 6 diagnosis, and 4
  verifier-adversarial tasks used only after the controller is frozen.

Every task must initially fail, pass with its evaluator-owned gold patch, have
deterministic public tests, and keep hidden evaluation outside the agent
worktree. Calibration and confirmatory task mutations must be disjoint from the
v0.1 and v0.2 task content.

No prompt, checklist, budget, parser, or stopping-rule change is permitted after
opening any `ConfirmatoryRepair-B-30` aggregate.

## 6. Budget and stopping rules

Proposed per-task maximum for every arm:

- 20,000 cumulative logical prompt tokens;
- 4,000 cumulative completion tokens;
- 5 model calls;
- 12 tool calls;
- 4 public-test runs;
- 4,096 tokens in any one model context;
- at most 3 maker attempts and 2 checker calls.

Calibration may reduce these maxima before freezing; it may not increase them
after confirmatory execution starts. The first maker output cap is identical in
all arms. Checker and maker completion usage are recorded separately as raw
events and included in total logical tokens.

The run pauses immediately on host-safety failure, infrastructure corruption,
manifest mismatch, evaluator leakage, or repeated protocol failure. It does not
retry infrastructure errors as if they were model failures.

## 7. Endpoints and decision rules

Primary endpoint: paired final objective success across the same 30 tasks.

Prespecified contrasts:

1. `bounded_retry - direct`;
2. `evidence_gated_loop - direct`;
3. `evidence_gated_loop - bounded_retry`.

Report for each contrast:

- percentage-point difference;
- rescues and regressions;
- exact paired test and Holm adjustment;
- task-clustered bootstrap interval;
- incremental logical tokens and wall time;
- success per 1K logical tokens.

A strategy earns a practical-benefit statement only when it has positive paired
uplift, at least three more rescues than regressions, no accounting/isolation
violation, and a task-clustered 95% bootstrap interval excluding zero. Exact
paired p-values remain visible even when this frozen practical rule is met.

Checker diagnostics are secondary: checklist distribution, approval failures,
true/false rejections, revision rescues/regressions, escalations, protocol
errors, fallback rate, and the fraction of checker calls that changed the final
candidate.

## 8. Implementation and testing

Use the Python standard library and the existing module boundaries.

- Controller and schemas: `src/edgeloopbench/controller.py`
- Strategy/config validation: `src/edgeloopbench/config.py`
- Append-only event emission: `src/edgeloopbench/experiment.py`
- Result parsing and paired summaries: `src/edgeloopbench/results.py`
- Static report: `src/edgeloopbench/report.py`
- Unit/integration tests: `tests/`
- New public task manifests: `tasks/topology-calibration/` and
  `tasks/confirmatory-b/`
- Hidden evaluator assets: `evaluators/topology-calibration/` and
  `evaluators/confirmatory-b/`

Behavioral changes follow RED -> GREEN -> REFACTOR. Required tests include:

- checker cannot return edits or access evaluator assets;
- deterministic checklist-to-verdict derivation;
- public failure retries without invoking checker;
- APPROVE selects A and exits early;
- UNKNOWN/protocol error escalates and preserves A;
- REJECT revises only with an actionable finding;
- failed revision or failed re-check restores A;
- approved re-check selects B;
- three-maker and two-checker caps are never exceeded;
- logical prompt/completion tokens include every role call;
- reports render the new strategy and candidate transitions.

Verification commands:

```bash
PYTHONPATH=src python3 -m unittest tests.test_controller -v
make check
PYTHONPATH=src python3 -m edgeloopbench validate \
  configs/experiments/v0.3/confirmatory-qwen35-4b.toml
```

## 9. Execution sequence

1. Approve this specification.
2. Add failing controller, config, result, and report tests.
3. Implement the smallest complete `evidence_gated_loop` slice.
4. Add and validate `TopologyCalibration-6`.
5. Run calibration and freeze prompts, budgets, controller revision, and model
   manifest before opening confirmatory results.
6. Add and validate `ConfirmatoryRepair-B-30` without model-driven tuning.
7. Run 90 confirmatory episodes on Qwen3.5 4B, one request at a time.
8. Generate a new report block in `results/OPEN-ME/` without overwriting v0.2.
9. State only the claims supported by the frozen endpoints and decision rules.

## 10. Boundaries

Always:

- append raw events and derive summaries;
- count logical prompt tokens even with cache reuse;
- pin model, runtime, prompts, controller, task commits, and manifests;
- run only the small 4B artifact on this host;
- keep effectiveness and serving conclusions separate.

Ask first:

- change the three strategy arms or primary endpoint;
- add dependencies;
- reduce the held-out suite below 30 tasks;
- use another model or host.

Never:

- expose hidden tests, evaluator paths, or gold patches to either role;
- tune after opening confirmatory aggregates;
- pool v0.2 and v0.3 percentages as one experiment;
- claim that the external repository empirically validated this inference
  topology; it supplied design principles, not benchmark evidence.

## 11. Approval record

The three-arm design, fresh 30-task endpoint suite, and evidence-gated checker
contract were approved on 2026-07-15 before calibration results existed.
