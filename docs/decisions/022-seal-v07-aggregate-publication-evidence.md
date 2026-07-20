# ADR 022: Seal one aggregate v0.7 publication-evidence authority

- Status: accepted
- Date: 2026-07-16

## Context

The formal campaign verifier authenticates the campaign ledger and 240
controller journals. The execution-envelope verifier independently reopens the
240 crash-safe execution records. The intervention verifier seals actual human,
orchestrator, and operational actions. Each verifier is necessary, but a
reporting caller could still select separately valid outputs from different
study bindings or bypass the execution and intervention gates by passing raw
campaign evidence directly to the frozen effectiveness analysis.

Publication needs one final authority that proves all evidence surfaces belong
to the exact pre-formal authorities sealed by ADR 019. This is an evidence
composition decision only; it does not alter the frozen estimands, bootstrap,
McNemar test, controller behavior, prompt budgets, or serving configuration.

## Decision

Add `intercode_v07_study_evidence.py` as the only v0.7 publication and report
entry point.

`verify_v07_study_evidence` accepts an exact verifier-sealed
`V07PreparedStudy`, never caller-supplied roots. It obtains
`prepared.bound_campaign_spec` and then, in order:

1. verifies the sealed formal campaign and all 240 controller journals with
   `verify_campaign_evidence`;
2. independently verifies the exact 240 execution envelopes with
   `verify_v07_execution_envelope_set`; and
3. reconstructs the Docker daemon identity from the prepared host pins and
   requires every formal before/after sample to match it; and
4. verifies the final sealed intervention journal, reconstructs its declaration
   evidence identity, and requires both declaration identities to equal the
   prepared study binding.

Any campaign, schedule, study-binding, controller-set, envelope-set, or final
intervention mismatch fails closed. Errors at this boundary contain no source,
runtime, evaluator, or evidence path.

The verifier emits `VerifiedV07StudyEvidence`, a sealed and path-free aggregate
that retains:

- authorization, qualification-campaign, calibration-gold-campaign,
  runtime-session, manifest, execution-pins, formal-campaign, and study-binding
  roots from ADR 019;
- the prepared host-identity root and exact Docker-daemon identity root;
- formal campaign, controller-log-set, and execution-envelope-set roots;
- final intervention declaration, journal, and summary roots; and
- exact 240/240 cardinalities plus automatic model-prompt and operator-event
  accounting.

Its aggregate digest is domain-separated by
`intercode-v0.7-study-evidence-v4`. It retains the verifier-issued component
objects privately so later validation can recheck component-to-root equality;
its canonical record contains no filesystem path, task query, model response,
command, observation, gold material, evaluator path, or outcome details.

ADR 029 supersedes this aggregate schema identity with
`intercode-v0.7-study-evidence-v5` before scoring. Revision v5 retains v4's
privacy and authority checks and additionally binds model-issued, replayed,
and total physical environment-action accounting.

Revision v4 additionally requires the exact manifest-pinned tokenizer artifact
SHA-256 for each model in frozen model order. The campaign verifier derives the
same map from all controller preflights and the aggregate verifier rejects a
campaign whose internally consistent tokenizer differs from the prepared
manifest.

Controller verification also treats any `infrastructure_invalid` event as
publication-fatal, derives `run_status` from the exact terminal-reason set, and
binds candidate-1 equality across arms to completion tokens and progress reward
as well as response identity. Environment topology is causal evidence:
Independent Sampling must create and close one `attempt-N` environment around
each action, while Direct, Raw, and Engineered arms must keep one shared
`episode` environment around all actions. The verifier checks create/action/
close order rather than accepting matching aggregate counts.

Execution envelopes are reopened relative to one already opened directory
descriptor using no-follow semantics and pre/post inode identity checks. A
directory-name swap cannot redirect the verifier to another envelope set.

`analyze_v07_study_effectiveness` accepts only
`VerifiedV07StudyEvidence`, revalidates the aggregate, and then delegates to the
already frozen v0.7 effectiveness math. The lower-level campaign analysis
function remains for its existing unit boundary, but it is not a publication
or report API. README, JSON, and HTML generation for v0.7 must start from the
aggregate wrapper and its result.

## Prompt-count interpretation

The aggregate's `automatic_model_prompt_count` is derived only from the 240
verified controller journals through `VerifiedCampaignEvidence`. It is not read
from the operator journal. Benchmark-model human prompts, orchestrator
instructions or approvals, and operational actions remain separate counters.
Operational preload, unload, restart, and reconciliation events are therefore
not mislabeled as benchmark prompts or test-time scaling calls.

## Alternatives considered

### Let reporting accept three verifier results independently

Rejected. Type correctness would not prove that the three results share the
same bound schedule, campaign ledger, controller set, or declaration identity.

### Analyze `VerifiedCampaignEvidence` directly and add caveats in README

Rejected. Documentation cannot enforce the mandatory execution-envelope and
intervention gates. A distinct aggregate type makes an incomplete report path
fail at the API boundary.

### Copy the effectiveness calculations into the aggregate module

Rejected. Duplicating the frozen estimand and inference code would create a
second statistical implementation that could drift. The aggregate wrapper
delegates after authority validation.

## Verification

Focused tests use a complete synthetic 240-episode campaign, 240 controller
journals, 240 execution envelopes, a sealed intervention journal, and local
in-memory qualification/runtime fixtures. They make no real Docker, Ollama,
model-generation, or network call:

```sh
PYTHONPATH=src python3 -m unittest \
  tests.test_intercode_v07_study_evidence \
  tests.test_intercode_v07_execution_evidence \
  tests.test_intercode_v07_analysis -v
python3 -m compileall -q \
  src/edgeloopbench/intercode_v07_study_evidence.py \
  tests/test_intercode_v07_study_evidence.py
```

The tests cover the complete bound path, raw-type rejection, aggregate-root
forgery, another study binding's campaign, prepared-host identity drift,
final intervention identity drift,
path-free canonical evidence, exact 240/240 cardinality, prompt accounting, and
the aggregate-only publication analysis wrapper.

## Consequences

- A complete campaign ledger alone cannot authorize a v0.7 publication.
- Every reported effectiveness result is traceable to one qualification,
  calibration, runtime, manifest, campaign, controller, envelope, and
  intervention authority set.
- Agent-effectiveness analysis remains separate from serving-efficiency
  evidence; this aggregate adds no throughput, latency, prefix-cache, or energy
  claim.
- Partial or reboot-interrupted runs remain useful raw evidence but cannot
  produce `VerifiedV07StudyEvidence` or a publication analysis.
- The authority proves internally consistent, reproducible evidence under the
  declared local process boundary. It is not tamper-proof against a malicious
  operator who controls the process and all raw files; publication must retain
  the raw append-only artifacts and source commit for external audit.
