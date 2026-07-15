# v0.3 evidence-gated execution record

- Status: **complete: 18-run final calibration and 90-run confirmatory result validated**
- Date: **2026-07-15**
- Host: **Apple Silicon, 16 GB unified memory**
- Model scope: **Qwen3.5 4B Q4_K_M only**
- Design: [v0.3 evidence-gated loop](../experiment-design-v0.3.md)

## Implemented and validated

- `evidence_gated_loop` uses a fresh read-only five-item checker.
- The controller derives `APPROVE`, `REJECT`, or `ESCALATE` from structured
  checklist evidence.
- A checker-guided Candidate B must pass public tests and a fresh re-check.
- Candidate A is restored on failed revision, failed re-check, escalation, or
  protocol error.
- Public-passing Candidate B is retained for evaluator-only telemetry even when
  the re-check rejects it; hidden outcomes never return to either model role.
- Maker attempts are capped at three; checker calls are capped at two.
- All role calls contribute to logical prompt and completion-token totals.

`TopologyCalibration-6` and `ConfirmatoryRepair-B-30` are disjoint from the old
suite. All 36 tasks fail initially, and every evaluator-owned gold patch passes
both public and hidden tests. The confirmatory category counts are frozen at 12
localized, 8 cross-file, 6 diagnosis, and 4 adversarial tasks.

## Completed calibration and frozen controller

Host state later changed materially after restart and memory recovery, so the
resume gate was evaluated from scratch. Three append-only calibration versions
were retained rather than overwritten:

1. the array checklist produced a protocol error on all 5 checker calls;
2. fixed checklist keys reduced structural errors, but verbose responses still
   truncated or contained empty evidence;
3. the final compact fixed-key contract completed 18/18 runs with 7 checker
   calls and zero protocol errors.

The final calibration had identical effectiveness in all arms (3/6). The
evidence-gated arm averaged 2,680.5 logical tokens and 33.4 seconds, compared
with 584.3 tokens and 5.5 seconds for direct. This established protocol
viability, not performance uplift. The controller was then frozen at SHA-256
`d2d6fcaa954a9a9608a46d88c3d5803f22be20b69e9d15296861c56995ba8c94`.

## Confirmatory result

The frozen manifest SHA-256 is
`592650a19fdc9d1eec9ef3af6122ed73a72deda88e1d9c191f83492dee5665b6`.
All 90 declared episodes completed with zero missing or invalid runs:

| Strategy | Success | Mean logical tokens | Mean wall time |
| --- | ---: | ---: | ---: |
| Direct | 10/30 (33.3%) | 602.9 | 6.7 s |
| Bounded Retry | 13/30 (43.3%) | 857.8 | 9.6 s |
| Evidence-Gated Loop | 13/30 (43.3%) | 2,859.5 | 40.5 s |

Retry versus direct rescued 3 tasks with no regressions (+10.0 percentage
points), but its task-clustered 95% bootstrap interval was [0.0, 23.3] points
and the exact paired p-value was 0.25. It therefore does not meet the frozen
practical-benefit rule requiring the interval to exclude zero.

Evidence-Gated Loop matched Retry task for task: 0 rescues, 0 regressions, and
30 unchanged outcomes. It added 2,001.7 logical tokens and 30.9 seconds per
task. Across 38 checker calls there were zero protocol errors, but every call
returned `REJECT`; no call returned `APPROVE` or `ESCALATE`. Thirteen correct
incumbent candidates were rejected. Ten Candidate B patches passed public
tests; one would have rescued an isolated evaluation failure, but the fresh
re-check rejected it and the conservative controller restored Candidate A.

The supported conclusion is narrow: on Qwen3.5 4B Q4_K_M and this fresh repair
suite, the engineered checker topology added no effectiveness beyond bounded
retry and imposed substantially worse serving efficiency. This is not a claim
that loop engineering is generally ineffective; it shows that checker quality
is a binding component of a useful loop.

## Aborted calibration smoke

The first one-episode calibration smoke was started on AC power with no model
reported by `ollama ps`. Before the first model response completed:

- system-wide free memory fell from about 35% to 14%;
- used swap rose from about 4.9 GB to 6.5 GB;
- the command was interrupted manually;
- no result record was written;
- one append-only `run_started` event remains as interruption evidence.

After explicitly unloading Qwen3.5 4B, free memory recovered, but observed swap
briefly exceeded 8 GB. This is an external host-load condition, not a measured
controller outcome. The interrupted episode is excluded from calibration and
cannot be summarized as a model failure.

A second fresh-log smoke was attempted only after AC power, no loaded model,
and about 49% free memory were confirmed. It also failed the preregistered
safety gate before the first response completed:

- free memory fell to about 15%;
- swap grew from about 6.3 GB to 7.5 GB during the request;
- swap reached about 9.0 GB after explicit unload;
- no result record was written; the fresh log contains one `run_started` event.

After two reproducible safety-gate failures, no third model attempt is allowed
under the current host load. The user must first reduce unrelated resident
workloads or restart the host, then the gate must be evaluated from scratch.

The controller was subsequently amended, before any completed calibration
result, to retain a public-passing but re-check-rejected Candidate B for isolated
diagnostics. The frozen calibration manifest now pins controller source SHA-256
`eb44d60e9ea6786f6a2beba7b354453b6367321886bbf6c69bee2be2f49017c1`.
Later calibration-driven schema revisions each used a new manifest identity;
the final frozen revision and result are recorded above.

## Resume gate

Do not resume model execution until all of the following hold:

1. AC power and no loaded Ollama model;
2. system-wide free memory is at least 45% before loading;
3. one isolated smoke episode completes without free memory dropping below 20%;
4. swap growth during that smoke is less than 1 GB;
5. the manifest and controller digests validate exactly.

If the smoke fails this gate again, pause rather than changing prompts, task
content, or confirmatory endpoints. Reducing context length or changing models
would create a new calibration manifest and must not be mixed with this one.
