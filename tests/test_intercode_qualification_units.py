from __future__ import annotations

import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from edgeloopbench.intercode_qualification_units import (
    QualificationIncompleteError,
    QualificationUnitError,
    QualificationUnitExpectation,
    QualificationUnitKey,
    QualificationUnitResult,
    QualificationUnitStatus,
    _QualificationAggregateEvidence,
    _create_synthetic_unit_repository,
    _reverify_aggregate_evidence,
    _synthetic_acquire_receipt,
    _synthetic_cleanup_receipt,
)


def digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def expectations() -> tuple[QualificationUnitExpectation, ...]:
    shared = {
        "suite_name": "InterCode-Bash-qualified@c3e46d8",
        "source_revision": "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8",
        "source_population_sha256": digest("population"),
        "source_corpus_sha256": digest("corpus"),
        "static_exclusion_audit_sha256": digest("audit"),
        "evaluator_sha256": digest("evaluator"),
        "state_normalization_sha256": digest("normalizer"),
    }
    return (
        QualificationUnitExpectation(
            task_id="bash-fs1-000",
            stratum="fs1",
            replay_index=1,
            source_capability_sha256=digest("cap-a"),
            image_sha256=digest("image-fs1"),
            **shared,
        ),
        QualificationUnitExpectation(
            task_id="bash-fs1-000",
            stratum="fs1",
            replay_index=2,
            source_capability_sha256=digest("cap-b"),
            image_sha256=digest("image-fs1"),
            **shared,
        ),
    )


def result(label: str, *, infrastructure_valid: bool = True) -> QualificationUnitResult:
    return QualificationUnitResult(
        infrastructure_valid=infrastructure_valid,
        setup_valid=True,
        network_required=False,
        state_supported=True,
        evaluator_valid=True,
        exit_policy_passed=True,
        official_reward=1.0,
        strict_success=True,
        initial_state_sha256=digest("initial"),
        normalized_output_sha256=digest(f"output-{label}"),
        observable_state_sha256=digest(f"state-{label}"),
    )


def acquire(attempt: object, label: str) -> tuple[object, str, str]:
    lifecycle = digest(f"lifecycle-{label}")
    container = digest(f"container-{label}")
    intent = attempt.record_acquire_intent(  # type: ignore[attr-defined]
        lifecycle_identity_sha256=lifecycle,
        planned_locator_sha256=digest(f"locator-{label}"),
    )
    receipt = _synthetic_acquire_receipt(
        intent,
        container_identity_sha256=container,
        image_sha256=attempt.binding.image_sha256,  # type: ignore[attr-defined]
        profile_sha256=digest("profile"),
        acquisition_receipt_sha256=digest(f"acquired-{label}"),
    )
    attempt.record_acquire_completion(intent, receipt)  # type: ignore[attr-defined]
    return intent, lifecycle, container


def cleanup(
    attempt: object,
    label: str,
    *,
    lifecycle: str,
    container: str | None,
    recovery: bool = False,
    present_before: bool = True,
    absent_after: bool = True,
    identity_match: bool = True,
    profile_match: bool = True,
    ambiguous: bool = False,
) -> None:
    intent = attempt.record_release_intent(recovery=recovery)  # type: ignore[attr-defined]
    receipt = _synthetic_cleanup_receipt(
        intent,
        lifecycle_identity_sha256=lifecycle,
        container_identity_sha256=container,
        container_present_before=present_before,
        container_absent_after=absent_after,
        identity_match=identity_match,
        profile_match=profile_match,
        ambiguous=ambiguous,
        cleanup_receipt_sha256=digest(f"cleanup-{label}"),
    )
    attempt.record_release_completion(intent, receipt)  # type: ignore[attr-defined]


def complete_attempt(repo: object, key: QualificationUnitKey, label: str, value=None):
    attempt = repo.open_or_start(key)  # type: ignore[attr-defined]
    _intent, lifecycle, container = acquire(attempt, label)
    attempt.record_result(value or result(label))
    cleanup(
        attempt,
        label,
        lifecycle=lifecycle,
        container=container,
    )
    attempt.mark_completed()
    attempt.seal()
    return attempt


