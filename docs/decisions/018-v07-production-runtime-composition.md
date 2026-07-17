# ADR 018: Seal the v0.7 production runtime composition

- Status: accepted
- Date: 2026-07-16

## Context

The v0.7 manifest can bind model and host identities, but a production caller
could still assemble a renderer, tokenizer, raw Ollama adapter, or Docker host
record from mutually inconsistent inputs. `OLLAMA_MAX_LOADED_MODELS=1` and
measured-request `keep_alive=-1` also make model-major transitions part of the
experimental boundary: the previous model must not remain resident when host
admission begins for the next model.

## Decision

Use `intercode_v07_runtime_factory.py` as the sole production composition
boundary, revision `intercode-v0.7-production-runtime-factory-v2`. It requires
a live launcher-issued `ManagedOllamaRuntimeReceipt`, a
verifier-issued `LocalModelAttestation`, a verifier-issued tokenizer-helper
attestation, and an exact Docker daemon observation matching
`DockerTelemetryPins`.

The tokenizer-helper verifier hashes the executable and its adjacent
`llama-tokenize.provenance.json`, rejects symlinks and duplicate JSON keys, and
requires the exact build recipe emitted by `tools/build_pinned_tokenizer.py`:

- Ollama commit `710292ff4f191d8da9f6a4230804fbc693338d4a`
- llama.cpp tag `b9840`, resolving to commit
  `8c146a8366304c871efc26057cc90370ccf58dad`
- static, CPU-only macOS arm64 `llama-tokenize` target with two build jobs

The generation configuration is identical for Qwen3.5 4B and Phi-4 Mini 3.8B
except for their pinned renderer/model identities and stop token:

| Setting | Frozen value |
| --- | ---: |
| Context / batch | `4096` / `128` |
| GPU layers / main GPU | `99` / `0` |
| mmap / threads | `true` / `8` |
| Draft prediction | `0` |
| Temperature / top-k / top-p | `0.2` / `40` / `0.9` |
| min-p / typical-p | `0.0` / `1.0` |
| Repeat window / penalty | `64` / `1.1` |
| Presence / frequency penalty | `0.0` / `0.0` |
| Keep-alive / request timeout | `-1` / `120 s` |
| Qwen stop token | `<|im_end|>` |
| Phi stop token | `<|end|>` |

The sealed model bundle contains exactly `V07TokenizerPins`,
`LlamaTokenizeCounter`, `ExactPromptPreparer`, and `OllamaRawModel`. Its public
record contains digests and frozen scalar settings only; local helper, model,
and provenance paths are excluded. `require_live()` rechecks runtime ownership,
helper/provenance identity, model-artifact identity, component wiring, the
exact stdlib tokenizer command runner, tokenizer artifact paths and verified
identities, cache ownership and accounting, and the exact loopback generation
transport object plus its host, port, and timeout. It also rejects added
instance attributes that could shadow tokenizer execution methods.
Post-construction replacement or corruption of any of those execution
callables or mutable boundaries is terminal.

`V07RuntimeSession` is the aggregate production authority. It admits exactly
one Qwen runtime and one Phi runtime sharing the same live managed receipt,
plus the `V07HostIdentityPins` derived from that receipt. Its `session_sha256`
covers both complete path-free model records, the complete path-free managed
runtime receipt, and the host identity. Callers select a model only by one of
the two frozen model IDs; a bare receipt digest is not a production binding.

Model residency changes accept only an exact, issuer-registered
`V07ManagedResidencyBoundary`; structural fakes and copied exact instances do
not carry authority. The boundary is bound to the same live managed receipt
and permits only fixed loopback `/api/generate` and `/api/ps` requests through
the proxy-free, redirect-rejecting HTTP opener. Control requests use an empty
prompt, `stream=false`, and exact `keep_alive` value. This follows Ollama's
[documented preload/unload
contract](https://github.com/ollama/ollama/blob/main/docs/api.md#load-a-model).

A first load requires an empty observed residency set.
A model-major switch requires the exact previous manifest digest, applies
`keep_alive=0`, proves the residency set is empty, applies the target load with
`keep_alive=-1`, and proves the exact target tag and manifest digest are the
only resident model. Any disagreement is terminal infrastructure-invalid; no
formal episode may start from that state. The typed residency boundary is also
bound to the session's exact managed-runtime receipt object and digest. A
transition receipt is minted only after the registered authority, live runtime,
and final sole-resident observation are all rechecked.

## Consequences

- Production orchestration must retain the sealed bundle and call
  session `require_live()` immediately before episode intent/model execution.
- Host admission for a model block occurs only after a sealed residency
  transition receipt for that model.
- The residency boundary is a narrow registered authority. Unit tests retain
  its exact implementation and intercept only the underlying fixed-loopback
  opener; they make no Docker, Ollama, model, or network calls.
