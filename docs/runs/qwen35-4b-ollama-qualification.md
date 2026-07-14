# Qwen3.5 4B Ollama qualification

Status: **qualified for control shakeout**

## Frozen operating point

- Host class: Apple M4 MacBook Air, 16 GB unified memory
- Runtime: Ollama `0.31.1`
- Runtime executable SHA-256:
  `67d4b4e0e8a6742b8fec7491ea67653c4cc802651a8fa396aa569af4e12026a2`
- Model tag: `qwen3.5:4b`
- Ollama model ID: `2a654d98e6fb`
- Weight blob SHA-256:
  `81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490`
- Model family and size: `qwen35`, 4.7B parameters
- Weight quantization: `Q4_K_M`
- Context: 4,096 tokens
- Processor observed after load: 100% GPU
- KV-cache quantization: `q8_0`
- Concurrency: one request, one loaded model
- Cloud access: disabled

The server was bound to `127.0.0.1:11434` with `OLLAMA_NO_CLOUD=1`,
`OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`,
`OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_CONTEXT_LENGTH=4096`,
`OLLAMA_FLASH_ATTENTION=1`, and `OLLAMA_KV_CACHE_TYPE=q8_0`.

## Adapter smoke observations

The fixed prompt was `Reply with exactly the single word: fixed`, seed 11,
temperature 0, and a 4,096-token context. These are qualification samples,
not benchmark scores.

| Thinking | Output limit | Final text | Prompt tokens | Completion tokens | Total duration | Decode rate | Stop reason |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| disabled | 16 | `fixed` | 20 | 2 | 0.421 s | 47.1 tok/s | stop |
| enabled | 64 | empty | 18 | 64 | 2.996 s | 23.9 tok/s | length |

The thinking-enabled response contained 244 characters in Ollama's separate
`thinking` field but did not reach a final answer. Therefore thinking mode is
an explicit controller factor. The initial control shakeout disables it; any
thinking-enabled run must use its own labeled manifest.

## First repair-path qualification

Task `python-localized-001` was prepared in an isolated worktree and presented
as a public-only snapshot. With the pinned full-file edit JSON schema, one
direct call at seed 11 produced an allowed source replacement that passed both
the public tests and the isolated evaluator.

- Prompt tokens: 631
- Completion tokens: 119
- Total duration: 7.393 seconds
- Stop reason: `stop`
- Public tests: passed
- Isolated evaluation: passed

Before schema-constrained edits were enabled, three attempts proposed the
correct semantic change but emitted invalid unified-diff hunk metadata. Those
attempts are interface qualification evidence, not effectiveness scores. The
accepted controller contract avoids treating diff serialization as repair
ability.
