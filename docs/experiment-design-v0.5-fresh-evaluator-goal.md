# EdgeLoopBench v0.5 fresh-evaluator goal loop

- Status: **proposed; official SWE-bench execution resource-blocked locally**
- Design date: **2026-07-15**
- External benchmark target: **SWE-bench Verified**
- Local qualification models: **Qwen3.5 4B Maker; separately accounted evaluator**

## Objective

Test whether a fresh stop evaluator can reduce human handoffs on long-horizon,
repository-level work without hiding its model-prompt, token, or latency cost.
The experiment is about unattended completion and controller behavior, not a
claim that a loop increases the foundation model's intrinsic capability.

## Official behavior being adapted

Claude Code `/goal` runs a complete main-agent turn and then sends the goal and
conversation to a configured small fast model. The evaluator returns a binary
decision and a short reason. A negative result starts another main-agent turn
with the reason available as guidance; a positive result clears the goal. The
evaluator cannot call tools and can judge only evidence surfaced in the
transcript.

This differs from:

- `/loop`, where elapsed time starts the next turn;
- a deterministic Stop hook, where a script can decide continuation;
- auto mode, which approves tools inside one turn but does not start another;
- schedules or routines, which can start work outside the current session.

Sources:

- https://code.claude.com/docs/en/goal
- https://code.claude.com/docs/en/scheduled-tasks
- https://code.claude.com/docs/en/routines

## Frozen topology

```text
goal + repository task
          |
          v
complete main-agent turn: inspect, edit, execute, verify
          |
          v
fresh evaluator sees goal + transcript only
          |
      +---+---+
      |       |
 yes: stop   no + reason
              |
              +----> next complete main-agent turn
```

The evaluator must use a fresh context and a separately pinned model identity.
Main-agent and evaluator prompt tokens, completion tokens, calls, and wall time
must be recorded separately. Hidden SWE-bench tests and gold patches must never
enter either context.

## Experimental arms

1. **Direct:** one complete main-agent turn, then final evaluation.
2. **Deterministic Stop:** another main-agent turn starts only when an
   agent-visible deterministic check fails.
3. **Fresh-Evaluator Goal:** after every complete turn, the independent
   evaluator returns achieved/not-achieved plus a reason.

The same task, Maker model, initial repository, tool policy, logical token
ceiling, and wall-time ceiling are paired across arms. Evaluator cost is part
of the Fresh-Evaluator arm, not free overhead.

## Benchmark and subset rule

Use SWE-bench Verified because it is a widely recognized repository-level
coding-agent benchmark with 500 engineer-validated solvable instances. Before
any model run, freeze a small pilot subset from the official dataset using
public metadata only. Selection must not inspect gold patches or test outcomes.

The official Docker harness is mandatory for any result labeled SWE-bench.
Host-only test execution or copied task fixtures may be used for engineering
debugging but must not be reported as a benchmark score.

## Primary metrics

- intervention-free objective completion;
- unresolved handoffs to the user;
- human prompt count, when a human actually continues an episode;
- main-agent prompt count;
- evaluator prompt count;
- automatic follow-up count;
- feedback-converged successes and regressions;
- logical prompt and completion tokens by role;
- time to first valid patch and total wall time;
- false-stop and unnecessary-continue rates against final objective outcome.

`Human prompt count` is observational. A failed Direct episode is an unresolved
handoff, not automatically one human prompt. Counterfactual human effort must
not be invented after the run.

## Qualification decision

The topology qualifies for a larger run only if it produces more
feedback-converged successes than regressions, reduces unresolved handoffs
relative to Direct, and has an explicit cost per avoided handoff. A successful
mechanism trace without a paired reduction in handoffs is evidence of recovery
capability, not deployment uplift.

## Current resource gate

The official SWE-bench Docker guide recommends at least 16 GB allocated to
Docker and about 120 GB of working storage for the standard workflow. This host
has 16 GB total memory and insufficient free disk for that envelope. A safety
smoke also revealed that starting Docker launches unrelated existing
containers, contaminating resource measurements. Therefore official execution
is blocked until an isolated x86-64 Docker host or compatible cloud runner with
adequate storage is explicitly placed in scope.

No SWE-bench performance result exists yet. This status is an infrastructure
exclusion, not a model or controller outcome.