class QualificationUnitJournalTests(unittest.TestCase):
    def test_complete_unit_has_exact_event_order_private_inode_and_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _create_synthetic_unit_repository(Path(directory), expectations())
            key = expectations()[0].key
            attempt = complete_attempt(repo, key, "a")

            inspection = attempt.inspect(require_sealed=True)
            self.assertEqual(inspection.status, QualificationUnitStatus.COMPLETED)
            self.assertEqual(inspection.generation, 0)
            self.assertEqual(attempt.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(attempt.path.stat().st_nlink, 1)
            self.assertEqual(
                inspection.event_types,
                (
                    "qualification_unit_started",
                    "container_acquire_intent",
                    "container_acquire_completed",
                    "qualification_unit_result",
                    "container_release_intent",
                    "container_release_completed",
                    "qualification_unit_completed",
                    "journal_sealed",
                ),
            )

    def test_bound_attempt_rejects_mode_link_and_inode_replacement(self) -> None:
        for mutation in ("mode", "link", "replace"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                repo = _create_synthetic_unit_repository(Path(directory), expectations())
                attempt = repo.open_or_start(expectations()[0].key)
                before = attempt.path.read_bytes()
                if mutation == "mode":
                    attempt.path.chmod(0o640)
                elif mutation == "link":
                    os.link(attempt.path, Path(directory) / "second-link")
                else:
                    attempt.path.unlink()
                    attempt.path.write_bytes(before)
                    attempt.path.chmod(0o600)

                with self.assertRaises(QualificationUnitError):
                    attempt.record_acquire_intent(
                        lifecycle_identity_sha256=digest("lifecycle"),
                        planned_locator_sha256=digest("locator"),
                    )

    def test_key_and_generation_are_deterministic_and_bounded(self) -> None:
        key = QualificationUnitKey("bash-fs1-000", 1)
        self.assertEqual(key, QualificationUnitKey("bash-fs1-000", 1))
        self.assertEqual(key.sha256, QualificationUnitKey("bash-fs1-000", 1).sha256)
        for generation in (-1, 2, True):
            with self.subTest(generation=generation):
                with self.assertRaises(ValueError):
                    expectations()[0].binding(generation)  # type: ignore[arg-type]

    def test_sealed_completed_unit_is_reopened_and_never_started_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _create_synthetic_unit_repository(root, expectations())
            key = expectations()[0].key
            completed = complete_attempt(repo, key, "a")
            before = completed.path.read_bytes()

            reopened_repo = _create_synthetic_unit_repository(root, expectations())
            reopened = reopened_repo.open_or_start(key)
            self.assertEqual(
                reopened.inspect(require_sealed=True).status,
                QualificationUnitStatus.COMPLETED,
            )
            with self.assertRaises(QualificationUnitError):
                reopened.record_acquire_intent(
                    lifecycle_identity_sha256=digest("new-lifecycle"),
                    planned_locator_sha256=digest("new-locator"),
                )
            self.assertEqual(reopened.path.read_bytes(), before)

    def test_pre_result_interruption_seals_aborted_before_one_generation_one_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _create_synthetic_unit_repository(Path(directory), expectations())
            key = expectations()[0].key
            interrupted = repo.open_or_start(key)
            _intent, lifecycle, container = acquire(interrupted, "g0")
            cleanup(
                interrupted,
                "g0-reconcile",
                lifecycle=lifecycle,
                container=container,
                recovery=True,
            )
            interrupted.mark_aborted()
            interrupted.seal()

            retry = repo.start_retry(key)
            self.assertEqual(retry.binding.generation, 1)
            complete_attempt(repo, key, "g1")
            self.assertEqual(
                repo.open_generation(key, 0).inspect(require_sealed=True).status,
                QualificationUnitStatus.ABORTED,
            )
            self.assertEqual(
                repo.open_generation(key, 1).inspect(require_sealed=True).status,
                QualificationUnitStatus.COMPLETED,
            )

    def test_interrupted_acquire_reconciles_container_found_by_planned_locator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = expectations()
            repo = _create_synthetic_unit_repository(root, expected)
            key = expected[0].key
            attempt = repo.open_or_start(key)
            lifecycle = digest("pending-create-lifecycle")
            attempt.record_acquire_intent(
                lifecycle_identity_sha256=lifecycle,
                planned_locator_sha256=digest("pending-create-locator"),
            )

            reopened = _create_synthetic_unit_repository(root, expected).open_or_start(
                key
            )
            discovered_container = digest("container-found-by-locator")
            cleanup(
                reopened,
                "pending-create-recovery",
                lifecycle=lifecycle,
                container=discovered_container,
                recovery=True,
                present_before=True,
            )
            reopened.mark_aborted()
            reopened.seal()

            inspection = reopened.inspect(require_sealed=True)
            self.assertEqual(QualificationUnitStatus.ABORTED, inspection.status)
            self.assertEqual(
                discovered_container,
                inspection.container_identity_sha256,
            )
            self.assertEqual(1, repo.start_retry(key).binding.generation)

    def test_second_pre_result_interruption_makes_qualification_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _create_synthetic_unit_repository(Path(directory), expectations())
            key = expectations()[0].key
            first = repo.open_or_start(key)
            first_intent = first.record_acquire_intent(
                lifecycle_identity_sha256=digest("g0-life"),
                planned_locator_sha256=digest("g0-locator"),
            )
            cleanup(
                first,
                "g0-absent",
                lifecycle=digest("g0-life"),
                container=None,
                recovery=True,
                present_before=False,
            )
            del first_intent
            first.mark_aborted()
            first.seal()
            repo.start_retry(key).record_acquire_intent(
                lifecycle_identity_sha256=digest("g1-life"),
                planned_locator_sha256=digest("g1-locator"),
            )

            with self.assertRaises(QualificationIncompleteError):
                repo.start_retry(key)

    def test_durable_result_is_retained_after_exact_recovery_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _create_synthetic_unit_repository(Path(directory), expectations())
            key = expectations()[0].key
            attempt = repo.open_or_start(key)
            _intent, lifecycle, container = acquire(attempt, "durable")
            expected_result_digest = attempt.record_result(result("durable"))

            reopened = _create_synthetic_unit_repository(
                Path(directory), expectations()
            ).open_or_start(key)
            cleanup(
                reopened,
                "durable-recovery",
                lifecycle=lifecycle,
                container=container,
                recovery=True,
            )
            reopened.mark_completed()
            reopened.seal()

            inspection = reopened.inspect(require_sealed=True)
            self.assertEqual(inspection.status, QualificationUnitStatus.COMPLETED)
            self.assertEqual(inspection.result_event_sha256, expected_result_digest)

    def test_identity_mismatch_or_ambiguous_cleanup_is_incomplete(self) -> None:
        cases = (
            {"identity_match": False},
            {"profile_match": False},
            {"ambiguous": True},
            {"absent_after": False},
        )
        for index, replacement in enumerate(cases):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as directory:
                repo = _create_synthetic_unit_repository(Path(directory), expectations())
                attempt = repo.open_or_start(expectations()[0].key)
                _intent, lifecycle, container = acquire(attempt, f"bad-{index}")
                attempt.record_result(result(f"bad-{index}"))

                with self.assertRaises(QualificationIncompleteError):
                    cleanup(
                        attempt,
                        f"bad-{index}",
                        lifecycle=lifecycle,
                        container=container,
                        **replacement,
                    )
                self.assertFalse(attempt.inspect().sealed)

    def test_unquarantined_partial_tail_is_incomplete_and_never_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _create_synthetic_unit_repository(Path(directory), expectations())
            key = expectations()[0].key
            attempt = repo.open_or_start(key)
            tail = b'{"type":"container_acquire_intent"'
            with attempt.path.open("ab") as stream:
                stream.write(tail)
                stream.flush()
                os.fsync(stream.fileno())

            with self.assertRaises(QualificationIncompleteError):
                repo.start_retry(key)
            self.assertTrue(attempt.path.read_bytes().endswith(tail))

    def test_typed_infrastructure_invalid_result_completes_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = _create_synthetic_unit_repository(Path(directory), expectations())
            key = expectations()[0].key
            attempt = complete_attempt(
                repo,
                key,
                "infra-invalid",
                result("infra-invalid", infrastructure_valid=False),
            )
            inspection = attempt.inspect(require_sealed=True)
            self.assertFalse(inspection.result.infrastructure_valid)  # type: ignore[union-attr]
            with self.assertRaises(QualificationIncompleteError):
                repo.start_retry(key)


class QualificationAggregateUnitTests(unittest.TestCase):
    def test_aggregate_rereads_all_keys_in_canonical_order_and_seals_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = expectations()
            repo = _create_synthetic_unit_repository(root / "units", expected)
            complete_attempt(repo, expected[0].key, "a")
            complete_attempt(repo, expected[1].key, "b")

            evidence = repo.seal_aggregate(root / "aggregate.jsonl")
            self.assertIsNot(type(evidence), _QualificationAggregateEvidence)
            with self.assertRaises(TypeError):
                evidence.__class__ = _QualificationAggregateEvidence
            self.assertRegex(evidence.aggregate_root_sha256, r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(evidence.aggregate_recovery_count, 0)
            self.assertEqual(
                tuple((replay.task_id, replay.replay_index) for replay in evidence._replays),
                tuple((item.task_id, item.replay_index) for item in expected),
            )
            self.assertEqual((root / "aggregate.jsonl").stat().st_mode & 0o777, 0o600)
            _reverify_aggregate_evidence(evidence, expected)

    def test_aggregate_counts_one_sealed_aborted_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = expectations()
            repo = _create_synthetic_unit_repository(root / "units", expected)
            first = repo.open_or_start(expected[0].key)
            first.record_acquire_intent(
                lifecycle_identity_sha256=digest("old-life"),
                planned_locator_sha256=digest("old-locator"),
            )
            cleanup(
                first,
                "old-absent",
                lifecycle=digest("old-life"),
                container=None,
                recovery=True,
                present_before=False,
            )
            first.mark_aborted()
            first.seal()
            repo.start_retry(expected[0].key)
            complete_attempt(repo, expected[0].key, "new")
            complete_attempt(repo, expected[1].key, "other")

            evidence = repo.seal_aggregate(root / "aggregate.jsonl")
            self.assertEqual(evidence.aggregate_recovery_count, 1)

    def test_aggregate_rejects_missing_extra_unsealed_or_duplicate_generation(self) -> None:
        cases = ("missing", "extra", "unsealed", "illegal-generation-one")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                expected = expectations()
                repo = _create_synthetic_unit_repository(root / "units", expected)
                if case != "missing":
                    complete_attempt(repo, expected[0].key, "a")
                if case not in ("unsealed", "missing"):
                    complete_attempt(repo, expected[1].key, "b")
                elif case == "unsealed":
                    repo.open_or_start(expected[1].key).record_acquire_intent(
                        lifecycle_identity_sha256=digest("pending"),
                        planned_locator_sha256=digest("pending-locator"),
                    )
                if case == "extra":
                    (root / "units" / "foreign.jsonl").write_text("x", encoding="utf-8")
                if case == "illegal-generation-one":
                    repo._create_generation_for_test(expected[0].key, 1)

                with self.assertRaises(QualificationIncompleteError):
                    repo.seal_aggregate(root / "aggregate.jsonl")

    def test_aggregate_reverification_detects_unit_or_index_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = expectations()
            repo = _create_synthetic_unit_repository(root / "units", expected)
            complete_attempt(repo, expected[0].key, "a")
            complete_attempt(repo, expected[1].key, "b")
            evidence = repo.seal_aggregate(root / "aggregate.jsonl")
            object.__setattr__(
                evidence,
                "aggregate_root_sha256",
                digest("forged-root"),
            )

            with self.assertRaises(QualificationUnitError):
                _reverify_aggregate_evidence(evidence, expected)

    def test_aggregate_repr_and_public_projection_expose_no_unit_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = expectations()
            repo = _create_synthetic_unit_repository(root / "units", expected)
            complete_attempt(repo, expected[0].key, "a")
            complete_attempt(repo, expected[1].key, "b")
            evidence = repo.seal_aggregate(root / "aggregate.jsonl")

            rendered = repr(evidence)
            self.assertIn(evidence.aggregate_root_sha256, rendered)
            for forbidden in (
                "bash-fs1-000",
                "container-",
                "lifecycle-",
                str(root),
                "result_event",
            ):
                self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
