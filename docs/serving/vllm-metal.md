# vLLM-Metal research track

On Apple Silicon, “vLLM” in this repository means the separate community-maintained `vllm-metal` plugin under the vLLM project. Core vLLM does not directly provide a PyTorch MPS backend. vLLM-Metal uses MLX for compute, native Metal kernels, the vLLM scheduler, and an OpenAI-compatible serving surface.

## Requirements

The current vLLM-Metal installation guide requires:

- Apple Silicon;
- a native arm64 Python 3.12 environment;
- Xcode command-line tools.

MLX separately requires macOS 14 or newer, so Sonoma is an operational transitive requirement rather than a statement from the vLLM-Metal install page. Follow the versioned upstream installation guide and record the exact release or commit. Do not install ordinary `vllm` and assume it will use MPS.

Always bind explicitly to loopback; current vLLM defaults can listen on all interfaces:

```bash
vllm serve "$MODEL" --host 127.0.0.1 --port 8000
```

The example manifest records this command. Do not expose the unauthenticated research endpoint to a LAN.

## Initial operating points

The paged example environment file exposes the two most useful first controls:

```text
VLLM_METAL_USE_PAGED_ATTENTION=1
VLLM_METAL_MEMORY_FRACTION=0.7
```

Paged attention can provide shared-prefix reuse for compatible architectures. The automatic path may reserve a high fraction of unified memory, so sweep `0.5`, `0.7`, and `0.9` explicitly and monitor host pressure. A setting that maximizes theoretical cache capacity but forces swap is not an edge optimum.

The separate `vllm-metal-nonpaged.env.example` sets paged attention to `0` and memory fraction to `auto`. A numeric `VLLM_METAL_MEMORY_FRACTION` is invalid on the non-paged path; never combine the paged `0.7` profile with a non-paged baseline.

## Optimization sequence

1. Establish a non-paged baseline with the dedicated `auto` memory profile, or use a separately documented default baseline.
2. Enable paged attention at one safe memory fraction.
3. Measure cold and warm repeated-prefix requests.
4. Sweep memory fraction with the workload otherwise frozen.
5. Test speculative decoding with a compatible draft method.
6. Test TurboQuant KV compression only after the paged baseline is stable.
7. Profile an identified bottleneck with bounded Metal capture or `xctrace`.

Keep weight quantization, KV compression, speculative decoding, and prefix caching as distinct experimental factors.

## Agent-loop-specific opportunities

- Repeated system prompts and task prefixes are natural prefix-cache workloads.
- N-gram speculation may help code, JSON, and repeated structured tool output.
- Gemma 4 multi-token-prediction draft models create a model-native speculation study.
- Paged KV and TurboQuant may trade memory for more useful loop history or more iterations.

## Speculative-decoding activation contract

Current vLLM-Metal speculative paths require paged attention, synchronous scheduling via `--no-async-scheduling`, and greedy-compatible requests with `temperature=0`. Non-greedy requests can silently skip drafting, so record accepted draft tokens rather than labeling a server “speculative” from its startup flag alone.

Gemma 4 MTP is currently narrow: matching BF16 assistant checkpoints for E2B, E4B, or 31B, one speculative token, and no separate assistant KV cache because the assistant reads the target paged cache. Generic draft-model methods can allocate their own KV state; include that memory in qualification. Freeze the speculative configuration JSON in the manifest.

Model support and automatic prefix-cache behavior vary by architecture and release. Record capability detection rather than assuming feature parity. Current support documentation lists Gemma 4 and GLM-4.7-Flash as experimental paths; hybrid architectures may have different cache behavior.

In particular, the current support matrix does not enable automatic prefix caching for Qwen3.5/3.6 hybrid architectures. Qwen3.5 remains a useful Ollama control model, but it is not the checkpoint for a vLLM-Metal prefix-cache ablation.

## Profiling warning

Full Metal frame capture can slow execution dramatically and create multi-gigabyte traces. It belongs in a dedicated diagnostic run, never in a latency benchmark. Use full Xcode rather than only its command-line tools, set `MTL_CAPTURE_ENABLED=1` before server startup, bound prompt and generation lengths, and begin with a conservative `VLLM_METAL_MEMORY_FRACTION=0.1`. Replaying a large paged-KV capture can make the laptop unresponsive. Record profiling state in every run.

Sources: [vLLM Apple Silicon installation](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/), [vLLM-Metal overview](https://docs.vllm.ai/projects/vllm-metal/en/latest/), [configuration](https://docs.vllm.ai/projects/vllm-metal/en/latest/configuration/), [supported models](https://docs.vllm.ai/projects/vllm-metal/en/latest/supported_models/), [speculative decoding](https://docs.vllm.ai/projects/vllm-metal/en/latest/speculative_decoding/), [TurboQuant](https://docs.vllm.ai/projects/vllm-metal/en/latest/turboquant/), and [profiling](https://docs.vllm.ai/projects/vllm-metal/en/latest/profiling/).
