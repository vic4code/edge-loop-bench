# EdgeLoopBench

**Budget-aware agent loops on Apple Silicon.**

EdgeLoopBench asks a deliberately narrow question:

> Under a fixed compute budget, when does loop engineering improve task success enough to justify its extra inference cost?

The project is designed for a MacBook Air and open-weight models. It compares direct agent episodes, bounded retry loops, and maker-verifier loops without confusing reasoning quality with server speed. Ollama is the practical baseline; vLLM-Metal and MLX-LM are optimization and systems-research backends.

This repository is a research harness, not another agent CLI and not a model runtime. Existing CLIs may be integrated later, but the first controller stays small so token accounting, stopping rules, and tool access remain auditable.

## Why this topic matters

Agent loops can improve a weak first answer, but every retry, verifier pass, and repeated context costs tokens, time, memory, and energy. Cloud APIs make those costs monetary; a local model makes them visible systems constraints. Apple Silicon is especially interesting because CPU and GPU share memory, so model weights, KV cache, the operating system, and the benchmark all compete for the same resource.

The project therefore produces two separate result tables:

1. **Agent effectiveness** — objective task success under identical logical token and tool budgets.
2. **Serving efficiency** — latency, throughput, memory, thermal behavior, and optional energy for identical request shapes.

A later deployment experiment may combine them under a wall-time or energy budget. It must never be presented as proof that one loop strategy reasons better.

## Initial experiment

The minimum useful study uses:

- one Apple Silicon MacBook Air;
- one pinned open-weight model and one pinned OpenAI-compatible server;
- six offline Python repair tasks;
- direct, bounded-retry, and maker-verifier strategies;
- two calibrated budget tiers and two seeds;
- 72 total agent runs;
- fixed-prompt serving microbenchmarks before the agent runs.

The primary endpoint is objective verified success: the final patch passes hidden tests, leaves public tests passing, changes no prohibited files, and applies cleanly.

## Runtime direction

| Backend | Role | Apple GPU path |
| --- | --- | --- |
| Ollama | Lowest-friction baseline | Native Metal |
| vLLM-Metal | Paged KV, scheduling, speculative decoding, profiling | MLX compute plus native Metal kernels |
| MLX-LM | Apple-native reference and cache/quantization experiments | MLX and Metal |

Core vLLM does not provide a direct PyTorch MPS backend. On Apple Silicon, this project uses the separate community-maintained `vllm-metal` plugin documented by vLLM.

## Candidate model ladder

Start small enough to run a full experiment matrix before trying a headline model:

1. **Qwen3.5 4B Q4** — small control and harness shakeout.
2. **Gemma 4 E2B or E4B** — primary edge-model candidate.
3. **GLM-4.7-Flash Q4** — 30B-A3B stretch candidate for a 32 GB machine, not a default for 8–16 GB systems.

Artifact size is not total runtime memory. Context length and KV cache can dominate the remaining headroom. See [the model matrix](docs/model-matrix.md) before downloading a model.

### Detected development machine

The checked-in, privacy-scrubbed inventory is an **M4 MacBook Air with 16 GB unified memory and an 8-core GPU**. For this machine, the practical sequence is Qwen3.5 4B for harness shakeout, then Qwen3.5 9B or Gemma 4 12B for the main study. GLM-4.7-Flash's 19 GB Q4 artifact is outside the safe operating envelope and should be tested only on a 32 GB host. See [`configs/hardware.m4-air-16gb.json`](configs/hardware.m4-air-16gb.json).

## Repository status

Version `0.1.0` is a runnable Mac-native qualification harness. It provides:

- a frozen experimental vocabulary and causal boundary;
- machine-readable experiment manifests;
- validation for unfair or malformed experiment plans;
- manifest-bound JSONL result summaries that expose their bindings, reject mixed revisions, enforce logical and deployment budgets, and compute paired strategy deltas;
- runtime setup profiles and an evidence-backed model shortlist;
- six deterministic offline repair tasks with isolated evaluation;
- direct, bounded-retry, and maker-verifier controllers with logical-token accounting;
- append-only, resumable manifest execution through loopback Ollama; and
- self-contained HTML plus JSON reports that keep effectiveness separate from serving.

