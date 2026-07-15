# EdgeLoopBench v0.6 InterCode-Bash causal loop study

- Status: **approved for implementation; measured scoring blocked by qualification gates**
- Design date: **2026-07-15**
- External benchmark source: **InterCode-Bash / NL2Bash**
- Primary local model: **Qwen3.5 4B**
- Gated replication model: **Phi-4-mini**

### Pre-calibration amendment — 2026-07-15

Before any model calibration or confirmatory output was generated, the prompt
boundary was made executable rather than aspirational. The controller now
stores an alternating typed `user`/`assistant` transcript, renders it with one
of two restricted and hashed raw-prompt profiles, and obtains an exact offline
token count before deciding whether to issue a request. This replaces the
implementation draft's flattened `Assistant response:` text transcript; it
does not change any observed result because measured scoring had not started.

The same amendment binds Ollama `raw = true`, `think = false`, and
`truncate = false`, so the bytes counted offline are the bytes handed to the
runtime tokenizer. A preflight/backend token-count disagreement is an
infrastructure-invalid four-arm block, never a model failure or an after-the-
fact budget overrun.

The same pre-outcome amendment adds a 65,536-byte rendered-prompt ceiling
before tokenization. Crossing it is a typed budget stop on the previously
selected checkpoint, not an infrastructure failure and not a sent prompt. The
pinned tokenizer's in-memory LRU retains at most 128 prompts and 8 MiB of
prompt bytes; cache hits still count the complete logical tokens again.

The initial all-qualified-task, three-seed, `K = 10` matrix was also rejected
before model output as infeasible on the declared 16 GB host: even 160 qualified
tasks would permit 29,760 generation requests across two models before
requeues. Qualification still covers all 200 source rows, but effectiveness is
estimated from an outcome-independent, 50-task stratified hash-randomized sample
of the qualified population. Both models use confirmatory seeds `[11, 29]` and
`K = 6`. This is an explicit local feasibility amendment, not a claim to have
run full-population InterCode performance or the published ten-attempt cap.

## 1. Research question

Under a fixed logical-token and action budget, and when a benchmark-native
evaluator can score every attempt, how much objective success comes from:

1. additional independent samples;
2. stateful execution feedback; and
3. a frozen engineered loop layered on the same feedback?

The primary loop-engineering contrast is `engineered_loop - raw_feedback_loop`.
The sampling and raw-feedback arms explain the mechanism; they prevent extra
test-time compute from being mislabeled as loop design.

Claude's official loop guidance defines loops as repeated work until a stop
condition and recommends explicit success criteria, quantitative verification,
bounded usage, independent review, and pilot-first rollout. v0.6 operationalizes
that narrow control-system idea: the user supplies the task once, automatic
follow-ups are counted, the verifier supplies the stop signal, and the
controller has a fixed turn/token cap. The guidance is not itself an evaluation
topology and reports no performance uplift, so it does not define an
"official" benchmark arm. Rollback, checkpoint selection, and the no-progress
guard below are EdgeLoop-owned treatment components.

This is an interactive shell-command study of a turn/goal-conditioned loop. It
is not a SWE-bench score, a repository-level software-engineering result, a
reproduction of Claude Code `/goal`, a test of time-based or proactive loops, or
evidence about unobserved human prompt counts.

## 2. Why InterCode and why Bash only

InterCode was published at NeurIPS 2023 specifically to evaluate interactive
coding with execution feedback. Its Single Turn and Try Again comparison is a
closer fit to this question than the repository's small synthetic repair suites.

The source boundary is the official GitHub repository at commit
`c3e46d827cfc9d4c704ec078f7abf9f41e3191d8`. The four committed NL2Bash files
contain 200 rows across four filesystem strata:

| Source file | Rows | SHA-256 |
| --- | ---: | --- |
| `nl2bash_fs_1.json` | 60 | `60f88e1aacc7ebba535093f9890c5c33203f4e5f32958e0e94fbe90ec4f01c82` |
| `nl2bash_fs_2.json` | 53 | `8f4ce24e535fab782fda607e37db2ae1d6c5f99993c638d1ac0a7e0b542f633e` |
| `nl2bash_fs_3.json` | 60 | `a2d4ec8bc7ad69a4e2fb3eb84033994cf65ee9cfb355e3e63099df67a339b2e1` |
| `nl2bash_fs_4.json` | 27 | `ce41b89450f87765a02a51df259ca0c1762e8249185c022adb089147e2c16200` |

The paper's Python count cannot be reconstructed as a canonical split. It
reports 117 MBPP tasks, while the pinned data file contains all 974 tasks and no
split field. Published GPT-3.5 and GPT-4 result artifacts contain different row
sets. Python is therefore excluded from v0.6 rather than silently inventing an
"InterCode-317" suite.

The separate 24-row `test_queries.json` file has SHA-256
`d24a7a1eb61c2621c48a42f942d08f6aa02066630ab49c2a07de2530a226e0aa`.
Its queries and `(query, gold)` pairs have zero exact overlap with the 200-row
population. It is used only for integration and model calibration and is never
pooled with confirmatory results.

InterCode code is MIT-licensed. The NL2Bash dataset is separately MIT-licensed;
the surrounding NL2Bash source repository is GPL-3.0. Attribution and the
upstream license texts must ship with any vendored benchmark material.

## 3. Offline qualification defines the scored population

The upstream 200 rows are the source population, not automatically the scored
population. Some rows require DNS or external networking, some setup scripts
are not fail-fast, and the upstream Docker tags are mutable. EdgeLoopBench does
not add network-dependent tasks.

Before any model sees a confirmatory task, the qualification command must:

1. assign stable IDs `bash-fs{1..4}-{zero-based-row:03d}`;
2. build four native `linux/arm64` images from the pinned setup scripts and
   immutable base-image digests;
3. run setup under a fail-fast wrapper and verify a frozen required-fixture
   inventory; every documented setup correction or exception is source-hashed;
