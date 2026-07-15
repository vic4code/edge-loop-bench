from __future__ import annotations

import json
import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

from edgeloopbench.journal import (
    GENESIS_EVENT_SHA256,
    JournalIntegrityError,
    JournalPartialTailError,
    JournalSealedError,
    JournalSecurityError,
    append_journal_event,
    canonical_event_bytes,
    inspect_journal,
    recover_partial_tail,
    seal_journal,
)


class JournalTests(unittest.TestCase):
    def test_append_assigns_monotonic_hash_chained_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"

            first = append_journal_event(path, {"type": "run_started", "task_id": "fs1-001"})
            second = append_journal_event(path, {"type": "attempt_completed", "attempt": 1})

            records = self._records(path)
            self.assertEqual(records, [first, second])
            self.assertEqual([record["sequence"] for record in records], [1, 2])
            self.assertEqual(first["previous_event_sha256"], GENESIS_EVENT_SHA256)
            self.assertEqual(second["previous_event_sha256"], first["event_sha256"])
            for record in records:
                self.assertEqual(
                    record["event_sha256"],
                    sha256(canonical_event_bytes(record)).hexdigest(),
                )
                self.assertNotIn("timestamp", record)
                self.assertNotIn("host_path", record)

            inspection = inspect_journal(path)
            self.assertEqual(inspection.record_count, 2)
            self.assertEqual(inspection.next_sequence, 3)
            self.assertEqual(inspection.last_event_sha256, second["event_sha256"])
            self.assertFalse(inspection.sealed)
            self.assertIsNone(inspection.partial_tail)

    def test_seal_is_explicit_terminal_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            append_journal_event(path, {"type": "run_started"})

            seal = seal_journal(path)

            self.assertEqual(seal["type"], "journal_sealed")
            self.assertEqual(seal["sealed_event_count"], 1)
            inspection = inspect_journal(path, require_sealed=True)
            self.assertTrue(inspection.sealed)
            self.assertEqual(inspection.record_count, 2)
            with self.assertRaises(JournalSealedError):
                append_journal_event(path, {"type": "late_event"})
            with self.assertRaises(JournalSealedError):
                seal_journal(path)

    def test_require_sealed_detects_deleted_terminal_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            append_journal_event(path, {"type": "run_started"})
            seal_journal(path)
            lines = path.read_bytes().splitlines(keepends=True)
            path.write_bytes(b"".join(lines[:-1]))

            with self.assertRaisesRegex(JournalIntegrityError, "not sealed"):
                inspect_journal(path, require_sealed=True)

    def test_inspection_detects_edit_delete_reorder_and_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.jsonl"
            append_journal_event(original, {"type": "one", "value": "a"})
            append_journal_event(original, {"type": "two", "value": "b"})
            append_journal_event(original, {"type": "three", "value": "c"})
            seal_journal(original)
            lines = original.read_bytes().splitlines(keepends=True)

            edited = root / "edited.jsonl"
            edited.write_bytes(b"".join(lines).replace(b'"value":"a"', b'"value":"z"', 1))
            deleted = root / "deleted.jsonl"
            deleted.write_bytes(b"".join([lines[0], lines[2], lines[3]]))
            reordered = root / "reordered.jsonl"
            reordered.write_bytes(b"".join([lines[1], lines[0], lines[2], lines[3]]))
            duplicated = root / "duplicated.jsonl"
            duplicated.write_bytes(b"".join([lines[0], lines[1], lines[1], lines[2], lines[3]]))

            for path in (edited, deleted, reordered, duplicated):
                with self.subTest(path=path.name):
                    with self.assertRaises(JournalIntegrityError):
                        inspect_journal(path, require_sealed=True)

    def test_noncanonical_or_duplicate_key_records_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "events.jsonl"
            record = append_journal_event(path, {"type": "run_started"})
            canonical_line = path.read_bytes()

            spaced = root / "spaced.jsonl"
            spaced.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
            duplicate_key = root / "duplicate-key.jsonl"
            duplicate_key.write_bytes(
                canonical_line.replace(b'"sequence":1', b'"sequence":1,"sequence":1')
            )

            with self.assertRaisesRegex(JournalIntegrityError, "canonical"):
                inspect_journal(spaced)
            with self.assertRaisesRegex(JournalIntegrityError, "duplicate JSON key"):
                inspect_journal(duplicate_key)

    def test_partial_tail_is_reported_and_append_never_recovers_it_silently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            append_journal_event(path, {"type": "run_started"})
            complete_bytes = path.read_bytes()
            path.write_bytes(complete_bytes + b'{"sequence":2,"type":"attempt')

            inspection = inspect_journal(path)

            self.assertIsNotNone(inspection.partial_tail)
            assert inspection.partial_tail is not None
            self.assertEqual(inspection.partial_tail.byte_offset, len(complete_bytes))
            self.assertEqual(inspection.partial_tail.byte_length, len(path.read_bytes()) - len(complete_bytes))
            before = path.read_bytes()
            with self.assertRaises(JournalPartialTailError):
                append_journal_event(path, {"type": "attempt_completed"})
            self.assertEqual(path.read_bytes(), before)

    def test_explicit_recovery_truncates_only_final_non_newline_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            first = append_journal_event(path, {"type": "run_started"})
            complete_bytes = path.read_bytes()
            path.write_bytes(complete_bytes + b'{"event_sha256":"incomplete"}')

            recovered = recover_partial_tail(path)

            self.assertEqual(path.read_bytes(), complete_bytes)
            self.assertEqual(recovered.record_count, 1)
            self.assertEqual(recovered.last_event_sha256, first["event_sha256"])
            self.assertIsNone(recovered.partial_tail)
            second = append_journal_event(path, {"type": "attempt_completed"})
            self.assertEqual(second["sequence"], 2)

    def test_recovery_refuses_complete_newline_terminated_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            append_journal_event(path, {"type": "run_started"})
            path.write_bytes(path.read_bytes() + b"not-json\n")
            before = path.read_bytes()

            with self.assertRaises(JournalIntegrityError):
                recover_partial_tail(path)

            self.assertEqual(path.read_bytes(), before)

    def test_recovery_requires_a_partial_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            append_journal_event(path, {"type": "run_started"})

            with self.assertRaisesRegex(JournalPartialTailError, "no partial tail"):
                recover_partial_tail(path)

    def test_short_writes_are_retried_until_the_full_record_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            real_write = os.write

            def short_write(descriptor: int, payload: object) -> int:
                view = memoryview(payload)  # type: ignore[arg-type]
                return real_write(descriptor, view[: max(1, len(view) // 3)])

            with mock.patch("edgeloopbench.journal.os.write", side_effect=short_write):
                appended = append_journal_event(path, {"type": "run_started", "payload": "x" * 200})

            self.assertEqual(self._records(path), [appended])
            self.assertEqual(inspect_journal(path).record_count, 1)

    def test_interrupted_append_leaves_an_inspectable_recoverable_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            append_journal_event(path, {"type": "run_started"})
            real_write = os.write
            calls = 0

            def crash_after_prefix(descriptor: int, payload: object) -> int:
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise OSError("simulated crash")
                view = memoryview(payload)  # type: ignore[arg-type]
                return real_write(descriptor, view[: max(1, len(view) // 2)])

            with mock.patch(
                "edgeloopbench.journal.os.write", side_effect=crash_after_prefix
            ):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    append_journal_event(path, {"type": "attempt_completed"})

            damaged = inspect_journal(path)
            self.assertEqual(damaged.record_count, 1)
            self.assertIsNotNone(damaged.partial_tail)
            recovered = recover_partial_tail(path)
            self.assertEqual(recovered.record_count, 1)
            self.assertIsNone(recovered.partial_tail)
            self.assertEqual(
                append_journal_event(path, {"type": "attempt_completed"})["sequence"],
                2,
            )

    def test_symlinks_and_non_regular_targets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.jsonl"
            real.write_bytes(b"")
            symlink = root / "events.jsonl"
            symlink.symlink_to(real)

            with self.assertRaises(JournalSecurityError):
                append_journal_event(symlink, {"type": "run_started"})
            with self.assertRaises(JournalSecurityError):
                inspect_journal(symlink)
            with self.assertRaises(JournalSecurityError):
                inspect_journal(root)

    def test_reserved_fields_and_non_json_payloads_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"

            for event in (
                {"type": "x", "sequence": 99},
                {"type": "x", "previous_event_sha256": "0" * 64},
                {"type": "x", "event_sha256": "0" * 64},
                {"type": "journal_sealed"},
                {"type": "x", "value": float("nan")},
            ):
                with self.subTest(event=event):
                    with self.assertRaises(ValueError):
                        append_journal_event(path, event)

            self.assertFalse(path.exists())

    @staticmethod
    def _records(path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
