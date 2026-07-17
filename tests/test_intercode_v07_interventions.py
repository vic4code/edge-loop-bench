from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edgeloopbench import intercode_v07_interventions as intervention_module
from edgeloopbench.intercode_campaign_ledger import (
    CAMPAIGN_MODELS,
    CAMPAIGN_TASK_IDS,
    CampaignSpec,
)
from edgeloopbench.intercode_v07_interventions import (
    V07InterventionError,
    V07InterventionPhase,
    VerifiedV07InterventionSummary,
    append_benchmark_model_human_prompt,
    append_operational_action,
    append_operational_reconciliation,
    append_operational_restart,
    append_orchestrator_operator_approval,
    append_orchestrator_operator_instruction,
    declare_v07_intervention_journal,
    seal_v07_intervention_journal,
    verify_v07_intervention_journal,
)
from edgeloopbench.journal import append_journal_event, seal_journal


class InterCodeV07InterventionTests(unittest.TestCase):
    def test_sealed_summary_separates_human_orchestrator_and_operational_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interventions.jsonl"
            episode = CampaignSpec(CAMPAIGN_TASK_IDS).episodes[0]
            declare_v07_intervention_journal(path)
            append_benchmark_model_human_prompt(
                path,
                phase=V07InterventionPhase.CALIBRATION,
                model_id=CAMPAIGN_MODELS[0],
            )
            append_orchestrator_operator_instruction(
                path,
                phase=V07InterventionPhase.PREPARATION,
            )
            append_orchestrator_operator_approval(
                path,
                phase=V07InterventionPhase.QUALIFICATION,
            )
            append_operational_action(
                path,
                phase=V07InterventionPhase.QUALIFICATION,
            )
            append_operational_restart(
                path,
                phase=V07InterventionPhase.CALIBRATION,
                model_id=CAMPAIGN_MODELS[1],
            )
            append_operational_reconciliation(
                path,
                phase=V07InterventionPhase.CONFIRMATORY,
                episode=episode,
            )
            seal_v07_intervention_journal(path)

            summary = verify_v07_intervention_journal(path)

        self.assertIs(type(summary), VerifiedV07InterventionSummary)
        self.assertEqual(summary.intervention_event_count, 6)
        self.assertEqual(summary.benchmark_model_human_prompt_count, 1)
        self.assertEqual(summary.orchestrator_operator_event_count, 2)
        self.assertEqual(summary.operational_event_count, 3)
        self.assertIsNone(summary.automatic_model_prompt_count)
        self.assertEqual(
            summary.automatic_model_prompt_source,
            "controller_evidence_only",
        )
        self.assertEqual(
            summary.counts_by_category,
            {
                "benchmark_model_human_prompt": 1,
                "operational_action": 1,
                "operational_reconciliation": 1,
                "operational_restart": 1,
                "orchestrator_operator_approval": 1,
                "orchestrator_operator_instruction": 1,
            },
        )
        self.assertEqual(summary.counts_by_phase["calibration"], 2)
        self.assertEqual(summary.counts_by_phase["qualification"], 2)
        record = summary.canonical_record()
        self.assertNotIn(directory, json.dumps(record, sort_keys=True))
        self.assertIs(record["operator_approval_is_benchmark_model_prompt"], False)
        self.assertIs(record["unresolved_handoff_is_human_prompt"], False)

    def test_automatic_prompts_and_unresolved_handoffs_cannot_be_caller_entered(self) -> None:
        for event_type in ("automatic_model_prompt", "unresolved_handoff"):
            with self.subTest(event_type=event_type), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "interventions.jsonl"
                declare_v07_intervention_journal(path)
                append_journal_event(path, {"type": event_type})
                seal_journal(path)
                with self.assertRaisesRegex(V07InterventionError, "event category"):
                    verify_v07_intervention_journal(path)

    def test_scope_is_typed_and_cannot_carry_free_form_or_mismatched_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interventions.jsonl"
            episode = CampaignSpec(CAMPAIGN_TASK_IDS).episodes[0]
            declare_v07_intervention_journal(path)
            with self.assertRaises(TypeError):
                append_operational_action(path, note="restart Docker")  # type: ignore[call-arg]
            with self.assertRaises(V07InterventionError):
                append_operational_action(path, phase="calibration")  # type: ignore[arg-type]
            with self.assertRaises(V07InterventionError):
                append_operational_action(path, model_id="other-model")
            with self.assertRaises(V07InterventionError):
                append_operational_action(
                    path,
                    model_id=CAMPAIGN_MODELS[1],
                    episode=episode,
                )

    def test_declaration_is_exclusive_mode_0600_and_symlink_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "interventions.jsonl"
            declare_v07_intervention_journal(path)
            self.assertEqual(os.stat(path, follow_symlinks=False).st_mode & 0o777, 0o600)
            with self.assertRaises(V07InterventionError):
                declare_v07_intervention_journal(path)

            real = root / "real.jsonl"
            real.write_bytes(b"")
            link = root / "link.jsonl"
            link.symlink_to(real)
            with self.assertRaises(V07InterventionError):
                declare_v07_intervention_journal(link)
            with self.assertRaises(V07InterventionError):
                verify_v07_intervention_journal(link)

    def test_verifier_rejects_mode_hash_chain_and_named_file_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            wrong_mode = root / "wrong-mode.jsonl"
            declare_v07_intervention_journal(wrong_mode)
            seal_v07_intervention_journal(wrong_mode)
            wrong_mode.chmod(0o644)
            with self.assertRaisesRegex(V07InterventionError, "mode 0600"):
                verify_v07_intervention_journal(wrong_mode)

            tampered = root / "tampered.jsonl"
            declare_v07_intervention_journal(tampered)
            append_operational_action(tampered)
            seal_v07_intervention_journal(tampered)
            tampered.write_bytes(tampered.read_bytes().replace(b"operational_action", b"operational_restart"))
            with self.assertRaises(V07InterventionError):
                verify_v07_intervention_journal(tampered)

            replaced = root / "replaced.jsonl"
            declare_v07_intervention_journal(replaced)
            seal_v07_intervention_journal(replaced)
            original_read = intervention_module._read_all_from_descriptor

            def replace_after_read(descriptor: int, maximum_bytes: int) -> bytes:
                payload = original_read(descriptor, maximum_bytes)
                replaced.rename(root / "original-inode.jsonl")
                replaced.write_bytes(payload)
                replaced.chmod(0o600)
                return payload

            with mock.patch.object(
                intervention_module,
                "_read_all_from_descriptor",
                side_effect=replace_after_read,
            ), self.assertRaisesRegex(V07InterventionError, "identity changed"):
                verify_v07_intervention_journal(replaced)

    def test_summary_cannot_be_forged_or_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interventions.jsonl"
            declare_v07_intervention_journal(path)
            seal_v07_intervention_journal(path)
            summary = verify_v07_intervention_journal(path)

        with self.assertRaises(V07InterventionError):
            dataclasses.replace(summary)
        forged = object.__new__(VerifiedV07InterventionSummary)
        for field in dataclasses.fields(summary):
            object.__setattr__(forged, field.name, getattr(summary, field.name))
        with self.assertRaisesRegex(V07InterventionError, "builder-issued"):
            forged.canonical_record()


if __name__ == "__main__":
    unittest.main()