4. run every gold command twice, each time in a new `--network none` container;
5. require a successful frozen exit-status policy, official reward `1.0`, strict
   gold-versus-gold success, and identical initial-state, normalized stdout/
   stderr, and declared observable-filesystem digests across both replays;
6. exclude network-requiring, nondeterministic, unsupported, setup-invalid, or
   evaluator-invalid rows by a machine-readable reason code;
7. commit the ordered inclusion/exclusion manifest, image digests, source-file
   hashes, evaluator revision, and suite SHA-256 before model scoring begins.

Qualification evidence is accepted only from the trusted collector. The 400
logical `(task_id, replay_index)` units each have a deterministic key and a
separate mode-`0600`, hash-chained attempt journal. For every unit the trusted
adapter durably appends start, container-acquire intent/completion, result,
container-release intent/completion, unit-completion, and a terminal seal.
Each acquire is bound to a source-owned opaque task capability, the exact
image/evaluator/state-normalizer pins, and globally unique replay and container
lifecycle identities. The second replay cannot reuse either identity from the
first.

A sealed completed unit is reused after a reboot and never generated again. If
an attempt stops before a durable result, exact resource reconciliation seals
that attempt as aborted and may create one replacement generation for only
that logical unit. If the result is durable but cleanup is incomplete, recovery
may retain the result only after exact presence-or-absence reconciliation and
a durable cleanup receipt. A second pre-result interruption, an ownership or
profile mismatch, an unquarantined partial record, or ambiguous cleanup leaves
qualification incomplete; it is not an exclusion record and cannot be hidden
by another retry. Typed `infrastructure_valid=false` results are completed
observations and are not retried.

Aggregation requires exactly one sealed completed generation for all 400 keys,
every earlier generation sealed aborted, and canonical ordering by task ID and
replay index. Its private root binds the ordered unit terminal roots and result
digests. The public projection exposes only that aggregate root and an
aggregate recovery count, never a per-unit digest or recovery trace.

The raw result/lifecycle submission API is not public. A construction-sealed,
module-private collector capability is issued only to the Docker qualification
adapter; generic controllers, CLIs, model-facing code, and callers that merely
possess an `InterCodeSource` cannot mint replay evidence or reload arbitrary
JSONL as trusted evidence. Unit tests use that private capability with synthetic
facts only to exercise state-machine logic. Such fixtures are not measured
qualification evidence. Formal scoring remains blocked until the Docker adapter
owns the capability and derives container identities, results, and cleanup from
the inspected isolated lifecycle.

Static evaluator-side exclusions are frozen in the gold-free
`docs/audits/intercode-bash-static-exclusions-v1.json` artifact. Its exact
SHA-256 binds the complete task-to-reason map and source-corpus identity. Both
the collector start record and the public qualification manifest carry that
digest, so changing one task or reason requires a new audited artifact and
manifest identity.

Private qualification serialization is the complete set of sealed unit and
aborted-attempt journals plus the sealed aggregate index. It retains per-replay
state/output digests and hashed lifecycle identities on the trusted host only.
The separate public manifest projection contains ordered task IDs, strata,
inclusion bits, reason codes, counts, frozen pins, the aggregate sealed root,
aggregate recovery count, and the static-audit digest. It never serializes a
per-replay digest, lifecycle/container identity, private capability, gold
command, output, diagnostic, recovery locator, or path.

There are no outcome-dependent model exclusions. A row that passes gold replay
but defeats a model is a valid failure. The resulting suite is named
`InterCode-Bash-qualified@c3e46d8`; it is not called the unqualified full 200.
Filesystem-stratum results are mandatory. Qualification summaries weight every
qualified task once; effectiveness summaries use the separately declared
stratified-sample weights below.

Qualification must retain at least 160 tasks and these per-stratum floors:
48/60 for fs1, 42/53 for fs2, 48/60 for fs3, and 21/27 for fs4. Falling below
any floor aborts v0.6 confirmatory scoring; it does not authorize replacing or
cherry-picking rows. Before model scoring, the frozen qualified count is used to
publish paired-McNemar sensitivity across a declared discordance grid and the
task-cluster bootstrap's attainable precision. Failure to resolve a small true
effect remains an inconclusive outcome, not permission to weaken the endpoint.

After qualification and before any model calibration output, select exactly 50
confirmatory tasks with fixed stratum quotas `fs1/fs2/fs3/fs4 = 15/13/15/7`.
Within each stratum, order included task IDs by SHA-256 over the UTF-8 salt
`edgeloopbench-v0.6-intercode-confirmatory-50-v1`, a NUL byte, the
qualification selection-frame SHA-256, another NUL byte, and task ID; take the
first quota and
publish the complete ordered sample manifest. This is a reproducible
hash-randomized design under the stated SHA-256 pseudorandom-ranking assumption,
not an independently witnessed randomness-beacon draw. The salt and algorithm
are committed before qualification; task text is never used as a ranking
covariate, while its already-frozen population digest remains bound into the
selection frame. Model outcomes cannot affect inclusion. The first `4/3/3/2`
sampled tasks by stratum form a nested
12-task post-hoc trajectory diagnostic subset. Qualification evidence still
covers the complete 200-row source population; the 50-task sample is the
performance instrument.

The selection frame hashes only the frozen source/static-audit identities and
the ordered public inclusion/reason records. It deliberately excludes replay
lifecycle IDs and the private evidence root, so rerunning identical
qualification evidence cannot act as a controllable random seed. The published
sample manifest still binds the complete qualified-suite SHA-256 separately.

