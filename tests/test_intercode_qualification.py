from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import edgeloopbench.intercode_qualification as qualification_module

from edgeloopbench.intercode_qualification import (
    CLOCK_DEPENDENT_TASK_IDS,
    MIN_QUALIFIED_COUNT,
    MIN_QUALIFIED_PER_STRATUM,
    QUALIFIED_SUITE_NAME,
    STATIC_EXCLUSION_AUDIT_SHA256,
    UNSUPPORTED_METADATA_TASK_IDS,
    GoldReplay,
    QualificationError,
    QualificationManifest,
    QualificationReason,
    _QualificationEvidenceCollector,
    _QualificationReplayResult,
    _create_docker_qualification_collector,
    build_qualification_manifest,
)
from edgeloopbench.intercode_qualification_units import (
    _QualificationAggregateEvidence,
    QualificationUnitStatus,
)
from edgeloopbench.intercode_source import (
    INTERCODE_REVISION,
    STATIC_EXCLUSION_AUDIT_RELATIVE_PATH,
    PublicBashTask,
    load_intercode_source,
)
from edgeloopbench.intercode_sampling import (
    CONFIRMATORY_QUOTAS,
    DIAGNOSTIC_QUOTAS,
    ConfirmatorySampleManifest,
    build_confirmatory_sample,
)
from edgeloopbench.intercode_schedule import (
    CONFIRMATORY_SEEDS,
    INTERACTIVE_ARMS,
    ConfirmatoryBlockSchedule,
    build_confirmatory_block_schedule,
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def image_pins() -> dict[str, str]:
    return {f"fs{number}": digest(f"image:{number}") for number in range(1, 5)}


def passing_result(task_id: str, stratum: str) -> _QualificationReplayResult:
    return _QualificationReplayResult(
        infrastructure_valid=True,
        setup_valid=True,
        network_required=False,
        state_supported=True,
        evaluator_valid=True,
        exit_policy_passed=True,
        official_reward=1.0,
        strict_success=True,
        initial_state_sha256=digest(f"initial:{stratum}"),
        normalized_output_sha256=digest(f"output:{task_id}"),
        observable_state_sha256=digest(f"state:{task_id}"),
    )


def collect_evidence(
    source: object,
    journal_path: Path,
    replacements: dict[tuple[str, int], dict[str, object]] | None = None,
) -> _QualificationAggregateEvidence:
    collector = _create_docker_qualification_collector(
        source=source,  # type: ignore[arg-type]
        journal_path=journal_path,
        image_sha256_by_stratum=image_pins(),
        evaluator_sha256=digest("evaluator-v1"),
        state_normalization_sha256=digest("state-normalization-v1"),
    )
    for task in source.tasks:  # type: ignore[attr-defined]
        reference = source.private_reference(task.task_id)  # type: ignore[attr-defined]
        for replay_index in (1, 2):
            result = passing_result(task.task_id, task.stratum)
            replacement = (replacements or {}).get((task.task_id, replay_index))
            if replacement:
                result = dataclasses.replace(result, **replacement)
            intent = collector._prepare_replay(
                task_reference=reference,
                replay_index=replay_index,
                lifecycle_identity_sha256=digest(
                    f"lifecycle:{task.task_id}:{replay_index}"
                ),
                planned_locator_sha256=digest(
                    f"locator:{task.task_id}:{replay_index}"
                ),
            )
            collector._record_acquisition(
                intent,
                container_identity_sha256=digest(
                    f"container:{task.task_id}:{replay_index}"
                ),
                profile_sha256=digest("profile-v1"),
                acquisition_receipt_sha256=digest(
                    f"acquisition:{task.task_id}:{replay_index}"
                ),
            )
            collector._record_result(intent, result)
            collector._record_cleanup(
                intent,
                container_destroyed=True,
                cleanup_verified=True,
            )
    return collector._seal()


class QualificationReplayResultValidationTests(unittest.TestCase):
    def test_result_requires_bounded_canonical_fields(self) -> None:
        valid = passing_result("bash-fs1-000", "fs1")
        invalid_values = (
            {"official_reward": math.nan},
            {"official_reward": 1.1},
            {"strict_success": 1},
            {"network_required": "false"},
            {"initial_state_sha256": "mutable"},
        )
        for replacement in invalid_values:
            with self.subTest(replacement=replacement):
                with self.assertRaises(QualificationError):
                    dataclasses.replace(valid, **replacement)


class QualificationTrustBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = load_intercode_source(PROJECT_ROOT)

    def test_raw_replay_submission_surface_is_not_public(self) -> None:
        self.assertNotIn("QualificationReplayResult", qualification_module.__all__)
        self.assertNotIn("QualificationEvidenceCollector", qualification_module.__all__)
        self.assertFalse(hasattr(qualification_module, "QualificationReplayResult"))
        self.assertFalse(hasattr(qualification_module, "QualificationEvidenceCollector"))

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(QualificationError, "trusted Docker adapter"):
                _QualificationEvidenceCollector(
                    source=self.source,
                    journal_path=Path(directory) / "forged.jsonl",
                    image_sha256_by_stratum=image_pins(),
                    evaluator_sha256=digest("eval"),
                    state_normalization_sha256=digest("norm"),
                )

    def test_direct_gold_replay_construction_is_rejected(self) -> None:
        values = {
            field.name: None
            for field in dataclasses.fields(GoldReplay)
        }
        with self.assertRaisesRegex(QualificationError, "collector-sealed"):
            GoldReplay(**values)

    def test_acquire_intent_is_durable_before_external_container_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qualification.jsonl"
            collector = _create_docker_qualification_collector(
                source=self.source,
                journal_path=path,
                image_sha256_by_stratum=image_pins(),
                evaluator_sha256=digest("evaluator-v1"),
                state_normalization_sha256=digest("state-normalization-v1"),
            )
            task = self.source.tasks[0]
            intent = collector._prepare_replay(
                task_reference=self.source.private_reference(task.task_id),
                replay_index=1,
                lifecycle_identity_sha256=digest("prepared-lifecycle"),
                planned_locator_sha256=digest("prepared-locator"),
            )
            unit_path = Path(f"{path}.units") / (
                "unit-bash-fs1-000-r1-g0.jsonl"
            )
            before_create = unit_path.read_bytes()
            self.assertIn(b'"type":"container_acquire_intent"', before_create)
            self.assertNotIn(b'"type":"container_acquire_completed"', before_create)
            with self.assertRaisesRegex(QualificationError, "acquisition completion"):
                collector._record_result(
                    intent,
                    passing_result(task.task_id, task.stratum),
                )

            collector._record_acquisition(
                intent,
                container_identity_sha256=digest("inspected-container"),
                profile_sha256=digest("inspected-profile"),
                acquisition_receipt_sha256=digest("inspected-acquisition"),
            )
            self.assertIn(
                b'"type":"container_acquire_completed"',
                unit_path.read_bytes(),
            )

    def test_pending_acquire_recovery_seals_abort_then_starts_generation_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qualification.jsonl"
            task = self.source.tasks[0]
            first = _create_docker_qualification_collector(
                source=self.source,
                journal_path=path,
                image_sha256_by_stratum=image_pins(),
                evaluator_sha256=digest("evaluator-v1"),
                state_normalization_sha256=digest("state-normalization-v1"),
            )
            lifecycle = digest("pending-lifecycle")
            first._prepare_replay(
                task_reference=self.source.private_reference(task.task_id),
                replay_index=1,
                lifecycle_identity_sha256=lifecycle,
                planned_locator_sha256=digest("pending-locator"),
            )

            recovered = _create_docker_qualification_collector(
                source=self.source,
                journal_path=path,
                image_sha256_by_stratum=image_pins(),
                evaluator_sha256=digest("evaluator-v1"),
                state_normalization_sha256=digest("state-normalization-v1"),
            )
            status = recovered._reconcile_interrupted_replay(
                task_reference=self.source.private_reference(task.task_id),
                replay_index=1,
                lifecycle_identity_sha256=lifecycle,
                container_identity_sha256=digest("found-by-planned-locator"),
                container_present_before=True,
                container_absent_after=True,
                identity_match=True,
                profile_match=True,
                ambiguous=False,
                cleanup_receipt_sha256=digest("recovery-cleanup"),
            )
            self.assertEqual(QualificationUnitStatus.ABORTED, status)
            replacement = recovered._prepare_replay(
                task_reference=self.source.private_reference(task.task_id),
                replay_index=1,
                lifecycle_identity_sha256=digest("replacement-lifecycle"),
                planned_locator_sha256=digest("replacement-locator"),
            )
            self.assertEqual(1, replacement._attempt.binding.generation)

    def test_copied_replay_lifecycle_and_container_identities_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            collector = _create_docker_qualification_collector(
                source=self.source,
                journal_path=Path(directory) / "qualification.jsonl",
                image_sha256_by_stratum=image_pins(),
                evaluator_sha256=digest("evaluator-v1"),
                state_normalization_sha256=digest("state-normalization-v1"),
            )
            task = self.source.tasks[0]
            reference = self.source.private_reference(task.task_id)
            lifecycle = digest("same-lifecycle")
            container = digest("same-container")
            first = collector._prepare_replay(
                task_reference=reference,
                replay_index=1,
                lifecycle_identity_sha256=lifecycle,
                planned_locator_sha256=digest("first-locator"),
            )
            collector._record_acquisition(
                first,
                container_identity_sha256=container,
                profile_sha256=digest("profile"),
                acquisition_receipt_sha256=digest("first-acquisition"),
            )
            collector._record_result(first, passing_result(task.task_id, task.stratum))
            collector._record_cleanup(
                first, container_destroyed=True, cleanup_verified=True
            )

            with self.assertRaisesRegex(QualificationError, "lifecycle identity"):
                collector._prepare_replay(
                    task_reference=reference,
                    replay_index=2,
                    lifecycle_identity_sha256=lifecycle,
                    planned_locator_sha256=digest("different-locator"),
                )
            second = collector._prepare_replay(
                task_reference=reference,
                replay_index=2,
                lifecycle_identity_sha256=digest("different-lifecycle"),
                planned_locator_sha256=digest("second-locator"),
            )
            with self.assertRaisesRegex(QualificationError, "container identity"):
                collector._record_acquisition(
                    second,
                    container_identity_sha256=container,
                    profile_sha256=digest("profile"),
                    acquisition_receipt_sha256=digest("second-acquisition"),
                )

    def test_failed_cleanup_is_durable_and_prevents_a_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qualification.jsonl"
            collector = _create_docker_qualification_collector(
                source=self.source,
                journal_path=path,
                image_sha256_by_stratum=image_pins(),
                evaluator_sha256=digest("evaluator-v1"),
                state_normalization_sha256=digest("state-normalization-v1"),
            )
            task = self.source.tasks[0]
            intent = collector._prepare_replay(
                task_reference=self.source.private_reference(task.task_id),
                replay_index=1,
                lifecycle_identity_sha256=digest("lifecycle"),
                planned_locator_sha256=digest("locator"),
            )
            collector._record_acquisition(
                intent,
                container_identity_sha256=digest("container"),
                profile_sha256=digest("profile"),
                acquisition_receipt_sha256=digest("acquisition"),
            )
            collector._record_result(intent, passing_result(task.task_id, task.stratum))
            with self.assertRaisesRegex(QualificationError, "cleanup"):
                collector._record_cleanup(
                    intent,
                    container_destroyed=False,
                    cleanup_verified=True,
                )
            unit_path = Path(f"{path}.units") / (
                "unit-bash-fs1-000-r1-g0.jsonl"
            )
            private_journal = unit_path.read_bytes()
            self.assertIn(b'"container_absent_after":false', private_journal)
            self.assertIn(b'"profile_match":true', private_journal)
            with self.assertRaisesRegex(QualificationError, "active replay"):
                collector._seal()


class QualificationManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = load_intercode_source(PROJECT_ROOT)
        cls._temporary = tempfile.TemporaryDirectory()
        root = Path(cls._temporary.name)

        replacements: dict[tuple[str, int], dict[str, object]] = {}
        simple_failures = (
            ("bash-fs1-000", {"network_required": True}),
            ("bash-fs1-001", {"setup_valid": False}),
            ("bash-fs1-002", {"state_supported": False}),
            ("bash-fs1-003", {"evaluator_valid": False}),
            ("bash-fs1-004", {"infrastructure_valid": False}),
            ("bash-fs1-005", {"exit_policy_passed": False}),
            ("bash-fs1-006", {"official_reward": 0.99}),
            ("bash-fs1-007", {"strict_success": False}),
        )
        for task_id, replacement in simple_failures:
            replacements[(task_id, 1)] = replacement
        replacements[("bash-fs1-020", 2)] = {
            "initial_state_sha256": digest("different:initial")
        }
        replacements[("bash-fs1-021", 2)] = {
            "normalized_output_sha256": digest("different:output")
        }
        replacements[("bash-fs1-022", 2)] = {
            "observable_state_sha256": digest("different:state")
        }
        for index in range(4):
            replacements[(f"bash-fs4-{index:03d}", 1)] = {
                "state_supported": False
            }

        # Journal durability itself has dedicated real-fsync tests. Avoid two
        # thousand synchronous disk flushes while exercising qualification
        # state-machine semantics here.
        with mock.patch("edgeloopbench.journal.os.fsync"):
            cls.passing_evidence = collect_evidence(
                cls.source, root / "passing.jsonl"
            )
            cls.failure_evidence = collect_evidence(
                cls.source,
                root / "failure.jsonl",
                replacements,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def build(
        self, evidence: _QualificationAggregateEvidence
    ) -> QualificationManifest:
        return build_qualification_manifest(
            source=self.source,
            evidence=evidence,
            image_sha256_by_stratum=image_pins(),
            evaluator_sha256=digest("evaluator-v1"),
            state_normalization_sha256=digest("state-normalization-v1"),
        )

    def test_all_passing_rows_create_ordered_admitted_manifest(self) -> None:
        manifest = self.build(self.passing_evidence)

        self.assertEqual(QUALIFIED_SUITE_NAME, manifest.suite_name)
        self.assertEqual(INTERCODE_REVISION, manifest.source_revision)
        self.assertEqual(180, manifest.qualified_count)
        self.assertEqual(
            {"fs1": 55, "fs2": 45, "fs3": 56, "fs4": 24},
            dict(manifest.qualified_by_stratum),
        )
        self.assertTrue(manifest.scoring_admitted)
        self.assertEqual(
            ("bash-fs1-000", "bash-fs1-001", "bash-fs1-002"),
            tuple(record.task_id for record in manifest.records[:3]),
        )
        for record in manifest.records:
            if record.task_id in CLOCK_DEPENDENT_TASK_IDS:
                self.assertEqual(
                    (QualificationReason.CLOCK_DEPENDENT,),
                    record.exclusion_reasons,
                )
            elif record.task_id in UNSUPPORTED_METADATA_TASK_IDS:
                self.assertEqual(
                    (QualificationReason.UNSUPPORTED_METADATA,),
                    record.exclusion_reasons,
                )
            else:
                self.assertTrue(record.included)
                self.assertEqual((), record.exclusion_reasons)
        self.assertRegex(manifest.suite_sha256, r"^sha256:[0-9a-f]{64}$")
        manifest.require_scoring_admitted()

    def test_confirmatory_hash_sample_is_deterministic_stratified_and_nested(
        self,
    ) -> None:
        qualification = self.build(self.passing_evidence)

        first = build_confirmatory_sample(qualification)
        second = build_confirmatory_sample(qualification)
        decoded = json.loads(first.canonical_bytes())

        self.assertEqual(first, second)
        self.assertEqual(50, len(first.task_ids))
        self.assertEqual(12, len(first.diagnostic_task_ids))
        self.assertEqual(
            dict(CONFIRMATORY_QUOTAS),
            {
                stratum: sum(
                    task_id.startswith(f"bash-{stratum}-")
                    for task_id in first.task_ids
                )
                for stratum in CONFIRMATORY_QUOTAS
            },
        )
        self.assertEqual(
            dict(DIAGNOSTIC_QUOTAS),
            {
                stratum: sum(
                    task_id.startswith(f"bash-{stratum}-")
                    for task_id in first.diagnostic_task_ids
                )
                for stratum in DIAGNOSTIC_QUOTAS
            },
        )
        self.assertTrue(set(first.diagnostic_task_ids) < set(first.task_ids))
        self.assertEqual(
            dict(qualification.qualified_by_stratum),
            dict(first.qualified_by_stratum),
        )
        self.assertEqual(qualification.suite_sha256, first.qualified_suite_sha256)
        self.assertRegex(first.selection_frame_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first.sample_sha256, decoded["sample_sha256"])
        serialized = first.canonical_bytes().decode("utf-8").lower()
        self.assertNotIn("gold", serialized)
        self.assertNotIn("query", serialized)
        self.assertNotIn("evidence_root", serialized)

    def test_confirmatory_sample_is_sealed_and_detects_post_build_tampering(self) -> None:
        qualification = self.build(self.passing_evidence)
        sample = build_confirmatory_sample(qualification)
        values = {
            field.name: getattr(sample, field.name)
            for field in dataclasses.fields(sample)
        }

        with self.assertRaisesRegex(ValueError, "builder-sealed"):
            ConfirmatorySampleManifest(**values)

        object.__setattr__(sample, "task_ids", tuple(reversed(sample.task_ids)))
        with self.assertRaises(ValueError):
            sample.canonical_bytes()

    def test_confirmatory_block_schedule_is_deterministic_and_balanced(self) -> None:
        sample = build_confirmatory_sample(self.build(self.passing_evidence))

        first = build_confirmatory_block_schedule(sample)
        second = build_confirmatory_block_schedule(sample)

        self.assertEqual(first, second)
        self.assertEqual(100, len(first.blocks))
        self.assertEqual(
            {(task_id, seed) for task_id in sample.task_ids for seed in CONFIRMATORY_SEEDS},
            {(block.task_id, block.replicate_seed) for block in first.blocks},
        )
        for position in range(4):
            self.assertEqual(
                Counter({arm: 25 for arm in INTERACTIVE_ARMS}),
                Counter(block.arm_order[position] for block in first.blocks),
            )
        adjacent = Counter(
            pair
            for block in first.blocks
            for pair in zip(block.arm_order, block.arm_order[1:])
        )
        self.assertEqual(
            Counter(
                {
                    (left, right): 25
                    for left in INTERACTIVE_ARMS
                    for right in INTERACTIVE_ARMS
                    if left != right
                }
            ),
            adjacent,
        )
        decoded = json.loads(first.canonical_bytes())
        self.assertEqual(first.schedule_sha256, decoded["schedule_sha256"])

    def test_confirmatory_block_schedule_is_builder_sealed_and_sample_bound(self) -> None:
        sample = build_confirmatory_sample(self.build(self.passing_evidence))
        schedule = build_confirmatory_block_schedule(sample)
        values = {
            field.name: getattr(schedule, field.name)
            for field in dataclasses.fields(schedule)
        }

        with self.assertRaisesRegex(ValueError, "builder-sealed"):
            ConfirmatoryBlockSchedule(**values)

        object.__setattr__(sample, "task_ids", tuple(reversed(sample.task_ids)))
        with self.assertRaises(ValueError):
            build_confirmatory_block_schedule(sample)

    def test_aggregation_is_deterministic_for_the_same_sealed_evidence(self) -> None:
        first = self.build(self.passing_evidence)
        second = self.build(self.passing_evidence)

        self.assertEqual(first.canonical_bytes(), second.canonical_bytes())
        self.assertEqual(first.suite_sha256, second.suite_sha256)
        decoded = json.loads(first.canonical_bytes())
        self.assertEqual(first.suite_sha256, decoded["suite_sha256"])
        self.assertNotIn("gold", first.canonical_bytes().decode("utf-8").lower())
        self.assertNotIn("command", decoded["records"][0])

    def test_aggregator_rejects_pin_drift_and_reverifies_all_unit_bytes(self) -> None:
        with self.assertRaisesRegex(QualificationError, "image pins"):
            build_qualification_manifest(
                source=self.source,
                evidence=self.passing_evidence,
                image_sha256_by_stratum={
                    **image_pins(),
                    "fs1": digest("altered-image"),
                },
                evaluator_sha256=digest("evaluator-v1"),
                state_normalization_sha256=digest("state-normalization-v1"),
            )
        with self.assertRaisesRegex(QualificationError, "evaluator pin"):
            build_qualification_manifest(
                source=self.source,
                evidence=self.passing_evidence,
                image_sha256_by_stratum=image_pins(),
                evaluator_sha256=digest("altered-evaluator"),
                state_normalization_sha256=digest("state-normalization-v1"),
            )
        with self.assertRaisesRegex(QualificationError, "normalizer pin"):
            build_qualification_manifest(
                source=self.source,
                evidence=self.passing_evidence,
                image_sha256_by_stratum=image_pins(),
                evaluator_sha256=digest("evaluator-v1"),
                state_normalization_sha256=digest("altered-normalizer"),
            )

        snapshot = self.passing_evidence._unit_snapshots[0]
        original_bytes = snapshot.journal_bytes
        object.__setattr__(
            snapshot,
            "journal_bytes",
            original_bytes.replace(b'"strict_success":true', b'"strict_success":false', 1),
        )
        try:
            with self.assertRaisesRegex(QualificationError, "reverification"):
                self.build(self.passing_evidence)
        finally:
            object.__setattr__(snapshot, "journal_bytes", original_bytes)

        original_root = self.passing_evidence.aggregate_root_sha256
        object.__setattr__(
            self.passing_evidence,
            "aggregate_root_sha256",
            digest("altered-aggregate-root"),
        )
        try:
            with self.assertRaisesRegex(QualificationError, "reverification"):
                self.build(self.passing_evidence)
        finally:
            object.__setattr__(
                self.passing_evidence,
                "aggregate_root_sha256",
                original_root,
            )

    def test_public_projection_contains_only_aggregate_private_provenance(self) -> None:
        manifest = self.build(self.passing_evidence)
        public = manifest.canonical_bytes()
        decoded = json.loads(public)
        self.assertEqual(
            self.passing_evidence.aggregate_root_sha256,
            decoded["evidence_root_sha256"],
        )
        self.assertEqual(0, decoded["aggregate_recovery_count"])
        self.assertEqual(
            STATIC_EXCLUSION_AUDIT_SHA256,
            decoded["static_exclusion_audit_sha256"],
        )
        for forbidden in (
            b"replays",
            b"initial_state_sha256",
            b"normalized_output_sha256",
            b"observable_state_sha256",
            b"lifecycle_identity_sha256",
            b"container_identity_sha256",
            b"source_capability_sha256",
        ):
            self.assertNotIn(forbidden, public)
        reference = self.source.private_reference(self.source.tasks[0].task_id)
        raw_gold = self.source.gold_for_evaluator(reference).encode("utf-8")
        self.assertNotIn(raw_gold, public)
        self.assertTrue(
            all(
                raw_gold not in snapshot.journal_bytes
                for snapshot in self.passing_evidence._unit_snapshots
            )
        )

    def test_each_dynamic_failure_has_a_machine_readable_reason(self) -> None:
        manifest = self.build(self.failure_evidence)
        by_id = {record.task_id: record for record in manifest.records}
        expected = (
            ("bash-fs1-000", QualificationReason.NETWORK_REQUIRED),
            ("bash-fs1-001", QualificationReason.SETUP_INVALID),
            ("bash-fs1-002", QualificationReason.UNSUPPORTED_STATE),
            ("bash-fs1-003", QualificationReason.EVALUATOR_INVALID),
            ("bash-fs1-004", QualificationReason.INFRASTRUCTURE_INVALID),
            ("bash-fs1-005", QualificationReason.GOLD_EXIT_POLICY_FAILED),
            ("bash-fs1-006", QualificationReason.OFFICIAL_REWARD_FAILED),
            ("bash-fs1-007", QualificationReason.STRICT_GOLD_REPLAY_FAILED),
            ("bash-fs1-020", QualificationReason.NONDETERMINISTIC_INITIAL_STATE),
            ("bash-fs1-021", QualificationReason.NONDETERMINISTIC_OUTPUT),
            ("bash-fs1-022", QualificationReason.NONDETERMINISTIC_OBSERVABLE_STATE),
        )
        for task_id, reason in expected:
            with self.subTest(task_id=task_id):
                self.assertIn(reason, by_id[task_id].exclusion_reasons)

        public = manifest.canonical_bytes().decode("utf-8")
        self.assertNotIn("diagnostic", public.lower())
        self.assertNotIn("gold_command", public.lower())

    def test_scoring_gate_enforces_every_stratum_floor(self) -> None:
        manifest = self.build(self.failure_evidence)

        self.assertGreaterEqual(manifest.qualified_count, MIN_QUALIFIED_COUNT)
        self.assertLess(
            manifest.qualified_by_stratum["fs4"],
            MIN_QUALIFIED_PER_STRATUM["fs4"],
        )
        self.assertFalse(manifest.scoring_admitted)
        with self.assertRaisesRegex(QualificationError, "fs4"):
            manifest.require_scoring_admitted()

    def test_static_exclusion_map_is_exactly_bound_to_the_audit_artifact(self) -> None:
        artifact_bytes = (
            PROJECT_ROOT / STATIC_EXCLUSION_AUDIT_RELATIVE_PATH
        ).read_bytes()
        artifact = json.loads(artifact_bytes)
        self.assertEqual(
            {
                "audit_schema_version",
                "exclusions",
                "source_corpus_sha256",
                "source_revision",
            },
            set(artifact),
        )
        self.assertNotIn("gold", artifact_bytes.decode("utf-8").lower())
        expected = {
            **{task_id: ["clock_dependent"] for task_id in CLOCK_DEPENDENT_TASK_IDS},
            **{
                task_id: ["unsupported_metadata"]
                for task_id in UNSUPPORTED_METADATA_TASK_IDS
            },
        }
        self.assertEqual(expected, artifact["exclusions"])
        self.assertEqual(
            STATIC_EXCLUSION_AUDIT_SHA256,
            "sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
        )

    def test_public_manifest_detects_post_construction_tampering(self) -> None:
        replacements = (
            ("suite name", "suite_name", "forged"),
            ("record order", "records", tuple(reversed(self.build(self.passing_evidence).records))),
            ("qualified_count", "qualified_count", 999),
            ("suite_sha256", "suite_sha256", digest("forged")),
        )
        for label, field, value in replacements:
            with self.subTest(label=label):
                manifest = self.build(self.passing_evidence)
                object.__setattr__(manifest, field, value)
                with self.assertRaises(QualificationError):
                    manifest.canonical_bytes()

    def test_manifest_and_evidence_are_not_publicly_constructible(self) -> None:
        manifest = self.build(self.passing_evidence)
        values = {
            field.name: getattr(manifest, field.name)
            for field in dataclasses.fields(manifest)
        }
        with self.assertRaisesRegex(QualificationError, "aggregator-sealed"):
            QualificationManifest(**values)
        with self.assertRaisesRegex(ValueError, "builder-sealed"):
            _QualificationAggregateEvidence(
                index_bytes=b"x",
                unit_snapshots=(),
                replays=(),
                aggregate_root_sha256=digest("root"),
                aggregate_recovery_count=0,
            )

    def test_source_validation_recomputes_public_calibration_and_private_pins(self) -> None:
        def assert_rejected(source: object, label: str) -> None:
            with self.assertRaisesRegex(QualificationError, label):
                _create_docker_qualification_collector(
                    source=source,  # type: ignore[arg-type]
                    journal_path=Path(self._temporary.name) / f"{label}.jsonl",
                    image_sha256_by_stratum=image_pins(),
                    evaluator_sha256=digest("eval"),
                    state_normalization_sha256=digest("norm"),
                )

        public_tampered = load_intercode_source(PROJECT_ROOT)
        first = public_tampered.tasks[0]
        object.__setattr__(
            public_tampered,
            "_tasks",
            (
                PublicBashTask(
                    task_id=first.task_id,
                    query=first.query + " ",
                    stratum=first.stratum,
                ),
                *public_tampered.tasks[1:],
            ),
        )
        assert_rejected(public_tampered, "public population")

        calibration_tampered = load_intercode_source(PROJECT_ROOT)
        first_calibration = calibration_tampered.calibration_tasks[0]
        object.__setattr__(
            calibration_tampered,
            "_calibration_tasks",
            (
                PublicBashTask(
                    task_id=first_calibration.task_id,
                    query=first_calibration.query + " ",
                    stratum=first_calibration.stratum,
                ),
                *calibration_tampered.calibration_tasks[1:],
            ),
        )
        assert_rejected(calibration_tampered, "calibration population")

        private_tampered = load_intercode_source(PROJECT_ROOT)
        reference = private_tampered.private_reference(private_tampered.tasks[0].task_id)
        private_gold = dict(private_tampered._gold_by_reference)  # type: ignore[attr-defined]
        private_gold[reference] += " "
        object.__setattr__(private_tampered, "_gold_by_reference", private_gold)
        assert_rejected(private_tampered, "source corpus")

    def test_source_validation_requires_exact_loader_type(self) -> None:
        source = load_intercode_source(PROJECT_ROOT)

        class SourceSubclass(type(source)):
            pass

        forged = object.__new__(SourceSubclass)
        for slot in type(source).__slots__:
            object.__setattr__(forged, slot, getattr(source, slot))
        with self.assertRaisesRegex(QualificationError, "exact InterCodeSource"):
            _create_docker_qualification_collector(
                source=forged,
                journal_path=Path(self._temporary.name) / "forged.jsonl",
                image_sha256_by_stratum=image_pins(),
                evaluator_sha256=digest("eval"),
                state_normalization_sha256=digest("norm"),
            )


if __name__ == "__main__":
    unittest.main()
