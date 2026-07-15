# Model and memory matrix

Research snapshot: **2026-07-14**

## Recommendation

Do not start by making the largest model fit. Start by proving the full experiment matrix with a small model, then move upward only when measured task quality justifies lower throughput and memory headroom.

The first shortlist is:

1. Qwen3.5 4B as the primary 16 GB confirmatory model.
2. Phi-4-mini as a separately calibrated small-model replication candidate.
3. Mid-tier artifacts only on a host with measured headroom; do not treat them
   as pending work for the 16 GB profile.

## Candidate matrix

| Candidate | Architecture and model limit | Approximate packaged or Q4 size | Qualification hypothesis (unmeasured) | Research role | License |
| --- | --- | ---: | --- | --- | --- |
| Phi-4-mini-instruct | 3.8B dense, 128K | 2.5 GB Ollama Q4_K_M | 8 GB at 4K–8K | Small control; function-call reliability needs measurement | MIT |
| Qwen3.5 4B | 4B hybrid, 262K | 3.4 GB Ollama artifact | 8 GB at short context | Current small coding/tool baseline | Apache-2.0 |
| Gemma 4 E2B | 2.3B effective, 5.1B including embeddings, 128K | 2.9 GB Google Q4 load estimate; Ollama Q4_K_M is 7.2 GB | 8 GB only with an explicitly compact checkpoint at 4K–8K | Primary smallest Gemma edge candidate | Apache-2.0 |
| Gemma 4 E4B | 4.5B effective, 8B total, 128K | 4.5 GB Google Q4 load estimate; Ollama Q4_K_M is 9.6 GB | 16 GB preferred | Higher-quality Gemma edge candidate | Apache-2.0 |
| GLM-4-9B-0414 | 9B dense, 32K | 5.5–6.5 GB estimated Q4 | 16 GB | Mid-tier GLM control; adapter work may be needed | MIT |
| Qwen3.5 9B | 9B hybrid, 262K | 6.6 GB Ollama artifact | 16 GB at moderate context | Mid-tier coding/tool baseline | Apache-2.0 |
| Gemma 4 12B Unified | 11.95B dense, 256K | 6.7 GB Google Q4 estimate | 16 GB at moderate context | Strong main Gemma candidate | Apache-2.0 |
| gpt-oss-20b | 21B total, 3.6B active, 128K | MXFP4; vendor targets 16 GB | 24 GB preferred | Agentic MoE comparison; Harmony template is mandatory and reasoning state must survive tool turns | Apache-2.0 |
| Gemma 4 26B A4B | 25.2B total, 3.8B active, 256K | 14.4 GB Google Q4 estimate; about 18 GB packaged | 24 GB short-context stretch; 32 GB preferred | Gemma MoE optimization | Apache-2.0 |
| Devstral Small 2 | 24B dense, 256K | 15 GB Ollama Q4_K_M | 32 GB | Coding-specific upper tier | Apache-2.0 |
| GLM-4.7-Flash | 30B total, 3B active MoE, about 198K | 19 GB Ollama Q4_K_M | 32 GB at modest context | Headline GLM stretch model | MIT |
| Qwen3-Coder 30B A3B | 30.5B total, 3.3B active, 262K | 19 GB Ollama Q4 | 32 GB | Coding-specific MoE comparison | Apache-2.0 |

Sizes come from official vendor memory tables or official Ollama artifacts where available. They are not interchangeable: packaging, quantization recipes, embeddings, and metadata differ. The GLM-4-9B-0414 range is an estimate from 9B weights at roughly four bits plus quantization metadata and loading overhead; it is not an observed Ollama artifact.

## Memory-tier guidance

### 8 GB

Use Phi-4-mini, Qwen3.5 4B, or an explicitly compact Gemma 4 E2B Q4 checkpoint such as Google's `gemma-4-E2B-it-qat-q4_0-gguf`. Do **not** assume Ollama's unqualified `gemma4:e2b` is the 2.9 GB Google estimate: the current packaged Q4_K_M artifact is 7.2 GB. Begin at 4K context, qualify 8K, and stop if swap or memory pressure makes repeated runs unstable.

### 16 GB

Use Qwen3.5 4B, Phi-4-mini, or another 2–4 GB artifact that passes calibration.
Observed 9B pressure on a 16 GB M3 host left insufficient headroom for dependable
loop execution. Mid-tier models require a separately measured safe host; see
[ADR 010](decisions/010-small-model-confirmatory-profile.md).

### 24 GB

This tier is comfortable for the 4B–12B candidates and more credible for gpt-oss-20b. Gemma 4 26B A4B is a short-context experiment. A 19 GB artifact is too close to the limit for a dependable loop workload.

### 32 GB

GLM-4.7-Flash, Qwen3-Coder 30B A3B, Devstral Small 2, and Gemma 4 26B become plausible at modest context. Qualification is still required; model weights share memory with the OS, runtime, KV cache, and benchmark processes.

## Important interpretation rules

- **Active MoE parameters are not resident parameters.** A 30B-A3B model still needs the full quantized expert weights available.
- **Model context limit is not laptop context capacity.** Treat 128K–262K as architecture metadata until measured.
- **Weight fit is not workload fit.** Reserve headroom for KV cache, temporary allocations, tools, and macOS.
- **Checkpoint format is an experimental factor.** GGUF-versus-MLX results are end-to-end stack comparisons, not pure engine comparisons.
- **Tool support is empirical.** A model card's function-calling claim does not guarantee correct parser behavior in every runtime.
- **Templates are part of the checkpoint.** gpt-oss requires Harmony, including preservation of reasoning state through tool turns.
- **Prefix-cache capability is architecture-specific.** Current vLLM-Metal automatic prefix caching does not cover Qwen3.5/3.6 hybrid models, so use a supported architecture for that ablation.
- **Fanless steady state matters.** Add sustained 20–30 minute qualification after short microbenchmarks.

## Selection gates

For the first real run:

1. Run the control model through all six tasks and all controller paths.
2. Qualify one Gemma 4 candidate at 4K, 8K, and 16K context where feasible.
3. Compare calibration success per total logical token, not only tokens per second.
4. Freeze exactly one primary checkpoint before the 72-run matrix.
5. Run GLM-4.7-Flash only if the host has 32 GB and qualification leaves safe memory headroom.

See [official sources](sources.md) for the model cards and artifact registries supporting this snapshot.