Measured execution is sequential. Direct uses one fresh agent container per
episode. Independent uses one fresh agent container per executable attempt.
Raw and Engineered each use one fresh agent container whose state persists only
within that episode. Every attempt-level reward and every strict audit uses two
fresh, model-inaccessible evaluation replicas: one created from the exact
candidate checkpoint image and one clean gold replay created from the scoped
original image. Both are destroyed, with durable release completion, before an
evaluation result capability is issued. Docker-side evidence collection uses
only the pinned absolute tools and root-owned collector. The final comparison
is a bounded, path-free pure function in the trusted host controller; it does
not execute a command, open a filesystem path, or resolve a symlink. Its source
and policy digests are frozen as the evaluator identity. The unused
`Dockerfile.evaluator` scaffold is not a measured-run input and conveys no
scoring authority.

All containers have unique names, no network, no host project mount, no Docker
socket, no persistent writable volume, bounded memory/CPU/PIDs, dropped
capabilities, and `no-new-privileges`. Every remaining agent container is
destroyed after the episode. Git reset inside a reused container is not an
accepted reset.

## 4. Three information channels

The adapter keeps these channels separate:

| Channel | Consumer | Allowed content |
| --- | --- | --- |
| `agent_observation` | model | bounded normalized execution output, or the frozen parser retry string |
| `controller_stop_signal` | controller; model only where declared by the arm | scalar InterCode reward and `official_success` bit |
| `objective_evaluator_output` | final analysis only | withheld evaluator result and integrity diagnostics |

Gold commands, evaluator filesystem paths, reward components, evaluation
stdout, and filesystem-diff details never enter a model request. Upstream
trajectory serialization is not used because it records gold and evaluator
internals.

The paper's Try Again prompt exposes output plus a scalar reward derived from
gold. v0.6 preserves that fact instead of presenting it as ordinary shell
feedback. Therefore any observed benefit is conditional on access to an
equivalent attempt-level verifier. It is not evidence that an unaided loop can
recognize task completion.

The benchmark-compatible endpoint is `official_success = (reward == 1.0)`.
Because the pinned upstream Bash reward has known weak equivalence checks, a
separately frozen strict evaluator also runs after controller stop. Online, it
evaluates only the final selected checkpoint and returns nothing to the model or
controller. After the complete run is sealed, retained checkpoints may be
strictly evaluated for an explicitly oracle-labeled diagnostic. The evaluator
compares each requested checkpoint with a clean gold replay over the qualified
observable surface. `strict_success` on the final checkpoint is the correctness
endpoint for EdgeLoop claims; official success is reported alongside it. A
positive claim requires selected-checkpoint official/strict disagreement in no
more than 1.0% of valid Qwen episodes and an absolute disagreement-rate gap of
no more than 1.0 percentage point between any two arms. Otherwise the controller
verifier is declared misaligned and the result remains descriptive.

The official and strict filesystem representations are deliberately separate.
The official calculation consumes trusted, evaluator-side equivalents of the
pinned upstream parser's whitespace-tokenized `(path, status)` change units and
SHA-256 identities of the exact `md5sum`/`md5deep` output it would compare. It
preserves arbitrary candidate status tokens in the filesystem-diff set,
upstream's `A`/`??`/`C` content filter, its omission of every other status from
content checks, and its single-unit weighting for an untracked directory. A
candidate hash is required only for an `A`/`??`/`C` key also present in the
qualified gold surface; malformed or unsafe candidate-side tokenization gets a
frozen synthetic score `0.0`, strict failure, and remains in the effectiveness
denominator. It is never reclassified as infrastructure-invalid. Gold-side
unrepresentability remains `evaluator_invalid` during qualification. Mode,
owner, symlink target, and hard-link topology are never substituted into the
official scalar; they belong to the strict normalized snapshot only.

The agent image retains the upstream-style initial `/.git` repository and
`/.gitignore`, but both are root-owned and read-only to UID 65532. They encode
only the public initial filesystem and contain no gold, evaluator code, private
task reference, or host path. A build-time permission audit proves they cannot
be replaced or mutated by the agent. Official collection uses absolute
`/usr/bin/git`, fixed arguments, `GIT_OPTIONAL_LOCKS=0`, an explicit safe
directory, and a controller-owned environment. The logical-state collector
does not hash or serialize Git internals; the frozen image identity binds that
immutable baseline separately.

Evaluator-private attempt objects are non-dataclass, non-pickleable capability
objects. Qualification records and their per-task evidence digests remain on
the trusted host only and are absent from agent images, prompts, observations,
and generic event serializers.

## 5. Four causal arms

Let `K` be the frozen maximum attempts. All four arms use the same action
grammar, model, initial task state, cumulative budget ceilings, and candidate
seed schedule.

### A. `direct`

Send the shared initial request once, execute one Bash command, obtain the stop
signal, select that checkpoint, and run final evaluation. Unused budget remains
unused.

### B. `independent_verified_sampling`

For each attempt, create a clean environment and a fresh model context, send
the same initial request bytes, and use the next candidate seed. No earlier
command, output, score, or failure bit enters the next request. The controller
may stop on the first official success because the same attempt-level verifier
is available to all multi-attempt arms. If no attempt succeeds, the last
checkpoint is the predeclared final selection.

This arm is evaluator-guided test-time sampling, not a reward-blind deployable
selector. Post-hoc `any_strict_success@K` is labeled an oracle diagnostic and is
never used during execution.

### C. `raw_feedback_loop`

Use one persistent task environment and one continuous model transcript. After
each failed attempt, append only the frozen InterCode-style observation packet:

```text
Output: <bounded command stdout, or No output>
Reward: <scalar reward>
```

There is no rollback, deterministic diagnosis, action deduplication, or
controller summary. Stop on official success or budget exhaustion. This is the
closest v0.6 arm to the published Try Again topology, but the matched first
request and EdgeLoop isolation/evaluator rules make it an adaptation rather
than a leaderboard reproduction.

### D. `engineered_loop`

Use the same persistent state and expose the same output and scalar reward as
Raw. Only after the shared first failure, add a deterministic packet containing:

