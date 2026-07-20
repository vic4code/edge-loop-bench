# v0.7 InterCode-Bash 30-task run ledger

This file is an append-only operational ledger. Add dated entries below; never
rewrite an earlier entry to make a later state look cleaner. Episode evidence
belongs in mode-`0600`, hash-chained journals and summaries must be derived from
those journals.

## Entry 000 — preregistration prepared

- Date: 2026-07-16, Asia/Taipei
- Status: **not started**
- Measured model prompts: **0**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**
- Design: [`experiment-design-v0.7-intercode-30task.md`](../experiment-design-v0.7-intercode-30task.md)
- Decision: [`ADR 013`](../decisions/013-intercode-30task-compromise.md)

### Frozen design identity

| Item | Preregistered value |
| --- | --- |
| InterCode revision | `c3e46d827cfc9d4c704ec078f7abf9f41e3191d8` |
| Source corpus SHA-256 | `b71d029f20453f96a2872b9c1a79d716f48443009acbbf916d63d0d09efc5391` |
| Static-exclusion audit SHA-256 | `ab8e1121971ff22426afa3394bb5469bae2ec7d3c6c45e323ecfe55237feb35e` |
| Eligible frame | `fs1/fs2/fs3/fs4 = 55/45/56/24` |
| Sample quota | `9/8/9/4` |
| Ordered sample-manifest SHA-256 | `da5355df187c85b248469c6238c4f4c61dbfcca34c290e4163b55292d287fc60` |
| Controller revision | `interactive-controller-v4-v07-preregistered-topology` |
| Campaign schema | `intercode-30task-campaign-ledger-v3` |
| Campaign schedule SHA-256 | `8ef3e22d28119b9724399dad001064c1ee7841b89590dec88f7c0a676ae3cc7b` |
| Execution-envelope revision | `intercode-v0.7-execution-envelope-v1` |
| Calibration-design SHA-256 | `ba34a2886f4307c86ff562aa75c7c96c180552eebe1d242948375fab9eecd219` |
| Strict-evaluator SHA-256 | `e3ce3f3785e1ec6ad5bad87d14632834d5af0307a2143442987d48410787b3d1` |
| Models | `qwen3.5:4b`, `phi4-mini:3.8b` |
| Confirmatory seed | `11` |
| Attempt cap | `K = 4` |
| Arms | Direct, Independent Verified Sampling, Raw Feedback Loop, Engineered Loop |
| Confirmatory prompt ceiling | `780` across both models |
| Calibration set | `bash-calibration-000..003`, at most `13` prompts/model |
| Primary estimand | Qwen Engineered minus Raw weighted strict-success difference |
| Resume unit | sealed execution envelope; pending work is never reissued |

### Pre-execution gates

| Gate | State at Entry 000 |
| --- | --- |
| Source and audit revalidation | pending |
| Native offline images and clean-reset proof | pending |
| Selected-task double gold replay | `0/30` |
| Strict evaluator adversarial tests | pending |
| Prompt/controller/progress/runtime manifest | pending |
| Qwen calibration | `0/4` episodes |
| Phi calibration | `0/4` episodes |
| Wall-time estimate and 1.5x planning bound | pending calibration |
| `make check` and leak-focused review | pending |
| Host admission | pending |

### Confirmatory coverage

| Model | Direct | Independent | Raw | Engineered | Complete tasks |
| --- | ---: | ---: | ---: | ---: | ---: |
| Qwen3.5 4B | 0/30 | 0/30 | 0/30 | 0/30 | 0/30 |
| Phi-4 Mini 3.8B | 0/30 | 0/30 | 0/30 | 0/30 | 0/30 |

### Accounting placeholders

These are deliberately blank until derived from sealed evidence:

- actual initial model prompts: pending;
- additional independent-sample prompts: pending;
- automatic feedback-conditioned follow-ups: pending;
- actual human interventions: pending;
- logical prompt/completion tokens: pending;
- unresolved and avoided unresolved handoffs: pending;
- active wall time and host-safety events: pending;
- invalid or interrupted episode keys: pending.

No README table or HTML result should point to v0.7 while this entry remains the
latest ledger state.

