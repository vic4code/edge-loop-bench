# Ollama baseline on Apple Silicon

Ollama is the first baseline because it offers native Metal execution, packaged models, a local API, and useful per-request timing counters with minimal setup.

## Scope and safety

- Run the native macOS process. Docker Desktop on macOS does not provide GPU passthrough for Ollama.
- Keep the listener on localhost. This benchmark does not provide authentication or network hardening.
- Set `OLLAMA_HOST=127.0.0.1:11434` explicitly so an inherited setting cannot expose the server.
- Set `OLLAMA_NO_CLOUD=1` to disable Ollama cloud models and web search. This is not egress isolation and does not by itself prove local-only execution.
- Pin the Ollama version and model digest in every run manifest.

## Reproducible single-request baseline

The example environment file in `configs/runtimes/ollama.env.example` configures:

```text
OLLAMA_HOST=127.0.0.1:11434
OLLAMA_NO_CLOUD=1
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_KEEP_ALIVE=-1
OLLAMA_CONTEXT_LENGTH=16384
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
```

This is a starting operating point, not a universal optimum. A 16K allocation can be too large on an 8 GB machine or with a large checkpoint.

Choose one launch mechanism and record it:

- **Shell-owned server:** load the environment file in that shell, then run `ollama serve`.
- **macOS application:** call `launchctl setenv` for every variable, fully quit and restart the Ollama application, and record that procedure. Sourcing a shell file does not reconfigure the background application.

The validated experiment manifest repeats the exact command and environment so the result does not depend on an undocumented shell state.

## Why these controls matter

- Parallel requests multiply context memory, so concurrency one is the causal baseline.
- One resident model avoids unrelated evictions and reloads.
- Infinite keep-alive separates model loading from warm loop iterations.
- Flash Attention can reduce long-context memory where supported.
- Q8 KV cache is a memory/quality compromise. Weight Q4 and KV Q8 are separate factors.

Ollama reports load duration, prompt-token count and duration, generated-token count and duration, and total duration in its native API. Store the raw response before converting nanoseconds to summary units.

After a warm-resident series, release the model with `ollama stop <model>` or a request whose `keep_alive` is `0` before switching models or backends. Infinite residency is an experimental condition, not a machine-wide default to leave behind.

## Suggested ablations

Change one factor per plan:

1. cold load versus warm resident;
2. 4K, 8K, and 16K context;
3. `f16`, `q8_0`, then `q4_0` KV cache;
4. Flash Attention off versus on;
5. one versus two concurrent requests after the single-request baseline;
6. repeated stable prefix versus unique prefix.

For every point, record `ollama ps` output so allocated context, model residency, size, and CPU/GPU placement can be audited.

To substantiate a local-only claim, combine a pinned local model digest, disabled cloud features, a loopback bind, runtime logs, and denied or observed outbound network access. Do not infer it from `OLLAMA_NO_CLOUD` alone.

## Failure conditions

Do not publish an operating point as suitable when it:

- swaps persistently;
- unloads or reloads unexpectedly within the measured warm phase;
- falls back substantially to CPU without disclosure;
- produces parser-incompatible tool calls;
- throttles so strongly that run order dominates the result.

Sources: [Ollama macOS](https://docs.ollama.com/macos), [GPU support](https://docs.ollama.com/gpu), [FAQ](https://docs.ollama.com/faq), [context length](https://docs.ollama.com/context-length), and [API usage metrics](https://docs.ollama.com/api/usage).
