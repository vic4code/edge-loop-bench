# Three-model loop comparison on M3 MacBook Pro

Run date: **2026-07-14**

This qualification compares Direct, Bounded Retry, and Maker–Verifier on the
same MicroRepair-6 tasks, seeds 11 and 29, and small and medium logical budgets.
All 216 declared runs completed with zero missing or invalid records.

## Frozen environment

- Host: M3 MacBook Pro (Mac15,3), 16 GB unified memory, 10-core GPU.
- Runtime: Ollama 0.31.1, loopback only, one loaded model and one parallel
  request.
- Serving controls: 4,096-token context, Flash Attention enabled, Q8 KV cache.
- Generation: temperature 0, thinking disabled, `full-file-edits-v1` schema.
- Controller revision: `4e64e12f258e4fd27173a2d50279d8957ba3fd27`.
- Weight quantization: Q4_K_M for every model. KV quantization is recorded
  separately and is not part of the weight label.

Pinned weight blobs:

| Model | Weight blob SHA-256 |
| --- | --- |
| Qwen3.5 4B | `81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490` |
| Gemma 4 12B | `1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606` |
| Qwen3.5 9B | `dec52a44569a2a25341c4e4d3fee25846eed4f6f0b936278e3a3c900bb99d37c` |

## Agent-effectiveness result

Each row contains 12 paired task/seed observations. The two budget tiers have
the same success rates; costs differ when a controller uses additional calls.

| Model | Strategy | Success | Small mean tokens | Medium mean tokens | Small mean wall | Medium mean wall |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Qwen3.5 4B | Direct | 66.7% | 779 | 779 | 8.8 s | 8.5 s |
| Qwen3.5 4B | Bounded Retry | 83.3% | 1,466 | 1,988 | 14.9 s | 19.0 s |
| Qwen3.5 4B | Maker–Verifier | 50.0% | 1,667 | 1,667 | 17.9 s | 17.9 s |
| Gemma 4 12B | Direct | 83.3% | 827 | 827 | 27.8 s | 29.8 s |
| Gemma 4 12B | Bounded Retry | 83.3% | 1,182 | 1,716 | 33.9 s | 51.2 s |
| Gemma 4 12B | Maker–Verifier | 66.7% | 1,743 | 1,743 | 52.7 s | 54.8 s |
| Qwen3.5 9B | Direct | 100.0% | 826 | 826 | 18.3 s | 18.1 s |
| Qwen3.5 9B | Bounded Retry | 100.0% | 826 | 826 | 17.7 s | 17.2 s |
| Qwen3.5 9B | Maker–Verifier | 83.3% | 1,766 | 1,766 | 37.7 s | 35.8 s |

Relative to Direct, Qwen3.5 4B Bounded Retry rescued 2 of 12 paired
observations and regressed none in each budget tier. Its Maker–Verifier arm
rescued none and regressed 2. Gemma 4 12B Bounded Retry produced no outcome
transitions, while Maker–Verifier regressed 2 observations in each budget tier.
Qwen3.5 9B Bounded Retry stopped after its successful first attempts, while
Maker–Verifier regressed 2 observations per budget tier.

### Medium-budget baseline uplift

`Direct` is the within-model baseline. Success uplift is the paired
percentage-point difference from Direct; token and wall figures are mean-cost
multipliers. A positive success delta with a cost multiplier above 1 is a
quality/cost trade-off, not a free performance gain.

| Model | Loop | Success vs Direct | Token cost vs Direct | Wall time vs Direct |
| --- | --- | ---: | ---: | ---: |
| Qwen3.5 4B | Bounded Retry | +16.7 pp | 2.55x | 2.24x |
| Qwen3.5 4B | Maker–Verifier | -16.7 pp | 2.14x | 2.10x |
| Gemma 4 12B | Bounded Retry | +0.0 pp | 2.08x | 1.72x |
| Gemma 4 12B | Maker–Verifier | -16.7 pp | 2.11x | 1.84x |
| Qwen3.5 9B | Bounded Retry | +0.0 pp | 1.00x | 0.95x |
| Qwen3.5 9B | Maker–Verifier | -16.7 pp | 2.14x | 1.98x |

### Controller semantics tested in this run

- **Direct:** one model call, replacement-edit validation and application, one
  public-test run, then isolated evaluation only when public tests pass.
- **Bounded Retry:** rebuild the prompt from the evolving worktree after a
  rejected edit or failed public test, include only sanitized feedback, and
  repeat within the shared logical call, token, tool, and test caps.
- **Maker–Verifier:** the first call makes an edit; the second call is instructed
  to review the requirements and current implementation and may return another
  replacement edit. This is a tested review-and-revise loop, not the independent
  read-only `APPROVE`/`REJECT` verifier described as the target design in the
  experiment protocol.

Hidden evaluator output is never returned to any strategy. Therefore, no loop
can repair a patch from hidden-test feedback.

## Interpretation boundary

This is a qualification over six deterministic repair tasks, not a broad claim
about general coding ability. It supports a narrower conclusion: bounded retry
helped the 4B model when failures were recoverable, but added no success for
Gemma 4 12B or when the 9B model already solved every Direct episode. The tested
Maker–Verifier controller was harmful for all three primary models.

Phi-4-mini remains archived as qualification evidence but is excluded from the
primary comparison because Direct, Bounded Retry, and Maker–Verifier all scored
0%, leaving no outcome variation for estimating a loop effect.

Serving efficiency is not inferred from this table. GPU throughput, memory,
thermal behavior, and energy require fixed-request serving ablations with one
factor changed at a time.
