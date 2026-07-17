from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edgeloopbench import intercode_v07_interventions as intervention_module
from edgeloopbench.intercode_campaign_ledger import (
    CAMPAIGN_MODELS,
    CAMPAIGN_TASK_IDS,
    CampaignEpisode,
    CampaignSpec,
    advance_campaign,
)
from edgeloopbench.intercode_replay_environment import (
    V07_STRICT_REPLAY_EVALUATOR_SHA256,
)
from edgeloopbench.intercode_source import load_intercode_source
from edgeloopbench.intercode_v07_authorization import (
    build_v07_campaign_authorization,
)
from edgeloopbench.intercode_v07_calibration import (
    V07_CALIBRATION_TASK_IDS,
    build_v07_calibration_design,
    evaluate_v07_calibration,
    evaluate_v07_planning_gate,
    verify_v07_calibration_evidence,
)
from edgeloopbench.intercode_v07_docker_qualification import (
    V07TrustedGoldMaterial,
    run_v07_docker_calibration_gold,
    run_v07_docker_qualification,
)
from edgeloopbench.intercode_v07_interventions import (
    V07InterventionError,
    VerifiedV07InterventionDeclaration,
    append_operational_action,
    declare_v07_intervention_journal,
    seal_v07_intervention_journal,
    verify_v07_intervention_declaration,
    verify_v07_intervention_journal,
)
from edgeloopbench.intercode_v07_manifest import (
    V07BudgetPins,
    V07DesignPins,
    build_v07_artifact_pins,
    build_v07_execution_pins,
    build_v07_precalibration_manifest,
)
from edgeloopbench.intercode_v07_runtime_factory import (
    V07ModelRuntime,
    V07RuntimeSession,
    attest_v07_tokenizer_helper,
    build_v07_host_identity,
    build_v07_runtime_session,
)
from edgeloopbench.intercode_v07_study_binding import (
    V07BeforeNewIntent,
    V07PreparedStudy,
    V07StudyBinding,
    V07StudyBindingError,
    prepare_v07_study,
)
from edgeloopbench.model_adapter import PHI4_MINI_RAW_PROFILE, QWEN35_RAW_PROFILE
from tests.test_intercode_v07_calibration import write_evidence_files
from tests.test_intercode_v07_docker_qualification import (
    IMAGE_IDS,
    FakeQualificationDocker,
    FakeQualificationExecutor,
    image_set,
)
from tests.test_intercode_v07_manifest import (
    execution as unrelated_execution_pins,
    source_inventory,
    source_repository_root,
)
from tests import test_intercode_v07_runtime_factory as runtime_test_module
from tests.test_intercode_v07_runtime_factory import (
    docker_fixture,
    launch_runtime,
    write_tokenizer_provenance,
)


