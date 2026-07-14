# MLX-LM reference track

MLX is Apple's array framework for Apple Silicon. CPU and GPU share unified memory, which makes MLX-LM a useful reference for understanding model conversion, quantization, prompt caching, and KV-cache trade-offs without pretending that unified memory is unlimited.

## Role in EdgeLoopBench

Use MLX-LM for:

- Apple-native inference and quantization experiments;
- prompt-cache reuse across loop iterations;
- rotating fixed-size KV-cache studies;
- draft-model speculative decoding;
- a reference point for vLLM-Metal's MLX-backed execution.

Its built-in HTTP server is explicitly development-oriented and has only basic security checks. Bind it to localhost and do not treat it as a production network service.

Keep two transport tracks separate:

- **HTTP server:** local OpenAI-like requests, automatic LRU prompt caching, cache-size/byte controls, and draft-model flags.
- **`mlx_lm.generate` or Python API:** prompt-cache files, rotating/fixed KV experiments such as `--max-kv-size`, and lower-level generation control.

`--max-kv-size` is not a current `mlx_lm.server` option.

## Baseline discipline

- Pin the MLX-LM release and exact model revision.
- Start with standard 4-bit and 8-bit checkpoints before learned or mixed-bit recipes.
- Record conversion command, group size, source checksum, and generated artifact checksum.
- In generation/API experiments, treat `--max-kv-size` as a quality-affecting intervention, not a free memory optimization.
- Warm and cold prompt caches are separate conditions.

The HTTP server automatically maintains an LRU prompt cache with a default size of ten entries. For a cold-cache baseline, restart the server or pass `--prompt-cache-size 0`; for warm-cache runs, pin `--prompt-cache-size` and `--prompt-cache-bytes`. Record `usage.prompt_tokens_details.cached_tokens` so a hidden hit cannot be mislabeled as cold.

## Suggested experiment order

1. HTTP server with `--prompt-cache-size 0` and an uncached 4K prompt.
2. HTTP server with a pinned cache size and repeated stable prefix, verifying `cached_tokens`.
3. 4K, 8K, and 16K request shapes where memory allows.
4. Standard 8-bit versus 4-bit weights with the same task protocol.
5. `mlx_lm.generate` or Python API with default versus bounded KV size, including task-success regression.
6. HTTP or API draft-model speculation on fixed prompts, with its exact flags recorded.
7. Learned quantization only after the simpler baselines are reproducible.

MLX-LM documents macOS memory wiring for unusually large models. EdgeLoopBench will never alter privileged `iogpu.wired_limit_mb` settings automatically. Any manual change must be disclosed in the hardware manifest and restored by the operator.

Sources: [MLX](https://github.com/ml-explore/mlx), [unified memory](https://ml-explore.github.io/mlx/build/html/usage/unified_memory.html), [MLX-LM](https://github.com/ml-explore/mlx-lm), [HTTP server](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md), and [release history](https://github.com/ml-explore/mlx-lm/releases).
