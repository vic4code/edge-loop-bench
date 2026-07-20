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

## Entry 012 — third production attempt exposed an fs1 fixture omission

- Date: 2026-07-20, Asia/Taipei
- Phase: Docker image build
- Status: **instrument failure before qualification or model loading**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Three orphaned Playwright automation daemons using temporary profiles were
terminated without touching the user's Chrome or Atlas sessions. With Docker's
2,048 MiB VM kept genuinely awake, VM pressure then held at level `1` and free
memory reached 51 percent. The benchmark-owned keeper was identity-checked and
removed before production. Attempt 3 passed preflight at pressure level `1`,
52 percent free memory, 48,397,824,000 free bytes on the Docker-data
filesystem, zero running containers, and a live managed Ollama ownership
check.

The first fs1 image build then failed before producing an image record. A
separate diagnostic cache-only build reproduced the exact public fixture
failure: the derived script assigned a fixed mtime to
`/testbed/recent.txt` without first creating the empty file retained by the
pinned upstream script. The attempt directory contains only preflight, source
inventory, Docker identity, intervention-prefix, and one image-build plan
event. No image was admitted, no task container or model was opened, and no
model prompt was issued.

ADR 024 requires the missing empty fixture to be restored, a regression test
to fail before the implementation, every derived source pin to be updated, and
the complete repository plus real four-image build and gold qualification to
pass before a fresh production attempt.

## Entry 013 — final-layer sticky-root invariant failed closed

- Date: 2026-07-20, Asia/Taipei
- Phase: non-scoring image diagnosis
- Status: **instrument hardening before qualification**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

After restoring the missing fs1 file, a real cache-only build passed fixture
setup, ownership, Git-baseline construction, and collector installation. The
final writable-surface audit then rejected the image. An isolated, no-network,
read-only diagnostic image stopped immediately before that audit reported `/`
as UID 0 mode `0755`: the earlier-layer `chmod 1777 /` was not the mode seen at
the later overlay mount boundary.

ADR 025 retains the strict audit and moves sticky-root finalization into the
same final filesystem `RUN` immediately before audit execution. A future build
must prove both that the audit passes and that an isolated exported container
observes UID 0 mode `01777`. This diagnostic created no scoring image,
qualification row, model residency, or model prompt.

## Entry 014 — sibling fixture and build-context audit

- Date: 2026-07-20, Asia/Taipei
- Phase: pre-production instrument review
- Status: **instrument hardening before live image qualification**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Before the next live build, an independent source review found the same empty
fixture omission in four fs2 paths and five fs3 paths. It also found that the
recursive `docker/intercode/**` exception admitted a local Python bytecode
cache to the daemon context even though that cache was outside the recorded
context digest. Regression tests first failed on all nine missing fixtures and
on the broad context exception.

ADR 026 explicitly restores the nine empty fixtures and replaces the recursive
exception with a reviewed file-by-file context allowlist. The setup-script and
`.dockerignore` identities changed, so attempt 3 remains append-only and cannot
be resumed. No Docker task, tokenizer request, model load, or prompt was issued
during this review.

## Entry 015 — Linux ACL capability metadata failed closed

- Date: 2026-07-20, Asia/Taipei
- Phase: non-scoring fs1 image qualification
- Status: **instrument portability fix before image admission**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

The corrected fs1 build passed fixture construction and reached the final
writable-surface audit, which stopped with `acl_unverified`. In an isolated,
no-network container, the pinned Ubuntu Python accepted the same open
descriptor in `os.listxattr()` and returned an empty list, while omitting that
callable from `os.supports_fd`. The collector had trusted the incomplete
capability set instead of attempting the fail-closed descriptor operation.

ADR 027 removes only that metadata precheck. Actual missing support, call
failure, POSIX ACLs, or unexpected extended attributes remain hard failures.
The collector and Dockerfile identities changed. The failed build admitted no
image, opened no task or model runtime, and issued no prompt.

## Entry 016 — four build audits passed; runtime root mode separated

- Date: 2026-07-20, Asia/Taipei
- Phase: non-scoring four-image qualification
- Status: **build-qualified; runtime initialization under test**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