class V07InterventionDeclarationTests(unittest.TestCase):
    def test_verifier_issues_path_free_identity_before_final_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interventions.jsonl"
            declare_v07_intervention_journal(path)
            append_operational_action(path)

            declaration = verify_v07_intervention_declaration(path)
            record = declaration.canonical_record()

        self.assertIs(type(declaration), VerifiedV07InterventionDeclaration)
        self.assertRegex(record["declaration_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn(directory, json.dumps(record, sort_keys=True))

    def test_declaration_identity_rejects_forgery_and_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interventions.jsonl"
            declare_v07_intervention_journal(path)
            declaration = verify_v07_intervention_declaration(path)

            with self.assertRaises(V07InterventionError):
                dataclasses.replace(
                    declaration,
                    declaration_sha256="sha256:" + "0" * 64,
                )

            payload = path.read_bytes().replace(
                b"controller_evidence_only",
                b"caller_supplied_prompts",
            )
            path.write_bytes(payload)
            with self.assertRaises(V07InterventionError):
                verify_v07_intervention_declaration(path)

    def test_declaration_verifier_rejects_named_file_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "interventions.jsonl"
            declare_v07_intervention_journal(path)
            original_read = intervention_module._read_all_from_descriptor

            def replace_after_read(descriptor: int, maximum_bytes: int) -> bytes:
                payload = original_read(descriptor, maximum_bytes)
                path.rename(root / "original-inode.jsonl")
                path.write_bytes(payload)
                path.chmod(0o600)
                return payload

            with mock.patch.object(
                intervention_module,
                "_read_all_from_descriptor",
                side_effect=replace_after_read,
            ), self.assertRaisesRegex(V07InterventionError, "identity changed"):
                verify_v07_intervention_declaration(path)

    def test_final_summary_retains_the_verified_declaration_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interventions.jsonl"
            declare_v07_intervention_journal(path)
            declaration = verify_v07_intervention_declaration(path)
            append_operational_action(path)
            seal_v07_intervention_journal(path)

            summary = verify_v07_intervention_journal(path)

        self.assertEqual(summary.declaration_sha256, declaration.declaration_sha256)
        self.assertEqual(
            summary.journal_instance_sha256,
            declaration.journal_instance_sha256,
        )

    def test_separate_journals_have_distinct_instance_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            declare_v07_intervention_journal(first)
            declare_v07_intervention_journal(second)

            first_declaration = verify_v07_intervention_declaration(first)
            second_declaration = verify_v07_intervention_declaration(second)

        self.assertNotEqual(
            first_declaration.journal_instance_sha256,
            second_declaration.journal_instance_sha256,
        )
        self.assertNotEqual(
            first_declaration.declaration_sha256,
            second_declaration.declaration_sha256,
        )


class V07StudyBindingConstructionTests(unittest.TestCase):
    def test_public_construction_is_closed(self) -> None:
        with self.assertRaises(V07StudyBindingError):
            V07StudyBinding(  # type: ignore[call-arg]
                authorization_sha256="sha256:" + "1" * 64,
                qualification_campaign_sha256="sha256:" + "2" * 64,
                calibration_gold_campaign_sha256="sha256:" + "3" * 64,
                runtime_session_sha256="sha256:" + "4" * 64,
                intervention_declaration_sha256="sha256:" + "5" * 64,
                intervention_declaration_evidence_sha256="sha256:" + "6" * 64,
                intervention_journal_instance_sha256="sha256:" + "c" * 64,
                manifest_sha256="sha256:" + "7" * 64,
                execution_pins_sha256="sha256:" + "8" * 64,
                formal_schedule_sha256="sha256:" + "9" * 64,
                formal_campaign_sha256="sha256:" + "a" * 64,
                study_binding_sha256="sha256:" + "b" * 64,
            )

        self.assertTrue(dataclasses.is_dataclass(V07PreparedStudy))


class V07PreparedStudyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        binary = cls.root / "ollama"
        helper = cls.root / "llama-tokenize"
        binary.write_bytes(b"pinned runtime")
        helper.write_bytes(b"pinned tokenizer")
        binary.chmod(0o755)
        helper.chmod(0o755)
        cls.runtime = launch_runtime(binary)
        tokenizer = attest_v07_tokenizer_helper(
            helper_path=helper,
            provenance_path=write_tokenizer_provenance(helper),
        )
        cls.qwen = runtime_test_module.V07RuntimeFactoryTests.build_bundle(
            profile=QWEN35_RAW_PROFILE,
            runtime=cls.runtime,
            root=cls.root,
            tokenizer_attestation=tokenizer,
        )
        cls.phi = runtime_test_module.V07RuntimeFactoryTests.build_bundle(
            profile=PHI4_MINI_RAW_PROFILE,
            runtime=cls.runtime,
            root=cls.root,
            tokenizer_attestation=tokenizer,
        )
        docker_pins, docker_daemon = docker_fixture()
        host_identity = build_v07_host_identity(
            docker_pins=docker_pins,
            docker_daemon=docker_daemon,
            runtime_receipt=cls.runtime.receipt,
        )
        cls.runtime_session = build_v07_runtime_session(
            models=(cls.qwen, cls.phi),
            host_identity=host_identity,
        )
        inventory = source_inventory()
        cls.execution_pins = build_v07_execution_pins(
            source_inventory=inventory,
            host_identity=host_identity,
        )
        source = load_intercode_source()
        cls.image_set = image_set(
            source_inventory_sha256=cls.execution_pins.source_inventory_sha256
        )
        cls.qualification = run_v07_docker_qualification(
            source=source,
            journal_path=cls.root / "qualification.jsonl",
            image_set=cls.image_set,
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
            execution_pins=cls.execution_pins,
            docker_cli=FakeQualificationDocker(),  # type: ignore[arg-type]
            action_executor=FakeQualificationExecutor(),  # type: ignore[arg-type]
        )
        cls.calibration_gold = run_v07_docker_calibration_gold(
            source=source,
            image_set=cls.image_set,
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
            execution_pins=cls.execution_pins,
            docker_cli=FakeQualificationDocker(  # type: ignore[arg-type]
                task_ids=V07_CALIBRATION_TASK_IDS
            ),
            action_executor=FakeQualificationExecutor(),  # type: ignore[arg-type]
        )
        artifacts = build_v07_artifact_pins(
            source_inventory=inventory,
            qualification_evidence=cls.qualification.evidence,
        )
        cls.manifest = build_v07_precalibration_manifest(
            artifacts=artifacts,
            models=(cls.qwen.model_identity, cls.phi.model_identity),
            host_identity=host_identity,
            execution=cls.execution_pins,
            budgets=V07BudgetPins(),
            design=V07DesignPins(),
        )
        calibration_root = cls.root / "calibration"
        calibration_root.mkdir()
        journal, logs = write_evidence_files(
            calibration_root,
            manifest_sha256=cls.manifest.manifest_sha256,
            calibration_campaign_sha256=(
                cls.calibration_gold.calibration_campaign_sha256
            ),
        )
        cls.calibration_evidence = verify_v07_calibration_evidence(
            build_v07_calibration_design(source),
            precalibration_manifest_sha256=cls.manifest.manifest_sha256,
            calibration_campaign_sha256=(
                cls.calibration_gold.calibration_campaign_sha256
            ),
            calibration_journal_path=journal,
            controller_log_paths=logs,
        )
        dispositions = tuple(
            evaluate_v07_calibration(cls.calibration_evidence, model_id)
            for model_id in CAMPAIGN_MODELS
        )
        cls.authorization = build_v07_campaign_authorization(
            manifest=cls.manifest,
            qualification_evidence=cls.qualification.evidence,
            calibration_evidence=cls.calibration_evidence,
            dispositions=dispositions,
            planning_gate=evaluate_v07_planning_gate(dispositions),
            source_inventory=inventory,
        )
        cls.intervention_path = cls.root / "interventions.jsonl"
        declare_v07_intervention_journal(cls.intervention_path)
        cls.intervention_declaration = verify_v07_intervention_declaration(
            cls.intervention_path
        )
        cls.spec = CampaignSpec(CAMPAIGN_TASK_IDS)
        cls.prepared = prepare_v07_study(
            authorization=cls.authorization,
            qualification=cls.qualification,
            calibration_gold=cls.calibration_gold,
            calibration_evidence=cls.calibration_evidence,
            runtime_session=cls.runtime_session,
            intervention_declaration=cls.intervention_declaration,
            manifest=cls.manifest,
            execution_pins=cls.execution_pins,
            campaign_spec=cls.spec,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.runtime.close()
        cls._temporary.cleanup()

    @classmethod
    def prepare_with(cls, **changes: object) -> V07PreparedStudy:
        values: dict[str, object] = {
            "authorization": cls.authorization,
            "qualification": cls.qualification,
            "calibration_gold": cls.calibration_gold,
            "calibration_evidence": cls.calibration_evidence,
            "runtime_session": cls.runtime_session,
            "intervention_declaration": cls.intervention_declaration,
            "manifest": cls.manifest,
            "execution_pins": cls.execution_pins,
            "campaign_spec": cls.spec,
        }
        values.update(changes)
        return prepare_v07_study(**values)  # type: ignore[arg-type]

    def fresh_intervention_path(self, name: str) -> Path:
        path = self.root / name
        declare_v07_intervention_journal(path)
        return path

    def prepared_intervention_path(
        self, name: str
    ) -> tuple[Path, V07PreparedStudy]:
        path = self.fresh_intervention_path(name)
        declaration = verify_v07_intervention_declaration(path)
        return path, self.prepare_with(intervention_declaration=declaration)

    def test_preparation_binds_all_exact_authorities_without_paths_or_outcomes(self) -> None:
        prepared = self.prepared
        binding = prepared.binding
        record = binding.canonical_record()

        self.assertIs(type(prepared), V07PreparedStudy)
        self.assertIs(type(binding), V07StudyBinding)
        self.assertEqual(
            record["authorization_sha256"],
            self.authorization.authorization_sha256,
        )
        self.assertEqual(
            record["qualification_campaign_sha256"],
            self.qualification.qualification_campaign_sha256,
        )
        self.assertEqual(
            record["calibration_gold_campaign_sha256"],
            self.calibration_gold.calibration_campaign_sha256,
        )
        self.assertEqual(
            record["runtime_session_sha256"],
            self.runtime_session.session_sha256,
        )
        self.assertEqual(
            record["intervention_declaration_sha256"],
            self.intervention_declaration.declaration_sha256,
        )
        self.assertEqual(record["manifest_sha256"], self.manifest.manifest_sha256)
        self.assertEqual(
            record["execution_pins_sha256"],
            self.execution_pins.execution_pins_sha256,
        )
        self.assertEqual(record["formal_schedule_sha256"], self.spec.schedule_sha256)
        self.assertEqual(
            record["study_binding_sha256"],
            prepared.study_binding_sha256,
        )
        self.assertIsNone(prepared.campaign_spec.study_binding_sha256)
        self.assertEqual(
            prepared.bound_campaign_spec.study_binding_sha256,
            prepared.study_binding_sha256,
        )
        self.assertEqual(
            prepared.bound_campaign_spec.schedule_sha256,
            prepared.campaign_spec.schedule_sha256,
        )
        encoded = json.dumps(prepared.canonical_record(), sort_keys=True).lower()
        self.assertNotIn(self._temporary.name.lower(), encoded)
        for forbidden in ("_path", "trusted_gold", "gold_command", "outcome"):
            self.assertNotIn(forbidden, encoded)

    def test_operational_accessors_return_only_bound_typed_authorities(self) -> None:
        episode = self.spec.episodes[0]

        self.assertIs(self.prepared.campaign_spec, self.spec)
        self.assertIs(self.prepared.execution_pins, self.execution_pins)
        model_runtime = self.prepared.model_runtime(episode.model_id)
        trusted_gold = self.prepared.trusted_gold_for_episode(episode)

        self.assertIs(type(model_runtime), V07ModelRuntime)
        self.assertIs(model_runtime, self.qwen)
        self.assertIs(type(trusted_gold), V07TrustedGoldMaterial)
        self.assertEqual(trusted_gold.task_id, episode.task_id)
        self.assertEqual(repr(trusted_gold), "<V07TrustedGoldMaterial redacted>")

        wrong = CampaignEpisode(
            episode.episode_index,
            episode.model_id,
            CAMPAIGN_TASK_IDS[1],
            episode.arm,
            episode.seed,
        )
        with self.assertRaisesRegex(V07StudyBindingError, "episode"):
            self.prepared.trusted_gold_for_episode(wrong)

    def test_preparation_rejects_cross_campaign_gold_and_execution_pins(self) -> None:
        images = dict(IMAGE_IDS)
        images["fs1"] = "sha256:" + "f" * 64
        mismatched_gold = run_v07_docker_calibration_gold(
            source=load_intercode_source(),
            image_set=image_set(
                images=images,
                source_inventory_sha256=(
                    self.execution_pins.source_inventory_sha256
                ),
            ),
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
            execution_pins=self.execution_pins,
            docker_cli=FakeQualificationDocker(  # type: ignore[arg-type]
                task_ids=V07_CALIBRATION_TASK_IDS
            ),
            action_executor=FakeQualificationExecutor(),  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(V07StudyBindingError, "calibration gold"):
            self.prepare_with(calibration_gold=mismatched_gold)
        with self.assertRaisesRegex(V07StudyBindingError, "execution"):
            self.prepare_with(execution_pins=unrelated_execution_pins())

    def test_before_new_intent_revalidates_source_runtime_and_declaration(self) -> None:
        path, prepared = self.prepared_intervention_path(
            "callback-interventions.jsonl"
        )
        callback = prepared.before_new_intent_callback(
            repository_root=source_repository_root(),
            intervention_journal_path=path,
        )

        self.assertIs(type(callback), V07BeforeNewIntent)
        self.assertIsNone(callback(self.spec.episodes[0]))

        target = source_repository_root() / "src/edgeloopbench/interactive_controller.py"
        original = target.read_bytes()
        try:
            target.write_bytes(b"CONTROLLER = 999\n")
            with self.assertRaisesRegex(V07StudyBindingError, "pre-intent"):
                callback(self.spec.episodes[0])
        finally:
            target.write_bytes(original)

        with mock.patch.object(
            V07RuntimeSession,
            "require_live",
            side_effect=RuntimeError("/Users/private/runtime drift"),
        ), self.assertRaises(V07StudyBindingError) as raised:
            callback(self.spec.episodes[0])
        self.assertNotIn("/Users/private", str(raised.exception))

    def test_before_new_intent_rejects_wrong_episode_and_tampered_declaration(self) -> None:
        path, prepared = self.prepared_intervention_path(
            "tampered-interventions.jsonl"
        )
        callback = prepared.before_new_intent_callback(
            repository_root=source_repository_root(),
            intervention_journal_path=path,
        )
        expected = self.spec.episodes[0]
        wrong = CampaignEpisode(
            expected.episode_index,
            expected.model_id,
            expected.task_id,
            "engineered_loop",
            expected.seed,
        )
        with self.assertRaisesRegex(V07StudyBindingError, "episode"):
            callback(wrong)

        path.write_bytes(
            path.read_bytes().replace(
                b"controller_evidence_only",
                b"caller_supplied_prompts",
            )
        )
        with self.assertRaisesRegex(V07StudyBindingError, "pre-intent"):
            callback(expected)

    def test_campaign_writes_no_declaration_or_intent_when_revalidation_fails(self) -> None:
        intervention_path, prepared = self.prepared_intervention_path(
            "pre-intent-failure-interventions.jsonl"
        )
        intervention_path.write_bytes(
            intervention_path.read_bytes().replace(
                b"controller_evidence_only",
                b"caller_supplied_prompts",
            )
        )
        callback = prepared.before_new_intent_callback(
            repository_root=source_repository_root(),
            intervention_journal_path=intervention_path,
        )
        campaign_path = self.root / "pre-intent-failure-campaign.jsonl"

        with self.assertRaisesRegex(V07StudyBindingError, "pre-intent"):
            advance_campaign(
                campaign_path,
                self.prepared.bound_campaign_spec,
                lambda _episode: self.fail("executor must not run"),  # type: ignore[arg-type]
                before_new_intent=callback,
            )

        self.assertFalse(campaign_path.exists())

    def test_prepared_binding_cannot_be_replaced_or_forged(self) -> None:
        with self.assertRaises(V07StudyBindingError):
            dataclasses.replace(
                self.prepared.binding,
                formal_campaign_sha256="sha256:" + "0" * 64,
            )
        with self.assertRaises(V07StudyBindingError):
            dataclasses.replace(self.prepared)


if __name__ == "__main__":
    unittest.main()
