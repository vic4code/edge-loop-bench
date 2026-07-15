# v0.4 goal-skill loop qualification run

## Status

Complete qualification pilot for the two models that passed the local host
safety gate: Phi-4 Mini 3.8B and Qwen3.5 4B.

## Research question

Does a goal-based controller with a frozen verification skill and up to five
Maker attempts improve verified repair success over Direct or ordinary
three-attempt Bounded Retry enough to justify its additional inference cost?

## Design

- Date: 2026-07-15, Asia/Taipei
- Host: Apple M3, 16 GB unified memory
- Runtime: Ollama 0.31.1, Metal, one loaded model, one request at a time
- Weight quantization: Q4_K_M
- KV-cache quantization: q8_0
- Context limit: 4,096 tokens
- Decoding: temperature 0.0, thinking disabled
- Controller digest: `8584906769beea90299476cb1380d310a1e088c50e1b026f351b1c4f1189935c`
- Tasks: eight fresh, deterministic, offline Python repairs
- Categories: three localized, two cross-file, two diagnosis, one adversarial
- Pairing: one frozen seed, the same task under all three strategies
- Episodes: 24 per model, 48 valid episodes total
- Invalid, missing, or over-budget episodes: zero

The public task bundle contains source, instructions, and public tests. Hidden
tests, gold edits, evaluator paths, and evaluator outcomes remain outside the
agent worktree and never return to the model.

## Models

| Model | Artifact SHA-256 | Completed |
| --- | --- | ---: |
| Phi-4 Mini 3.8B Q4_K_M | `3c168af1dea0a414299c7d9077e100ac763370e5a98b3c53801a958a47f0a5db` | 24/24 |
| Qwen3.5 4B Q4_K_M | `81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490` | 24/24 |

Qwen3.5 9B failed the pre-run host-safety smoke when system-wide free memory
pressure fell to 13%. It produced no valid endpoint record and was excluded
before outcome analysis. Gemma 4 12B was not loaded because it is larger than
the failed 9B safety candidate. These are resource exclusions, not performance
results.

## Results

### Agent effectiveness and cost

| Model | Strategy | Success | Mean logical tokens | Mean wall time | Success / 1K tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| Phi-4 Mini | Direct | 1/8 (12.5%) | 607 | 6.4 s | 0.206 |
| Phi-4 Mini | Bounded Retry | 1/8 (12.5%) | 1,794 | 14.1 s | 0.070 |
| Phi-4 Mini | Goal Skill Loop | 1/8 (12.5%) | 3,694 | 26.7 s | 0.034 |
| Qwen3.5 4B | Direct | 3/8 (37.5%) | 582 | 6.5 s | 0.644 |
| Qwen3.5 4B | Bounded Retry | 3/8 (37.5%) | 1,088 | 10.8 s | 0.345 |
| Qwen3.5 4B | Goal Skill Loop | 3/8 (37.5%) | 1,837 | 18.1 s | 0.204 |

### Paired transitions against Direct

| Model | Strategy | Delta | 95% task bootstrap CI | Rescued | Regressed | Exact paired p |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Phi-4 Mini | Bounded Retry | +0.0 pp | [0.0, 0.0] | 0 | 0 | 1.000 |
| Phi-4 Mini | Goal Skill Loop | +0.0 pp | [0.0, 0.0] | 0 | 0 | 1.000 |
| Qwen3.5 4B | Bounded Retry | +0.0 pp | [0.0, 0.0] | 0 | 0 | 1.000 |
| Qwen3.5 4B | Goal Skill Loop | +0.0 pp | [-37.5, +37.5] | 1 | 1 | 1.000 |

For Qwen3.5 4B, Goal Skill Loop rescued `v04-cross-file-002` but regressed
`v04-localized-001`. The aggregate success rate therefore hides a meaningful
change in which tasks succeed. For Phi-4 Mini, every paired outcome was
unchanged.

## Interpretation

This topology did not qualify for a larger confirmatory uplift claim on either
model. Goal Skill Loop consumed 6.09 times Direct's tokens on Phi and 3.16
times Direct's tokens on Qwen without net success improvement. Relative to
Bounded Retry, it added about 1,900 tokens and 12.6 seconds per Phi task, and
750 tokens and 7.2 seconds per Qwen task.

The result does not show that loop engineering is universally ineffective. It
shows that a well-specified loop can still have zero net value when additional
attempts cannot reliably turn visible evidence into better edits. A loop must
be evaluated by rescues, regressions, and cost—not call count alone.

## Evidence integrity

Raw events remain local and append-only because they contain full model output.
The committed comparison payload contains manifests, derived run records, arm
summaries, paired transitions, and coverage without model prose.

| Local evidence | Lines | SHA-256 |
| --- | ---: | --- |
| Phi run records | 24 | `d92cd57da689c4a758c3ef30541a0c35dc05f2e8390d6e53f8fea0b3a97330a0` |
| Phi raw events | 223 | `15af1bf842175f14e22ddd576c72efde3ca5b089e9ae185b87c583680b5da6d9` |
| Qwen run records | 24 | `3932a5cf29a2761ef89bfd6a3dd97d12ae71cecd9e4f84e2ef468a7f91a04fcc` |
| Qwen raw events | 182 | `f707700e2fb4fd081ceadde84bab2286f1846c1730e81445d947240656d50bea` |
| Published comparison JSON | derived | `b1136cb53d8acaf1f170203b0c893f0432dd34dc92f9a4eeaa7677c2637c410c` |
| Published standalone HTML | derived | `07ba3a38d5eb63f4fd3df3fab3a747d01847fa47f5bf46e2931fea266d8cb378` |

## Reproduction

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

Repeated execution resumes from the append-only result identities rather than
rerunning completed episodes.
