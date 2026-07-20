# ADR 034: Bind Docker iidfile projection semantics

- Status: Accepted before scoring
- Date: 2026-07-20
- Scope: InterCode image identity transport and manifest reproducibility

## Context

The first two production attempts that passed stable host admission both built
the pinned fs1 image but stopped before appending an image event. Each private
manifest contained only its v2 plan header and a 71-byte iidfile. No model was
loaded and no model prompt was issued.

An isolated cached build reproduced the failure. The controller reserved a
mode-`0600` regular inode, but Docker CLI 27.3.1 removed that pathname and
created a different mode-`0644` inode containing the full image ID. The held
reservation descriptor then had link count zero. The pinned Docker CLI source
defines this behavior: it removes the iidfile before the build and writes the
successful image ID with requested mode `0666` afterward ([build.go lines
209-214](https://github.com/docker/cli/blob/v27.3.1/cli/command/image/build.go#L209-L214),
[393-399](https://github.com/docker/cli/blob/v27.3.1/cli/command/image/build.go#L393-L399)).
The inherited host umask projects that request to the observed `0644`.

The prior same-inode contract was therefore incompatible with the pinned real
client. Relaxing identity checks generally would make the image authority
ambiguous, while continuing to require the reservation inode would make every
live build fail.

## Decision

The image-build plan and manifest schemas advance to v3. The plan core now
binds iidfile protocol revision
`docker-remove-recreate-private-parent-v1`, projected mode `0644`, and
normalized mode `0600`; these fields therefore change the plan digest. v1 and
v2 manifests are not migrated or resumed.

For every stratum, the controller:

1. opens and retains the owner-mode-`0700` parent directory;
2. reserves the absent basename with `O_EXCL`, `O_NOFOLLOW`, mode `0600`, and
   a retained descriptor;
3. after Docker returns, requires that exact empty reservation inode to remain
   unchanged except for link count becoming zero;
4. opens Docker's new basename relative to the retained parent with
   `O_NOFOLLOW` and `O_NONBLOCK`;
5. requires one owner-controlled regular link, exact projected mode `0644`,
   and at most 72 bytes, then immediately normalizes that same inode to `0600`;
6. bounded-reads the same descriptor twice, requires identical content and one
   full `sha256:` image ID, and revalidates parent, pathname, and inode identity
   around those reads and removal.

The full image ID remains the only image authority and is still inspected
directly for every pinned platform, role, network, filesystem, collector, and
build-plan label. Docker stdout is only a consistency check. A symlink, FIFO,
directory, hard link, wrong owner, unexpected mode, oversized or torn payload,
parent drift, path replacement, or concurrent same-inode content change fails
closed before a manifest image event is appended. Operating-system failures
are reported as typed errors without local paths.

## Consequences

- Live Docker behavior is accepted without weakening the private-directory or
  content-addressed image authority.
- A different host umask or Docker projection mode is a reproducibility stop,
  not an implicitly accepted runtime variation.
- Failed v2 attempts remain append-only diagnostic evidence but cannot be
  confused with v3 build evidence.
- The protocol change occurred before calibration, confirmatory scoring, model
  loading, or any model prompt, so it cannot be outcome-informed.