- attempt and remaining model/action/token budgets;
- last command and bounded stdout;
- admissibility, state-change digest, score, best score, and score delta;
- normalized repeated-action and repeated-state signature counts;
- whether a rollback to the highest-scoring checkpoint occurred;
- one frozen instruction to form a new failure hypothesis and issue a
  meaningfully different command.

Checkpoint selection and rollback use the declared benchmark-native score; no
private reward component is available. A lower score restores the highest-score
checkpoint. Equal scores retain the current state. Two repeated no-progress
signatures force an exploration instruction; a third terminates the episode.
The first official success always stops and is retained.

A checkpoint record is the frozen tuple of a complete container writable-layer
snapshot, canonical working directory, logical state digest, runtime-profile
digest, attempt index, executed-action bytes, exit and admissibility status,
private full normalized stdout/stderr bytes and SHA-256 digests, and bounded
agent-observation output. The Docker image ID is the snapshot identity; it is
not the logical state digest. An immutable evaluation record is created
separately and binds the provenance, official score, and success bit to the
checkpoint reference and logical state digest. Provenance distinguishes an
actual evaluator result from the preregistered candidate-surface zero default;
the latter binds a policy revision and never fabricates an evaluator identity.
This separation prevents an evaluator result from being smuggled into or
changing checkpoint identity.

The opaque storage handle, snapshot image ID, full private output, evaluator
record, and host/container paths never enter a prompt or publishable event.
Restore creates a new container from the exact snapshot image, verifies its
frozen runtime profile, restores the canonical working directory, recollects
the logical state, and requires the digest to equal the checkpoint record. It
never rewrites the transcript or presents stored output as a new execution.
After rollback, the next engineered packet contains the actual regressed
bounded action/output plus separate restored-checkpoint metadata. If the arm
stops after no progress, it selects the checkpoint with the highest bound
official evaluation and uses that checkpoint's associated private action,
output, and state for strict final evaluation. Official success comes from the
checkpoint-bound evaluation record and is not recomputed or fabricated during
restore.

Ties select the latest checkpoint with the highest bound score. The immutable
checkpoint and evaluation records remain associated but non-interchangeable;
restoring filesystem state does not recreate stdout and is never logged as a
model-visible execution. A missing checkpoint, including an episode containing
only parser failures, has `official_success = false` and
`strict_success = false`.

Every model-issued command runs under a post-action process-delta audit. A
background or residual process makes the candidate inadmissible; the controller
terminates the contaminated container and restores from the preceding full
snapshot in a new container. PID, IPC, and other ephemeral namespaces are
recreated, so off-snapshot runtime state cannot survive rollback.

Working-directory persistence is a source-declared safe adaptation of pinned
InterCode `BashEnv`, which keeps `self.workdir`, resolves `.` and `..` through
`simplify_path`, executes a rewritten `cd` from `/`, and updates the stored
directory only on exit status zero. The initial directory is `/`. EdgeLoop's
frozen action wrapper accepts exactly `^cd ([A-Za-z0-9._/-]+)$`, rejects a path
that begins with `-`, canonicalizes the target with the pinned lexical
algorithm, and executes `cd -- <absolute-target>` as one container-side action.
Shell expansions, options, quoting, multiple operands, and compound commands
are typed `invalid_text` model-policy failures and are never executed. All
other actions execute in the last attested directory; a token such as `cdrom`
is an ordinary command. This deliberately does not reproduce upstream's unsafe
string interpolation or its ambiguous `action.startswith("cd")` handling.

This arm is a package treatment. Without later component ablations, any uplift
cannot be attributed specifically to rollback, diagnosis formatting, or the
no-progress guard.

## 6. First-call identity and stochastic schedule

For a given `(task, model, replicate)`, candidate 1 must be byte-identical across
all arms:

- identical rendered prompt bytes and prompt SHA-256;
- identical sampling seed, temperature, context limit, and output cap;
- identical model/runtime state policy;
- identical initial environment digest and action parser.

Strategy-specific text begins only after candidate 1 fails. Attempts `j > 1`
use one deterministic `candidate_seed(replicate_seed, j)` schedule shared by
Independent, Raw, and Engineered. Confirmatory replicate seeds are
`[11, 29]`. Temperature is nonzero; calibration must verify that the pinned
runtime honors seeds and produces an effective unique-sample count. Repeated
prefixes count again as logical prompt tokens even when the backend reuses a
physical cache.

The common initial prompt, retry templates, parser, stdout normalization and
truncation, candidate-seed function, `K = 6`, and all numeric budgets are
hashed in a pre-calibration gate manifest. `B*` is computed from the frozen
templates, context policy, output cap, and observation cap before any model
calibration output exists; it is not selected from arm outcomes. Calibration
may reject a model or the entire design, but it cannot tune these confirmatory
parameters. No confirmatory model output may be opened until the same identities
are copied into a committed confirmatory manifest.

The final manifest separately pins model ID, immutable model revision and
artifact SHA-256, tokenizer and chat-template revisions, weight quantization,
KV-cache quantization, effective context, runtime version/artifact, and every
runtime flag. Weight and KV-cache quantization are never collapsed into one
field.

Controller history is an alternating typed transcript. Candidate output is an
`assistant` turn; the frozen observation, scalar reward, and any deterministic
controller packet form the following `user` turn. The model output is not
flattened into a second synthetic user message. Direct and every Independent
candidate contain only the common initial user turn. Raw and Engineered retain
the same typed history after candidate 1. Unsupported roles, tools, images,
system messages, or non-alternating sequences are rejected rather than
silently rendered.

