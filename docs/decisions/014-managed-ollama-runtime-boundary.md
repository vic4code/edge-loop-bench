# ADR 014: Own the measured Ollama runtime process

- Status: Accepted for v0.7 implementation
- Date: 2026-07-16

## Context

Hashing an Ollama executable and probing `ollama --version` does not prove that
the process answering `127.0.0.1:11434` is that executable, was launched with
the preregistered environment, or remains the process admitted by the study.
Attaching to an already-running desktop service would leave KV-cache policy,
parallelism, cloud behavior, and process ownership outside the experimental
trust boundary.

The v0.7 runtime gate therefore needs to own one short-lived Ollama server. It
must remain testable without starting a model, process, or network listener.

## Decision

Add a managed runtime boundary with these semantics:

1. Before process creation, inspect the exact IPv4 loopback endpoint
   `127.0.0.1:11434`. Any listener or serving response is a hard failure; the
   launcher never adopts or terminates a pre-existing service.
2. Securely hash one absolute, regular, executable, non-symlink Ollama binary,
   require its preregistered SHA-256, and, while the just-checked endpoint is
   empty, require the exact v0.31.1 client output
   `Warning: could not connect to a running Ollama instance` followed by
   `Warning: client version is 0.31.1`. The server-present output is not
   accepted at this pre-launch boundary. Recheck the binary after server
   admission.
3. Invoke `[absolute_binary, "serve"]` directly with `shell=False`, closed
   standard streams, a new session, and no command interpolation.
4. Construct the child environment from an allowlist of inherited runtime
   necessities only (`HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, and `TMPDIR`) plus
   these exact settings:

   | Variable | Value |
   | --- | --- |
   | `OLLAMA_CONTEXT_LENGTH` | `4096` |
   | `OLLAMA_FLASH_ATTENTION` | `1` |
   | `OLLAMA_HOST` | `127.0.0.1:11434` |
   | `OLLAMA_KEEP_ALIVE` | `-1` |
   | `OLLAMA_KV_CACHE_TYPE` | `q8_0` |
   | `OLLAMA_MAX_LOADED_MODELS` | `1` |
   | `OLLAMA_NO_CLOUD` | `1` |
   | `OLLAMA_NUM_PARALLEL` | `1` |

   Proxy, dynamic-loader, tracing, and caller-provided `OLLAMA_*` variables are
   not inherited. The frozen Ollama map and the complete sanitized child
   environment receive separate canonical SHA-256 identities.
5. Admit the server only while the owned child is live, the loopback version
   endpoint reports exactly `0.31.1`, and the listener PID set is exactly the
   child PID. An occupied endpoint with missing, additional, or replacement
   ownership is a hard failure.
6. Return a managed handle and an authority-issued, path-free receipt. The
   receipt records only the PID and canonical identities for the runtime
   binary, version, frozen environment, complete sanitized environment,
   endpoint, KV cache, and receipt itself. It contains no executable, home, or
   temporary path. Public construction, `dataclasses.replace`, unregistered
   object fabrication, field mutation, closed receipts, endpoint ownership
   drift, and child-environment drift are rejected.
7. `close()` first invalidates live receipt use, then terminates only the
   launcher-owned child handle, waits for a bounded interval, and uses a
   bounded kill fallback. It never targets a discovered listener PID and is
   idempotent. Context-manager exit calls the same operation.
8. Local model attestation accepts a live managed-runtime receipt instead of
   caller-supplied runtime version, runtime SHA-256, or KV-cache label. It
   independently rehashes the executable path against the receipt before
   projecting those three identities into the path-free model attestation.

Endpoint ownership collection is platform-specific and stays behind an
interface. The macOS implementation may use the fixed system `lsof` binary;
HTTP identity probing uses the existing fixed loopback, proxy-free,
redirect-rejecting boundary.

## Threat model

- **Spoofing:** a desktop Ollama or another listener impersonates the measured
  runtime. Mitigation: empty endpoint precondition plus exact listener PID.
- **Tampering:** the executable, receipt, launch environment, or listener is
  replaced after an earlier check. Mitigation: stable file hashing, sealed
  receipt registry, repeated live validation, and independent model-attestation
  rehashing.
- **Information disclosure:** inherited proxy, credential, or local path data
  enters published evidence. Mitigation: environment allowlist and digest-only
  path-free receipt fields.
- **Denial of service:** startup or shutdown hangs. Mitigation: bounded version,
  readiness, terminate, and kill waits.
- **Elevation of privilege:** shell expansion or a discovered PID is executed
  or signalled. Mitigation: absolute argv execution with no shell and signalling
  only through the owned child handle.

## Verification

Use standard-library `unittest` with injected fake version runner, child
launcher, clock, sleeper, and endpoint inspector. Focused tests must prove:

- exact argv and frozen sanitized environment;
- pre-existing endpoint rejection before launch;
- wrong version, binary replacement, dead child, and wrong/additional listener
  PID rejection;
- forged, replaced, mutated, closed, endpoint-drifted, and environment-drifted
  receipts are rejected;
- `close()` is bounded, signals only the owned child, and is idempotent;
- model attestation obtains runtime version, runtime SHA-256, and `q8_0` only
  from a live receipt and rejects a post-launch binary replacement.

Run:

```sh
PYTHONPATH=src python3 -m unittest \
  tests.test_intercode_managed_ollama \
  tests.test_intercode_local_model -v
python3 -m compileall -q \
  src/edgeloopbench/intercode_managed_ollama.py \
  src/edgeloopbench/intercode_local_model.py \
  tests/test_intercode_managed_ollama.py \
  tests/test_intercode_local_model.py
```

These tests must not execute a real Ollama process, bind a socket, load a
model, or contact any network endpoint.

## Consequences

Runtime configuration becomes measured evidence rather than an operator
assertion. A user must stop any desktop Ollama service before v0.7 admission.
The boundary adds platform-specific listener ownership inspection and a
managed process lifecycle, but model and serving identities can no longer be
silently mixed across unrelated processes.
