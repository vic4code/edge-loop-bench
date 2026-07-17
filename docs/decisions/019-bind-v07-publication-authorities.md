# ADR 019: Bind every v0.7 publication authority before formal intents

- Status: accepted
- Date: 2026-07-16

## Context

The pre-calibration manifest, campaign authorization, trusted Docker
qualification, calibration-gold replay, runtime session, and intervention
journal each issue a valid root. Validity in isolation is insufficient: a
caller could combine qualification gold from one campaign, calibration
evidence from another, a live runtime from a different manifest, and a formal
schedule that happens to have the right shape. Such a run would be internally
well-typed but would not be publishable evidence for one experiment.

The intervention journal has a second timing constraint. Its final verified
summary exists only after the journal is sealed, while the declaration must be
bound before the first confirmatory intent. Accepting a caller-supplied digest
at that boundary would prove only digest syntax, not journal authority.

## Decision

Introduce `intercode_v07_study_binding.py` as the publication-composition
boundary.

`prepare_v07_study` accepts only the exact verifier- or builder-issued types:

- `V07CampaignAuthorization`;
- `V07DockerQualificationResult` and `V07CalibrationGoldResult`;
- `VerifiedV07CalibrationEvidence`;
- `V07RuntimeSession`;
- `VerifiedV07InterventionDeclaration`;
- `V07PrecalibrationManifest` and `V07ExecutionPins`; and
- the frozen 30-task `CampaignSpec`.

It fails closed unless qualification evidence agrees with both authorization
and manifest, calibration evidence carries the exact calibration-gold campaign
root and authorization roots, execution pins agree with authorization and
manifest, the runtime session's host and two model identities agree with the
manifest, and the intervention declaration and formal schedule agree with the
same study.

The resulting `V07StudyBinding` is verifier-sealed and path-free. Its canonical
record contains only these identities and their domain-separated aggregate
roots:

- authorization SHA-256;
- qualification-campaign SHA-256;
- calibration-gold-campaign SHA-256;
- runtime-session SHA-256;
- intervention declaration and declaration-evidence SHA-256 values;
- manifest and execution-pins SHA-256 values;
- formal schedule and formal-campaign SHA-256 values; and
- final `study_binding_sha256`.

The final study-binding root must be written into the production campaign
declaration and every execution envelope. A legacy unbound ledger may remain
available for old unit tests, but an unbound ledger cannot pass the v0.7
publication verifier.

Before a new intent, `V07PreparedStudy.before_new_intent_callback` runs inside
the campaign lock. It requires an exact scheduled episode, revalidates the
clean committed source through the retained authorization, requires the exact
runtime session to remain live, securely reopens the still-unsealed
intervention journal, and requires its verifier-issued declaration identity to
match the binding. It serializes no repository or journal path. The local
one-argument callback handle carries those paths only for the operational
revalidation call and has no canonical record.

The prepared study exposes only the operational handles needed by the formal
orchestrator: the schedule-only `campaign_spec`, exact `execution_pins`, exact
model lookup delegated to the sealed runtime session, and
`trusted_gold_for_episode`. Gold lookup first requires equality with the frozen
episode at that schedule index and returns only `V07TrustedGoldMaterial`, whose
contents remain opaque. Public task resolution deliberately stays outside this
object. The production attempt factory must resolve only the scheduled
`episode.task_id` from the manifest-pinned InterCode source, require its task,
stratum, image, and capability identities to agree with the bound qualification
authority, and never pass the opaque material or evaluator path to the
controller.

`verify_v07_intervention_declaration` is a read-only pre-seal verifier. It uses
the same bounded, no-follow, owner/mode, hash-chain, and named-file identity
checks as final intervention verification, then emits an unforgeable path-free
declaration identity. The sealed `VerifiedV07InterventionSummary` remains a
separate mandatory publication gate; declaration verification does not replace
final intervention accounting.

## Trust boundary

This binding consumes the exact `V07RuntimeSession` and calls its live
revalidation method. It does not independently elevate or repair runtime
authority. Runtime sufficiency remains governed by ADR 018 and its hostile
tests, including residency-boundary and mutable-component revalidation. The
study binding is necessary but not sufficient until those runtime checks and
the bound-ledger publication verifier are green.

## Verification

Focused tests use temporary files, in-memory Docker/action fakes, and a fake
managed-runtime process. They make no real Docker, Ollama, model-generation, or
network call:

```sh
PYTHONPATH=src python3 -m unittest \
  tests.test_intercode_v07_study_binding \
  tests.test_intercode_v07_interventions -v
python3 -m compileall -q \
  src/edgeloopbench/intercode_v07_study_binding.py \
  src/edgeloopbench/intercode_v07_interventions.py \
  tests/test_intercode_v07_study_binding.py
```

## Consequences

- Callers cannot authorize publication by passing unrelated raw SHA-256
  strings or separately valid authority objects.
- A reboot, source drift, runtime loss, declaration replacement, or schedule
  mismatch stops before a new durable intent.
- Reconciliation of an already-durable intent remains a separate no-model-call
  path and does not create new work.
- Canonical binding evidence contains no trusted gold, task outcome, evaluator
  path, repository path, journal path, or host-local timestamp.
