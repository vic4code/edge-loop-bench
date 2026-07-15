# Primary sources

Retrieved on **2026-07-14** unless noted otherwise. Runtime capabilities change quickly; published experiments must pin a version or commit in addition to citing documentation.

## Apple and MLX

- [MLX repository](https://github.com/ml-explore/mlx) — Apple Silicon framework and platform scope.
- [MLX unified memory](https://ml-explore.github.io/mlx/build/html/usage/unified_memory.html) — shared CPU/GPU memory behavior.
- [MLX installation](https://ml-explore.github.io/mlx/build/html/install.html) — current platform requirements.
- [MLX-LM repository](https://github.com/ml-explore/mlx-lm) — inference, conversion, quantization, caching, and server examples.
- [MLX-LM server warning](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md) — development-server security limitations.

## vLLM-Metal

- [vLLM GPU installation matrix](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/) — official Apple Silicon route to vLLM-Metal.
- [vLLM-Metal documentation](https://docs.vllm.ai/projects/vllm-metal/en/latest/) — architecture and feature overview.
- [Installation](https://docs.vllm.ai/projects/vllm-metal/en/latest/installation/) — macOS, Python, and toolchain requirements.
- [Configuration](https://docs.vllm.ai/projects/vllm-metal/en/latest/configuration/) — paged attention and memory settings.
- [Supported models](https://docs.vllm.ai/projects/vllm-metal/en/latest/supported_models/) — architecture-specific support status.
- [Speculative decoding](https://docs.vllm.ai/projects/vllm-metal/en/latest/speculative_decoding/) — MTP, draft-model, and n-gram paths.
- [TurboQuant](https://docs.vllm.ai/projects/vllm-metal/en/latest/turboquant/) — KV-cache compression behavior and upstream reduction claims.
- [GPU profiling](https://docs.vllm.ai/projects/vllm-metal/en/latest/profiling/) — Metal capture methods and overhead warning.

## Ollama

- [macOS support](https://docs.ollama.com/macos) — Apple Silicon requirements.
- [GPU support](https://docs.ollama.com/gpu) — Metal execution.
- [FAQ](https://docs.ollama.com/faq) — concurrency, keep-alive, Flash Attention, KV-cache quantization, and Docker limitation.
- [Context length](https://docs.ollama.com/context-length) — memory-dependent defaults and agent guidance.
- [Native API usage metrics](https://docs.ollama.com/api/usage) — token and duration fields.
- [OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility) — supported compatibility surface.
- [Official releases](https://github.com/ollama/ollama/releases) — versions and upstream performance claims.

## Gemma 4

- [Google Gemma 4 model card](https://ai.google.dev/gemma/docs/core/model_card_4) — model architecture, capabilities, license, and intended use.
- [Google Gemma release history](https://ai.google.dev/gemma/docs/releases) — family release dates.
- [Google Gemma core guide](https://ai.google.dev/gemma/docs/core) — vendor memory estimates and caveats.
- [Gemma 4 31B Hugging Face card](https://huggingface.co/google/gemma-4-31B) — official family parameter and context table.
- [Gemma 4 E2B QAT Q4_0 GGUF](https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf) — exact compact checkpoint option.
- [Ollama Gemma 4 artifacts](https://ollama.com/library/gemma4/tags) — packaged artifact sizes.

## GLM

- [GLM-4.7-Flash model card](https://huggingface.co/zai-org/GLM-4.7-Flash) — architecture, context, coding/tool evaluation, and license.
- [GLM-4-9B-0414 model card](https://huggingface.co/zai-org/GLM-4-9B-0414) — smaller GLM capabilities and format.
- [Official GLM repository](https://github.com/zai-org/GLM-4.5) — serving examples and implementation lineage.
- [Ollama GLM-4.7-Flash artifacts](https://ollama.com/library/glm-4.7-flash/tags) — packaged Q4 size.

## Other candidates

- [Qwen3.5 4B](https://huggingface.co/Qwen/Qwen3.5-4B) and [Qwen3.5 9B](https://huggingface.co/Qwen/Qwen3.5-9B) — official architecture, context, tools, and license.
- [Ollama Qwen3.5 artifacts](https://ollama.com/library/qwen3.5/tags) — packaged artifact sizes.
- [Qwen3-Coder 30B A3B](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct) — official coding-model card.
- [Ollama Qwen3-Coder artifacts](https://ollama.com/library/qwen3-coder/tags) — packaged artifact sizes.
- [Phi-4-mini-instruct](https://huggingface.co/microsoft/Phi-4-mini-instruct) — official model and function-calling limitations.
- [Ollama Phi-4-mini artifacts](https://ollama.com/library/phi4-mini/tags) — packaged artifact sizes.
- [gpt-oss-20b](https://huggingface.co/openai/gpt-oss-20b) — official architecture, Harmony format, and memory positioning.
- [OpenAI gpt-oss with Ollama](https://developers.openai.com/cookbook/articles/gpt-oss/run-locally-ollama) — local runtime and Harmony handling guidance.
- [Devstral Small 2](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512) — official coding focus and hardware positioning.
- [Ollama Devstral Small 2 artifacts](https://ollama.com/library/devstral-small-2/tags) — packaged artifact sizes.

## Interactive coding benchmarks

Retrieved on **2026-07-15**.

- [InterCode paper](https://papers.neurips.cc/paper_files/paper/2023/file/4b175d846fb008d540d233c188379ff9-Paper-Datasets_and_Benchmarks.pdf) — benchmark formulation, Single Turn and Try Again strategies, task counts, prompts, and reported results.
- [InterCode source at the pinned commit](https://github.com/princeton-nlp/intercode/tree/c3e46d827cfc9d4c704ec078f7abf9f41e3191d8) — environment, experiment, data, Docker, and license source for v0.6 qualification.
- [Pinned NL2Bash data description](https://github.com/princeton-nlp/intercode/blob/c3e46d827cfc9d4c704ec078f7abf9f41e3191d8/data/nl2bash/README.md) — the four filesystem strata and 200-row source population.
- [Pinned Try Again experiment](https://github.com/princeton-nlp/intercode/blob/c3e46d827cfc9d4c704ec078f7abf9f41e3191d8/experiments/eval_n_turn.py) — action, submit, reward-feedback, and stopping implementation inspected for the adapted protocol.
- [Pinned Bash reward](https://github.com/princeton-nlp/intercode/blob/c3e46d827cfc9d4c704ec078f7abf9f41e3191d8/intercode/envs/bash/bash_env.py) — gold-derived reward and evaluator-detail boundary.
- [Google Research MBPP split](https://github.com/google-research/google-research/blob/master/mbpp/README.md) — canonical task-ID splits used to audit the unreconstructable InterCode-Python 117 count.
- [NL2Bash dataset license](https://github.com/TellinaTool/nl2bash/blob/master/data/bash/LICENSE) — separate MIT license for the underlying dataset.

## Loop-engineering guidance

Retrieved on **2026-07-15**.

- [Claude: Getting started with loops](https://claude.com/blog/getting-started-with-loops) — official definition of a loop as repeated work until a stop condition, the turn/goal/time/proactive taxonomy, quantitative verification guidance, independent review, bounded usage, and pilot-first advice. It motivates controls; it does not report a benchmark uplift or prescribe the EdgeLoop rollback policy.
- [Loop Engineering reference repository at `6a67035`](https://github.com/cobusgreyling/loop-engineering/tree/6a670357ab748e20d14752bda82999a97f8afc6f) — community patterns for schedules, isolated worktrees, skills, sub-agents, durable state, budgets, and human gates. EdgeLoop cites this as a systems reference, not an official Claude implementation or a source of measured performance claims.

## Claim labels

Repository prose uses these interpretations:

- **Official fact:** directly stated in a linked primary source.
- **Upstream claim:** a performance or quality number reported by the project or vendor and not yet reproduced here.
- **Estimate:** derived from parameter count or packaged artifact size; methodology is stated.
- **Measured here:** produced by a pinned EdgeLoopBench run with raw data and a manifest.