## Entry 001 — Docker admission refused safely

- Date: 2026-07-16 18:20 Asia/Taipei
- Phase: pre-execution infrastructure inventory
- Status: **safety stop; no measured episode started**
- Measured model prompts: **0**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Docker Desktop startup automatically activated 15 unrelated pre-existing
containers. During the read-only inventory, VM pressure changed from normal to
warning level `2`, swap use increased from approximately `10.73` GiB to
`11.78` GiB, and host free disk decreased from approximately `34` GiB to `33`
GiB. Ollama reported zero resident models throughout.

The admission gate therefore refused image build, qualification, calibration,
and model loading. Docker Desktop was returned to its prior non-serving state;
no container was stopped, removed, updated, or pruned individually, and no
benchmark image was built. A later attempt requires explicit operator approval
to stop the unrelated auto-start containers and recover enough disk before the
next host admission.

## Entry 002 — benchmark-owned idle Ollama server stopped

- Date: 2026-07-16 20:03 Asia/Taipei
- Phase: pre-execution runtime preparation
- Status: **safe scoped cleanup; host admission still refused**
- Measured model prompts: **0**
- Confirmatory episodes: **0/240**

The prior benchmark tmux session `edgeloop-v02-ollama` had no resident model.
It was stopped so the managed v0.7 runtime can later require an empty
`127.0.0.1:11434` endpoint and own the serving process from launch through
shutdown. A loopback listener check after the stop found no listener.

VM pressure remained at warning level `2`, so no managed server, model,
qualification container, calibration episode, or confirmatory episode was
started. No Docker container, image, volume, model artifact, or build cache was
changed in this entry.

## Entry 003 — live version preflight incompatibility corrected before launch

- Date: 2026-07-16 20:36 Asia/Taipei
- Phase: pre-execution runtime validation
- Status: **framework correction; host admission still refused**
- Measured model prompts: **0**
- Confirmatory episodes: **0/240**

A read-only invocation of the pinned `/opt/homebrew/bin/ollama` binary exposed
a unit-test-only assumption before any server was launched. With the loopback
endpoint empty, Ollama `0.31.1` returns an exact two-line client warning from
`--version`; the managed launcher had required the server-present one-line
form and therefore could not have started on this host. A failing test was
added first, the pre-launch parser was changed to accept only the observed
empty-endpoint form, and the managed-runtime plus local-model suites passed
`14/14`. The same read-only probe then passed against the real binary.

No Ollama server, model, Docker daemon, container, image build, calibration
episode, or confirmatory episode was started. This correction changes the
committed runtime-controller source identity and must be included in the clean
v0.7 source inventory before any manifest is authorized.

## Entry 004 — formal campaign authority added before measurement

- Date: 2026-07-16, Asia/Taipei
- Phase: pre-execution evidence-chain hardening
- Status: **preregistration implementation revision; host admission still refused**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**

The production review found that a schedule-valid campaign journal could be
mixed with otherwise valid authorities from another preparation. Before any
model request, campaign schema `v3` was therefore superseded by
`intercode-30task-campaign-ledger-v4`. A runnable campaign now requires the
verifier-issued `study_binding_sha256` in its declaration; a schedule-only
`CampaignSpec` cannot create a declaration or intent.

This run-specific binding does not change the 30 tasks, Williams order, arms,
models, seed, attempt cap, estimands, or inference plan. The schema revision is
part of the frozen schedule identity, so the schedule SHA-256 changed from
`8ef3e22d28119b9724399dad001064c1ee7841b89590dec88f7c0a676ae3cc7b` to
`7eaf21a911e12f3f4f639313b7281e0227bfa83ed70471e9057aa03daa03fbc2`.
The new value supersedes Entry 000 for any future v0.7 execution. Earlier
entries are retained rather than rewritten.

## Entry 005 — bind every formal artifact to its execution authority

- Date: 2026-07-16, Asia/Taipei
- Status: framework correction; no calibration or formal model request issued
- Previous schedule SHA-256:
  `sha256:7eaf21a911e12f3f4f639313b7281e0227bfa83ed70471e9057aa03daa03fbc2`