After declaring `.dockerenv` as runtime-injected and hardening the pinned
base's unused world-writable Pebble directory, canonical fs1, fs2, fs3, and fs4
builds each passed the strict writable-surface audit. Their diagnostic image
IDs were respectively `sha256:cc4df6b7…`, `sha256:8db18c53…`,
`sha256:18f7abf3…`, and `sha256:9ae1cd48…`.

An isolated fs1 container with no network, read-only rootfs, all capabilities
dropped, and no-new-privileges confirmed that the collector can produce a
representable runtime state. The same container also proved that Docker resets
the runtime root mount to UID 0 mode `0755`, even though the build layer and
audit observed `01777`. ADR 028 therefore separates build-content audit from
one fixed runtime sticky-root initialization and attestation. No Ollama process,
model residency, task action, calibration row, or prompt was created.

## Entry 017 — replay accounting and classifier amended before scoring

- Date: 2026-07-20, Asia/Taipei
- Phase: adversarial pre-scoring design review
- Status: **schema amendment; no model outcome observed**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

The review found that checkpoint restore physically replays prior Bash actions
but the v4 controller recorded only model-issued actions. ADR 029 adds a
separate bounded replay counter and derives total physical tool executions.
It also corrects the positive-result classifier so an estimate at or above the
`+5pp` practical threshold cannot be labeled below threshold merely because
another inferential condition failed.

The current superseding identities are controller
`interactive-controller-v5-replay-accounting`, campaign ledger
`intercode-30task-campaign-ledger-v5`, execution envelope v3, campaign evidence
v5, calibration evidence v4, execution-envelope set v2, study evidence v5,
analysis v4, production runner v3, and schedule
`sha256:6bc3f7904f1a9bc47fa5fe6244cdc6a89ff7dca61abbf439607e93b3eba3c921`.
Entries 000, 004, and 005 remain historical rather than being rewritten. No
model, prompt, candidate action, strict result, or treatment-dependent signal
informed the amendment.

## Entry 018 — runtime root passed; unsupported Desktop quota failed closed

- Date: 2026-07-20, Asia/Taipei
- Phase: non-scoring Docker runtime qualification
- Status: **runtime root qualified; storage enforcement under amendment**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

An exact, diagnostic-only fs1 container was started with network `none`, all
capabilities dropped, no-new-privileges, and UID/GID `65532:65532`. The fixed
root-only initialization changed `/` to UID 0, GID 0, mode `01777`; its exact
attestation passed, UID 65532 created and removed one root-level probe, the
trusted collector passed, and the inspected security fields remained exact.
The container was then removed by its diagnostic identity.

The first invocation through the production `DockerCli` stopped before
container creation because Docker Desktop's `overlay2` backing filesystem does
not support the frozen per-container `--storage-opt size` request. The daemon
reported that this option requires XFS mounted with `pquota`. No task action,
tokenizer request, model residency, or model prompt occurred, and exact-label
cleanup found no created container. A pre-scoring amendment must replace this
unsupported request with an explicitly recorded, fail-closed Desktop safety
profile without describing a sampled guard as a hard writable-layer quota.

## Entry 019 — Desktop storage and lifecycle chain qualified

- Date: 2026-07-20, Asia/Taipei
- Phase: non-scoring Docker runtime qualification
- Status: **live runtime chain passed; repository verification pending**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

ADR 030 replaces the unsupported writable-layer hard-quota claim with the
frozen `sampled-size-rw-no-hard-quota-v1` profile: a 256 MiB sampled `SizeRw`
watchdog, 16 MiB `RLIMIT_FSIZE`, 0.25-second sampling interval, one-second
probe timeout, the existing 64 MiB logical-state ceiling, and exact cleanup.
This is explicitly a sampled abort guard with possible sampling overshoot, not
a per-container capacity guarantee.

Live qualification then exposed two Docker Desktop 27.3.1 projections before
the diagnostic action could run. A created container reported
`OomKillDisable=false` but projected the same default as JSON `null` after
start, and `docker top` rejected blank PID headings. ADR 031 adds a
lifecycle-specific, typed OOM-field rule and an explicit four-column process
header. Missing or enabled OOM-disable state and every other process-table
shape still fail closed.

