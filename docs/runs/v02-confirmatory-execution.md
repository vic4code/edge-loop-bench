# v0.2 calibration and confirmatory execution record

- Status: **small-model protocol amended; Qwen3.5 4B aggregate opened**
- Host: **MacBook Pro Mac15,3, Apple M3, 16 GB unified memory, 10-core GPU**
- Runtime: **Ollama 0.31.1, one loaded model, one request at a time, 4096 context, Flash Attention on, q8_0 KV cache**
- Controller under test: **read-only-verifier-v2**
- Confirmatory data: **ConfirmatoryRepair-30** under [`tasks/confirmatory`](../../tasks/confirmatory/README.md)

## Frozen comparison

The primary endpoint is final-candidate objective success on 30 paired tasks.
Direct, Bounded Retry, and Maker–Verifier use the same first Maker prompt,
decoding seed, per-call output cap, model artifact, and total logical budget.
The task, not the model call or repeated seed, is the inferential unit.

| Model | Role in study | Weight quantization | Blob SHA-256 |
| --- | --- | --- | --- |
| Qwen3.5 4B | Primary small-model result | Q4_K_M | `81fb60c7…40490` |
| Qwen3.5 9B | Excluded partial host-safety block | Q4_K_M | `dec52a44…9d37c` |
| Gemma 4 12B | Not opened on this 16 GB host | Q4_K_M | `1278394b…a606` |

Model-to-model Direct results are descriptive. Causal loop conclusions are
within a model: Retry minus Direct and Maker–Verifier minus Direct.

## Calibration evidence so far

Qwen3.5 4B completed all 18 MicroRepair-6 calibration episodes under manifest
`v02-calibration-qwen35-4b`:

- Direct: 4/6 objective success;
- Bounded Retry: 4/6;
- Maker–Verifier: 4/6;
- every first Maker output was byte-identical across the three arms for the
  same task;
- all four invoked verifier calls returned valid `APPROVE` verdict objects;
- zero verifier protocol errors;
- two tasks never produced a public-passing Candidate A, so no verifier was
  invoked for those tasks.

This is a schema and controller qualification result, not a new performance
uplift claim. Its wall-time measurements are excluded because the later block
boundary check found that the host was on battery power.

## Confirmatory execution status

- Qwen3.5 4B: 90/90 declared runs, 90 unique run keys, zero missing,
  zero duplicate result keys.
- Qwen3.5 9B: 49/90 declared runs retained, 49 unique run keys, excluded
  from effectiveness aggregates.
- Gemma 4 12B: 0/90; confirmatory execution was not opened.

The scope amendment in
[ADR 010](../decisions/010-small-model-confirmatory-profile.md) was recorded
before opening the completed Qwen3.5 4B aggregate. The original endpoint,
contrasts, and decision rule remain unchanged.

## Interrupted attempts retained

Two Qwen3.5 9B attempts are retained as raw append-only evidence but excluded:

1. a five-result partial attempt was stopped when code-revision drift was
   detected between the pinned manifest and a newly loaded runner process;
2. a four-result partial attempt with the corrected revision was stopped when
   the Mac block-boundary check reported battery power and heavy pre-existing
   swap use.

No result line was edited or promoted. Later AC-powered resumes reached 49
unique Qwen3.5 9B results, but the block remained incomplete and was excluded
after the host-safety observations below. Gemma confirmatory execution was not
started.

## Power and memory gate

The stopped block reported:

- battery power, 38% remaining;
- approximately 13.4 GB swap in use;
- no throttled VM pages at the observation point.

After a user-reported host reboot, controlled one-run resumes showed the 9B
artifact reducing system-wide free memory to roughly 17–20%. One completed run
left about 3.4 GB of swap allocated after unload. This does not prove the model
caused the reboot, but it is sufficient to reject further mid-tier execution on
this host.

Formal runs require AC power. Swap is recorded as a block condition rather than
silently cleared; success conclusions remain separate from wall-time and memory
observations. Serving-efficiency claims require a later one-factor-at-a-time
ablation and are not derived from this effectiveness run.

## Small-model report command

```bash
PYTHONPATH=src python3 -m edgeloopbench report \
  results/v0.2/raw/confirmatory/qwen35-4b-runs.jsonl \
  --manifest configs/experiments/v0.2/confirmatory-qwen35-4b.toml \
  --output results/OPEN-ME/current
```

Open [`results/OPEN-ME/index.html`](../../results/OPEN-ME/index.html). This hub
shows the current formal result and every historical model report with their
interpretation boundaries.

## Planned report decision rule

A loop is called a practical measured benefit only if it has positive paired
uplift, at least three more rescued than regressed tasks (at least +10 pp on 30
tasks), and no accounting or isolation violation. It is statistically resolved
only if the task-clustered 95% bootstrap interval also excludes zero. Otherwise
the report says promising but inconclusive, or no measured uplift.

## Qwen3.5 4B confirmatory result

Coverage is complete: 90/90 declared runs, zero missing, and zero invalid.
Results are paired across the same 30 tasks.

| Strategy | Verified success | Mean logical tokens | Mean wall time | Budget-exhausted runs |
| --- | ---: | ---: | ---: | ---: |
| Direct | 10/30 (33.3%) | 593.9 | 8.06 s | 0 |
| Bounded Retry | 14/30 (46.7%) | 992.4 | 12.69 s | 11 |
| Maker–Verifier | 14/30 (46.7%) | 1502.4 | 24.14 s | 11 |

Both loop arms rescued the same four tasks and regressed none:

- `confirm-adversarial-001`;
- `confirm-diagnosis-004`;
- `confirm-localized-008`;
- `confirm-localized-011`.

Relative to Direct, each loop gained **+13.3 percentage points** with four
rescues, zero regressions, and a task-clustered bootstrap 95% interval of
**+3.3 to +26.7 points**. This meets the frozen practical-benefit rule and its
bootstrap resolution rule. The unadjusted exact paired p-value is `0.125` for
each contrast (`0.25` after Holm adjustment of the two confirmatory tests), so
the exact test does not reject at a conventional 0.05 threshold. Both facts are
reported; the bootstrap result is not relabeled as universal significance.

Maker–Verifier produced exactly the same 30 task outcomes as Bounded Retry:
zero rescues, zero regressions, and a 0-point paired difference. It consumed an
additional 510.0 logical tokens and 11.45 seconds per task on average. Across
its 30 runs, the verifier returned 18 `APPROVE` verdicts, no `REJECT` verdicts,
and one protocol-error `ESCALATE` that used the preserved fallback; 11 runs
never reached a verifier call. No Candidate B was generated.

### Conclusion

**Bounded Retry helped on this suite** for the pinned Qwen3.5 4B artifact: it
raised verified success from 33.3% to 46.7% under the same task-level budget.
The added success cost about 399 logical tokens and 4.63 seconds per task on
average, and success per 1K tokens decreased from 0.561 to 0.470.

**The read-only verifier showed no measured benefit beyond Bounded Retry on
this suite.** Maker–Verifier matched Retry's successes but used substantially
more tokens and time. Because the verifier never issued a valid `REJECT` and no
Candidate B was generated, these data test verifier overhead and approval
behavior more than verifier-guided repair.

This is one pinned small model on one original 30-task repair suite. It supports
a local statement about this controller/model/task combination, not a general
claim that retry loops always help or that verifier loops never help. Wall time
is descriptive effectiveness telemetry, not a serving-efficiency conclusion.