The Qwen3.5 4B profile is the restricted no-tool/no-image subset of Ollama
v0.31.1's `qwen3.5` renderer: ChatML user/assistant turns followed by the
official explicit empty `<think>...</think>` no-think prefill. Its Ollama
renderer source SHA-256 is
`6ca6abea759548962ea23189691c5def15cb86704c114f17182784fd159b4872`.
The Phi-4-mini profile reduces its pinned 655-byte template layer
`813f53fdc6e58d35bb1c3853c93266380e9ca918a993e8eab193e8ede5d3a603`
to alternating `<|user|>`, `<|assistant|>`, and `<|end|>` turns. Each complete
profile definition has its own canonical SHA-256 in the run manifest.
The canonical definition includes the exact turn template, generation suffix,
content-normalization rule, and assistant reasoning-stripping rule; the pinned
controller source revision binds the generic rendering algorithm.

## 7. Attempt and parser accounting

One generated response consumes one attempt, one model call, and its complete
logical prompt/completion tokens. If parsing fails, it consumes no model-issued
environment action and no evaluator call. The controller records
`official_success = false` and a synthetic parser-default score of `0.0`, tagged
as non-evaluator-derived. Direct stops with no checkpoint. Independent advances
to a fresh context and seed without receiving the failure. Raw receives the
frozen parser retry string plus the tagged zero score; Engineered receives the
same facts in its deterministic packet. The first failed response is still
identical across arms.

An executable command consumes one model-issued environment action and one
attempt-level evaluator call even when its exit status is nonzero. Parser,
timeout, reserved-action, output-truncation, and admissibility rules are frozen
before calibration.

A model-issued timeout, private-output overflow, unsafe output-text violation,
residual/background process, declared filesystem byte/file/depth overflow,
unsupported `cd` form, or termination of its own task container is a valid
model policy failure and remains in the effectiveness denominator. The
contaminated container is destroyed and the candidate is inadmissible; it is
not re-labeled as infrastructure merely because cleanup was required. An
unexpectedly stopped container is attributed to the action only after the
Docker boundary re-inspects the exact immutable container identity and full
frozen security profile. The loop may receive only the corresponding frozen
policy observation and must continue from a clean prior checkpoint where its
arm permits continuation. Failure to prove exact run-scoped cleanup, exact
container state, a Docker audit result, or a host executor result is
infrastructure-invalid.

An inadmissible policy failure consumes one attempt, model call, logical token
usage, and model-issued environment action. It consumes no checkpoint creation
and no evaluator call because there is no candidate state to score. The adapter
must first recreate the initial state or restore the preceding stable
checkpoint, then return that restored state digest with `state_changed=false`.
It also returns a recovery-completed bit and evidence digest bound to the
destroy/create-or-restore lifecycle. The controller records a typed synthetic
`0.0` action-policy failure, keeps the previous selected checkpoint, and may
send only the frozen policy observation to Raw or Engineered. This mandatory
common safety recovery has its own counter and ceiling; it is not counted as
the Engineered arm's treatment rollback.

Checkpoint creation and restore are deterministic controller-maintenance
operations, not model-issued environment actions or evaluator calls. Every
executed candidate is checkpointed so selected-policy and sealed post-hoc
audits remain possible; Engineered additionally uses restore online. Creates
and treatment restores have separate counters and manifest ceilings from
common safety recoveries, and their wall/disk cost is recorded. They do not
reduce the shared candidate-attempt ceiling. Direct simply underuses the shared
create ceiling. No inference or model-generated summary is hidden inside a
maintenance operation.

For an admissible action, the trusted adapter first collects the logical state
with the frozen gold-free collector, then pauses the exact container, commits
its full writable layer with Docker's commit-time pause disabled because the
container is already frozen, inspects and size-checks the resulting immutable
image, and issues an opaque, one-shot attestation held in the Docker boundary's
private registry. The durable plan already binds the exact source-container
digest. The checkpoint store, not its caller, consumes the token from that same
issuer and verifies the deterministic private tag, full image ID, exact source
container, source image, inspected parent, run identity, size, and metadata
digest. A free attestation constructor or caller-supplied fact object is not an
admitted completion path. Only then may the resource acquisition complete. The
trusted adapter receives a non-serializable private completion capability with
the verified snapshot image ID; the controller receives only its opaque public
checkpoint projection. The tag alias is removed
under its own durable intent/completion before evaluation, while the full image
ID remains owned for restore and scoring. Any missing or ambiguous step
invalidates the complete four-arm block.

Each episode scope has a mode-`0600`, hash-chained private resource ledger with
intent/completion records for container and image ownership, plus a separate
private checkpoint/evaluation journal. The journal is bound to its exclusively
created inode and owner-controlled parent; later appends never use `O_CREAT`
and reject mode, owner, link-count, parent, size, or inode drift. It records the
plan, attestation consumption, alias release, evaluation intents/results, and a
durable barrier. The evaluation-plan digest, candidate-surface default-policy
digest, and whether every checkpoint requires post-hoc strict scoring are fixed
in the episode scope before the first action; callers cannot substitute a
different zero-score policy or choose an empty requirement at cleanup time.
Before the first deletion, the store proves there are no pending transitions,
the journal's exact terminal record count and chain root equal the private
barrier permit, and the ledger's exact owned-resource set equals the complete
reverse-order cleanup plan. The same verified journal inode remains locked
through cleanup and terminal sealing. Ledger and semantic-journal creation both
include file and parent-directory `fsync`. The checkpoint store
outlives the interactive environment: strict and post-hoc evaluation finish
before exact cleanup and terminal sealing. Docker export, CRIU, and in-place Git reset are not checkpoint
implementations for this study.

The enclosing four-arm block journal durably records a deterministic
`scope_bootstrap_intent` before either per-scope file is created, followed by a
`scope_bootstrap_completed` event binding both genesis roots plus their original
file and parent identity digests. Creation order is resource ledger first,
semantic journal second. After a crash, neither file
means no scope resource exists; a ledger without a semantic journal is an
incomplete bootstrap that still requires ledger inspection; a semantic journal
without a ledger is an integrity failure; and any file missing or replaced
after bootstrap completion blocks automatic deletion, even if byte-identical
content was copied onto a different inode. This closes the otherwise
unobservable two-file bootstrap window.

