# EdgeLoopBench

**A reproducible local study of when agent loops help—and when they only spend
more inference.**

[Open the current interactive result](results/OPEN-ME/index.html) ·
[Read the v0.4 run record](docs/runs/v04-goal-skill-loop-pilot.md) ·
[Inspect the v0.4 design](docs/experiment-design-v0.4.md) ·
[Review the proposed faithful `/goal` experiment](docs/experiment-design-v0.5-fresh-evaluator-goal.md)

## Abstract

Loop engineering is often presented as a way to improve agent quality by
letting a model retry, inspect evidence, and decide when to stop. The important
question is not whether a loop can make more model calls. It is whether those
calls produce enough additional verified successes to justify their logical
token and wall-time cost.

EdgeLoopBench tests that question with pinned open-weight models on Apple
Silicon. The current v0.4 pilot compares one-call Direct, three-attempt Bounded
Retry, and a five-attempt Goal Skill Loop adapted from Anthropic's official
loop guidance. It uses eight fresh offline Python repair tasks, isolated hidden
evaluation, identical episode-level budgets, append-only raw events, and
within-model paired statistics.

The motivation for local inference is practical: exploratory controller work
can consume many repeated prompts before a topology is worth scaling. Running
small models locally avoids spending cloud API tokens during qualification and
makes memory, latency, and token overhead directly observable. Local inference
is not treated as free; hardware time and resource pressure are part of the
experimental record.

## Main result

On this eight-task qualification suite, neither loop improved aggregate
verified success over Direct.

| Model | Strategy | Verified success | Mean logical tokens | Mean wall time |
| --- | --- | ---: | ---: | ---: |
| Phi-4 Mini 3.8B | Direct | 1/8 (12.5%) | 607 | 6.4 s |
| Phi-4 Mini 3.8B | Bounded Retry | 1/8 (12.5%) | 1,794 | 14.1 s |
| Phi-4 Mini 3.8B | Goal Skill Loop | 1/8 (12.5%) | 3,694 | 26.7 s |
| Qwen3.5 4B | Direct | 3/8 (37.5%) | 582 | 6.5 s |
| Qwen3.5 4B | Bounded Retry | 3/8 (37.5%) | 1,088 | 10.8 s |
| Qwen3.5 4B | Goal Skill Loop | 3/8 (37.5%) | 1,837 | 18.1 s |

The paired outcomes are more informative than the aggregate tie:

- Phi-4 Mini had zero rescues and zero regressions under either loop.
- Qwen3.5 4B Goal Skill Loop rescued one cross-file task but regressed one
  localized task: net rescue `0`, exact paired `p = 1.0`, task-bootstrap 95%
  interval `[-37.5, +37.5]` percentage points.
- Qwen Goal Skill Loop used `3.16×` Direct's logical tokens and `2.80×` its
  wall time. Phi used `6.09×` the tokens and `4.16×` the wall time.

The supported conclusion is deliberately narrow: this goal-and-skill topology
did not qualify for a larger performance-uplift claim on these models and
tasks. The result is not evidence that all loop engineering fails. It is
evidence that additional test-time compute has value only when the model can
reliably convert visible failure evidence into a better candidate.

## Why the v0.4 adaptation did not improve the total

The controller worked as specified; the task distribution did not consistently
need the capability that a loop adds. These repairs were static and fully
agent-visible: the instruction, source, and public tests were already present
before the first model call. A loop is most defensible when execution reveals
new information that was unavailable at the start—runtime errors, changing
external state, measurements, user corrections, or verifier scores.

The raw event chains separate three effects that an aggregate score can hide:

1. **Actual feedback recovery.** On Qwen `v04-cross-file-001`, Goal Skill Loop
   first returned `99.9` instead of `90.0`, observed the public-test failure,
   corrected the formula on call two, and passed isolated evaluation. This
   proves that the feedback loop can recover without human intervention.
2. **Prompt/skill effect, not iteration.** On `v04-cross-file-002`, the Goal
   arm passed on its first call while Direct failed. The paired outcome is a
   rescue, but no retry occurred; the frozen verification skill changed the
   initial answer.
