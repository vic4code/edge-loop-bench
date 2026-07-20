# InterCode-Bash derived image and setup corrections

Status: source-pinned, statically tested, **not built or Docker-qualified**.

This note binds the correction layer used to construct the candidate
`InterCode-Bash-qualified@c3e46d8` images. It does not turn the upstream 200
rows into a qualified suite and does not authorize measured model scoring.
Qualification still requires fresh offline gold replay, fixture inventory,
strict-evaluator tests, immutable built-image digests, and the host safety gate.

## Provenance boundary

The raw evidence is retained byte-for-byte under
`vendor/intercode/c3e46d827cfc9d4c704ec078f7abf9f41e3191d8/`. The upstream
revision is `c3e46d827cfc9d4c704ec078f7abf9f41e3191d8`.
The corresponding upstream source is the
[InterCode Docker directory at that commit](https://github.com/princeton-nlp/intercode/tree/c3e46d827cfc9d4c704ec078f7abf9f41e3191d8/docker).

| Raw upstream asset | SHA-256 |
| --- | --- |
| `docker/nl2bash.Dockerfile` | `c8b52b44cc276921f1b139d49562152792872c7b013261b748305a78d4230189` |
| `docker/bash_scripts/setup_nl2b_fs_1.sh` | `02b9a2206d809a9fca03b755e61b94618248a400fd3132ac61d32b6f3009dd3f` |
| `docker/bash_scripts/setup_nl2b_fs_2.sh` | `05c3109c4e9999e661d66c6d74137f0238b88017ec9cf884abdda0499e94ff1d` |
| `docker/bash_scripts/setup_nl2b_fs_3.sh` | `5e8d9f832f272c31dfb73567e75d33efb970d4e4bf9a8e691582d4fa09422d09` |
| `docker/bash_scripts/setup_nl2b_fs_4.sh` | `c5fb550aa1578fe2454e8ab06221165df90311231cb71d3d9b0ce036a8235274` |
| `docker/docker.gitignore` | `5479a1cafa260c77e836e8601ba9a345d39df777dc9cb07d6a93f0ac29b69166` |

The original InterCode Dockerfile uses `ubuntu:latest`. It remains provenance
evidence only and is never a measured-run input.

## Derived base and tool layer

Both derived Dockerfiles start from the native `linux/arm64` Ubuntu child
manifest `sha256:2e05d3b43282818e548d97f7a7c4dd7cab14760603972353e5cecdac0839146b`.
This is the current derived base, not the original InterCode image. Its digest
pins the platform-specific base bytes; it is not a digest for a final EdgeLoop
image. Final fs1..fs4 agent image digests remain unset until a reviewed build
and inspection are completed. The evaluator identity is a source/policy digest,
not an image digest.

`Dockerfile.agent` and the retained non-scoring `Dockerfile.evaluator` scaffold
contain byte-identical common package instructions. Apt uses
`--no-install-recommends`. The agent layer includes the
commands declared by the source tasks, including `md5deep`, `ncompress`,
`rename`, `g++`, `dig`, `ping`, `pstree`, `tree`, `cpio`, `jq`, `column`, and
`cal`, Git, plus the standard GNU text/file utilities.

The agent image preserves the upstream root-level Git baseline for public
environment fidelity. The byte-exact upstream `docker.gitignore` limits that
baseline to the fixture surface instead of the installed operating system.
After final fixture ownership is set, the build creates the initial commit at
a fixed timestamp. It then rebuilds the Git index from `HEAD` so every index
stat-cache field is zero, normalizes `/.git` and `/.gitignore` mtimes, and makes
the metadata root-owned and read-only. Sticky-root semantics prevent UID 65532
from replacing either path. Gold commands, scoring evaluator code, private task
references, and host paths never enter the agent image or daemon build context.
Scoring invokes absolute system Git with fixed arguments and optional locks
disabled, so candidate configuration or `PATH` cannot change the observation.

The root `.dockerignore` excludes the workspace by default. It admits only the
two reviewed Dockerfiles, the non-scoring evaluator placeholder, the state
collector, the four exact setup scripts, and the parent chain to the
byte-pinned upstream `docker.gitignore`. Source rows, gold data, scoring
evaluator material, bytecode caches, local results, logs, and host Git history
remain outside the daemon build context. Every admitted file is part of the
recorded context digest.

The agent image also contains a root-owned mode-`0555` standard-library state
collector. Its measured argv is fixed to `/usr/bin/python3 -I -S -B
/opt/edgeloop/state_collector.py --profile fsN`; it never invokes a shell or
consults `PATH`. A build-time audit fails closed unless `/` is UID 0 and mode
`01777`, the model-writable surface is completely covered, captured state has
no extended attributes, and POSIX ACLs are absent. Runtime collection also
requires empty `/dev/shm` and `/dev/mqueue` plus header-only SysV IPC tables.
On Linux, ACL inspection calls `os.listxattr()` on the already verified open
descriptor and fails closed on an unavailable or rejected descriptor call; it
does not rely on the runtime's incomplete `os.supports_fd` metadata.
The build audit permits `.dockerenv` to be absent only because Docker injects
that pinned root name at container start; runtime collection still requires
it. The pinned base's unused `/var/lib/pebble/default` directory is normalized
from world-writable to root-owned mode `0755` before the audit.
Its output is bounded canonical JSON over sorted relative paths, content, type,
mode, UID/GID, symlink targets, and complete path-derived hardlink groups.
Inodes, devices, timestamps, gold data, and evaluator paths are not emitted or
digested.

Package installation is a build-time operation. The fixture scripts contain
no download or package-install step. The image cannot itself enforce Docker
network isolation, so the runtime adapter must create every measured agent,
candidate-replica, and clean-gold container with `NetworkMode=none`; inspection
must fail closed if it does not. The installed `curl` binary exists because an upstream row declares
it, but that row must be excluded by offline qualification and the binary has
no usable runtime network in an admitted container.

Fixture construction runs as root during the image build. Model actions run as
numeric UID/GID `65532:65532` after the fixture roots are handed to that user.
The upstream working directory `/` is preserved for relative-command fidelity,
but `/` remains root-owned and is mode `1777`: the agent can create and remove
its own top-level outputs without deleting root-owned system entries. The
final filesystem layer reapplies mode `1777` immediately before the strict
writable-surface audit. This is required because an overlay mount does not
prove that root-directory metadata set in an earlier layer remains visible.
Docker's exported runtime mount is then initialized to mode `1777` by one
fixed root-only trust-boundary operation and independently attested before any
agent action; the image's configured and action-execution user remains numeric
UID/GID `65532:65532`. The
derived image narrowly pre-creates agent-owned `/usr/workspace` for the fs3
directory-copy row; it does not chown `/usr`. `HOME` is an agent-owned
`/home/agent`. Qualification must still reject any row whose behavior diverges
under this frozen non-privileged policy.

## Correction policy

The derived scripts live under `docker/intercode/setup/`. Every script uses
`set -euo pipefail`, fixes `TZ=UTC` where fixtures exist, and gives generated
fixtures deterministic mtimes anchored at `2023-05-31 23:59:58 UTC`. Explicit
relative-age fixtures are then assigned fixed timestamps. This removes build
clock and BSD/GNU `date` dependence; it does not emulate a 2023 runtime clock.
Rows whose semantics still depend on wall time are excluded unless replay
proves them deterministic under the frozen protocol.

### fs1

The source script has no fatal setup typo. Its dated `touch` both creates the
empty `/testbed/recent.txt` fixture and assigns its mtime. The first live
derived build exposed that the deterministic-timestamp rewrite had retained
only the later no-dereference timestamp assignment, which cannot create a
missing path. The correction now creates that empty file explicitly before
normalization. The derived script otherwise adds fail-fast behavior and
deterministic timestamps while retaining the original fixture contents,
paths, modes, and two explicitly dated files.

### fs2

Three source defects are corrected:

- `echo - e ...` becomes a deterministic `printf`, so no stray `-` or `e` file
  is created and `text3.txt` receives the intended two lines;
- `touch .placeholder /system/folder3/backup_dbg/backup` becomes a single
  explicit target, `/system/folder3/backup_dbg/backup/.placeholder`;
- malformed `20230522359.59` becomes `202305022359.59` (May 2 at 23:59:59),
  restoring the missing zero and the intended old-file ordering.

The folder2 archive is also emitted with sorted members, fixed ownership,
fixed member mtimes, and a timestamp-free gzip header.

The four upstream dated `touch` commands also create empty fixtures. The
derived no-dereference timestamp form does not create a missing path, so the
correction explicitly creates those four empty files before normalization.

### fs3

Five source defects are corrected:

- invalid `mkdir ... -d "1 year ago"` is split into directory creation and the
  fixed timestamp `202205312359.59`;
- the macOS-only nested `date -v-1d` expression becomes
  `202305302359.59`, one day before the fixed anchor;
- impossible February 31 and April 31 dates become the last valid days of
  those months, `202302282359.59` and `202304302359.59`;
- the archive member `/workspace/dir1/new.sh`, which is never created, becomes
  the existing `/workspace/new.sh`;
- `yes '' | head -n 10` is expressed as a bounded loop, preserving ten blank
  lines without making fail-fast `pipefail` abort on `yes` receiving SIGPIPE.

The archive uses sorted members, fixed ownership and mtimes, and a
timestamp-free gzip header.

The five upstream dated `touch` commands also create empty fixtures. The
correction explicitly creates all five before normalization; this additionally
ensures that `recent.txt` exists before it is included in the archive.

### fs4

The upstream script intentionally creates no fixtures. The derived script only
adds fail-fast behavior and retains `file_system_version=4`; no synthetic fs4
tree is invented.

## Derived source hashes

These hashes identify the reviewed correction scripts. A change requires this
note and the static tests to change together.

| Derived asset | SHA-256 |
| --- | --- |
| `docker/intercode/setup/setup_nl2b_fs_1.sh` | `8a6a7e86384f0118adc30446d8fcf678137eb7de1ecc2d1a7caa6fa3bcc9a76b` |
| `docker/intercode/setup/setup_nl2b_fs_2.sh` | `6b4357910069649f9b76974f649300b0cd44053a8e592e3ddc44fdc3343abca4` |
| `docker/intercode/setup/setup_nl2b_fs_3.sh` | `bfbe25f6d21b84adfcf09b8dd9c4516e13f993ce905d0e8816313db08b97810d` |
| `docker/intercode/setup/setup_nl2b_fs_4.sh` | `e155eece189f409162571aa0f300a1a7f57ea216adbe8dec36e6b73affd94858` |

For pre-build review only, the remaining derived source hashes are:

| Derived asset | SHA-256 |
| --- | --- |
| `docker/intercode/Dockerfile.agent` | `a74d041ff6fdd5d54f3a5bd6d25779af090ce63fb9c9d24483adb106b514f6d1` |
| `docker/intercode/Dockerfile.evaluator` | `318fc5e51345036ada580f2552ae8fed61d37d31c9853eddcd3a893fd9c22ffa` |
| `docker/intercode/evaluator_placeholder.py` | `de4642dd71f18a3b5f1bfcb7a73f99292129aa9e73a25034a49d76269cd32cad` |
| `docker/intercode/state_collector.py` | `28cdd90502bb9b5d6ede8800bde5378a9f828ade09f97c08f60f49201626f6f5` |
| `.dockerignore` | `875b9b99193b7c98fc25ee9ae017c771cd5a2a854f920dd0e1523ab3ba5223ce` |

The collector's semantic pins are independent of its source bytes:

| Collector semantic record | SHA-256 |
| --- | --- |
| root baseline names | `sha256:06dcf54e33c9412b1c0bb2cf7ddab33848169e640012209b9d05c81ee1da457f` |
| collection and writable-surface policy | `sha256:1645f88e660e5c002af6a9b2a20aba06a8003cd4068008e38b417dd704b70794` |
| fs1..fs4 profile set | `sha256:19e2b86952ab1bb93d6a4648d00d200421cd328064e6caf6da4575e9a194c8d3` |

## Image-build control contract

The build utility is a private, pre-qualification instrument. Its default
mode is read-only: it validates the repository inputs and real Docker client,
then emits one canonical deterministic plan. Docker mutation is unreachable
unless the operator supplies the literal, unabbreviated `--execute` flag;
long-option abbreviation is disabled. Planning and tests must not contact a
daemon, build an image, start a model, or use a network.

The supported command surface is:

```text
python -m edgeloopbench.intercode_image_build \
  --repo-root /absolute/path/to/edge-loop-bench \
  --docker-binary /absolute/path/to/the/real/docker-binary \
  --docker-binary-sha256 sha256:<64-lowercase-hex> \
  --docker-endpoint unix:///absolute/local/docker.sock \
  --docker-client-version <exact-version> \
  --docker-server-version <exact-version>
```

Execution adds `--execute`, an absolute private `--manifest`, an absolute
Docker data path, and a private canonical `HostSafetyPins` JSON record. The
implementation is Python standard-library only and lives in
`src/edgeloopbench/intercode_image_build.py`; focused offline tests live in
`tests/test_intercode_image_build.py`.

Each plan binds all of the following before mutation:

- the content hash of a canonical, absolute, non-symlink, executable Docker
  client plus exact client and server versions;
- one explicit canonical local Unix endpoint; inherited `DOCKER_HOST` and
  `DOCKER_CONTEXT` are rejected rather than cleared silently;
- the canonical non-symlink repository root as the only build context,
  platform `linux/arm64`, the reviewed `Dockerfile.agent` SHA-256, root
  `.dockerignore` SHA-256, the four setup scripts, state collector, and
  byte-pinned upstream `docker.gitignore`;
- exactly four ordered `FILE_SYSTEM_VERSION` values, `1` through `4`.

The builder intentionally creates no image tag. An earlier deterministic-tag
design was rejected because Docker provides no atomic "create this tag only if
absent" operation: checking a predictable tag and then building with it leaves
a check-to-mutation race that can overwrite a concurrently created tag. The
private iidfile's full content-addressed image ID is the sole image authority.
A manifest-missing stratum always executes its exact pinned build; it never
adopts an existing image by label, tag, prefix, or predicted ID.
The corrected plan and manifest schemas are both `v2`; obsolete tag-bearing
`v1` records are not migrated or resumed.

`--execute` must use the existing `HostTelemetryCollector` and
`HostSafetyPolicy` immediately before every possible build. The final
admission occurs after input re-hashing, iidfile creation, and every other
potentially slow preparation step, directly before the `image build` process
is invoked. Admission is quiescent and fail-closed: VM pressure is exactly
`1`, there are zero resident
Ollama models, zero running containers, no power/thermal/disk/memory policy
failure, and the observed Docker binary, endpoint, client version, and server
version equal the plan and host-safety pins. Caller-declared expected
resources cannot relax this gate. Any unrelated running container stops the
utility without cleanup or build.

One nonblocking exclusive lock on the canonical repository directory is held
for the complete execute/resume operation. This makes cooperating utility
invocations across different manifest paths mutually exclusive; a second
invocation fails before telemetry or Docker access.

Every build uses fixed `shell=False` argv, the real pinned client, explicit
`--host`, `--platform linux/arm64`, repository-root context,
`Dockerfile.agent`, one exact `FILE_SYSTEM_VERSION`, and no `--tag`. After a
successful build, the full image ID is inspected directly. Each build receives
a fixed per-stratum `--iidfile` beneath the
manifest's private mode-`0700` parent. The controller proves that pathname
absent, creates a mode-`0600` regular file with `O_EXCL` and `O_NOFOLLOW`,
retains its inode, bounds and exactly parses the full `sha256:` image ID, and
revalidates pathname/inode identity. Docker stdout is never the authority for
the image identity. The image is admitted only when ID, OS, architecture, agent and
network-policy labels, filesystem version, fs-specific collector profile, and
all collector source/policy/root/profile-set/argv pins are exact. Binary and
build-input hashes are rechecked around each mutation and inspection; symlink,
path, or content drift fails closed.

The private manifest is mode `0600`, canonical JSONL, append-only, sequenced,
and hash-chained. Its first event binds the complete plan; each later event
binds one inspected fs image by full ID. The journal retains both its file and
private parent directory descriptors and initial identities. File mode, link
count, file pathname/inode identity, and parent pathname/inode identity are
revalidated before and after every read and append and immediately before a
successful return. An absent manifest starts a new execution. A complete valid
prefix containing fs1 through fsN is resumable: existing records are
re-attested by their recorded full IDs, then fsN+1 through fs4 are rebuilt
without lookup or adoption. Empty existing files, missing terminal newlines,
malformed or duplicate JSON fields, sequence gaps, out-of-order/duplicate strata,
changed plan identity, broken hashes, or an invalid/missing recorded image are
partial or tampered state and stop without mutation. The utility never
truncates, repairs, retags, prunes, or deletes an existing image.

Offline tests use real temporary files plus bounded fake telemetry/Docker
runners. They prove default plan-only behavior, exact ID-only argv and
inspections, last-moment quiescent admission, resume from a valid prefix,
refusal of torn/tampered/replaced journal state, unrelated-container refusal,
symlink and context-drift refusal, and the absence of every image-tagging or
image-deletion command. A real build remains blocked until
those tests and the repository checks pass and an operator explicitly invokes
`--execute` with reviewed live pins.

## Evaluator execution scope

`Dockerfile.evaluator` remains a non-scoring, standard-library placeholder. It
emits `status=not_implemented` and exits with code 78; it is not built, invoked,
or pinned by a measured run. A real evaluation invocation instead starts one
fresh candidate replica from the exact checkpoint image and one clean-gold
replica from the matching fs1..fs4 agent image. The trusted adapter collects
bounded evidence with image-pinned absolute tools, durably destroys both
replicas, and only then issues it to the source-pinned host comparator. That
comparator is a pure function over typed immutable values: it cannot run a
command, open a path, follow a symlink, or emit diagnostics to the model.
Measured scoring remains blocked until this complete boundary is tested
adversarially and its source/policy digest is frozen.