Opaque checkpoint plans, completion objects, evaluator material, and
finalization permits are process-local and are never reconstructed after a
reboot. The old semantic journal may be reopened only for typed read-only
inspection. Even a durable ready-to-finalize barrier cannot authorize scoring
or normal cleanup in a new process. Instead the complete block is invalidated,
its old artifacts remain immutable evidence, exact resource recovery is
authorized by the separate block recovery journal below, and any retry starts
with fresh run/scope identities.

That mode-`0600` block-recovery journal binds the run, model, block, frozen
schedule, pre-calibration gate manifest, recovery-contract digest, and requeue
generation (`0` or `1`). Its externally retained file/parent inode anchor is
mandatory on reopen. It holds the durable block-invalidation authority and
exact reverse cleanup intent/completion records, while source ledgers remain
inode-anchored and read-only. Its cleanup plan is accepted only after the
separate typed source-ledger inspection proves that exact plan complete; the
journal cannot turn an arbitrary resource tuple into deletion authority. Every
frozen scope must recover before the block
can become terminally invalidated and sealed. If the recovery journal itself
ends in one unframed write, those bytes are first copied to an exact-mode,
hash-named quarantine and both file and directory are synced; only then may the
journal be truncated to its last validated record and append quarantine
evidence. Newline-terminated or earlier corruption is never repairable.

## 8. Calibration and model gates

Run the 24 disjoint quickstart tasks first. They qualify mechanics and
model-task fit; they do not estimate the confirmatory effect. Before the full
gate, an eight-task `2/2/2/2`-stratum pilot runs all four arms at `K = 2`, at
most 56 generation requests per model, to verify the complete lifecycle and
estimate wall time. Pilot outcomes cannot change prompts, thresholds, sample,
`K`, seeds, or budgets.

A model advances only when all frozen gates pass:

- a 96-call seed probe sends each of the 24 calibration tasks' byte-identical
  initial prompt under seeds `[11,29,47,83]`; at least 87/96 responses
  parse to one bounded Bash action;
- the first 72 of those same calls, covering all 24 tasks and seeds
  `[11,29,47]`, are executed as Direct episodes rather than generated again;
  strict
  success must be between 8/72 and 57/72 inclusive, avoiding a clear floor or
  ceiling;
- at least 80% of parsed commands from those 72 Direct episodes are admissible;
- at least 12/24 seed-probe tasks produce two or more distinct normalized parsed
  actions, and the sum of within-task unique parsed actions is at least 48/96;
- no evaluator leakage test, reset-isolation test, or accounting invariant
  fails;
- a 30-minute combined model-plus-Docker thermal qualification, capped at 64
  fixed load requests per model, passes;
- the `K = 2` pilot projects no more than 72 hours of active generation plus
  Docker work for the frozen confirmatory matrix. Failure aborts the design; it
  does not authorize opening outcomes and silently shrinking the sample.

All denominators, seed schedules, normalization rules, and the treatment/block
order are committed in the pre-calibration gate manifest. Infrastructure-
invalid calibration calls abort the gate rather than disappearing from a
denominator. Thresholds cannot change after calibration output is viewed.

Qwen3.5 4B is the primary model. Phi-4-mini receives the same calibration and
advances as a separately pinned replication only if it passes the gates. A
failed gate is reported as a model-task-fit or infrastructure exclusion, not as
evidence that loops do not work.

If both models qualify, calibration/pilot permits at most 304 generation
requests total and host load at most 128. These ceilings count every request,
including parser failures; qualification's 400 gold replays use no model
prompt.

## 9. Budgets, ordering, and host safety

Every arm receives the same ceiling for cumulative logical prompt tokens,
completion tokens, model calls, model-issued environment actions, evaluator
calls, and per-call context. `K` is 6; the study reports a bounded local
adaptation and does not claim the published Try Again ten-attempt cap.
Before a call, the controller renders the exact raw prompt and counts it with
`llama-tokenize` built from Ollama v0.31.1 commit
`710292ff4f191d8da9f6a4230804fbc693338d4a`, its pinned llama.cpp tag `b9840`
(full commit `8c146a8366304c871efc26057cc90370ccf58dad`), and Ollama's compatibility
patches. The helper loads the same GGUF in vocab-only mode and receives the
prompt on stdin with `--ids --no-escape --log-disable`; model-default special
tokens and special-token parsing remain enabled, matching llama-server's
completion path. Helper and GGUF paths are canonicalized after their SHA-256
checks, and their file identities are rechecked before every tokenization,
including cache hits.

A call whose prompt would cross `B*` is not issued; the episode stops on its
previously selected checkpoint. The per-call output cap is clamped to both the
remaining cumulative completion budget and the remaining per-call context.
Rejected preflights retain count, digest, artifact, and remaining-budget
evidence in the append-only journal, but do not count as model prompts or model
calls.
Ollama is called through `/api/generate` with the already rendered prompt,
`raw = true`, `think = false`, `truncate = false`, and `shift = false`. Any
mismatch between the pinned preflight count and Ollama's total logical
`prompt_eval_count` makes the complete four-arm block infrastructure-invalid.
The request also pins `keep_alive = -1`, the HTTP deadline, the JSON action
schema, every sampling/repetition penalty, batch/thread/GPU/mmap setting, and
all other v0.31.1 `api.Options` fields instead of inheriting Modelfile or server
defaults. The model name is obtained only from the frozen rendering profile;
the configuration API has no independent model-tag field. Host admission binds
that tag to the declared local manifest, GGUF digest, runtime binary digest,
runtime commit/version, and KV-cache environment before a request is allowed.
Success curves are evaluated at attempts 1, 2, 4, and 6. Token-budget curves
are secondary diagnostics; calls have unequal cost.