After the corresponding RED/GREEN tests, an exact fs1 diagnostic passed the
full create, security-profile validation, runtime sticky-root attestation,
pre/during/post storage sampling, bounded UID-65532 root-level write/remove,
post-action process audit, trusted state collection, and exact-ID cleanup.
`SizeRw` was zero bytes before and after the removed probe. Docker Resource
Saver had restarted the two known AgentGPT containers immediately beforehand;
their exact identities were inspected and stopped without changing restart
policies. No benchmark task, tokenizer request, model load, calibration row,
formal row, or model prompt occurred.

## Entry 020 — replay verifier rebuilt the frozen best policy

- Date: 2026-07-20, Asia/Taipei
- Phase: adversarial pre-scoring evidence review
- Status: **forged replay topologies rejected; full check pending**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Adversarial rechaining showed that aggregate replay counts and prior-checkpoint
identity were insufficient to prove the frozen Engineered controller. The old
verification path could accept a restore completed after the next prompt
preflight or a restore to a prior checkpoint that was not the controller's
best checkpoint.

Campaign and calibration verification now reconstruct the reward trajectory,
latest-on-tie best checkpoint, exact replay depth, and terminal Engineered
selection. A restore is required only on a strict regression, must target that
prior best, and must complete before the next model preflight. Focused tests
reject duplicate restores, late restores, restore-on-tie, stale tie-best, and
wrong-target journals after their chains and aggregate counters are made
internally consistent. The focused campaign and calibration suite passed 34
tests. No live model-dependent event informed the correction.

## Entry 021 — pre-scoring repository gate passed

- Date: 2026-07-20, Asia/Taipei
- Phase: frozen-instrument release gate
- Status: **ready for a fresh production attempt**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

After integrating the image, runtime, storage-watchdog, replay-accounting, and
evidence-verifier corrections, `make check` passed 702 tests with one declared
skip. Python byte-compilation and the repository's configuration validations
also passed, as did `git diff --check`. The live Docker Desktop qualification
chain from Entry 019 remained the runtime evidence for this source revision.

No tokenizer request, model load, calibration episode, formal episode, or
model prompt preceded this gate. The next production attempt must begin from a
clean committed source inventory and must still pass its own append-only
preflight, image build, and 30-task-by-two-model gold qualification before
calibration is allowed to start.

## Entry 022 — final adversarial accounting and watchdog gate passed

- Date: 2026-07-20, Asia/Taipei
- Phase: frozen-instrument release gate
- Status: **ready for a fresh production attempt**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

A fresh-context review after Entry 021 found one remaining physical-cost
omission: policy-failure recovery rebuilds a fresh container and replays its
admitted action history, including in the Raw loop. It also found three
watchdog races: post-EOF writes could delay overflow handling, a local
controller kill could appear as model exit `-9`, and an exited-container
`SizeRw` race could change `container_terminated` into an infrastructure
failure. The new writable-layer policy kind also required end-to-end typed
propagation. No model-dependent signal informed any finding.

ADRs 032 and 033 record the corrections. Safety-recovery and checkpoint
replays now share an exact typed counter and the natural four-action triangular
cap of six. The conservative formal envelope is 720 replayed and 1,500 total
physical actions; sealed topology verification enforces the tighter reachable
maximum of 600 replayed and 1,380 physical actions. Raw and Engineered costs
therefore include every deterministic Bash execution, while Direct and
independent sampling must report zero replay.

The final superseding identities are controller
`interactive-controller-v6-recovery-replay-accounting`, campaign ledger and
campaign evidence v6, episode journal and execution envelope v4, calibration
journal and evidence v5, execution-envelope set v3, study evidence v6,
analysis v5, production runner v4, and schedule
`sha256:68325d5cb1edb7a0f01a338aa05cbfc92bd3c13381bb5c47fe3cf53a4fe27129`.

The complete repository gate then passed **710 tests** with one declared skip,
Python byte-compilation, all configuration validations, and the
`git diff --check` whitespace gate. Independent focused runs passed 205
integrated tests, 179 accounting
tests, 142 adversarial verifier tests, and 38 Docker-executor tests. No
tokenizer request, model load, calibration row, formal row, or model prompt was
created before this gate.

## Entry 023 — Docker wake race isolated before image mutation

- Date: 2026-07-20, Asia/Taipei
- Phase: production host admission, attempts 4 and 5
- Status: **safety stops; stable admission protocol established**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Attempts 4 and 5 each passed repository preflight but stopped at full host
admission. Waking Docker Desktop asynchronously restarted the same two known
AgentGPT containers; one admission also observed VM pressure level `2`. The
controller refused image build and created only its append-only intervention,
preflight, and source-inventory records.

