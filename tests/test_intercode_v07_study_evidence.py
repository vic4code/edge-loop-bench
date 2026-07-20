from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edgeloopbench.intercode_campaign_ledger import (
    CampaignEpisodeExecution,
    CampaignMatrix,
    load_complete_campaign_matrix,
    write_episode_execution_envelope,
)
from edgeloopbench.intercode_host_safety import DockerDaemonIdentity
from edgeloopbench.intercode_v07_interventions import (
    append_operational_action,
    declare_v07_intervention_journal,
    seal_v07_intervention_journal,
    verify_v07_intervention_declaration,
)
from edgeloopbench.intercode_v07_study_evidence import (
    V07StudyEvidenceError,
    VerifiedV07StudyEvidence,
    analyze_v07_study_effectiveness,
    verify_v07_study_evidence,
)
from tests import test_intercode_campaign_evidence as campaign_fixture
from tests import test_intercode_v07_study_binding as prepared_fixture


class V07StudyEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        prepared_fixture.V07PreparedStudyTests.setUpClass()
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.intervention_path = cls.root / "interventions.jsonl"
        declare_v07_intervention_journal(cls.intervention_path)
        declaration = verify_v07_intervention_declaration(cls.intervention_path)
        cls.prepared = prepared_fixture.V07PreparedStudyTests.prepare_with(
            intervention_declaration=declaration,
        )
        host = cls.prepared.execution_pins.host_safety.host_identity
        expected_daemon = DockerDaemonIdentity(
            binary_sha256=host.docker_binary_sha256,
            endpoint_sha256=host.docker_endpoint_sha256,
            client_version=host.docker_client_version,
            server_version=host.docker_server_version,
        )

        with mock.patch.object(
            campaign_fixture,
            "STUDY_BINDING_SHA256",
            cls.prepared.study_binding_sha256,
        ), mock.patch.object(
            campaign_fixture,
            "DOCKER_DAEMON_IDENTITY",
            expected_daemon,
        ):
            campaign_path, episode_directory, spec = campaign_fixture._build_campaign(
                cls.root / "campaign"
            )
        if spec != cls.prepared.bound_campaign_spec:
            raise AssertionError("campaign fixture did not use the prepared study")
        cls.campaign_path = campaign_path
        cls.episode_directory = episode_directory

        matrix = load_complete_campaign_matrix(campaign_path, spec)
        cls.envelope_directory = cls.root / "execution-envelopes"
        cls.envelope_directory.mkdir(mode=0o700)
        os.chmod(cls.envelope_directory, 0o700)
        for row in matrix.episodes:
            execution = CampaignEpisodeExecution(
                result=row.result,
                execution_authority_sha256=row.execution_authority_sha256,
                controller_log_sha256=row.controller_log_sha256,
                active_wall_time_ns=row.active_wall_time_ns,
                before_host_sample=row.before_host_sample,
                after_host_sample=row.after_host_sample,
            )
            write_episode_execution_envelope(
                cls.envelope_directory
                / f"episode-{row.episode.episode_index:04d}.execution.jsonl",
                row.episode,
                execution,
            )

        append_operational_action(cls.intervention_path)
        seal_v07_intervention_journal(cls.intervention_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()
        prepared_fixture.V07PreparedStudyTests.tearDownClass()

    def test_verifies_all_bound_surfaces_and_is_the_analysis_entry_point(self) -> None:
        evidence = verify_v07_study_evidence(
            self.prepared,
            campaign_journal_path=self.campaign_path,
            episode_log_directory=self.episode_directory,
            execution_envelope_directory=self.envelope_directory,
            intervention_journal_path=self.intervention_path,
        )
        analysis = analyze_v07_study_effectiveness(evidence)
        record = evidence.canonical_record()

        self.assertIs(type(evidence), VerifiedV07StudyEvidence)
        self.assertEqual(evidence.verified_episode_count, 240)
        self.assertEqual(evidence.verified_envelope_count, 240)
        self.assertEqual(
            evidence.study_binding_sha256,
            self.prepared.study_binding_sha256,
        )
        self.assertEqual(
            evidence.authorization_sha256,
            self.prepared.binding.authorization_sha256,
        )
        self.assertEqual(
            evidence.qualification_campaign_sha256,
            self.prepared.binding.qualification_campaign_sha256,
        )
        self.assertEqual(
            evidence.calibration_gold_campaign_sha256,
            self.prepared.binding.calibration_gold_campaign_sha256,
        )
        self.assertEqual(
            evidence.runtime_session_sha256,
            self.prepared.binding.runtime_session_sha256,
        )
        self.assertEqual(
            evidence.manifest_sha256,
            self.prepared.binding.manifest_sha256,
        )
        self.assertEqual(evidence.automatic_model_prompt_count, 240)
        self.assertEqual(evidence.model_issued_environment_action_count, 240)
        self.assertEqual(evidence.replayed_environment_action_count, 0)
        self.assertEqual(evidence.physical_environment_action_count, 240)
        self.assertEqual(evidence.operational_event_count, 1)
        self.assertEqual(analysis.verified_episode_count, 240)
        self.assertEqual(
            analysis.campaign_log_sha256,
            evidence.campaign_log_sha256,
        )
        self.assertNotIn(self._temporary.name, json.dumps(record, sort_keys=True))

        with self.assertRaisesRegex(ValueError, "VerifiedV07StudyEvidence"):
            analyze_v07_study_effectiveness(evidence._campaign_evidence)  # noqa: SLF001

    def test_rejects_unbound_types_and_forged_aggregate_roots(self) -> None:
        with self.assertRaisesRegex(ValueError, "V07PreparedStudy"):
            verify_v07_study_evidence(
                self.prepared.binding,  # type: ignore[arg-type]
                campaign_journal_path=self.campaign_path,
                episode_log_directory=self.episode_directory,
                execution_envelope_directory=self.envelope_directory,
                intervention_journal_path=self.intervention_path,
            )

        evidence = verify_v07_study_evidence(
            self.prepared,
            campaign_journal_path=self.campaign_path,
            episode_log_directory=self.episode_directory,
            execution_envelope_directory=self.envelope_directory,
            intervention_journal_path=self.intervention_path,
        )
        with self.assertRaises(V07StudyEvidenceError):
            dataclasses.replace(
                evidence,
                authorization_sha256="sha256:" + "0" * 64,
            )
        campaign = evidence._campaign_evidence  # noqa: SLF001
        original_binding = campaign.study_binding_sha256
        try:
            object.__setattr__(
                campaign,
                "study_binding_sha256",
                "sha256:" + "0" * 64,
            )
            with self.assertRaisesRegex(V07StudyEvidenceError, "campaign"):
                evidence.canonical_record()
        finally:
            object.__setattr__(
                campaign,
                "study_binding_sha256",
                original_binding,
            )

        original_matrix = campaign.matrix
        first = original_matrix.episodes[0]
        wrong_daemon = dataclasses.replace(
            first.before_host_sample.docker_daemon,
            binary_sha256="sha256:" + "0" * 64,
        )
        wrong_before = dataclasses.replace(
            first.before_host_sample,
            docker_daemon=wrong_daemon,
        )
        wrong_first = dataclasses.replace(
            first,
            before_host_sample=wrong_before,
        )
        try:
            object.__setattr__(
                campaign,
                "matrix",
                CampaignMatrix((wrong_first, *original_matrix.episodes[1:])),
            )
            with self.assertRaisesRegex(V07StudyEvidenceError, "host"):
                evidence.canonical_record()
        finally:
            object.__setattr__(campaign, "matrix", original_matrix)

    def test_rejects_a_campaign_from_another_study_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign, episodes, _spec = campaign_fixture._build_campaign(
                Path(temporary)
            )
            with self.assertRaisesRegex(V07StudyEvidenceError, "campaign"):
                verify_v07_study_evidence(
                    self.prepared,
                    campaign_journal_path=campaign,
                    episode_log_directory=episodes,
                    execution_envelope_directory=self.envelope_directory,
                    intervention_journal_path=self.intervention_path,
                )

    def test_rejects_campaign_tokenizer_not_pinned_by_prepared_study(self) -> None:
        from edgeloopbench import intercode_v07_study_evidence as evidence_module

        campaign = evidence_module.verify_campaign_evidence(
            self.campaign_path,
            self.episode_directory,
            self.prepared.bound_campaign_spec,
        )
        original = campaign.tokenizer_artifacts_by_model
        wrong = tuple(
            (model_id, "sha256:" + "0" * 64)
            for model_id, _artifact in original
        )
        try:
            object.__setattr__(campaign, "tokenizer_artifacts_by_model", wrong)
            with mock.patch.object(
                evidence_module,
                "verify_campaign_evidence",
                return_value=campaign,
            ), self.assertRaisesRegex(V07StudyEvidenceError, "tokenizer"):
                verify_v07_study_evidence(
                    self.prepared,
                    campaign_journal_path=self.campaign_path,
                    episode_log_directory=self.episode_directory,
                    execution_envelope_directory=self.envelope_directory,
                    intervention_journal_path=self.intervention_path,
                )
        finally:
            object.__setattr__(
                campaign,
                "tokenizer_artifacts_by_model",
                original,
            )

    def test_rejects_final_intervention_identity_drift(self) -> None:
        from edgeloopbench import intercode_v07_study_evidence as evidence_module

        actual = evidence_module.verify_v07_intervention_journal(
            self.intervention_path
        )
        object.__setattr__(
            actual,
            "declaration_sha256",
            "sha256:" + "0" * 64,
        )
        with mock.patch.object(
            evidence_module,
            "verify_v07_intervention_journal",
            return_value=actual,
        ), self.assertRaisesRegex(V07StudyEvidenceError, "intervention"):
            verify_v07_study_evidence(
                self.prepared,
                campaign_journal_path=self.campaign_path,
                episode_log_directory=self.episode_directory,
                execution_envelope_directory=self.envelope_directory,
                intervention_journal_path=self.intervention_path,
            )

    def test_rejects_substituted_empty_intervention_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replacement = Path(temporary) / "interventions.jsonl"
            declare_v07_intervention_journal(replacement)
            seal_v07_intervention_journal(replacement)

            with self.assertRaisesRegex(V07StudyEvidenceError, "intervention"):
                verify_v07_study_evidence(
                    self.prepared,
                    campaign_journal_path=self.campaign_path,
                    episode_log_directory=self.episode_directory,
                    execution_envelope_directory=self.envelope_directory,
                    intervention_journal_path=replacement,
                )


if __name__ == "__main__":
    unittest.main()