3. **Verifier blind spot.** On `v04-localized-001`, the Goal arm passed the
   public stop condition but failed isolated evaluation, while Direct passed.
   Because hidden feedback is correctly unavailable, another loop iteration
   could not repair it.

Thus, a loop controller is not a treatment that guarantees uplift. It provides
a control surface—goal, observations, retries, verification, and stop rules.
It helps only when the observations carry actionable new information, the
verifier aligns with the real objective, and the model can use the signal. On
this pilot, one genuine self-correction prevented a failure inside the Goal
arm, but it did not create a net advantage over an already-correct Direct arm.

### Post-run topology audit

The v0.4 name `goal_skill_loop` describes the implemented adaptation; it must
not be presented as a reproduction of Claude Code `/goal`. The official
topology starts a full main-agent turn, then asks a fresh small evaluator to
judge the goal from the transcript. A negative judgment and its reason start a
new main-agent turn. The evaluator does not run tools or inspect files itself.

In contrast, v0.4 runs deterministic public tests directly after each Maker
edit and returns sanitized test evidence to the same Maker role. That is a
test-driven bounded controller with a goal prompt and fixed skill. It omits the
fresh stop evaluator, separate evaluator-token accounting, and evaluator reason
channel that distinguish official `/goal`. This audit narrows the claim: v0.4
is evidence about the committed adaptation only.

### Prompt and human-handoff accounting

One model call is one logical model prompt. `Automatic follow-ups` are calls
after the first call in an episode. `Feedback-converged successes` are episodes
that failed an earlier visible check and later reached objective success.
`Unresolved handoffs` are final failures that would return control to a user;
they are not retroactively counted as human prompts because no manual
continuation was observed.

| Strategy | Model prompts | Automatic follow-ups | Feedback-converged successes | Unresolved handoffs |
| --- | ---: | ---: | ---: | ---: |
| Direct | 16 | 0 | 0 | 12/16 |
| Bounded Retry | 33 | 17 | 0 | 12/16 |
| Goal Skill Loop | 54 | 38 | 1 | 12/16 |

By model, Phi used `8 / 20 / 36` prompts for Direct / Bounded Retry /
Goal Skill Loop; Qwen used `8 / 13 / 18`. The only feedback-converged success
was Qwen Goal Skill Loop on `v04-cross-file-001`. Therefore v0.4 automated 38
Goal follow-up prompts but did not reduce the final number of user handoffs.
This is the operational reason the current loop does not yet support a
“hands-free” claim.

For a hands-free automation claim, the better primary endpoint is not only
clean-condition pass rate. It is **intervention-free completion**: how often an
agent recovers from execution feedback without asking a human to diagnose and
restart the task. That requires a benchmark with information-revealing,
multi-step environments rather than only static repair prompts.

## External benchmarks that exercise loops