The orchestrator was narrowed to the two previously inventoried exact
container IDs and compose identities. It stops only those IDs when they are
running, changes no restart policy, rejects every unknown running container,
and requires six consecutive normal-pressure, empty-container, empty-Ollama
samples five seconds apart before handing control to production. This bounded
stabilization allowed the next attempt to pass full admission. No image,
tokenizer request, model load, task action, calibration row, formal row, or
model prompt was created in attempts 4 or 5.

## Entry 024 — Docker iidfile protocol amended before scoring

- Date: 2026-07-20, Asia/Taipei
- Phase: production image build, attempts 6 and 7
- Status: **reproducible infrastructure stop; v3 correction under verification**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Attempts 6 and 7 each passed the stable host admission and completed the
pinned cached fs1 Docker build. Both then stopped before image inspection or
an image event: the private v2 manifest retained only its plan header and the
71-byte full image-ID file. Repetition under normal pressure proved this was a
deterministic compatibility failure rather than host pressure.

An isolated no-network cached build showed that Docker CLI 27.3.1 removes the
precreated mode-`0600` iidfile and creates a new mode-`0644` inode; the held
reservation becomes unlinked. The pinned Docker source confirms the same
remove-then-write behavior. ADR 034 replaces the impossible same-inode rule
with a private-parent-anchored remove/recreate protocol, advances image plan
and manifest schemas to v3, and binds the protocol and `0644` to `0600` mode
transition into the plan digest. Symlink, FIFO, directory, hard-link, owner,
mode, size, parent, path, and content drift remain fail-closed.

The failed attempt directories remain unedited diagnostic artifacts. No
tokenizer request, Ollama listener, resident model, qualification task,
calibration row, formal row, or model prompt existed in either attempt, so the
amendment could not use model outcomes. A new production root is required;
neither v2 manifest is eligible for resume.

## Entry 025 — iidfile v3 repository gate passed

- Date: 2026-07-20, Asia/Taipei
- Phase: corrected-instrument release gate
- Status: **ready for a fresh production attempt**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

The complete corrected repository passed **719 tests** with one declared skip,
Python byte-compilation, all configuration validations, and the sample summary
check. The focused image-build and v0.7 image-provenance suite passed 30 tests;
the image tests include Docker remove/recreate, FIFO nonblocking refusal,
symlink and hard-link refusal, exact mode and size gates, parent and output
replacement, torn payload, same-inode concurrent mutation, and redacted
operating-system failures. `git diff --check` also passed.

This gate still contains no model outcome: no tokenizer request, Ollama model
load, calibration row, confirmatory row, or model prompt has occurred. The next
permitted action is a clean committed v3 production attempt under the same
stable-normal host admission protocol.

## Entry 026 — admission handoff race remained fail-closed

- Date: 2026-07-20, Asia/Taipei
- Phase: production host admission, attempt 8
- Status: **safety stop; continuous Docker-wake supervision required**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Attempt 8 began from clean pushed commit `a9a950d` after six consecutive
five-second samples with pressure level `1`, zero running containers, and no
Ollama listener. Its persisted preflight was allowed with pressure `1`, free
memory `51%`, and 45,641,248,768 free disk bytes. The source inventory also
sealed the expected commit before the managed empty Ollama server launched.

Full host admission nevertheless denied the attempt before Docker identity
was written. The denied sample is intentionally not persisted, so its precise
reason cannot be reconstructed from the artifact. Immediate post-failure
inventory found the same two exact AgentGPT containers running again, which is
consistent with Docker Desktop waking them during the narrow handoff from the
external stability window to the production collector. Both exact identities
were re-inspected and stopped without changing restart policies.

The next operational attempt keeps the Docker daemon actively supervised only
until production durably writes `docker-identity.json`. During that interval,
the supervisor may stop only those two exact pre-inventoried identities and
must refuse any unknown running container; it exits before image build can
progress toward benchmark-owned containers. This changes no task, arm, model,
prompt, controller, evaluator, or scoring rule. Attempt 8 created no image
manifest, tokenizer request, resident model, qualification row, calibration
row, formal row, or model prompt.

## Entry 027 — polling supervisor lost the admission race

