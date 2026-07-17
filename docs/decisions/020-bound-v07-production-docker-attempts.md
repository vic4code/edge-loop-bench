# ADR 020: Bind each v0.7 production Docker attempt to one study episode

- Status: accepted
- Date: 2026-07-16

## Context

ADR 016 seals qualification evidence and opaque trusted reference material,
ADR 018 seals the two-model runtime session, and ADR 019 binds the formal
publication authorities. None of those boundaries previously constructed the
`DockerAttemptBoundary` consumed by the episode runner.

A generic closure around `DockerAttemptBoundary` would be too weak. It could
silently use a mutable or unqualified image, a task copied from another source,
limits different from the pre-calibration manifest, gold from another task or
campaign, or a reused run ID after partial construction. Those errors would
invalidate the paired comparison even if the controller journal later sealed.

The scoring design does not have a Docker evaluator container. ADR 012 keeps
`Dockerfile.evaluator` as a non-scoring placeholder. Strict comparison is an
in-memory evaluator closure over authority-sealed reference material. Creating
or claiming a scoring evaluator image here would change the experiment.

## Decision

Add `intercode_v07_attempt_factory.py` as the only production composition for
fresh formal and calibration attempt boundaries.

Its public construction APIs consume typed campaign authorities rather than a
caller-supplied campaign digest:

- `build_v07_formal_docker_attempt_factory` accepts an exact
  `V07PreparedStudy` and derives its `formal_campaign_sha256` and the exact
  episode-bound `V07TrustedGoldMaterial`;
- `build_v07_calibration_docker_attempt_factory` accepts an exact
  `V07CalibrationGoldResult` and derives its calibration campaign root and
  task-bound reference material; and
- `v07_attempt_run_id` derives `v07-<20 lowercase hex>` from the exact campaign
  root, episode index, attempt index, fixed `agent` role, parent manifest
  run-ID revision, and the nested
  `intercode-v0.7-run-id-campaign-episode-attempt-role-sha256-v1` revision.

Both builders fail closed unless all of the following agree:

- the episode is the exact row at that index in the frozen formal or
  calibration schedule;
- the `PublicBashTask` is the identical object resolved from the verified
  `InterCodeSource`, preventing a forged equal-looking task or changed query;
- formal tasks use their exact `fs1` through `fs4` setup, while calibration's
  public `calibration` stratum maps only to the preregistered `fs1` setup;
- the manifest is builder-sealed, contains both exact model identities, and
  carries the frozen `V07ExecutionPins` and four qualification image IDs;
- the selected agent image is the qualified image for that setup;
- the opaque reference material names the same task, source capability,
  qualified reference-replay image, evaluator revision, and state
  normalization revision; and
- the Docker/action dependencies provide only the bounded methods required by
  `DockerAttemptBoundary`.

The factory retains no `V07TrustedGoldMaterial`, `CandidateMaterial`, task
query, source object, manifest object, evaluator path, or host path. The episode
runner remains the only component that opens reference material and captures
it in `make_strict_evaluator`. The factory retains only path-free identities,
the exact immutable limits, and the injected Docker/action boundaries.

Direct arms may construct one attempt boundary. Every other arm may construct
at most four. A slot is consumed before Docker construction starts, so a
partial failure cannot reuse the same run ID. Internal policy recovery inside
one `DockerAttemptBoundary` remains part of that same logical attempt.
Cross-process reuse prevention remains the campaign pending-intent and
reconciliation responsibility; this in-memory factory is not a retry journal.

## Image identity interpretation

There are two identities to compare but only one runtime image role:

- **agent image identity:** the immutable image used for the live candidate
  attempt; and
- **evaluator reference image identity:**
  `V07TrustedGoldMaterial.image_id`, proving which qualified setup image
  produced the sealed reference replay.

They must be equal to the manifest's qualified image for the selected setup.
The second value does not authorize or imply an evaluator container.

## Remaining image-build bridge

The existing image builder creates and inspects the four immutable agent image
IDs. A production bridge still has to prove, under the v0.7 host session, that
those exact build results are the IDs passed to `run_v07_docker_qualification`
and then retained in the pre-calibration manifest. The attempt factory does not
repair or infer that provenance; it only consumes the already-qualified
manifest mapping.

No scoring evaluator-image bridge is required. The retained evaluator
Dockerfile remains intentionally outside measured execution. If the study is
later redesigned around an isolated evaluator process, that is a new
experimental variable and requires a new ADR, qualification matrix, and
manifest revision.

## Alternatives considered

### Accept a raw campaign SHA-256 and raw gold object

Rejected. Two syntactically valid values could come from different campaigns.
Typed `V07PreparedStudy` and `V07CalibrationGoldResult` authorities prevent
that mix-up at the public API.

### Reuse one container across attempts

Rejected. State carry-over would confound the arm comparison and make retry
behavior depend on prior candidates.

### Use the evaluator placeholder as a scoring container

Rejected. It contains no scoring authority and would contradict ADR 012 and
the current in-memory exact-comparison design.

## Verification

Focused tests use a verified vendored source plus in-memory Docker and action
fakes. They perform no real Docker, model, Ollama, or network call. They cover
deterministic IDs, all four non-direct attempt slots, the direct single-attempt
cap, consumed IDs after failed construction, formal/calibration schedule
separation, task/model/campaign mismatches, image/evaluator/capability
mismatches, and absence of retained private evaluator material.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests \
  -p 'test_intercode_v07_attempt_factory.py'
python3 -m compileall -q \
  src/edgeloopbench/intercode_v07_attempt_factory.py \
  tests/test_intercode_v07_attempt_factory.py
```

## Consequences

- Every live attempt starts from a fresh qualified setup with the exact
  manifest limits and a campaign-bound, non-reusable run ID.
- Formal and calibration factories cannot be cross-composed through the public
  API.
- Gold remains outside the agent, prompt, journal, factory state, and Docker
  image.
- The image-build-to-qualification provenance bridge remains a mandatory
  pre-run integration slice; this ADR does not claim it is complete.
