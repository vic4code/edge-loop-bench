from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from dataclasses import fields, replace
from hashlib import sha256
from pathlib import Path

from edgeloopbench.intercode_campaign_ledger import CAMPAIGN_TASK_IDS
from edgeloopbench.intercode_replay_environment import (
    V07_STRICT_REPLAY_EVALUATOR_SHA256,
)
from edgeloopbench.intercode_source import load_intercode_source
from edgeloopbench.intercode_v07_qualification import (
    V07_QUALIFICATION_NETWORK_MODE,
    V07_QUALIFICATION_PLATFORM,
    V07QualificationError,
    V07QualificationReplay,
    VerifiedV07QualificationEvidence,
    _issue_trusted_v07_qualification_replay,
    build_v07_qualification_evidence,
    verify_v07_qualification_evidence,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


class V07QualificationEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = load_intercode_source(PROJECT_ROOT)
        cls.tasks = {task.task_id: task for task in cls.source.tasks}

    def setUp(self) -> None:
        self.images = {
            stratum: digest(f"image-{stratum}")
            for stratum in ("fs1", "fs2", "fs3", "fs4")
        }
        self.normalizer = digest("v0.7-state-normalizer")
        self.provenance = {
            "source_inventory_sha256": digest("source-inventory"),
            "build_plan_sha256": digest("build-plan"),
            "build_manifest_sha256": digest("build-manifest"),
            "build_verification_sha256": digest("build-verification"),
            "image_set_sha256": digest("image-set"),
            "state_normalization_revision": (
                "intercode-v0.7-state-normalization-v1"
            ),
            "state_normalization_source_sha256": digest("normalizer-source"),
        }

    def replay(
        self,
        task_id: str,
        replay_index: int,
        *,
        output_variant: str = "stable",
    ) -> V07QualificationReplay:
        task = self.tasks[task_id]
        reference = self.source.private_reference(task_id)
        bound_task, capability = self.source.qualification_identity(reference)
        self.assertEqual(bound_task, task)
        return _issue_trusted_v07_qualification_replay(
            task_id=task_id,
            stratum=task.stratum,
            replay_index=replay_index,
            source_capability_sha256=capability,
            image_id=self.images[task.stratum],
            platform=V07_QUALIFICATION_PLATFORM,
            network_mode=V07_QUALIFICATION_NETWORK_MODE,
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
            state_normalization_sha256=self.normalizer,
            lifecycle_identity_sha256=digest(
                f"lifecycle-{task_id}-{replay_index}"
            ),
            container_identity_sha256=digest(
                f"container-{task_id}-{replay_index}"
            ),
            container_absent_before=True,
            clean_initial_state=True,
            container_profile_match=True,
            infrastructure_valid=True,
            setup_valid=True,
            evaluator_valid=True,
            gold_replay_passed=True,
            exit_policy_sha256=digest(f"exit-{task_id}"),
            initial_state_sha256=digest(f"initial-{task.stratum}"),
            normalized_stdout_sha256=digest(
                f"stdout-{task_id}-{output_variant}"
            ),
            normalized_stderr_sha256=digest(f"stderr-{task_id}"),
            observable_state_sha256=digest(f"state-{task_id}"),
            container_destroyed=True,
            container_absent_after=True,
            cleanup_verified=True,
        )

    def mutate(
        self,
        replay: V07QualificationReplay,
        **changes: object,
    ) -> V07QualificationReplay:
        facts = {field.name: getattr(replay, field.name) for field in fields(replay)}
        facts.update(changes)
        return _issue_trusted_v07_qualification_replay(**facts)

    def complete_replays(self) -> tuple[V07QualificationReplay, ...]:
        return tuple(
            self.replay(task_id, replay_index)
            for task_id in CAMPAIGN_TASK_IDS
            for replay_index in (1, 2)
        )

    def build(
        self,
        path: Path,
        replays: tuple[V07QualificationReplay, ...] | None = None,
    ) -> VerifiedV07QualificationEvidence:
        return build_v07_qualification_evidence(
            source=self.source,
            journal_path=path,
            **self.provenance,
            image_id_by_stratum=self.images,
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
            state_normalization_sha256=self.normalizer,
            replays=self.complete_replays() if replays is None else replays,
        )

    def test_builds_exact_30_by_2_sealed_mode_0600_public_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / "qualification.jsonl"
            evidence = self.build(journal)

            self.assertIs(type(evidence), VerifiedV07QualificationEvidence)
            self.assertEqual(evidence.task_count, 30)
            self.assertEqual(evidence.replay_count, 60)
            self.assertEqual(evidence.qualified_task_ids, CAMPAIGN_TASK_IDS)
            self.assertEqual(evidence.platform, "linux/arm64")
            self.assertEqual(evidence.network_mode, "none")
            self.assertEqual(
                dict(evidence.image_id_by_stratum),
                self.images,
            )
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
            lines = journal.read_bytes().splitlines()
            self.assertEqual(len(lines), 63)
            self.assertEqual(json.loads(lines[-1])["type"], "journal_sealed")

            public = evidence.to_public_record()
            encoded = json.dumps(public, sort_keys=True)
            for forbidden in (
                "gold_command",
                "normalized_stdout",
                "normalized_stderr",
                "observable_state",
                str(journal),
                temporary,
            ):
                self.assertNotIn(forbidden, encoded)
            self.assertEqual(public["evidence_root_sha256"], evidence.evidence_root_sha256)
            with self.assertRaisesRegex(V07QualificationError, "builder-sealed"):
                replace(evidence, task_count=29)
            with self.assertRaises(AttributeError):
                evidence.task_count = 29  # type: ignore[misc]

            reopened = verify_v07_qualification_evidence(
                source=self.source,
                journal_path=journal,
                **self.provenance,
                image_id_by_stratum=self.images,
                evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                state_normalization_sha256=self.normalizer,
            )
            self.assertEqual(reopened.to_public_record(), public)

    def test_requires_the_exact_frozen_replay_matrix_and_order(self) -> None:
        replays = self.complete_replays()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(V07QualificationError, "exact frozen order"):
                self.build(Path(temporary) / "missing.jsonl", replays[:-1])
            with self.assertRaisesRegex(V07QualificationError, "exact frozen order"):
                self.build(
                    Path(temporary) / "reordered.jsonl",
                    (replays[1], replays[0], *replays[2:]),
                )

    def test_binds_source_capability_images_evaluator_and_normalizer(self) -> None:
        replays = list(self.complete_replays())
        cases = (
            (
                self.mutate(
                    replays[0],
                    source_capability_sha256=digest("forged-capability"),
                ),
                "source capability",
            ),
            (self.mutate(replays[0], image_id=digest("other-image")), "image"),
            (
                self.mutate(
                    replays[0], evaluator_sha256=digest("other-evaluator")
                ),
                "evaluator",
            ),
            (
                self.mutate(
                    replays[0],
                    state_normalization_sha256=digest("other-normalizer"),
                ),
                "normalizer",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, (replacement, message) in enumerate(cases):
                with self.subTest(message=message):
                    altered = tuple([replacement, *replays[1:]])
                    with self.assertRaisesRegex(V07QualificationError, message):
                        self.build(Path(temporary) / f"case-{index}.jsonl", altered)

    def test_requires_native_isolation_validity_and_verified_cleanup(self) -> None:
        original = self.complete_replays()
        cases = (
            (self.mutate(original[0], platform="linux/amd64"), "platform"),
            (self.mutate(original[0], network_mode="bridge"), "network"),
            (self.mutate(original[0], clean_initial_state=False), "clean initial"),
            (
                self.mutate(original[0], infrastructure_valid=False),
                "infrastructure",
            ),
            (self.mutate(original[0], gold_replay_passed=False), "gold replay"),
            (
                self.mutate(original[0], container_absent_after=False),
                "cleanup",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, (replacement, message) in enumerate(cases):
                with self.subTest(message=message):
                    replays = tuple([replacement, *original[1:]])
                    with self.assertRaisesRegex(V07QualificationError, message):
                        self.build(Path(temporary) / f"case-{index}.jsonl", replays)

    def test_requires_unique_lifecycle_and_container_identities(self) -> None:
        original = list(self.complete_replays())
        cases = (
            (
                self.mutate(
                    original[1],
                    lifecycle_identity_sha256=original[0].lifecycle_identity_sha256,
                ),
                "lifecycle identity",
            ),
            (
                self.mutate(
                    original[1],
                    container_identity_sha256=original[0].container_identity_sha256,
                ),
                "container identity",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, (replacement, message) in enumerate(cases):
                with self.subTest(message=message):
                    altered = tuple([original[0], replacement, *original[2:]])
                    with self.assertRaisesRegex(V07QualificationError, message):
                        self.build(Path(temporary) / f"case-{index}.jsonl", altered)

    def test_requires_replay_pair_equality_for_all_strict_surfaces(self) -> None:
        original = list(self.complete_replays())
        fields = (
            "exit_policy_sha256",
            "normalized_stdout_sha256",
            "normalized_stderr_sha256",
            "observable_state_sha256",
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, field in enumerate(fields):
                with self.subTest(field=field):
                    original[1] = self.mutate(
                        original[1], **{field: digest(f"drift-{field}")}
                    )
                    with self.assertRaisesRegex(V07QualificationError, "replays disagree"):
                        self.build(
                            Path(temporary) / f"case-{index}.jsonl",
                            tuple(original),
                        )
                    original[1] = self.replay(CAMPAIGN_TASK_IDS[0], 2)

    def test_replay_facts_cannot_be_forged_with_direct_or_replace_construction(self) -> None:
        replay = self.complete_replays()[0]
        facts = {field.name: getattr(replay, field.name) for field in fields(replay)}
        with self.assertRaisesRegex(V07QualificationError, "trusted-adapter-sealed"):
            V07QualificationReplay(**facts)  # type: ignore[arg-type]
        with self.assertRaisesRegex(V07QualificationError, "trusted-adapter-sealed"):
            replace(replay, cleanup_verified=False)

    def test_rejects_unsealed_tampered_or_wrong_mode_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "good.jsonl"
            self.build(good)
            payload = good.read_bytes()

            unsealed = root / "unsealed.jsonl"
            unsealed.write_bytes(b"\n".join(payload.splitlines()[:-1]) + b"\n")
            os.chmod(unsealed, 0o600)
            with self.assertRaisesRegex(V07QualificationError, "sealed"):
                verify_v07_qualification_evidence(
                    source=self.source,
                    journal_path=unsealed,
                    **self.provenance,
                    image_id_by_stratum=self.images,
                    evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                    state_normalization_sha256=self.normalizer,
                )

            wrong_mode = root / "wrong-mode.jsonl"
            wrong_mode.write_bytes(payload)
            os.chmod(wrong_mode, 0o644)
            with self.assertRaisesRegex(V07QualificationError, "mode 0600"):
                verify_v07_qualification_evidence(
                    source=self.source,
                    journal_path=wrong_mode,
                    **self.provenance,
                    image_id_by_stratum=self.images,
                    evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                    state_normalization_sha256=self.normalizer,
                )

            symlink = root / "symlink.jsonl"
            symlink.symlink_to(good)
            with self.assertRaisesRegex(V07QualificationError, "non-symlink"):
                verify_v07_qualification_evidence(
                    source=self.source,
                    journal_path=symlink,
                    **self.provenance,
                    image_id_by_stratum=self.images,
                    evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                    state_normalization_sha256=self.normalizer,
                )

            tampered = root / "tampered.jsonl"
            altered = payload.replace(b'"cleanup_verified":true', b'"cleanup_verified":false', 1)
            tampered.write_bytes(altered)
            os.chmod(tampered, 0o600)
            with self.assertRaisesRegex(V07QualificationError, "hash"):
                verify_v07_qualification_evidence(
                    source=self.source,
                    journal_path=tampered,
                    **self.provenance,
                    image_id_by_stratum=self.images,
                    evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                    state_normalization_sha256=self.normalizer,
                )

    def test_refuses_existing_journal_and_non_distinct_four_image_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "existing.jsonl"
            existing.write_text("user-data", encoding="utf-8")
            with self.assertRaisesRegex(V07QualificationError, "already exists"):
                self.build(existing)
            self.assertEqual(existing.read_text(encoding="utf-8"), "user-data")

            duplicate_images = dict(self.images)
            duplicate_images["fs4"] = duplicate_images["fs1"]
            with self.assertRaisesRegex(V07QualificationError, "four distinct"):
                build_v07_qualification_evidence(
                    source=self.source,
                    journal_path=root / "duplicate-images.jsonl",
                    **self.provenance,
                    image_id_by_stratum=duplicate_images,
                    evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                    state_normalization_sha256=self.normalizer,
                    replays=self.complete_replays(),
                )


if __name__ == "__main__":
    unittest.main()
