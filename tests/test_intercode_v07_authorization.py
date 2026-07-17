from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.intercode_campaign_ledger import CAMPAIGN_MODELS
from edgeloopbench.intercode_source import load_intercode_source
from edgeloopbench.intercode_v07_authorization import (
    V07_AUTHORIZATION_SCHEMA_REVISION,
    V07AuthorizationError,
    V07CampaignAuthorization,
    build_v07_campaign_authorization,
)
from edgeloopbench.intercode_v07_calibration import (
    build_v07_calibration_design,
    evaluate_v07_calibration,
    evaluate_v07_planning_gate,
    verify_v07_calibration_evidence,
)
from tests.test_intercode_v07_calibration import (
    CALIBRATION_CAMPAIGN_SHA256,
    write_evidence_files,
)
from tests.test_intercode_v07_manifest import (
    build as build_manifest,
    qualification_evidence,
    source_inventory,
    source_repository_root,
)


def admitted_bundle(root: Path):  # type: ignore[no-untyped-def]
    root.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest()
    calibration_journal, controller_logs = write_evidence_files(
        root,
        manifest_sha256=manifest.manifest_sha256,
    )
    design = build_v07_calibration_design(load_intercode_source())
    evidence = verify_v07_calibration_evidence(
        design,
        precalibration_manifest_sha256=manifest.manifest_sha256,
        calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
        calibration_journal_path=calibration_journal,
        controller_log_paths=controller_logs,
    )
    dispositions = tuple(
        evaluate_v07_calibration(evidence, model_id) for model_id in CAMPAIGN_MODELS
    )
    gate = evaluate_v07_planning_gate(dispositions)
    return manifest, evidence, dispositions, gate