- Revised schedule SHA-256:
  `sha256:1c1573590b509b97593a5d0668e10a09e4d6870ff7e58aaf6adb4bdc1f497653`
- Reason: campaign execution envelopes, campaign terminals, and v0.7
  controller events now carry the exact execution-authority SHA-256. Formal
  episodes use the prepared study binding; calibration episodes use the
  pre-calibration manifest. Cross-study stale artifacts therefore fail before
  reconciliation or publication.
- Accounting impact: calibration prompts `0`; formal prompts `0`; prior raw
  results were not rewritten.

## Entry 006 — framework freeze candidate ready; host gate still closed

- Date: 2026-07-17, Asia/Taipei
- Phase: pre-execution production composition and evidence hardening
- Status: **framework verified; no calibration or formal model request issued**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

The v0.7-only suite passed `147/147` after adding the exact model-major formal
driver, production calibration runtime composer, image-provenance seal,
intervention-journal instance identity, tokenizer-artifact binding, fd-relative
execution-envelope verification, infrastructure-invalid rejection, causal
candidate-1 equality, and arm-specific environment-lifecycle validation.

Only two admitted small-model artifacts remain locally: `qwen3.5:4b` and
`phi4-mini:3.8b`. Unused 9B and 12B blobs were removed only after verifying no
remaining manifest referenced them. Available disk increased above the frozen
32 GiB admission minimum. No result journal or prior published result was
edited.

The live host probe still reported macOS VM pressure level `2`; the frozen
policy requires exactly level `1`. Therefore tokenizer build, Docker image
build, qualification, Ollama launch, calibration, and formal execution remain
withheld. This safety refusal is not a model or loop outcome and is excluded
from effectiveness claims.

## Entry 007 — complete production control path verified without model calls

- Date: 2026-07-17, Asia/Taipei
- Phase: pre-execution framework freeze
- Status: **production runner complete; host gate still closed**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**

The full repository check passed `669` tests with one expected APFS-specific
skip, followed by bytecode compilation, three manifest validations, and the
sample-summary smoke test. A new single-process production runner now composes
the clean source inventory, fresh artifact tree, managed Ollama, exact Docker
identity, four-image build and verification, image provenance, two small-model
and tokenizer attestations, selected-task qualification, calibration gold,
outcome-free manifest, eight-row calibration, planning gate, bound formal
study, model-major 240-row campaign, intervention seal, aggregate evidence, and
frozen analysis.

The shared model-phase manager carries actual residency across phase
boundaries. In particular, calibration's final Phi residency is the previous
runtime used when formal execution transitions back to Qwen; the formal
driver's first `None` callback cannot be misread as an empty Ollama server.

The latest read-only host probe still returned VM pressure level `2`, so the
production CLI was not invoked with `--execute`. Prompt accounting remains
exactly zero and no new performance claim is permitted.

## Entry 008 — preflight dependency order corrected and full check repeated

- Date: 2026-07-17, Asia/Taipei
- Phase: final framework review and live read-only preflight
- Status: **framework verified; host gate still closed**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

The production preflight originally required the pinned tokenizer helper to
already exist, while the host policy withheld tokenizer provisioning until the
preflight passed. Revision `intercode-v0.7-production-runner-v2` removes that
cycle: read-only preflight accepts a canonical future helper location, while
`--execute` requires the executable and provenance record before creating any
run artifact. Four focused tests cover the admitted gate, warning-pressure
refusal, mutation-free missing-helper refusal, and pre-provisioning preflight.

After that change, `make check` passed `674` tests with one expected APFS-only
skip, bytecode compilation, all three manifest validations, and the sample
summary smoke test. The live read-only sample reported VM pressure level `2`,
40 percent system-wide free memory, and 46,214,365,184 free bytes on the data
filesystem. Ollama was not running. Because the frozen pressure requirement is
exactly level `1`, no tokenizer build, Docker build, model load, prompt, or
experiment artifact was started.

## Entry 009 — scoped host recovery and tokenizer provisioning safety stops

- Date: 2026-07-20, Asia/Taipei
- Phase: pre-execution host recovery and tokenizer provisioning
- Status: **two build attempts stopped safely; no model request issued**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