It deliberately does **not** edit arbitrary repositories or expose evaluator
assets to the model. Energy collection, a measured serving report, and the
confirmatory multi-seed study remain later milestones.

## Quick start

The scaffold has no runtime Python dependencies.

```bash
make check
```

Validate a plan:

```bash
PYTHONPATH=src python3 -m edgeloopbench validate configs/experiments/smoke.toml
```

Summarize append-only JSONL results:

```bash
PYTHONPATH=src python3 -m edgeloopbench summarize \
  examples/results/sample-runs.jsonl \
  --manifest examples/results/sample-plan.toml
```

Prepare and inspect one public task:

```bash
PYTHONPATH=src python3 -m edgeloopbench task prepare python-localized-001 \
  --work-root /tmp/edgeloop-localized-001
PYTHONPATH=src python3 -m edgeloopbench task public-test \
  /tmp/edgeloop-localized-001
```

Run or resume the pinned 72-run Ollama shakeout. Raw model events and derived
run records are appended separately under the ignored `results/` directory:

```bash
PYTHONPATH=src python3 -m edgeloopbench run \
  configs/experiments/smoke.toml \
  --results results/qwen35-4b-smoke-runs.jsonl \
  --events results/qwen35-4b-smoke-events.jsonl
```

Use `--max-runs 1` for a short qualification slice. Repeating the command
skips run identities already present in the result log.

Render the offline analysis page:

```bash
PYTHONPATH=src python3 -m edgeloopbench report \
  results/qwen35-4b-smoke-runs.jsonl \
  --manifest configs/experiments/smoke.toml \
  --output results/qwen35-4b-report
```

Compare two or more complete experiments while allowing only the pinned model
artifact to vary:

```bash
PYTHONPATH=src python3 -m edgeloopbench compare \
  --experiment configs/experiments/smoke.toml results/qwen35-4b-full-runs.jsonl \
  --experiment configs/experiments/gemma4-12b-smoke.toml results/gemma4-12b-full-runs.jsonl \
  --experiment configs/experiments/qwen35-9b-smoke.toml results/qwen35-9b-full-runs.jsonl \
  --output results/three-model-loop-comparison
```

The completed three-model qualification and its interpretation boundary are
recorded in [the run note](docs/runs/three-model-loop-comparison.md).

The proposed confirmatory protocol—including deterministic Retry packets, a
read-only Maker–Verifier state machine, candidate preservation, task-level
statistics, and Mac run controls—is defined in the
[v0.2 experiment design](docs/experiment-design-v0.2.md). Existing qualification
results remain bound to their old controller revision and are not pooled with
v0.2.

Summaries reject undeclared, over-budget, manifest-mismatched, or silently missing runs by default. Manifest-bound agent results must report the largest context observed in any model call; deployment runs must also satisfy their declared wall-time and energy budgets. Use `--allow-incomplete` only when the resulting coverage counts are intentionally part of an exploratory partial analysis.

Inspect the protocol before interpreting any numbers:

- [Project specification](docs/spec.md)
- [Experiment protocol](docs/experiment-protocol.md)
- [Metrics contract](docs/metrics.md)
- [Research roadmap](docs/research-plan.md)
- [Official sources](docs/sources.md)

## Non-goals

- Claiming that local inference is free. Hardware time and energy still matter.
- Comparing different models and calling the result a loop-strategy effect.
- Comparing GGUF and MLX checkpoints and calling the result a pure server effect.
- Treating advertised context length as a feasible laptop operating point.
- Automatically changing privileged macOS memory settings.
- Exposing local development servers to untrusted networks.

## License

EdgeLoopBench is available under the [MIT License](LICENSE). Model weights, task sources, and runtimes retain their own licenses.