- Date: 2026-07-20, Asia/Taipei
- Phase: production host admission, attempt 9
- Status: **safety stop; no-scoring keep-awake method selected**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

After a controlled Docker Desktop restart with the same 4-CPU, 2,048-MiB
settings, host pressure eventually returned to `1`. Attempt 9 began only after
six consecutive five-second normal samples, zero running containers, no
Ollama listener, and a further five-second high-frequency handoff window.

Production full admission still denied before writing Docker identity. The
250-millisecond external supervisor then observed and stopped the same two
exact AgentGPT identities, but only after the production collector had already
completed its admission sample. This proves polling cannot close the wake
race; shortening it further would add load without creating synchronization.

The next operational attempt instead holds one read-only `docker events`
stream from before the stability window until production durably writes
`docker-identity.json`. Its only purpose is to prevent Docker Resource Saver
from sleeping and re-waking the daemon during handoff. The stream mutates no
container, image, volume, network, setting, or restart policy and terminates
before image build proceeds. Exact-container stopping and unknown-container
refusal remain unchanged. Attempt 9 created no Docker manifest, tokenizer
request, resident model, qualification row, calibration row, formal row, or
model prompt.

## Entry 028 — read-only event stream did not close the handoff race

- Date: 2026-07-20, Asia/Taipei
- Phase: production host admission, attempt 10 and pre-scoring amendment
- Status: **safety stop; bounded in-runner stabilization frozen**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

Attempt 10 held a persistent read-only `docker events` stream while six
consecutive samples showed pressure level `1`, zero running containers, and no
Ollama listener. Production still denied its one-shot full admission before
writing Docker identity; the external exact-ID steward observed and stopped
the same two pre-inventoried AgentGPT containers only afterward. Keeping the
daemon awake therefore did not synchronize the production sample with
container reconciliation.

The failed artifact contains only its intervention, preflight, and source
inventory evidence. It has no image manifest, tokenizer request, resident
model, qualification row, calibration row, formal row, or model prompt. The
attempt remains append-only and is not eligible for resume.

ADR 035 advances the production runner to
`intercode-v0.7-production-runner-v5-admission-stabilization`. Before image
planning, production now journals a bounded read-only stabilization. The
expected host resources remain empty. A denial is waitable only when its sole
reason is `RUNNING_CONTAINERS` and its nonempty observed IDs are a subset of
the zero-or-exactly-two full, sorted, unique IDs configured for the external
steward. Production does not stop or otherwise mutate those containers. All
other denials stop and seal immediately; success requires two fully allowed
samples 30 seconds apart within 600 seconds.

Every raw sample and derived decision is appended to a fresh `O_EXCL`,
owner-mode-`0600`, identity-bound, hash-chained journal. The terminal journal
is sealed and reverified, and its accepted sample is reproduced before image
mutation. This amendment changes no task, model, arm, prompt, controller,
evaluator, budget, or scoring rule and was frozen with zero model outcomes.

## Entry 029 — admission stabilization release gate passed

- Date: 2026-07-20, Asia/Taipei
- Phase: corrected-instrument release gate
- Status: **ready for fresh production attempt 11**
- Measured model prompts: **0**
- Calibration episodes: **0/8**
- Confirmatory episodes: **0/240**
- Performance result: **none**
- Uplift claim: **not permitted**

The current source, adversarial tests, ADR 035, design amendment, and
append-only attempt history passed the complete repository gate. `make check`
exited zero after **734 tests** in 141.979 seconds, with one declared skip and
no failure or error. Python byte-compilation, all three configuration
validations, report rendering, and the sample summary check also passed.

The focused production module passed 19 tests covering exact stewarded-ID
handling, unknown and mixed container refusal, hard second reasons, clean
sample streak resets, the exact 600-second boundary, managed-runtime liveness
on both sides of collection, private-journal identity replacement, same-inode
tail mutation, exact domain schemas, impossible post-pair-denial continuation,
sealed-before-build ordering, managed-runtime closure, and canonical CLI pair
binding. An independent fresh-context review found no remaining blocker.

This gate contains no model output. Attempt 11 must use a fresh artifact root
and the committed source identity. Its external steward may stop only the two
pre-inventoried full container IDs, records each successful stop as an
`operational_reconciliation`, refuses every unknown running ID, and exits
before Docker image work begins.
