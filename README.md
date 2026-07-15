# EdgeLoopBench

**A reproducible local study of when agent loops help—and when they only spend
more inference.**

[Open the current interactive result](results/OPEN-ME/index.html) ·
[Read the v0.4 run record](docs/runs/v04-goal-skill-loop-pilot.md) ·
[Inspect the experiment design](docs/experiment-design-v0.4.md)

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

The Goal Skill Loop maps the official goal-based pattern into a benchmark
controller:

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