Before measured scoring, each calibration-qualified model must separately pass
a 30-minute sustained model-plus-Docker load with append-only pre/post samples
for AC status, low-power mode, memory pressure, swap, thermal state, model
residency, and running containers. Only one model is resident at a time. No
unrelated Docker container may be running. Admission/cooldown thresholds and
the telemetry collector revision must be frozen in the manifest.

The frozen macOS safety policy samples every 30 seconds and fails closed on an
unavailable or unparseable probe. Admission requires AC power, Low Power Mode
off, VM pressure level `1`, `memory_pressure -Q` free percentage at least 25%,
no recorded thermal or performance warning, at least 32 GiB free on the Docker
data filesystem, no running container, and at most the one exact expected
Ollama model resident. A running phase aborts before the next request when VM
pressure differs from `1`, AC power is lost, Low Power Mode is enabled, free
percentage is below 12%, free disk is below 24 GiB, swap has grown by more than
1 GiB from the phase baseline or more than 512 MiB within the current block, a
thermal/performance warning appears, an unexpected model becomes resident, or
an unrelated container appears. Existing swap at admission is descriptive and
is not itself a rejection; only growth is gated. Cooldown requires two
consecutive samples 30 seconds apart with VM pressure `1`, at least 20% free,
no warning, no unexpected resident model or container, and less than 64 MiB
additional swap between samples. Failure to cool within 10 minutes stops the
run. Every running and cooldown sample must retain the admission boot-time
identity; a mismatch always routes through recovery rather than being treated
as an ordinary cooldown.

Each `(model, task, replicate)` is a four-arm block. Arm positions follow a
precomputed balanced Latin schedule whose complete order is hashed before the
run. The exact schedule is model-independent: rank the 100 `(task, seed)`
blocks by SHA-256 over the UTF-8 salt
`edgeloopbench-v0.6-intercode-block-order-v1`, a NUL byte, the confirmatory
sample SHA-256, a NUL byte, the task ID, a NUL byte, and the decimal seed.
Assign ranked block `i` to row `i mod 4` of the even-order Williams square
generated from arm order `direct`, `independent_verified_sampling`,
`raw_feedback_loop`, `engineered_loop`; its first row is positions
`[0, 1, 3, 2]` and each following row adds one modulo four. This gives every
arm 25 appearances in each position and every ordered adjacent arm pair 25
appearances across the 100 blocks. Qwen and every admitted replication model
reuse the identical task/seed and within-block order; each model manifest binds
the canonical schedule SHA-256. If a safety threshold is crossed, the entire
block is infrastructure-invalid and may be requeued once; only rerunning the
slower arm is forbidden.
Across both models, at most 12 blocks may be requeued, adding no more than 228
generation requests. A thirteenth requeue stops the run as incomplete rather
than changing the population or retry policy.
Timeout and budget exhaustion are valid model failures. Infrastructure failures
are separate and receive an arm-asymmetry sensitivity analysis. A positive
primary claim requires at least 99.0% valid coverage in every arm and no more
than a 1.0 percentage-point valid-coverage gap between any two arms. Otherwise
only the preregistered worst-case sensitivity is reported.

Execution is append-only and resumable from the first missing block. A block is
complete only when its journal durably contains all four ordered arm results,
four terminally sealed scope artifacts, the post-block host check, a
`block_completed` event, and a terminal seal. Such a block is skipped after a
reboot and never rerun. If all four arms and the post-check are durable but only
the metadata-only completion/seal transition is missing, recovery may verify
those exact roots and finish that transition without another model request.

Every other interrupted block is invalidated as a whole; no old arm is mixed
with a new one. Before any cleanup, a separate mode-`0600`, hash-chained block
recovery journal durably anchors the block/generation, model, run, schedule,
gate-manifest and Docker-recovery-contract digests, the block-invalidation
barrier, and the exact roots/counts/seal and partial-tail status of every old
resource and semantic journal. It then records an intent and an issuer-bound
receipt for each exact presence-or-absence reconciliation, scope recovery,
block invalidation, and terminal seal. Old source journals are never edited or
truncated. A partial tail in the recovery journal itself must first be copied
to a mode-`0600`, hash-named, fsynced quarantine artifact before only that
incomplete terminal record is truncated and its digest/length are appended.

Only after the recovery journal is sealed invalidated may generation 1 start
with new run and scope identities. Generation 1 cannot be requeued. A second
interruption makes the study incomplete. The global 12-block requeue count is
reconstructed from generation-1 block headers, not an in-memory counter.

Each model has 100 confirmatory four-arm blocks: 50 tasks times two seeds. One
block permits at most 19 generation requests (`1 + 6 + 6 + 6` across Direct,
Independent, Raw, and Engineered). Thus confirmatory scoring is capped at 1,900
requests per model and 3,800 if both qualify. Across calibration, host load,
confirmatory scoring, and the global requeue allowance, the two-model hard cap
is 4,460 generation requests. The corresponding confirmatory accounting is at
most 800 initial arm prompts, 3,000 automatic follow-up/independent prompts,
and zero human prompts unless an intervention actually occurs. Crossing any
ceiling yields an incomplete study, never an edited denominator.

## 10. Endpoints and analysis

The single primary estimand uses Qwen3.5 4B only. Let `B*` be the one cumulative
logical-prompt-token ceiling frozen before calibration and carried unchanged
into confirmatory scoring. Let `S_h` be the sampled tasks and `N_h` the complete
qualified count
in filesystem stratum `h`. For each sampled task `t` and arm `a`, define

```text
Y[t,a] = mean over seeds [11,29] of final selected-checkpoint strict_success
D[t]   = Y[t,engineered] - Y[t,raw]
Delta  = sum_h (N_h / sum_j N_j) * mean_{t in S_h}(D[t])
```