class InterCodeV07AuthorizationTests(unittest.TestCase):
    def test_builder_binds_path_free_manifest_qualification_calibration_and_execution_roots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, evidence, dispositions, gate = admitted_bundle(Path(directory))

            authorization = build_v07_campaign_authorization(
                manifest=manifest,
                qualification_evidence=qualification_evidence(),
                calibration_evidence=evidence,
                dispositions=dispositions,
                planning_gate=gate,
                source_inventory=source_inventory(),
            )
            record = authorization.canonical_record()

            self.assertIs(type(authorization), V07CampaignAuthorization)
            self.assertEqual(record["schema"], V07_AUTHORIZATION_SCHEMA_REVISION)
            self.assertEqual(record["manifest_sha256"], manifest.manifest_sha256)
            self.assertEqual(
                record["qualification_evidence_root_sha256"],
                qualification_evidence().evidence_root_sha256,
            )
            self.assertEqual(
                record["qualification_suite_sha256"],
                qualification_evidence().suite_sha256,
            )
            self.assertEqual(
                record["calibration_evidence_sha256"], evidence.evidence_sha256
            )
            self.assertEqual(
                record["calibration_disposition_sha256s"],
                [item.disposition_sha256 for item in dispositions],
            )
            self.assertEqual(
                record["execution_root_sha256"],
                manifest.execution.execution_pins_sha256,
            )
            for field in (
                "authorization_sha256",
                "calibration_root_sha256",
                "code_root_sha256",
                "planning_gate_sha256",
                "qualification_root_sha256",
                "runtime_root_sha256",
            ):
                self.assertRegex(record[field], r"^sha256:[0-9a-f]{64}$")
            rendered = json.dumps(record, sort_keys=True).lower()
            for forbidden in (
                "strict_success",
                "parsed_and_admissible",
                "task_id",
                "gold",
                "/users/",
                "/tmp/",
            ):
                self.assertNotIn(forbidden, rendered)

    def test_authorization_is_builder_sealed_and_rejects_provenance_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, evidence, dispositions, gate = admitted_bundle(root / "valid")

            with self.assertRaisesRegex(V07AuthorizationError, "builder-sealed"):
                V07CampaignAuthorization(  # type: ignore[call-arg]
                    manifest_sha256=manifest.manifest_sha256,
                    qualification_evidence_root_sha256=(
                        qualification_evidence().evidence_root_sha256
                    ),
                    qualification_suite_sha256=qualification_evidence().suite_sha256,
                    qualification_root_sha256="sha256:" + "1" * 64,
                    calibration_evidence_sha256=evidence.evidence_sha256,
                    calibration_journal_sha256=evidence.calibration_journal_sha256,
                    calibration_controller_log_set_sha256=(
                        evidence.controller_log_set_sha256
                    ),
                    calibration_disposition_sha256s=tuple(
                        item.disposition_sha256 for item in dispositions
                    ),
                    planning_gate_sha256="sha256:" + "2" * 64,
                    calibration_root_sha256="sha256:" + "3" * 64,
                    source_inventory_sha256=source_inventory().inventory_sha256,
                    code_root_sha256="sha256:" + "4" * 64,
                    runtime_root_sha256="sha256:" + "5" * 64,
                    execution_root_sha256=manifest.execution.execution_pins_sha256,
                    authorization_sha256="sha256:" + "6" * 64,
                )

            with self.assertRaisesRegex(V07AuthorizationError, "model order"):
                build_v07_campaign_authorization(
                    manifest=manifest,
                    qualification_evidence=qualification_evidence(),
                    calibration_evidence=evidence,
                    dispositions=tuple(reversed(dispositions)),
                    planning_gate=gate,
                    source_inventory=source_inventory(),
                )

            forged_gate = dataclasses.replace(
                gate,
                allowed=False,
                reason="planning_bound_exceeds_18_active_hours",
            )
            with self.assertRaisesRegex(V07AuthorizationError, "planning gate"):
                build_v07_campaign_authorization(
                    manifest=manifest,
                    qualification_evidence=qualification_evidence(),
                    calibration_evidence=evidence,
                    dispositions=dispositions,
                    planning_gate=forged_gate,
                    source_inventory=source_inventory(),
                )

            other_root = root / "other"
            other_root.mkdir()
            other_journal, other_logs = write_evidence_files(
                other_root,
                manifest_sha256="sha256:" + "b" * 64,
            )
            other_evidence = verify_v07_calibration_evidence(
                build_v07_calibration_design(load_intercode_source()),
                precalibration_manifest_sha256="sha256:" + "b" * 64,
                calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                calibration_journal_path=other_journal,
                controller_log_paths=other_logs,
            )
            other_dispositions = tuple(
                evaluate_v07_calibration(other_evidence, model_id)
                for model_id in CAMPAIGN_MODELS
            )
            with self.assertRaisesRegex(V07AuthorizationError, "manifest"):
                build_v07_campaign_authorization(
                    manifest=manifest,
                    qualification_evidence=qualification_evidence(),
                    calibration_evidence=other_evidence,
                    dispositions=other_dispositions,
                    planning_gate=evaluate_v07_planning_gate(other_dispositions),
                    source_inventory=source_inventory(),
                )

    def test_both_models_and_allowed_planning_gate_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = build_manifest()
            journal, logs = write_evidence_files(
                root,
                first_parse_failures=frozenset({1, 2}),
                manifest_sha256=manifest.manifest_sha256,
            )
            evidence = verify_v07_calibration_evidence(
                build_v07_calibration_design(load_intercode_source()),
                precalibration_manifest_sha256=manifest.manifest_sha256,
                calibration_campaign_sha256=CALIBRATION_CAMPAIGN_SHA256,
                calibration_journal_path=journal,
                controller_log_paths=logs,
            )
            dispositions = tuple(
                evaluate_v07_calibration(evidence, model_id)
                for model_id in CAMPAIGN_MODELS
            )
            gate = evaluate_v07_planning_gate(dispositions)
            self.assertFalse(dispositions[0].admitted)
            self.assertFalse(gate.allowed)

            with self.assertRaisesRegex(V07AuthorizationError, "both models"):
                build_v07_campaign_authorization(
                    manifest=manifest,
                    qualification_evidence=qualification_evidence(),
                    calibration_evidence=evidence,
                    dispositions=dispositions,
                    planning_gate=gate,
                    source_inventory=source_inventory(),
                )

    def test_revalidate_rechecks_the_same_clean_committed_source_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest, evidence, dispositions, gate = admitted_bundle(Path(directory))
            authorization = build_v07_campaign_authorization(
                manifest=manifest,
                qualification_evidence=qualification_evidence(),
                calibration_evidence=evidence,
                dispositions=dispositions,
                planning_gate=gate,
                source_inventory=source_inventory(),
            )
            source_root = source_repository_root()

            self.assertIs(authorization.revalidate(source_root), authorization)

            target = source_root / "src/edgeloopbench/interactive_controller.py"
            original = target.read_bytes()
            try:
                target.write_bytes(b"CONTROLLER = 5\n")
                with self.assertRaisesRegex(V07AuthorizationError, "revalidation"):
                    authorization.revalidate(source_root)
            finally:
                target.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
