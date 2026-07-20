# ADR 030: Replace the unsupported container storage quota with a sampled watchdog

- Status: Accepted before any v0.7 model scoring
- Date: 2026-07-20

## Context

The first v0.7 qualification attempt stopped during container creation, before
any prompt, model load, candidate, or scored outcome. Docker Desktop's pinned
`overlay2` backend rejected the requested per-container
`--storage-opt size=268435456` limit.

Docker documents `--storage-opt size` only for the `btrfs`, `overlay2`,
`windowsfilter`, and `zfs` storage drivers. For `overlay2`, the backing
filesystem must be XFS mounted with the `pquota` option. The admitted Docker
Desktop engine does not expose that required per-container quota capability.
Keeping the flag would therefore make the preregistered run impossible, while
silently treating another mechanism as an equivalent hard quota would make the
resource claim false.

Relevant upstream behavior is documented in:

- [Docker run storage-driver options](https://docs.docker.com/reference/cli/docker/container/run/#set-storage-driver-options-per-container)
- [Docker daemon `overlay2.size` prerequisites](https://docs.docker.com/reference/cli/dockerd/#overlay2-options)
- [Docker run ulimits](https://docs.docker.com/reference/cli/docker/container/run/#set-ulimits-in-container---ulimit)
- [Docker inspect container size](https://docs.docker.com/reference/cli/docker/container/inspect/)

## Decision

Replace the unsupported hard-quota claim with the explicitly named mode
`sampled-size-rw-no-hard-quota-v1`.

1. Container creation does not pass `--storage-opt`. Re-attestation requires
   `HostConfig.StorageOpt` to be empty; an unexpected storage option is a
   security-policy failure.
2. Every container receives `--ulimit fsize=16777216:16777216`. This is a
   16 MiB per-file process limit and is not represented as an aggregate
   writable-layer quota.
3. The trusted Docker boundary runs `docker container inspect --size -- ID`
   against the exact attested container and validates its full identity,
   labels, lifecycle state, and security profile before accepting a
   nonnegative integer `SizeRw`.
4. `DockerActionExecutor` samples `SizeRw` before an action, throughout the
   action every 0.25 seconds, and after the post-action process audit. Each
   probe has a 1-second timeout. The frozen threshold is 256 MiB.
5. A missing, malformed, timed-out, or identity-drifted sample fails closed.
   A during-action watchdog signal terminates the local `docker exec` process
   immediately, then removes and verifies only the exact run-labelled
   container. If the action-start marker was not captured, the row is
   infrastructure-invalid rather than attributed to the model. A proven
   action or post-action overflow is a policy failure. A pre-action overflow
   is infrastructure-invalid because it predates the candidate action.
6. The existing host admission and abort gates remain independent. Memory is
   512 MiB with no container swap, actions retain their 10-second deadline,
   and prompt, output, process, replay, and campaign caps are unchanged.

This watchdog is sampled enforcement. A write may overshoot the threshold
between samples or while Docker calculates `SizeRw`; it is not a hard quota
and publications must not describe it as one.

The canonical pre-calibration manifest is revised to
`intercode-v0.7-precalibration-manifest-v2`. Its execution identity includes
the storage-mode string, 256 MiB watchdog threshold, 16 MiB soft/hard `fsize`,
0.25-second cadence, and 1-second probe timeout. The qualification authority is
revised to `intercode-v0.7-docker-qualification-authority-v2`. These canonical
record changes produce new execution and manifest digests, so evidence sealed
under the prior identities cannot be mixed with a new run.

The amendment is outcome-independent: it was made after an infrastructure
failure and before any model-dependent observation. Qualification must restart
from a fresh source inventory and the revised manifest; no old replay fact is
edited or reclassified.

## Alternatives considered

### Mount the task root on tmpfs

Rejected. Docker documents that tmpfs mounts obscure existing container data
at the mount point and may be written to host swap. Mounting `/testbed` or the
root filesystem would change the frozen fixture and storage semantics rather
than merely replace a quota. See
[Docker tmpfs mounts](https://docs.docker.com/engine/storage/tmpfs/).

### Use Docker Desktop's disk-usage or virtual-disk limit

Rejected. Docker Desktop exposes engine-wide virtual-disk controls, not an
exact per-container task quota. Using them would couple the run to unrelated
engine state and would not support candidate-level attribution. See
[Docker Desktop resource settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/#resources).

### Make the task filesystem read-only

Rejected. InterCode-Bash tasks intentionally modify their filesystem. A
read-only root would change task validity and the estimand.

### Remove aggregate storage protection

Rejected. The 16 MiB `fsize` limit does not bound many small files. The sampled
exact-container watchdog preserves a fail-closed aggregate safety signal while
keeping its weaker semantics explicit.

## Consequences

- v0.7 can run on the admitted Docker Desktop backend without claiming an
  unsupported hard per-container quota.
- Disk-growth protection is bounded by sampling cadence and probe latency, so
  host free-space gates remain mandatory.
- Any probe ambiguity or overflow stops and cleans the exact container before
  another model request can be authorized.
- All qualification and formal evidence must bind manifest v2 and
  qualification-authority v2; prior attempted-run records remain historical
  infrastructure evidence only.