| Benchmark | What it measures | Fit for this project | Local-pilot constraint |
| --- | --- | --- | --- |
| [LongCLI-Bench](https://github.com/finyorko/longcli-bench) | 20 long-horizon CLI tasks; F2P/P2P tests, step score, and explicit `--give-test-output` self-correction turns | Best coding benchmark for comparing no feedback with one or more correction turns | Requires Docker, Python 3.12, and a compatible agent adapter |
| [Frontier-Eng](https://lab.einsia.ai/frontier-eng/) | Iterative propose–execute–evaluate optimization with frozen executable verifiers and continuous reward | Best direct test of whether deeper loop iterations improve an objective | Engineering simulators are heavy; even the 10-task lite set is difficult for small models |
| [General AgentBench](https://github.com/cxcscmu/General-AgentBench) | Sequential and parallel agent test-time scaling across coding, search, reasoning, and tools | Strong external comparison for context ceilings and verification gaps | Several tracks use external APIs and large hosted models |
| [SWE-Together](https://togetherbench.com/) | Final correctness plus the number of corrective user-feedback turns | Closest match to “how much can I take my hands off the keyboard?” | Repository containers and an LLM user simulator make it expensive locally |
| [RigorBench](https://github.com/MeherBhaskar/RigorBench) | Planning, verification, recovery, abstention, testing, and trajectory discipline | Useful process audit for whether an agent loops responsibly | Composite scoring and judge-based components are less objective than frozen executable verifiers |
| [Test-Time Interaction](https://test-time-interaction.github.io/) | Whether longer environment interaction enables exploration, backtracking, and new-information gathering | Strong evidence for the task class in which loops should matter | Web environments are network-dependent and therefore are not imported into this offline suite |

The recommended next external validation is a small preregistered
[SWE-bench Verified](https://www.swebench.com/SWE-bench/) pilot because it is
the most widely recognized repository-level coding-agent benchmark. The causal
comparison should hold task, model, and total budget fixed while changing only
the stop controller: Direct versus fresh-evaluator Goal. LongCLI-Bench remains
the lighter benchmark for explicitly varying zero versus one versus three
test-feedback turns, and Frontier-Eng v1-lite is the next choice when continuous
improvement—not binary repair—is the research question.

The official SWE-bench harness requires Docker. Its setup guide recommends at
least 16 GB allocated to Docker and roughly 120 GB of working storage for the
standard evaluation workflow. The current 16 GB host and available disk do not
meet that safety envelope, so the official pilot is resource-blocked locally;
running an unofficial host-only evaluator would not be reported as SWE-bench.
External suites should be used through adapters that preserve their upstream
environment, license, and evaluator rather than copied into this offline task
catalog.

## Research question

> Under a fixed episode-level logical budget, does a verifiable goal, a frozen
> verification skill, and a larger attempt cap produce positive net paired
> rescue over simpler controllers at an acceptable inference cost?

The primary endpoint is objective verified repair success. Token use, model
calls, public-test runs, and wall time are costs. Serving efficiency remains a
separate track and is never folded into an agent-quality score.

## Controllers

| Arm | Stop rule | Maximum Maker attempts | Feedback |
| --- | --- | ---: | --- |
| Direct | First outcome | 1 | None |
| Bounded Retry | Public pass or cap | 3 | Sanitized edit/public-test failure |
| Goal Skill Loop | Public goal achieved or cap | 5 | Same failure evidence plus frozen verification skill |

The Goal Skill Loop maps selected goal, verification-skill, and bounded-retry
ideas into a deterministic benchmark controller:

```text
visible task + fixed verification skill
                  │
                  ▼
          Maker emits full-file edits
                  │
          validate → public tests
             │              │
          failure          pass
             │              │
 sanitized evidence         └── stop
             │
       repeat, at most five attempts

hidden evaluation runs only after the episode
```

The skill instructs the model to inspect requirement coverage, boundary
conditions, state and side effects, cross-file contracts, and regression risk.
Public tests are the only agent-visible deterministic stop condition. Hidden
tests, gold edits, evaluator paths, and hidden outcomes never return to the
model.

This is an adaptation of Anthropic's published control principles, not a claim
to reproduce Claude Code's internal `/goal` evaluator. See
[Getting started with loops](https://x.com/i/article/2074204645845839872) and
the official [`/goal` documentation](https://code.claude.com/docs/en/goal).

## Experimental configuration

### Workload

- `OfficialLoopPilot-8`: eight original, deterministic, offline Python repairs
- category mix: three localized, two cross-file, two diagnosis, one adversarial
- every initial task fails its public tests
- an evaluator-owned gold edit passes both public and hidden layers
- one frozen seed; the same task is paired across all three strategies
- 24 episodes per model; 48 valid episodes in the published comparison

### Shared episode ceiling

| Resource | Maximum |
| --- | ---: |
| Logical prompt tokens | 30,000 |
| Completion tokens | 5,000 |
| Model calls | 5 |
| Tool calls | 12 |
| Public-test runs | 5 |
| Per-call context | 4,096 tokens |

Unused budget remains unused. This is important: Direct is not forced to make
five calls merely to equalize consumption. The comparison measures tested
controller behavior and reports the resulting cost.

### Models and runtime

| Variable | Phi experiment | Qwen experiment |
| --- | --- | --- |
| Model | Phi-4 Mini 3.8B | Qwen3.5 4B |
| Weight quantization | Q4_K_M | Q4_K_M |
| KV-cache quantization | q8_0 | q8_0 |
| Context | 4,096 | 4,096 |
| Runtime | Ollama 0.31.1 | Ollama 0.31.1 |
| Decoding | temperature 0.0, thinking off | temperature 0.0, thinking off |

Model revisions, artifact digests, controller digest, backend digest, prompts,
budgets, tasks, and seed are pinned in
[`configs/experiments/v0.4/`](configs/experiments/v0.4/).

### Host and safety boundary

The endpoint runs were executed on an Apple M3 host with 16 GB unified memory,
one loaded model and one request at a time. A resource guard paused execution
when system-wide free memory pressure fell below 18% or swap grew by more than
1 GB within a guarded batch.

Qwen3.5 9B failed its load smoke at 13% free memory pressure and produced no
valid endpoint result. Gemma 4 12B was not loaded because it exceeded the size
of the failed safety candidate. They are resource exclusions, not negative
model-quality results.

## Complete v0.4 experimental data

The tables below reproduce every committed episode record, not only successful
examples. `Obj` is isolated objective success; a public-test pass can still
produce `isolated_evaluation_failed`. Logical total is prompt plus completion
tokens. Wall time is observed end-to-end episode time. `Max ctx` is the largest
logical context seen by any call in that episode.

Fields invariant across all 48 records: seed `20260715`, budget tier `medium`,
run status `completed`, no energy observation, no verifier protocol error, no
fallback, and no Maker–Verifier candidate-A/candidate-B fields. Those invariant
values remain present in the machine-readable
[`comparison.json`](results/OPEN-ME/current/comparison.json).

### Exact arm-level accounting

| Model | Strategy | Success | Mean prompt | Mean completion | Mean total | Mean wall s | Mean calls | Mean tools | Mean public tests | Max call context |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Phi-4 Mini 3.8B | Direct | 1/8 | 455.375 | 151.250 | 606.625 | 6.417 | 1.000 | 1.625 | 0.625 | 736 |
| Phi-4 Mini 3.8B | Bounded Retry | 1/8 | 1,461.125 | 332.625 | 1,793.750 | 14.135 | 2.500 | 4.250 | 1.750 | 1,065 |
| Phi-4 Mini 3.8B | Goal Skill Loop | 1/8 | 3,070.500 | 623.000 | 3,693.500 | 26.692 | 4.500 | 7.375 | 2.875 | 1,261 |
| Qwen3.5 4B | Direct | 3/8 | 491.250 | 90.875 | 582.125 | 6.454 | 1.000 | 2.000 | 1.000 | 622 |
| Qwen3.5 4B | Bounded Retry | 3/8 | 922.750 | 164.750 | 1,087.500 | 10.834 | 1.625 | 3.250 | 1.625 | 844 |
| Qwen3.5 4B | Goal Skill Loop | 3/8 | 1,578.250 | 258.875 | 1,837.125 | 18.058 | 2.250 | 4.500 | 2.250 | 1,032 |

<details>
<summary><strong>Phi-4 Mini 3.8B — all 24 episode records</strong></summary>

| Task | Strategy | Obj | Prompt | Completion | Total | Wall s | Calls | Tools | Tests | Max ctx | Final reason |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `v04-adversarial-001` | `direct` | FAIL | 442 | 177 | 619 | 7.294 | 1 | 1 | 0 | 619 | `candidate_edit_rejected` |
| `v04-adversarial-001` | `bounded_retry` | FAIL | 1,506 | 299 | 1,805 | 12.763 | 3 | 5 | 2 | 641 | `public_tests_failed` |
| `v04-adversarial-001` | `goal_skill_loop` | FAIL | 3,148 | 335 | 3,483 | 16.835 | 5 | 10 | 5 | 724 | `public_tests_failed` |
| `v04-cross-file-001` | `direct` | FAIL | 481 | 255 | 736 | 13.226 | 1 | 2 | 1 | 736 | `public_tests_failed` |
| `v04-cross-file-001` | `bounded_retry` | FAIL | 1,752 | 456 | 2,208 | 18.561 | 3 | 5 | 2 | 868 | `candidate_edit_rejected` |
| `v04-cross-file-001` | `goal_skill_loop` | FAIL | 4,610 | 1,219 | 5,829 | 52.333 | 5 | 10 | 5 | 1,261 | `public_tests_failed` |
| `v04-cross-file-002` | `direct` | FAIL | 471 | 82 | 553 | 3.551 | 1 | 1 | 0 | 553 | `candidate_edit_rejected` |
| `v04-cross-file-002` | `bounded_retry` | FAIL | 1,934 | 406 | 2,340 | 17.770 | 3 | 5 | 2 | 1,016 | `public_tests_failed` |
| `v04-cross-file-002` | `goal_skill_loop` | FAIL | 3,187 | 491 | 3,678 | 22.015 | 5 | 7 | 2 | 872 | `candidate_edit_rejected` |
| `v04-diagnosis-001` | `direct` | FAIL | 450 | 91 | 541 | 3.848 | 1 | 2 | 1 | 541 | `public_tests_failed` |
| `v04-diagnosis-001` | `bounded_retry` | FAIL | 1,987 | 271 | 2,258 | 13.596 | 3 | 6 | 3 | 867 | `public_tests_failed` |
| `v04-diagnosis-001` | `goal_skill_loop` | FAIL | 3,809 | 668 | 4,477 | 30.026 | 5 | 10 | 5 | 963 | `public_tests_failed` |
| `v04-diagnosis-002` | `direct` | FAIL | 470 | 256 | 726 | 9.810 | 1 | 2 | 1 | 726 | `public_tests_failed` |
| `v04-diagnosis-002` | `bounded_retry` | FAIL | 2,199 | 583 | 2,782 | 25.225 | 3 | 6 | 3 | 1,065 | `public_tests_failed` |
| `v04-diagnosis-002` | `goal_skill_loop` | FAIL | 3,719 | 567 | 4,286 | 25.596 | 5 | 10 | 5 | 908 | `public_tests_failed` |
| `v04-localized-001` | `direct` | PASS | 445 | 95 | 540 | 3.772 | 1 | 2 | 1 | 540 | `success` |
| `v04-localized-001` | `bounded_retry` | PASS | 445 | 95 | 540 | 3.836 | 1 | 2 | 1 | 540 | `success` |
| `v04-localized-001` | `goal_skill_loop` | PASS | 523 | 96 | 619 | 4.509 | 1 | 2 | 1 | 619 | `success` |
| `v04-localized-002` | `direct` | FAIL | 449 | 164 | 613 | 6.109 | 1 | 1 | 0 | 613 | `candidate_edit_rejected` |
| `v04-localized-002` | `bounded_retry` | FAIL | 1,431 | 496 | 1,927 | 18.433 | 3 | 3 | 0 | 657 | `candidate_edit_rejected` |
| `v04-localized-002` | `goal_skill_loop` | FAIL | 2,819 | 852 | 3,671 | 32.882 | 5 | 5 | 0 | 739 | `candidate_edit_rejected` |
| `v04-localized-003` | `direct` | FAIL | 435 | 90 | 525 | 3.729 | 1 | 2 | 1 | 525 | `isolated_evaluation_failed` |
| `v04-localized-003` | `bounded_retry` | FAIL | 435 | 55 | 490 | 2.898 | 1 | 2 | 1 | 490 | `isolated_evaluation_failed` |
| `v04-localized-003` | `goal_skill_loop` | FAIL | 2,749 | 756 | 3,505 | 29.338 | 5 | 5 | 0 | 718 | `candidate_edit_rejected` |

</details>

<details>
<summary><strong>Qwen3.5 4B — all 24 episode records</strong></summary>

| Task | Strategy | Obj | Prompt | Completion | Total | Wall s | Calls | Tools | Tests | Max ctx | Final reason |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `v04-adversarial-001` | `direct` | FAIL | 476 | 146 | 622 | 8.075 | 1 | 2 | 1 | 622 | `public_tests_failed` |
| `v04-adversarial-001` | `bounded_retry` | FAIL | 1,848 | 438 | 2,286 | 25.556 | 3 | 6 | 3 | 832 | `public_tests_failed` |
| `v04-adversarial-001` | `goal_skill_loop` | FAIL | 3,456 | 500 | 3,956 | 33.901 | 5 | 10 | 5 | 825 | `public_tests_failed` |
| `v04-cross-file-001` | `direct` | PASS | 521 | 89 | 610 | 5.970 | 1 | 2 | 1 | 610 | `success` |
| `v04-cross-file-001` | `bounded_retry` | PASS | 521 | 89 | 610 | 5.916 | 1 | 2 | 1 | 610 | `success` |
| `v04-cross-file-001` | `goal_skill_loop` | PASS | 1,370 | 176 | 1,546 | 12.684 | 2 | 4 | 2 | 859 | `success` |
| `v04-cross-file-002` | `direct` | FAIL | 508 | 84 | 592 | 5.539 | 1 | 2 | 1 | 592 | `public_tests_failed` |
| `v04-cross-file-002` | `bounded_retry` | FAIL | 1,211 | 199 | 1,410 | 13.078 | 2 | 4 | 2 | 818 | `isolated_evaluation_failed` |
| `v04-cross-file-002` | `goal_skill_loop` | PASS | 588 | 228 | 816 | 12.395 | 1 | 2 | 1 | 816 | `success` |
| `v04-diagnosis-001` | `direct` | PASS | 482 | 79 | 561 | 10.453 | 1 | 2 | 1 | 561 | `success` |
| `v04-diagnosis-001` | `bounded_retry` | PASS | 482 | 79 | 561 | 8.887 | 1 | 2 | 1 | 561 | `success` |
| `v04-diagnosis-001` | `goal_skill_loop` | PASS | 562 | 161 | 723 | 14.959 | 1 | 2 | 1 | 723 | `success` |
| `v04-diagnosis-002` | `direct` | FAIL | 506 | 89 | 595 | 6.152 | 1 | 2 | 1 | 595 | `public_tests_failed` |
| `v04-diagnosis-002` | `bounded_retry` | FAIL | 1,241 | 198 | 1,439 | 12.565 | 2 | 4 | 2 | 844 | `isolated_evaluation_failed` |
| `v04-diagnosis-002` | `goal_skill_loop` | FAIL | 4,246 | 585 | 4,831 | 44.395 | 5 | 10 | 5 | 1,032 | `public_tests_failed` |
| `v04-localized-001` | `direct` | PASS | 481 | 94 | 575 | 5.639 | 1 | 2 | 1 | 575 | `success` |
| `v04-localized-001` | `bounded_retry` | PASS | 481 | 94 | 575 | 5.659 | 1 | 2 | 1 | 575 | `success` |
| `v04-localized-001` | `goal_skill_loop` | FAIL | 561 | 95 | 656 | 5.958 | 1 | 2 | 1 | 656 | `isolated_evaluation_failed` |
| `v04-localized-002` | `direct` | FAIL | 486 | 85 | 571 | 5.451 | 1 | 2 | 1 | 571 | `public_tests_failed` |
| `v04-localized-002` | `bounded_retry` | FAIL | 1,128 | 160 | 1,288 | 10.616 | 2 | 4 | 2 | 717 | `isolated_evaluation_failed` |
| `v04-localized-002` | `goal_skill_loop` | FAIL | 1,293 | 230 | 1,523 | 14.241 | 2 | 4 | 2 | 872 | `isolated_evaluation_failed` |
| `v04-localized-003` | `direct` | FAIL | 470 | 61 | 531 | 4.353 | 1 | 2 | 1 | 531 | `isolated_evaluation_failed` |
| `v04-localized-003` | `bounded_retry` | FAIL | 470 | 61 | 531 | 4.393 | 1 | 2 | 1 | 531 | `isolated_evaluation_failed` |
| `v04-localized-003` | `goal_skill_loop` | FAIL | 550 | 96 | 646 | 5.933 | 1 | 2 | 1 | 646 | `isolated_evaluation_failed` |

</details>

## Statistical analysis

All effectiveness comparisons are paired within model by task, budget tier,
and seed. Reports include:

- percentage-point success difference;
- rescued, regressed, and unchanged task outcomes;
- exact paired test;
- task-clustered bootstrap interval;
- incremental logical tokens and wall time;
- verified successes per 1,000 logical tokens.

With eight tasks, v0.4 is a qualification pilot, not a confirmatory coding
leaderboard. A zero-width bootstrap interval for Phi reflects identical paired
outcomes in this observed pilot; it should not be interpreted as universal
certainty.

## Reproduce

The harness uses the Python standard library. Ollama is the only runtime needed
for the current local experiment.

```bash
make check

PYTHONPATH=src python3 -m edgeloopbench validate \
  configs/experiments/v0.4/pilot-qwen35-4b.toml

PYTHONPATH=src python3 -m edgeloopbench run \
  configs/experiments/v0.4/pilot-qwen35-4b.toml \
  --results results/v0.4/pilot-qwen35-4b/raw/runs.jsonl \
  --events results/v0.4/pilot-qwen35-4b/raw/events.jsonl \
  --task-catalog tasks/official-loop-pilot \
  --evaluator-catalog evaluators/official-loop-pilot
```

The run command is resumable: completed manifest-bound run identities are
skipped, and new raw events are appended rather than rewriting evidence.

Generate a report:

```bash
PYTHONPATH=src python3 -m edgeloopbench compare \
  --experiment configs/experiments/v0.4/pilot-phi4-mini.toml \
    results/v0.4/pilot-phi4-mini/raw/runs.jsonl \
  --experiment configs/experiments/v0.4/pilot-qwen35-4b.toml \
    results/v0.4/pilot-qwen35-4b/raw/runs.jsonl \
  --output results/v0.4/comparison
```

## Evidence layout

```text
configs/experiments/v0.4/       pinned experimental identities
tasks/official-loop-pilot/      agent-visible offline tasks
evaluators/official-loop-pilot/ isolated hidden tests and gold edits
results/v0.4/*/raw/             local append-only events and run records
results/OPEN-ME/                committed self-contained HTML and JSON
docs/runs/                      immutable run interpretation and evidence hashes
docs/decisions/                 experimental and architectural decisions
```

Raw events contain full model output and remain local. The committed comparison
payload contains manifest metadata, metric records, coverage, summaries, and
paired transitions without publishing model prose.

## Threats to validity

- The suite contains eight generated Python repairs, not a broad software
  engineering benchmark.
- One seed cannot estimate decoding variance, although temperature is fixed at
  zero and task/controller pairing is exact.
- Public tests are an imperfect stopping proxy; a public pass can still fail
  hidden evaluation, and the controller is correctly forbidden to see why.
- Local wall time depends on host state. It is reported as observed cost, not a
  cross-runtime serving claim.
- Small models can exhibit capability floors that no controller topology can
  repair economically.

## Repository principles

- Separate agent effectiveness from serving efficiency.
- Count logical prompt tokens even when a backend reuses a prefix cache.
- Never expose hidden tests, gold edits, or evaluator paths to an agent.
- Pin revisions, prompts, runtime, controller, quantization, and manifests.
- Record weight and KV-cache quantization as different variables.
- Append raw events and derive summaries instead of editing results in place.
- Add no network-dependent benchmark tasks.

## Prior experiments and scope

Earlier v0.1–v0.3 reports remain historical evidence. They use different task
suites or controller revisions and are not pooled with v0.4. The current report
is the supported entry point; historical details remain under `docs/runs/` and
the published results archive.

EdgeLoopBench is a research instrument, not an autonomous coding CLI and not a
claim of peer review. The design borrows the discipline of an empirical ML
paper—explicit hypotheses, controlled variables, paired endpoints, immutable
evidence, and stated limitations—so negative results remain useful.

## License

EdgeLoopBench is available under the [MIT License](LICENSE). Model weights,
task sources, and runtimes retain their own licenses.
