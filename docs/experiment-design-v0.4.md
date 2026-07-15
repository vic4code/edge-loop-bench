# EdgeLoopBench v0.4 goal-and-skill loop qualification

- Status: **approved for a small-model qualification pilot**
- Design date: **2026-07-15**
- Target host: **16 GB Apple Silicon, resource-gated**
- Models: **pinned Qwen3.5 4B and Phi-4 Mini 3.8B Q4_K_M artifacts**
- Eligibility: **only models that pass the local host-safety gate**

## 1. Question and claims boundary

Test whether a controller adapted from Anthropic's official loop guidance is
more effective than Direct or ordinary Bounded Retry on a fresh offline repair
pilot. This is a qualification experiment, not a new performance-uplift claim.
A confirmatory claim would require a frozen controller and a disjoint 30+ task
suite after this pilot.

The experiment measures agent effectiveness. Logical tokens and wall time are
reported as costs, not serving-efficiency improvements.

## 2. Source-to-controller mapping

Anthropic's official “Getting started with loops” guidance describes multiple
loop primitives rather than one universal topology. For tasks with verifiable
exit criteria it recommends a goal-based loop, an explicit attempt cap, and
verification instructions encoded as a reusable skill. It also recommends
starting with the simplest suitable primitive and piloting before scaling.

EdgeLoopBench maps those ideas as follows:

| Official concept | Frozen benchmark behavior |
| --- | --- |
| Verifiable goal | Agent-visible public tests pass |
| Maximum turns | At most five maker attempts |
| Verification skill | The same fixed inspection checklist in every maker prompt |
| Evaluator stop check | The controller deterministically runs public tests |
| Pilot first | Eight fresh tasks and three paired arms |

This adapts the published control principles; it does not claim to reproduce
Claude Code's `/goal` runtime or its model-based stop evaluator. Time-triggered
and proactive loops do not match this offline repair endpoint and are excluded.
Fresh-agent code review is optional in the source guidance and is excluded so
the experiment isolates goal-plus-skill looping rather than repeating v0.3's
same-model checker.

## 3. Frozen strategies

All arms start from the same clean task worktree, use the same model artifact,
decoding settings, initial task evidence, edit schema, and episode-level budget.
Unused budget remains unused.

### Direct

One maker call followed by edit validation, public tests, and isolated hidden
evaluation.

### Bounded Retry

Up to three maker attempts. Edit-validation or public-test feedback is returned
after a failure. The strategy exits on the first public-test pass.

### Goal Skill Loop

Up to five maker attempts. Every call receives a fixed verification skill that
requires inspection of requirement coverage, boundary conditions, state and
side effects, cross-file contracts, and regression risk before emitting a
full-file edit. Failed edit validation or public tests provide deterministic
feedback for the next attempt. The controller exits on the first public-test
pass or when the five-attempt cap or shared budget is reached.

Public tests are the only agent-visible deterministic stop condition. Hidden
evaluation is run once after selection, is never returned to the model, and
cannot cause another attempt. A public pass with a hidden failure is therefore
an observable limitation, not a signal available to the loop.

## 4. Pilot data and budget

Create a fresh deterministic `OfficialLoopPilot-8` suite with eight tasks:
three localized, two cross-file, two diagnosis, and one adversarial repair.
All tasks are offline, initially failing, and separately validated to pass both
public and hidden checks with evaluator-owned gold edits.

The pilot has 24 episodes: eight tasks times three strategies at one frozen
seed. Every arm receives the same maximum per episode:

- 30,000 logical prompt tokens;
- 5,000 completion tokens;
- 5 model calls;
- 12 tool calls;
- 5 public-test runs;
- 4,096 tokens in any one call context.

Direct and Bounded Retry exit before using the full ceiling by design. Report
actual logical-token and wall-time cost, so any benefit from attempts four and
five remains visible as test-time scaling rather than “free” improvement.

## 5. Endpoints and decision rule

Primary pilot endpoint: paired final objective success. Prespecified contrasts:

1. `bounded_retry - direct`;
2. `goal_skill_loop - direct`;
3. `goal_skill_loop - bounded_retry`.

Report success, rescues, regressions, exact paired uncertainty, logical tokens,
wall time, attempts, and failure reasons. With only eight tasks, results may
qualify or reject the topology for a larger confirmatory run but cannot support
a general uplift statement. Equivalence to Bounded Retry is a valid conclusion.

Proceed to a disjoint 30+ task confirmation only if Goal Skill Loop has positive
paired uplift over both comparators, no isolation/accounting violation, and at
least two more rescues than regressions over Bounded Retry.

## 6. Reproducibility and safety

- Pin controller revision, task commits, prompt contract, seed, model artifact,
  quantization, Ollama version, and manifest digest before execution.
- Append raw events and derive all summaries; never edit episode evidence.
- Never expose evaluator paths, hidden tests, gold edits, or hidden outcomes.
- Run one Qwen3.5 4B request at a time, in small chunks, and unload between
  chunks. Pause on low free memory, abnormal swap growth, thermal pressure,
  infrastructure corruption, or manifest mismatch.
- Keep the v0.4 pilot report separate from `results/OPEN-ME/index.html`, which
  remains the canonical v0.3 report until a confirmatory result exists.

If an artifact breaches the host-safety gate during smoke execution, stop that
manifest without deleting append-only evidence and exclude it before endpoint
analysis. Each eligible model runs the same 24-episode matrix and is analyzed
against its own Direct baseline. Cross-model views may compare controller
effects, but must not pool task outcomes or hide model-specific failures.

## 7. Execution

1. Add failing config and controller tests.
2. Implement the smallest `goal_skill_loop` state-machine change.
3. Run targeted tests and `make check`.
4. Generate and independently validate the eight fresh tasks.
5. Freeze the manifest and run a one-task smoke test.
6. Run the remaining episodes in resource-gated chunks.
7. Generate a separate v0.4 pilot report and state only pilot-supported claims.

## 8. Post-run publication deviation

After all 48 eligible episodes were complete and the qualification decision was
fixed, `results/OPEN-ME/index.html` was updated from v0.3 to the v0.4 pilot at
the repository owner's request so the project retained one unambiguous current
entry point. The v0.3 report was moved unchanged to `results/published/v0.3/`.

This publication-only deviation did not change the controller, task suite,
models, budgets, endpoints, raw events, derived records, or decision rule. The
v0.4 result remains a qualification pilot and does not become a confirmatory
uplift claim by being the current report.
