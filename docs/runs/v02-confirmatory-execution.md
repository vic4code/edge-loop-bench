# v0.2 calibration and confirmatory execution record

- Status: **implementation complete; calibration partially executed; confirmatory results not yet opened**
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
| Qwen3.5 4B | Low-resource control | Q4_K_M | `81fb60c7…40490` |
| Qwen3.5 9B | Mid-tier primary | Q4_K_M | `dec52a44…9d37c` |
| Gemma 4 12B | Alternate-family mid-tier | Q4_K_M | `1278394b…a606` |

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

## Interrupted attempts retained

Two Qwen3.5 9B attempts are retained as raw append-only evidence but excluded:

1. a five-result partial attempt was stopped when code-revision drift was
   detected between the pinned manifest and a newly loaded runner process;
2. a four-result partial attempt with the corrected revision was stopped when
   the Mac block-boundary check reported battery power and heavy pre-existing
   swap use.

No result line was edited or promoted. A fresh experiment identity will be used
after the AC-power gate passes. Gemma calibration and every confirmatory run
remain unopened.

## Power and memory gate

The stopped block reported:

- battery power, 38% remaining;
- approximately 13.4 GB swap in use;
- no throttled VM pages at the observation point.

Formal runs require AC power. Swap is recorded as a block condition rather than
silently cleared; success conclusions remain separate from wall-time and memory
observations. Serving-efficiency claims require a later one-factor-at-a-time
ablation and are not derived from this effectiveness run.

## Planned report decision rule

A loop is called a practical measured benefit only if it has positive paired
uplift, at least three more rescued than regressed tasks (at least +10 pp on 30
tasks), and no accounting or isolation violation. It is statistically resolved
only if the task-clustered 95% bootstrap interval also excludes zero. Otherwise
the report says promising but inconclusive, or no measured uplift.
