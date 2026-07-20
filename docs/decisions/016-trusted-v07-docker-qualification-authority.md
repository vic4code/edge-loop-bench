# ADR 016: Use one trusted Docker authority for v0.7 qualification gold

- Status: Accepted; implementation required before calibration
- Date: 2026-07-16

## Context

The selected-sample qualification evidence format already requires 60 clean
gold replays, but its replay facts are typed inputs rather than proof that the
frozen Docker boundary actually produced them. The episode runner also accepts
raw `CandidateMaterial` as gold, so production callers can bypass source
capabilities, image pins, resource limits, and cleanup attestation while still
supplying a structurally valid object.

Gold commands and final evaluator material are private. They must not become
representable in prompts, public journals, exceptions, `repr`, or serialized
authorization records. At the same time, calibration and strict confirmatory
scoring need an in-memory gold value whose provenance can be distinguished
from caller-created candidate material.

## Decision

Introduce `intercode_v07_docker_qualification.py` as the sole trusted authority
for selected-task Docker qualification and production gold issuance.

1. The authority accepts an exact verified `InterCodeSource`, the exact frozen
   30-task v0.7 sample, and one verifier-sealed `VerifiedV07ImageSet`, plus the
   frozen evaluator identity, a Docker boundary, and a
   `DockerActionExecutor`. The image set binds the clean committed source
   inventory, four-image build plan, append-only build manifest, reopened build
   verification, exact `fs1` through `fs4` image IDs, and reviewed
   state-normalizer source/revision into one path-free root. Raw image mappings
   and caller-selected normalizer hashes are not production inputs. Gold is
   resolved only inside this authority from the source-owned
   `PrivateTaskReference`; no API accepts a gold string.
2. It executes the exact ordered `(task_id, replay_index)` matrix of 30 tasks by
   two replays. Each replay binds authority revision
   `intercode-v0.7-docker-qualification-authority-v2` and uses the exact
   manifest `V07_RUN_ID_POLICY_REVISION`: a qualification campaign digest,
   one-based matrix episode index, and role `qualification` produce
   `v07-<first20-lowercase-sha256>`. The 60 IDs are deterministic, unique,
   path-free, and within the Docker boundary's 24-character policy.
3. Every replay freezes `DockerLimits` to 512 MiB memory, no container swap,
   a 16 MiB `fsize` ulimit, the aggregate storage mode
   `sampled-size-rw-no-hard-quota-v1` with a 256 MiB writable-layer watchdog,
   one CPU, 64 PIDs, `nofile=1024`, and `nproc=64`. `DockerActionLimits` are
   exactly 10 seconds, 4096 private bytes, 2048 visible bytes, 4096-byte reads,
   an eight-chunk queue, a 0.25-second writable-layer sampling interval, and a
   1-second probe timeout. The watchdog is a fail-closed safety guard, not a
   hard aggregate quota. The already attested Docker image and CLI policies
   supply `linux/arm64` and network `none`.
4. Before creation, the exact run label must have no resources. The authority
   then requires one fresh exact container whose returned spec and image match,
   starts that same container, collects the initial trusted state, executes the
   private gold exactly once through `DockerActionExecutor`, converts the
   private streams and final trusted state through the same function used by
   `DockerAttemptBoundary`, removes only the exact container, and independently
   proves the run label is empty afterward. Any ambiguity fails closed.
5. Only authority-sealed `V07QualificationReplay` facts cross into the existing
   63-record qualification evidence builder. The authority builds and then
   independently verifies that evidence. Raw gold, host paths, Docker error
   details, and raw collector material never enter the qualification journal or
   public evidence.
6. After both replays for a task agree on exit policy, initial state,
   normalized stdout/stderr, and observable state, the authority issues one
   `V07TrustedGoldMaterial`. This immutable in-memory object is construction
   sealed, redacted in `repr`, non-copyable, and nonserializable. It binds task,
   source capability, image, evaluator, normalizer, and both replay receipts,
   while retaining the agreed `CandidateMaterial` privately. Production
   calibration and strict-scoring boundaries accept this sealed type, never a
   raw `CandidateMaterial`.
7. Lifecycle, container, and replay receipts are domain-separated hashes of
   public identities and trusted result digests. They contain no gold text,
   raw output, container name, identifier, or host path. All authority errors
   have one redacted public message and redacted `repr`.
8. The same authority separately issues calibration gold for exactly
   `bash-calibration-000` through `bash-calibration-003` from one agreed pair of
   clean replays per task. These upstream quickstart queries target the
   `/testbed` and `textfile1.txt` fixtures created by
   `setup_nl2b_fs_1.sh`; therefore their exact Docker policy is the admitted
   `fs1` image ID and the `fs1` collector profile. This eight-replay operation
   returns only sealed `V07TrustedGoldMaterial` and makes no selected-sample or
   public qualification claim. Formal-task IDs, later calibration IDs, and any
   caller-supplied mixture are rejected.

No qualification implementation performs work at import time, and tests use
typed fakes; this slice makes no real Docker, model, or network calls.

The image-set seal proves internal provenance consistency and reproducibility;
it is not a cryptographic signature against a malicious operator who controls
the local process, repository, and raw artifact store.

## Alternatives considered

### Let callers construct replay facts

Rejected. Builder sealing validates shape and matrix consistency but does not
prove a real clean Docker lifecycle or private source-capability resolution.

### Return raw `CandidateMaterial` from qualification

Rejected. It is indistinguishable from caller-created material and would keep
the production strict-scoring boundary forgeable.

### Put gold or Docker diagnostics in the qualification journal

Rejected. Gold is evaluator-only, diagnostics may contain host paths or private
bytes, and neither is needed to verify the public evidence roots.

### Reuse one container for both replays

Rejected. Qualification requires independent fresh lifecycle and container
identities with explicit absence before and after each replay.

## Consequences

- Qualification requires exactly 60 container lifecycles and action executions.
- A missing resource-absence proof, pin mismatch, policy failure, collector
  mismatch, cleanup ambiguity, or disagreement between duplicate gold replays
  aborts without issuing evidence or trusted gold.
- Calibration and formal scoring gain a provenance-bearing gold type while
  controllers and public artifacts remain unable to represent the gold value.
- Changing Docker limits, action limits, material conversion, run-ID policy, or
  receipt domains requires a new authority revision and new qualification.
