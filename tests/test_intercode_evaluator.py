from __future__ import annotations

import dataclasses
import math
import pickle
import traceback
import unittest
from unittest import mock

from edgeloopbench.intercode_evaluator import (
    MAX_NORMALIZED_OUTPUT_BYTES,
    MAX_PATH_BYTES,
    MAX_STATE_ENTRIES,
    MAX_SYMLINK_TARGET_BYTES,
    CanonicalStateSnapshot,
    CandidateObservationUnsupported,
    EvaluatorInputError,
    OfficialChange,
    OfficialChangeSnapshot,
    PrivateAttempt,
    StateEntry,
    _tfidf_similarity,
    adapted_compatible_evaluate,
    candidate_surface_failure_evaluation,
    evaluate_candidate_or_failure,
    hardlink_group_sha256,
    parse_candidate_status_tokens,
    parse_gold_status_tokens,
    strict_exact_success,
)
from edgeloopbench.interactive_environment import AttemptEvaluationKind


def digest(label: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def file_entry(
    path: str,
    content: str,
    *,
    mode: int = 0o644,
    uid: int = 0,
    gid: int = 0,
    hardlink_group: str | None = None,
) -> StateEntry:
    return StateEntry(
        path=path,
        kind="file",
        mode=mode,
        uid=uid,
        gid=gid,
        content_sha256=digest(content),
        symlink_target=None,
        hardlink_group_sha256=hardlink_group,
    )


def _default_official_changes(
    entries: tuple[StateEntry, ...],
) -> OfficialChangeSnapshot:
    changes: list[OfficialChange] = []
    for entry in entries:
        if entry.kind == "absent":
            changes.append(
                OfficialChange(
                    path=entry.path,
                    status="D",
                    hash_output_sha256=None,
                )
            )
        else:
            if entry.kind == "file":
                comparison = entry.content_sha256
            elif entry.kind == "symlink":
                comparison = digest(f"symlink:{entry.symlink_target}")
            else:
                comparison = digest(f"directory:{entry.path}")
            changes.append(
                OfficialChange(
                    path=entry.path,
                    status="??",
                    hash_output_sha256=comparison,
                )
            )
    return OfficialChangeSnapshot(tuple(changes))


def attempt(
    *entries: StateEntry,
    output: str = "done\n",
    official_changes: OfficialChangeSnapshot | None = None,
) -> PrivateAttempt:
    frozen_entries = tuple(entries)
    return PrivateAttempt(
        official_changes=(
            official_changes
            if official_changes is not None
            else _default_official_changes(frozen_entries)
        ),
        state=CanonicalStateSnapshot(entries=frozen_entries),
        normalized_output=output,
    )


class CanonicalStateSnapshotTests(unittest.TestCase):
    def test_snapshot_canonicalizes_entry_order_without_mutating_values(self) -> None:
        second = file_entry("workspace/z.txt", "second")
        first = file_entry("workspace/a.txt", "first")

        snapshot = CanonicalStateSnapshot(entries=(second, first))

        self.assertEqual(
            ("workspace/a.txt", "workspace/z.txt"),
            tuple(entry.path for entry in snapshot.entries),
        )
        self.assertEqual(digest("first"), snapshot.entries[0].content_sha256)

    def test_snapshot_rejects_duplicate_paths_even_when_entries_differ(self) -> None:
        with self.assertRaisesRegex(EvaluatorInputError, "duplicate state path"):
            CanonicalStateSnapshot(
                entries=(
                    file_entry("workspace/result.txt", "one"),
                    file_entry("workspace/result.txt", "two"),
                )
            )

    def test_state_paths_are_relative_canonical_posix_paths(self) -> None:
        invalid_paths = (
            "",
            "/workspace/result.txt",
            "workspace/../result.txt",
            "workspace/./result.txt",
            "workspace//result.txt",
            "workspace/result.txt/",
            "workspace\\result.txt",
            "workspace/result\n.txt",
            "workspace/\u202eresult.txt",
            "workspace/\x00result.txt",
            "a" * (MAX_PATH_BYTES + 1),
        )

        for path in invalid_paths:
            with self.subTest(path=repr(path)):
                with self.assertRaises(EvaluatorInputError):
                    file_entry(path, "content")

    def test_entry_fields_are_kind_specific_and_bounded(self) -> None:
        valid_entries = (
            file_entry("workspace/file.txt", "content", mode=0o755),
            StateEntry(
                path="workspace/directory",
                kind="directory",
                mode=0o750,
                uid=1000,
                gid=1000,
                content_sha256=None,
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
            StateEntry(
                path="workspace/link",
                kind="symlink",
                mode=0o777,
                uid=0,
                gid=0,
                content_sha256=None,
                symlink_target="/workspace/file.txt",
                hardlink_group_sha256=None,
            ),
            StateEntry(
                path="workspace/deleted.txt",
                kind="absent",
                mode=None,
                uid=None,
                gid=None,
                content_sha256=None,
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
        )
        self.assertEqual(4, len(CanonicalStateSnapshot(valid_entries).entries))

        invalid_entries = (
            dict(
                path="workspace/file",
                kind="file",
                mode=0o644,
                uid=0,
                gid=0,
                content_sha256="not-a-digest",
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
            dict(
                path="workspace/file",
                kind="file",
                mode=0o644,
                uid=0,
                gid=0,
                content_sha256=digest("content"),
                symlink_target="workspace/other",
                hardlink_group_sha256=None,
            ),
            dict(
                path="workspace/directory",
                kind="directory",
                mode=0o755,
                uid=0,
                gid=0,
                content_sha256=digest("content"),
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
            dict(
                path="workspace/link",
                kind="symlink",
                mode=0o777,
                uid=0,
                gid=0,
                content_sha256=None,
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
            dict(
                path="workspace/deleted",
                kind="absent",
                mode=0o644,
                uid=None,
                gid=None,
                content_sha256=None,
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
            dict(
                path="workspace/file",
                kind="socket",
                mode=0o644,
                uid=0,
                gid=0,
                content_sha256=None,
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
            dict(
                path="workspace/file",
                kind="file",
                mode=True,
                uid=0,
                gid=0,
                content_sha256=digest("content"),
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
            dict(
                path="workspace/file",
                kind="file",
                mode=0o10000,
                uid=0,
                gid=0,
                content_sha256=digest("content"),
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
            dict(
                path="workspace/file",
                kind="file",
                mode=0o644,
                uid=-1,
                gid=0,
                content_sha256=digest("content"),
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
            dict(
                path="workspace/file",
                kind="file",
                mode=0o644,
                uid=0,
                gid=True,
                content_sha256=digest("content"),
                symlink_target=None,
                hardlink_group_sha256=None,
            ),
        )
        for fields in invalid_entries:
            with self.subTest(fields=fields):
                with self.assertRaises(EvaluatorInputError):
                    StateEntry(**fields)

    def test_symlink_targets_are_container_paths_but_never_resolved(self) -> None:
        absolute = StateEntry(
            path="workspace/absolute-link",
            kind="symlink",
            mode=0o777,
            uid=0,
            gid=0,
            content_sha256=None,
            symlink_target="/workspace/target.txt",
            hardlink_group_sha256=None,
        )
        root = StateEntry(
            path="workspace/root-link",
            kind="symlink",
            mode=0o777,
            uid=0,
            gid=0,
            content_sha256=None,
            symlink_target="/",
            hardlink_group_sha256=None,
        )
        relative = StateEntry(
            path="workspace/relative-link",
            kind="symlink",
            mode=0o777,
            uid=0,
            gid=0,
            content_sha256=None,
            symlink_target="target.txt",
            hardlink_group_sha256=None,
        )
        self.assertEqual("/workspace/target.txt", absolute.symlink_target)
        self.assertEqual("/", root.symlink_target)
        self.assertEqual("target.txt", relative.symlink_target)

        invalid_targets = (
            "",
            "../target.txt",
            "/workspace/../target.txt",
            "/workspace//target.txt",
            "/workspace/target\n.txt",
            "/workspace/\u2066target.txt",
            "a" * (MAX_SYMLINK_TARGET_BYTES + 1),
        )
        for target in invalid_targets:
            with self.subTest(target=repr(target)):
                with self.assertRaises(EvaluatorInputError):
                    StateEntry(
                        path="workspace/link",
                        kind="symlink",
                        mode=0o777,
                        uid=0,
                        gid=0,
                        content_sha256=None,
                        symlink_target=target,
                        hardlink_group_sha256=None,
                    )

    def test_snapshot_rejects_mutable_wrong_typed_and_oversize_entries(self) -> None:
        with self.assertRaisesRegex(EvaluatorInputError, "entries must be a tuple"):
            CanonicalStateSnapshot(entries=[])
        with self.assertRaisesRegex(EvaluatorInputError, "StateEntry"):
            CanonicalStateSnapshot(entries=("workspace/file",))

        entries = tuple(
            StateEntry(
                path=f"workspace/deleted-{index:05d}",
                kind="absent",
                mode=None,
                uid=None,
                gid=None,
                content_sha256=None,
                symlink_target=None,
                hardlink_group_sha256=None,
            )
            for index in range(MAX_STATE_ENTRIES + 1)
        )
        with self.assertRaisesRegex(EvaluatorInputError, "too many state entries"):
            CanonicalStateSnapshot(entries=entries)

    def test_hardlink_groups_are_path_derived_without_raw_inodes(self) -> None:
        members = ("workspace/original.txt", "workspace/alias.txt")
        group = hardlink_group_sha256(members)
        snapshot = CanonicalStateSnapshot(
            entries=(
                file_entry(members[0], "shared", hardlink_group=group),
                file_entry(members[1], "shared", hardlink_group=group),
            )
        )

        self.assertEqual("sha256:", group[:7])
        self.assertEqual(group, hardlink_group_sha256(tuple(reversed(members))))
        self.assertEqual(
            {group},
            {entry.hardlink_group_sha256 for entry in snapshot.entries},
        )
        self.assertNotIn(
            "inode",
            {field.name for field in dataclasses.fields(StateEntry)},
        )

        with self.assertRaisesRegex(EvaluatorInputError, "hardlink group"):
            CanonicalStateSnapshot(
                entries=(
                    file_entry(
                        "workspace/original.txt",
                        "shared",
                        hardlink_group=digest("raw-inode-or-wrong-member-set"),
                    ),
                    file_entry(
                        "workspace/alias.txt",
                        "shared",
                        hardlink_group=digest("raw-inode-or-wrong-member-set"),
                    ),
                )
            )

        with self.assertRaisesRegex(EvaluatorInputError, "at least two"):
            CanonicalStateSnapshot(
                entries=(
                    file_entry(
                        "workspace/only.txt",
                        "shared",
                        hardlink_group=hardlink_group_sha256(
                            ("workspace/only.txt", "workspace/missing.txt")
                        ),
                    ),
                )
            )

        too_many_paths = tuple(
            f"workspace/link-{index:05d}" for index in range(MAX_STATE_ENTRIES + 1)
        )
        with self.assertRaisesRegex(EvaluatorInputError, "too many"):
            hardlink_group_sha256(too_many_paths)

    def test_hardlink_members_must_share_stable_metadata_and_content(self) -> None:
        members = ("workspace/a.txt", "workspace/b.txt")
        group = hardlink_group_sha256(members)

        with self.assertRaisesRegex(EvaluatorInputError, "hardlink members"):
            CanonicalStateSnapshot(
                entries=(
                    file_entry(members[0], "one", hardlink_group=group),
                    file_entry(members[1], "two", hardlink_group=group),
                )
            )


class PrivateAttemptTests(unittest.TestCase):
    def test_private_attempt_repr_redacts_state_and_output(self) -> None:
        private = attempt(
            file_entry("workspace/hidden-gold-name.txt", "hidden-content"),
            output="HIDDEN GOLD OUTPUT",
        )

        self.assertEqual("<PrivateAttempt redacted>", repr(private))
        self.assertNotIn("HIDDEN GOLD OUTPUT", repr(private))
        self.assertNotIn("hidden-gold-name", repr(private))
        self.assertEqual("<StateEntry redacted>", repr(private.state.entries[0]))

    def test_private_attempt_refuses_generic_serializers(self) -> None:
        private = attempt(
            file_entry("workspace/GOLD-CANARY.txt", "secret"),
            output="GOLD OUTPUT CANARY",
        )

        self.assertFalse(dataclasses.is_dataclass(private))
        for serialize in (
            dataclasses.asdict,
            vars,
            pickle.dumps,
        ):
            with self.subTest(serializer=serialize.__name__):
                with self.assertRaises(TypeError):
                    serialize(private)  # type: ignore[arg-type]

    def test_private_output_must_be_bounded_normalized_utf8_text(self) -> None:
        invalid_outputs = (
            "contains\rreturn",
            "contains\x00nul",
            "contains\x1bescape",
            "contains\u2028separator",
            "contains\u2066bidi",
            "contains\ud800surrogate",
            "x" * (MAX_NORMALIZED_OUTPUT_BYTES + 1),
        )
        for output in invalid_outputs:
            with self.subTest(output=repr(output[:30])):
                with self.assertRaises(EvaluatorInputError):
                    attempt(output=output)

        with self.assertRaisesRegex(EvaluatorInputError, "text"):
            PrivateAttempt(
                official_changes=OfficialChangeSnapshot(()),
                state=CanonicalStateSnapshot(()),
                normalized_output=b"bytes",  # type: ignore[arg-type]
            )

    def test_invalid_private_unicode_is_not_retained_in_exception_chain(self) -> None:
        canary = "GOLD-CANARY\ud800TAIL"
        try:
            attempt(output=canary)
        except EvaluatorInputError as error:
            rendered = "".join(traceback.format_exception(error))
            self.assertIsNone(error.__cause__)
            self.assertNotIn("GOLD-CANARY", rendered)
            self.assertNotIn("TAIL", rendered)
        else:  # pragma: no cover - the unsafe value must always be rejected
            self.fail("invalid private Unicode was accepted")

    def test_private_output_preserves_safe_whitespace_exactly(self) -> None:
        private = attempt(output="one\ttwo\n")
        self.assertEqual("one\ttwo\n", private.normalized_output)


class OfficialChangeSnapshotTests(unittest.TestCase):
    def test_change_units_are_sorted_and_status_specific(self) -> None:
        snapshot = OfficialChangeSnapshot(
            (
                OfficialChange("workspace/z.txt", "M", None),
                OfficialChange("workspace/new-tree/", "??", digest("tree-output")),
                OfficialChange("workspace/a.txt", "A", digest("file-output")),
            )
        )

        self.assertEqual(
            ("workspace/a.txt", "workspace/new-tree/", "workspace/z.txt"),
            tuple(change.path for change in snapshot.changes),
        )
        self.assertEqual("<OfficialChange redacted>", repr(snapshot.changes[0]))

    def test_change_units_accept_full_safe_parser_tokens_and_reject_bad_evidence(self) -> None:
        accepted = (
            ("workspace/file.txt", "R", None),
            ("workspace/file.txt", "AM", None),
            ("->", "T", None),
            ("../token-from-parser", "X", None),
            ("workspace/tracked-dir/", "M", None),
            ("workspace/candidate-only.txt", "A", None),
        )
        for path, status, hash_output in accepted:
            with self.subTest(accepted=(path, status)):
                OfficialChange(path, status, hash_output)

        invalid = (
            ("workspace/file with space.txt", "M", None),
            ("workspace/file.txt", "", None),
            ("workspace/old.txt", "R", digest("unexpected")),
            ("workspace/new.txt", "A", "not-a-digest"),
        )
        for path, status, hash_output in invalid:
            with self.subTest(path=path, status=status):
                with self.assertRaises(EvaluatorInputError):
                    OfficialChange(path, status, hash_output)  # type: ignore[arg-type]

        duplicate = OfficialChange("workspace/file.txt", "M", None)
        deduplicated = OfficialChangeSnapshot((duplicate, duplicate))
        self.assertEqual((duplicate,), deduplicated.changes)

    def test_whitespace_parser_preserves_nonstandard_pairs_and_fails_typed(self) -> None:
        self.assertEqual(
            (("old", "R"), ("new", "->"), ("typed", "T")),
            parse_candidate_status_tokens("R old -> new\nT typed\n"),
        )
        self.assertEqual(
            (("x", "M"),),
            parse_candidate_status_tokens("M x\nM x\n"),
        )
        with self.assertRaises(CandidateObservationUnsupported):
            parse_candidate_status_tokens("R old ->")
        with self.assertRaises(EvaluatorInputError) as gold_error:
            parse_gold_status_tokens("R old ->")
        self.assertNotIsInstance(gold_error.exception, CandidateObservationUnsupported)
        too_many = "".join(
            f"M path-{index}\n" for index in range(MAX_STATE_ENTRIES + 1)
        )
        with self.assertRaises(CandidateObservationUnsupported):
            parse_candidate_status_tokens(too_many)
        duplicate_bomb = "M same\n" * (MAX_STATE_ENTRIES + 1)
        self.assertEqual(
            (("same", "M"),),
            parse_candidate_status_tokens(duplicate_bomb),
        )

        fallback = candidate_surface_failure_evaluation()
        self.assertEqual(0.0, fallback.reward)
        self.assertFalse(fallback.official_success)
        self.assertEqual(
            AttemptEvaluationKind.CANDIDATE_SURFACE_FAILURE,
            fallback.evaluation_kind,
        )

    def test_composite_maps_only_candidate_build_errors_to_denominator_failure(self) -> None:
        gold = attempt(output="same")

        def unsupported() -> PrivateAttempt:
            raise CandidateObservationUnsupported("private candidate detail")

        fallback = evaluate_candidate_or_failure(unsupported, gold)
        self.assertEqual(
            AttemptEvaluationKind.CANDIDATE_SURFACE_FAILURE,
            fallback.evaluation_kind,
        )

        def infrastructure_error() -> PrivateAttempt:
            raise OSError("daemon failed")

        with self.assertRaises(OSError):
            evaluate_candidate_or_failure(infrastructure_error, gold)

        def generic_evaluator_bug() -> PrivateAttempt:
            raise EvaluatorInputError("unexpected builder bug")

        with self.assertRaisesRegex(EvaluatorInputError, "builder bug"):
            evaluate_candidate_or_failure(generic_evaluator_bug, gold)

        corrupt_gold = attempt(
            output="same",
            official_changes=OfficialChangeSnapshot(
                (OfficialChange("workspace/missing-hash", "A", None),)
            ),
        )
        with self.assertRaisesRegex(EvaluatorInputError, "gold hash"):
            evaluate_candidate_or_failure(unsupported, corrupt_gold)


class AdaptedCompatibleEvaluationTests(unittest.TestCase):
    def test_identical_state_and_output_produce_exact_official_success(self) -> None:
        candidate = attempt(
            file_entry("workspace/result.txt", "same"),
            output="completed\n",
        )
        gold = attempt(
            file_entry("workspace/result.txt", "same"),
            output="completed\n",
        )

        result = adapted_compatible_evaluate(candidate, gold)

        self.assertEqual(1.0, result.reward)
        self.assertTrue(result.official_success)
        self.assertEqual(
            AttemptEvaluationKind.EVALUATOR_DERIVED,
            result.evaluation_kind,
        )
        self.assertEqual(
            {"reward", "official_success", "evaluation_kind"},
            {field.name for field in dataclasses.fields(result)},
        )

    def test_path_diff_uses_upstream_erf_weight_and_python_rounding(self) -> None:
        candidate = attempt(
            file_entry("workspace/extra.txt", "extra"),
            output="",
        )
        gold = attempt(
            file_entry("workspace/missing.txt", "missing"),
            output="",
        )

        result = adapted_compatible_evaluate(candidate, gold)

        # Base 0.01 + p1 round(0.33 * (1-erf(2)), 2) == 0.00
        # + upstream's no-common-change default 0.33 + exact empty output 0.33.
        self.assertEqual(0.67, result.reward)
        self.assertFalse(result.official_success)

    def test_common_added_change_fraction_uses_upstream_hash_evidence(self) -> None:
        candidate = attempt(
            file_entry("workspace/correct.txt", "same"),
            file_entry("workspace/wrong.txt", "candidate", mode=0o755),
            output="alpha beta",
        )
        gold = attempt(
            file_entry("workspace/correct.txt", "same"),
            file_entry("workspace/wrong.txt", "gold", mode=0o644),
            output="gamma delta",
        )

        result = adapted_compatible_evaluate(candidate, gold)

        # p1=0.33, p2=round(0.33 * 1/2, 2)=0.17, p3=0.0.
        self.assertEqual(0.51, result.reward)

    def test_common_symlink_hash_evidence_must_match(self) -> None:
        candidate = attempt(
            StateEntry(
                path="workspace/link",
                kind="symlink",
                mode=0o777,
                uid=0,
                gid=0,
                content_sha256=None,
                symlink_target="/workspace/candidate",
                hardlink_group_sha256=None,
            ),
            output="candidate",
        )
        gold = attempt(
            StateEntry(
                path="workspace/link",
                kind="symlink",
                mode=0o755,
                uid=0,
                gid=0,
                content_sha256=None,
                symlink_target="/workspace/gold",
                hardlink_group_sha256=None,
            ),
            output="gold",
        )

        result = adapted_compatible_evaluate(candidate, gold)

        self.assertEqual(0.34, result.reward)

    def test_upstream_status_filter_skips_modified_and_deleted_content(self) -> None:
        for status in ("M", "D"):
            with self.subTest(status=status):
                candidate = attempt(
                    file_entry("workspace/existing.txt", "candidate", mode=0o755),
                    output="same",
                    official_changes=OfficialChangeSnapshot(
                        (OfficialChange("workspace/existing.txt", status, None),)
                    ),
                )
                gold = attempt(
                    file_entry("workspace/existing.txt", "gold", mode=0o644),
                    output="same",
                    official_changes=OfficialChangeSnapshot(
                        (OfficialChange("workspace/existing.txt", status, None),)
                    ),
                )

                official = adapted_compatible_evaluate(candidate, gold)

                self.assertEqual(1.0, official.reward)
                self.assertTrue(official.official_success)
                self.assertFalse(strict_exact_success(candidate, gold))

    def test_nonstandard_statuses_participate_in_diff_but_never_hash_filter(self) -> None:
        gold = attempt(
            output="same",
            official_changes=OfficialChangeSnapshot(
                (OfficialChange("workspace/item", "T", None),)
            ),
        )
        identical = attempt(
            output="same",
            official_changes=OfficialChangeSnapshot(
                (OfficialChange("workspace/item", "T", None),)
            ),
        )
        different = attempt(
            output="same",
            official_changes=OfficialChangeSnapshot(
                (OfficialChange("workspace/item", "AM", None),)
            ),
        )

        self.assertEqual(1.0, adapted_compatible_evaluate(identical, gold).reward)
        self.assertEqual(0.67, adapted_compatible_evaluate(different, gold).reward)

    def test_missing_candidate_hash_is_a_scored_mismatch_but_gold_hash_is_required(self) -> None:
        candidate = attempt(
            output="same",
            official_changes=OfficialChangeSnapshot(
                (OfficialChange("workspace/new.txt", "A", None),)
            ),
        )
        gold = attempt(
            output="same",
            official_changes=OfficialChangeSnapshot(
                (OfficialChange("workspace/new.txt", "A", digest("gold")),)
            ),
        )

        self.assertEqual(0.67, adapted_compatible_evaluate(candidate, gold).reward)
        with self.assertRaisesRegex(EvaluatorInputError, "gold hash"):
            adapted_compatible_evaluate(candidate, candidate)

    def test_upstream_added_untracked_and_copied_changes_compare_hash_output(self) -> None:
        for status in ("A", "??", "C"):
            with self.subTest(status=status):
                candidate = attempt(
                    output="same",
                    official_changes=OfficialChangeSnapshot(
                        (OfficialChange("workspace/new.txt", status, digest("candidate")),)
                    ),
                )
                gold = attempt(
                    output="same",
                    official_changes=OfficialChangeSnapshot(
                        (OfficialChange("workspace/new.txt", status, digest("gold")),)
                    ),
                )

                official = adapted_compatible_evaluate(candidate, gold)

                self.assertEqual(0.67, official.reward)
                self.assertFalse(official.official_success)

    def test_official_change_units_preserve_untracked_directory_weighting(self) -> None:
        directory_change = OfficialChangeSnapshot(
            (OfficialChange("workspace/new-tree/", "??", digest("recursive-output")),)
        )
        candidate = attempt(
            file_entry("workspace/new-tree/a.txt", "a"),
            file_entry("workspace/new-tree/b.txt", "b"),
            output="same",
            official_changes=directory_change,
        )
        gold = attempt(
            file_entry("workspace/new-tree/a.txt", "a"),
            file_entry("workspace/new-tree/b.txt", "b"),
            output="same",
            official_changes=directory_change,
        )

        official = adapted_compatible_evaluate(candidate, gold)

        self.assertEqual(1, len(candidate.official_changes.changes))
        self.assertEqual(1.0, official.reward)

    def test_metadata_and_hardlink_topology_are_strict_only(self) -> None:
        paths = ("workspace/original.txt", "workspace/alias.txt")
        group = hardlink_group_sha256(paths)
        official_changes = OfficialChangeSnapshot(
            (
                OfficialChange(paths[0], "??", digest("hash-output-a")),
                OfficialChange(paths[1], "??", digest("hash-output-b")),
            )
        )
        hardlinked = attempt(
            file_entry(paths[0], "same", mode=0o755, uid=1000, hardlink_group=group),
            file_entry(paths[1], "same", mode=0o755, uid=1000, hardlink_group=group),
            output="same",
            official_changes=official_changes,
        )
        copied = attempt(
            file_entry(paths[0], "same", mode=0o644, uid=0),
            file_entry(paths[1], "same", mode=0o644, uid=0),
            output="same",
            official_changes=official_changes,
        )

        self.assertTrue(adapted_compatible_evaluate(hardlinked, copied).official_success)
        self.assertFalse(strict_exact_success(hardlinked, copied))

    def test_tfidf_matches_sklearn_default_reference_fixture(self) -> None:
        similarity = _tfidf_similarity(
            "The quick brown fox",
            "the quick blue fox",
        )

        # Reference from sklearn TfidfVectorizer defaults used by pinned
        # InterCode: smooth idf log((1+n)/(1+df))+1 and L2 normalization.
        self.assertAlmostEqual(0.6029748160380571, similarity, places=15)
        self.assertEqual(0.20, round(0.33 * similarity, 2))

        result = adapted_compatible_evaluate(
            attempt(output="The quick brown fox"),
            attempt(output="the quick blue fox"),
        )
        # Upstream rounds components but does not round their final float sum.
        self.assertEqual(0.8700000000000001, result.reward)

    def test_tfidf_matches_token_case_count_and_empty_vocabulary(self) -> None:
        self.assertEqual(1.0, _tfidf_similarity("Alpha alpha", "alpha ALPHA"))
        self.assertEqual(1.0, _tfidf_similarity("a !", "a !"))
        self.assertEqual(0.0, _tfidf_similarity("a !", "b ?"))
        self.assertEqual(0.0, _tfidf_similarity("", "two tokens"))
        self.assertEqual(1.0, _tfidf_similarity("", ""))

    def test_public_result_cannot_expose_gold_or_component_diagnostics(self) -> None:
        gold = attempt(
            file_entry("workspace/GOLD-CANARY.txt", "secret"),
            output="GOLD OUTPUT CANARY",
        )
        result = adapted_compatible_evaluate(attempt(output="candidate"), gold)

        serialized_public_values = repr(dataclasses.asdict(result))
        self.assertNotIn("GOLD-CANARY", serialized_public_values)
        self.assertNotIn("GOLD OUTPUT CANARY", serialized_public_values)
        self.assertNotIn("diff", serialized_public_values.lower())
        self.assertNotIn("path", serialized_public_values.lower())

    def test_official_tfidf_stop_can_disagree_with_strict_exact_output(self) -> None:
        candidate = attempt(output="Alpha Beta")
        gold = attempt(output="beta alpha")

        public_result = adapted_compatible_evaluate(candidate, gold)

        self.assertEqual(1.0, public_result.reward)
        self.assertTrue(public_result.official_success)
        self.assertFalse(strict_exact_success(candidate, gold))

    def test_wrong_input_and_nonfinite_internal_reward_fail_closed(self) -> None:
        with self.assertRaisesRegex(EvaluatorInputError, "PrivateAttempt"):
            adapted_compatible_evaluate(
                "candidate",  # type: ignore[arg-type]
                attempt(),
            )

        with mock.patch(
            "edgeloopbench.intercode_evaluator._tfidf_similarity",
            return_value=math.nan,
        ):
            with self.assertRaisesRegex(EvaluatorInputError, "finite"):
                adapted_compatible_evaluate(attempt(), attempt())


class StrictExactEvaluationTests(unittest.TestCase):
    def test_strict_success_requires_exact_state_and_output(self) -> None:
        reference = attempt(
            file_entry("workspace/result.txt", "same", mode=0o644),
            output="done\n",
        )
        exact = attempt(
            file_entry("workspace/result.txt", "same", mode=0o644),
            output="done\n",
        )
        wrong_mode = attempt(
            file_entry("workspace/result.txt", "same", mode=0o755),
            output="done\n",
        )
        wrong_output = attempt(
            file_entry("workspace/result.txt", "same", mode=0o644),
            output="done",
        )

        exact_result = strict_exact_success(exact, reference)

        self.assertIs(type(exact_result), bool)
        self.assertTrue(exact_result)
        self.assertFalse(strict_exact_success(wrong_mode, reference))
        self.assertFalse(strict_exact_success(wrong_output, reference))

    def test_strict_distinguishes_copies_hardlinks_and_ownership(self) -> None:
        paths = ("workspace/original.txt", "workspace/alias.txt")
        group = hardlink_group_sha256(paths)
        reference = attempt(
            file_entry(paths[0], "same", uid=1000, gid=1000, hardlink_group=group),
            file_entry(paths[1], "same", uid=1000, gid=1000, hardlink_group=group),
        )
        copied_files = attempt(
            file_entry(paths[0], "same", uid=1000, gid=1000),
            file_entry(paths[1], "same", uid=1000, gid=1000),
        )
        wrong_owner = attempt(
            file_entry(paths[0], "same", uid=0, gid=0, hardlink_group=group),
            file_entry(paths[1], "same", uid=0, gid=0, hardlink_group=group),
        )

        self.assertFalse(strict_exact_success(copied_files, reference))
        self.assertFalse(strict_exact_success(wrong_owner, reference))

    def test_strict_comparison_is_order_independent_after_sorting(self) -> None:
        first = file_entry("workspace/a.txt", "a")
        second = file_entry("workspace/b.txt", "b")

        self.assertTrue(
            strict_exact_success(
                attempt(first, second),
                attempt(second, first),
            )
        )

    def test_strict_rejects_wrong_input_types_without_exposing_values(self) -> None:
        with self.assertRaisesRegex(EvaluatorInputError, "PrivateAttempt") as context:
            strict_exact_success(attempt(), object())  # type: ignore[arg-type]
        self.assertNotIn("object at", str(context.exception))


if __name__ == "__main__":
    unittest.main()
