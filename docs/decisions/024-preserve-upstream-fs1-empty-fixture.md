# ADR 024: Preserve the upstream fs1 empty timestamp fixture

- Status: accepted
- Date: 2026-07-20

## Context

The first real v0.7 image build reached the derived fs1 setup script and
failed before qualification. The raw pinned InterCode script creates an empty
`/testbed/recent.txt` with `touch -m -t202305312359.59`. The derived script
moved timestamp normalization to its final block but retained only the later
timestamp assignment, not the file creation. Under fail-fast execution that
made the setup terminate with `No such file or directory`.

This is an instrument-construction defect, not a task, model, controller, or
loop outcome. Production attempt 3 issued no model prompt and produced no
calibration or formal episode.

## Decision

Create the empty `/testbed/recent.txt` fixture explicitly in the derived fs1
text-file block, before common mtime normalization. Retain the raw vendored
script byte-for-byte and retain the fixed final timestamp assignment. Add a
static regression that requires the creation to precede the timestamp update,
then update the correction note and every source hash pin together.

The prior derived fs1 hash and every image-build plan that binds it are
superseded. Failed production directories remain append-only evidence. A new
production attempt must use a fresh artifact root and a clean source inventory;
it may not resume or reinterpret attempt 3.

## Consequences

- The derived fixture again contains the same empty path intended by upstream.
- The fix does not expose gold commands, evaluator material, or hidden paths to
  the agent.
- Image build and offline gold qualification must pass before any model prompt.
- No effectiveness or serving-efficiency conclusion can be drawn from the
  failed build.