The two seeds stay inside each task cluster. Stratum weights target the complete
qualified InterCode-Bash population while the 50-task hash sample controls
local cost; this is an estimate, not direct performance measurement on every
qualified row. The primary result is `Delta` at `B*` under the simultaneously frozen
completion-token, call, action, evaluator, maintenance, context, and timeout
ceilings. Prefix selection uses each controller's frozen rule: Direct's sole
checkpoint; Independent's first official success or latest executed checkpoint;
Raw's current checkpoint; and Engineered's latest highest-score checkpoint.
Phi, filesystem strata, official reward, other budgets, and attempt/token curves
are secondary or replication analyses.

Required output includes:

- strict and official success at fixed logical-token budgets;
- success at attempts 1, 2, 4, and 6;
- final-checkpoint success on all sampled episodes and post-hoc oracle
  any-checkpoint success on the nested diagnostic subset;
- model prompts, automatic feedback-conditioned follow-ups, independent sample
  prompts, environment actions, and evaluator calls;
- logical prompt/completion tokens and wall time;
- time/tokens to first successful checkpoint;
- paired rescues, regressions, and net rescues;
- repeated normalized actions, no-progress cycles, and admissibility errors;
- unresolved handoffs and paired avoided unresolved handoffs;
- infrastructure-invalid rates and extra tokens per net rescue.

Online curves use the controller that was actually run. An early official
success is absorbing at later attempt budgets; if that stop is a strict false
positive, strict success remains false at all later unexecuted points. Direct's
one selected result is carried forward as the no-extra-compute baseline.
Every episode's final selected checkpoint receives strict evaluation. To bound
trusted evaluator/evaluation-replica churn, full post-hoc strict curves
evaluate every actually executed checkpoint only in the predeclared nested
12-task diagnostic subset, after online execution is sealed, and never create
unexecuted attempts.
This yields at most 800 final strict evaluations plus 1,152 diagnostic
checkpoint evaluations, or 1,952 strict evaluations across both models. A
post-hoc audit remains a separate fresh invocation even when it targets the
same selected checkpoint as the final audit.
`time/tokens to first strict success` and oracle any-checkpoint success are
therefore explicitly diagnostic-subset endpoints, reported separately from the
population-weighted selected-policy curve.

`human_prompt_count` is recorded only if a human actually intervenes. During an
autonomous benchmark it is expected to remain zero. A failed episode is an
unresolved handoff, not an invented human prompt.

Primary hypothesis:

> `engineered_loop` improves strict success over `raw_feedback_loop` by at least
> 5 percentage points at `B*`.

The practical qualification rule requires both a point estimate of at least
`+5.0` percentage points and a task-clustered paired 95% bootstrap interval
whose lower bound is above zero. It also requires more rescues than regressions,
the numeric verifier-alignment and valid-coverage gates above, and a reported
extra-token point estimate and interval per net rescue. Token cost has no
post-hoc pass threshold; it is `not applicable` when net rescues are zero.
The primary ordinary interval is the 2.5th and 97.5th percentiles of 10,000
replicates using PRNG seed `20260715`. Each replicate resamples task clusters
with replacement within each Bash filesystem stratum, carries both seeds and
all arms, and reapplies the frozen `N_h/N` weights. A finite-population-
corrected standard-error interval is reported only as a labeled sensitivity,
not substituted for the conservative primary interval.

With 50 task clusters, the approximate ordinary 95% half-width is 7, 11, or 17
percentage points when the across-task paired-effect standard deviation is
0.25, 0.40, or 0.60 (approximately 6, 9, or 14 points with finite-population
correction). The design is therefore not reliably powered to prove a true
five-point effect. The `+5` threshold and lower-bound-above-zero rule remain
unchanged, but a null or interval crossing zero is inconclusive and cannot be
reported as equivalence or evidence that loops have no effect.

Secondary contrasts are Raw minus Independent Verified Sampling and Independent
Verified Sampling minus Direct. Raw minus Independent changes persistent state,
continuous transcript, prior actions/output, and model-visible score jointly;
it identifies the complete stateful interaction package, not scalar feedback
alone. If verified sampling matches Raw, that package is not justified at the
tested budget. If Raw helps but Engineered does not, interaction is useful but
this engineering package is unproven. A confidence interval crossing zero is
inconclusive regardless of point estimate.

Cross-model directional consistency requires the same sign on every
calibration-qualified model. A strong cross-model uplift statement additionally
requires both model-specific clustered intervals to have lower bounds above
zero. One-model success is reported only for that pinned model; a Phi gate
failure is a model-task-fit exclusion, not negative loop evidence.

Wall time, thermal, memory, swap, and maintenance telemetry are descriptive
host-safety and cost measurements. v0.6 makes no serving-efficiency conclusion.

Any effectiveness conclusion is limited to the hash-randomized qualified
InterCode-Bash population, verifier-assisted `K <= 6` topology, two frozen
local models that pass calibration, and the pinned runtime/quantization. It is
not a full-200 performance run, SWE-bench evidence, a loop without an attempt-
level scorer, or proof that rollback, packet formatting, or the no-progress
guard individually caused a package-level contrast.

## 11. Scoring remains blocked until these artifacts exist

- vendored source/attribution and immutable task-source hashes;
- four offline-qualified image digests and a clean-reset proof;
- ordered task inclusion/exclusion manifest and suite SHA-256;
- ordered 50-task stratified sample and nested 12-task diagnostic manifests,
  including salt/algorithm/quota hashes and qualified-population weights;
- frozen common prompt, raw packet, engineered packet, parser, and controller
  source hashes;
- strict evaluator tests, including adversarial cases for modified files;
- numeric budgets, `K`, generation parameters, and full block-order hash;
- model, tokenizer, chat-template, runtime, weight-quantization, and separately
  recorded KV-cache-quantization pins, plus seed-diversity evidence;
- 30-minute host qualification and admission/cooldown thresholds;
- a complete `make check` pass and leak-focused diff review.

Until these gates pass, v0.6 may run fake-environment tests, gold replay, reset
qualification, and one-task smoke checks only. No performance uplift is claimed.