After an unexpected workstation restart, Docker Desktop's daemon was recovered
and the 15 unrelated auto-start containers named in the live inventory were
temporarily stopped without deletion. Docker Desktop's benchmark-session VM
allocation was reduced from 8,092 MiB and eight CPUs to 4,096 MiB and four
CPUs. The daemon reported the new limits, then was stopped before native
tokenizer compilation. These are operational host controls, not measured
serving-efficiency factors; no model serving or effectiveness episode occurred.

Two fresh tokenizer work directories reached warning VM pressure level `2`
while CMake's FetchContent path cloned the pinned llama.cpp tag graph. Each
attempt was interrupted immediately under the safety policy. Partial ignored
build trees were retained for diagnosis and never admitted as artifacts. The
failure was isolated to source provisioning rather than compilation, model
loading, controller behavior, or scoring.

Before another attempt, runtime-factory revision v3 changes the frozen recipe
to shallow-fetch exactly `refs/tags/b9840`, verify commit
`8c146a8366304c871efc26057cc90370ccf58dad`, pre-apply the compatibility patch
from pinned Ollama commit `710292ff4f191d8da9f6a4230804fbc693338d4a`, and
configure against that local source. Tests were added before implementation.
The directly replayable structured plan passed fresh-context review, and the
full repository check passed 676 tests with one expected APFS-only skip plus
compile, manifest-validation, and summary-smoke gates. No experimental result
may be inferred from this entry.

## Entry 010 — first production attempt rejected by macOS listener framing

- Date: 2026-07-20, Asia/Taipei
- Phase: managed Ollama launch
- Status: **infrastructure-invalid before model loading**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Production attempt `v07-production-20260720-attempt1` passed its read-only host
preflight with VM pressure level `1`, 44 percent free memory, more than 47 GB
free disk, and zero running containers. It created only the private artifact
tree, source inventory, preflight record, and intervention prefix before the
managed Ollama boundary rejected endpoint ownership. The owned server was
closed; no image, task container, model residency, tokenizer request, model
prompt, calibration row, or formal row started.

Live diagnosis reproduced the exact server version and same owned process PID,
but the production listener parser returned an empty PID set. macOS
`/usr/sbin/lsof -Fp` emits both `p<PID>` and mandatory `f<FD>` records; the
parser had accepted only `p` records and rejected the legitimate descriptor
line. A regression test using the observed `p4242\nf3\n` framing was added
before the parser change. The correction accepts only numeric `p` and `f`
records, extracts only PIDs, and retains exact single-owner equality. A later
attempt must use a fresh artifact root and a new intervention-journal identity.

## Entry 011 — second production attempt denied after Docker VM wake

- Date: 2026-07-20, Asia/Taipei
- Phase: full host admission
- Status: **infrastructure-invalid before image build or model loading**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Before attempt 2, the Docker Desktop VM allocation was reduced to 2,048 MiB
and four CPUs; formal task containers retain their separately frozen 512 MiB
limit. This operational allocation is not a serving-efficiency ablation. The
fresh attempt passed its own preflight at VM pressure level `1`, 47 percent free
memory, more than 47 GB free disk, and zero reported running containers. The
managed Ollama ownership fix also passed on the live host.

The first production Docker access then observed two unrelated AgentGPT
containers, so full host admission refused the attempt and closed the owned
Ollama process. Docker's event log places both `restart: always` starts at
18:27:03 Asia/Taipei; the Docker backend process itself retained its 18:13:44
start time. This timing is consistent with Docker Desktop Resource Saver
stopping an idle Linux VM and the production access waking it, at which point
restart-policy containers returned. Docker documents the default five-minute
idle transition and 3–10 second VM wake behavior in its [Resource Saver
guide](https://docs.docker.com/desktop/use-desktop/resource-saver/).

Attempt 2 created only the private artifact tree, source inventory, preflight
record, and intervention prefix. It built no image, opened no task container,
loaded no model, issued no tokenizer or model request, and consumed no prompt.
A fresh attempt must explicitly wake the VM, stop the two exact unrelated
containers, verify an empty running set, and enter production immediately
before the next idle transition. Existing restart policies are not edited.
