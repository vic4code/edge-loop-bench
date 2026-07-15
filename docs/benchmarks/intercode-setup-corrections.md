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
pins the platform-specific base bytes; it is not a digest for either final
EdgeLoop image. Final agent and evaluator image digests remain unset until a
reviewed build and inspection are completed.

`Dockerfile.agent` and `Dockerfile.evaluator` contain byte-identical common
package instructions so the four agent variants and evaluator can reuse that
dependency layer. Apt uses `--no-install-recommends`. The layer includes the
commands declared by the source tasks, including `md5deep`, `ncompress`,
`rename`, `g++`, `dig`, `ping`, `pstree`, `tree`, `cpio`, `jq`, `column`, and
`cal`, plus the standard GNU text/file utilities.

The derived image deliberately does not reproduce the upstream root-level
Git baseline. Committing the complete installed operating system into `/.git`
would duplicate package bytes, enlarge build cache, and give an agent a
mutable evaluator mechanism. Baseline and candidate state are instead captured
outside the model channel through Docker-layer checkpoints and the private
evaluator. A root `.dockerignore` admits only `docker/intercode/**` to the
build context, so local results, logs, Git history, and vendored gold data are
never sent to the daemon during these builds.

Package installation is a build-time operation. The fixture scripts contain
no download or package-install step. The image cannot itself enforce Docker
network isolation, so the runtime adapter must create every measured agent and
evaluator container with `NetworkMode=none`; inspection must fail closed if it
does not. The installed `curl` binary exists because an upstream row declares
it, but that row must be excluded by offline qualification and the binary has
no usable runtime network in an admitted container.

Fixture construction runs as root during the image build. Model actions run as
numeric UID/GID `65532:65532` after the fixture roots are handed to that user.
The upstream working directory `/` is preserved for relative-command fidelity,
but `/` remains root-owned and is mode `1777`: the agent can create and remove
its own top-level outputs without deleting root-owned system entries. The
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

The source script has no known fatal setup typo. The derived script adds
fail-fast behavior and deterministic timestamps, while retaining the original
fixture contents, paths, modes, and two explicitly dated files.

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

### fs4

The upstream script intentionally creates no fixtures. The derived script only
adds fail-fast behavior and retains `file_system_version=4`; no synthetic fs4
tree is invented.

## Derived source hashes

These hashes identify the reviewed correction scripts. A change requires this
note and the static tests to change together.

| Derived asset | SHA-256 |
| --- | --- |
| `docker/intercode/setup/setup_nl2b_fs_1.sh` | `3fe38c065ceb7d82a0105c413128d47788f4fd731f30ccc8a4a4d58200663c58` |
| `docker/intercode/setup/setup_nl2b_fs_2.sh` | `29381bf8d1fade3ca86561f3e6bd129a9bbdddcf00f5e5236cc6358dd91d839f` |
| `docker/intercode/setup/setup_nl2b_fs_3.sh` | `7d55db5d64d14ea8b4b72d86fa0fa68e7ed9fdeaa461fcfe8b80ff1f011d7026` |
| `docker/intercode/setup/setup_nl2b_fs_4.sh` | `e155eece189f409162571aa0f300a1a7f57ea216adbe8dec36e6b73affd94858` |

For pre-build review only, the remaining derived source hashes are:

| Derived asset | SHA-256 |
| --- | --- |
| `docker/intercode/Dockerfile.agent` | `1b517c32b59548974d4cdc9005326e34088094d9ebe645493d0cae3e80dc5912` |
| `docker/intercode/Dockerfile.evaluator` | `103107c2d9bdc906380f6862ca0775adab6bf4de354aff1c9a6a4b3773a434fc` |
| `docker/intercode/evaluator_placeholder.py` | `de4642dd71f18a3b5f1bfcb7a73f99292129aa9e73a25034a49d76269cd32cad` |
| `.dockerignore` | `effea9dab4a4907f298a1af85886ab8539a79a4b86c80f97c250aebd58952ca5` |

## Evaluator image scope

`Dockerfile.evaluator` currently contains only a non-scoring, standard-library
placeholder. It emits `status=not_implemented` and exits with code 78. It does
not inspect a candidate, expose diagnostics, or return a success value. This
fail-closed placeholder prevents a source-only image scaffold from being
mistaken for the frozen attempt-level or strict evaluator. No benchmark run may
begin until the real evaluator is specified, tested adversarially, source- and
image-pinned, and proven isolated from the model channel.
