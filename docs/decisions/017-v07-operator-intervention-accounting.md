# ADR 017: Journal actual v0.7 operator interventions

- Status: Accepted for v0.7 implementation
- Date: 2026-07-16

## Context

Controller evidence can derive automatic model-call counts, but it cannot show
whether a human manually prompted a benchmark model or merely approved an
orchestrator operation. Treating approvals, restarts, or unresolved handoffs as
human prompts would overstate manual effort and make manual-versus-loop
reporting uninterpretable.

## Decision

Before any v0.7 infrastructure mutation, create an exclusive mode-`0600`,
owner-owned, hash-chained intervention journal. Its first record freezes the
manifest-defined intervention revision, v0.7 study identity, exact campaign
schedule SHA-256, and a fresh random path-free `journal_instance_sha256`.
Otherwise, two empty but structurally identical journals would have been
substitutable across runs. The journal admits only these caller-entered event
categories:

1. `benchmark_model_human_prompt` — a human actually issued a prompt to a
   benchmark model;
2. `orchestrator_operator_instruction` and
   `orchestrator_operator_approval` — instructions or approvals addressed to
   the orchestrator;
3. `operational_action`, `operational_restart`, and
   `operational_reconciliation` — host/runtime execution work.

Automatic initial, independent-sample, and feedback-conditioned model prompts
are never appended through this interface. They remain derived from sealed
controller evidence. An operator approval is not a benchmark-model prompt, and
an unresolved handoff is not retroactively counted as a human prompt.

Events contain only their exact category plus optional typed phase, frozen
model ID, and exact confirmatory `CampaignEpisode` identity. They contain no
free-form note, raw prompt, action payload, command, reason, output, timestamp,
or filesystem path.

Sealing is terminal. Independent verification reopens the same regular
non-symlink file with no-follow semantics, requires exact owner/mode, bounds
bytes and records, validates canonical JSON and the complete hash chain from a
single descriptor snapshot, and rechecks named-file identity after reading.
It returns an authority-issued, path-free summary binding the journal root and
file digest, study/schedule identities, the exact journal-instance identity,
and detached counts by exact category and phase. The pre-formal study binding
retains that same instance identity and publication verification requires the
sealed summary to match it. Public construction, `dataclasses.replace`,
cross-run empty-journal substitution, tampering, symlink or mode drift, and path
replacement during verification are rejected.

## Interface and verification

`declare_v07_intervention_journal`, six category-specific append functions,
`seal_v07_intervention_journal`, and `verify_v07_intervention_journal` are the
only public mutation/verification operations. The implementation uses only the
Python standard library and the existing journal framing primitives.

Focused tests run with:

```sh
PYTHONPATH=src python3 -m unittest tests.test_intercode_v07_interventions -v
python3 -m compileall -q \
  src/edgeloopbench/intercode_v07_interventions.py \
  tests/test_intercode_v07_interventions.py
```

Tests use temporary files only. They perform no model, network, Docker, Ollama,
or benchmark-environment operation.

## Consequences

The measured report can state observed manual benchmark prompts separately
from orchestrator supervision and operational recovery. It still cannot infer
counterfactual human effort or convert unresolved work into invented prompts.
