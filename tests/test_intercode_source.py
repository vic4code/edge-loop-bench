from __future__ import annotations

import dataclasses
import importlib
import json
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from edgeloopbench.intercode_source import (
    CALIBRATION_POPULATION_SHA256,
    EXPECTED_CALIBRATION_COUNT,
    EXPECTED_SOURCE_COUNTS,
    INTERCODE_REVISION,
    MAX_VENDOR_FILE_BYTES,
    NL2BASH_REVISION,
    PUBLIC_POPULATION_SHA256,
    SOURCE_CORPUS_SHA256,
    VENDORED_FILE_SHA256,
    InterCodeSourceError,
    _decode_task_rows,
    load_intercode_source,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InterCodeSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = load_intercode_source(PROJECT_ROOT)

    def test_loads_exact_public_and_calibration_populations(self) -> None:
        self.assertEqual(200, len(self.source.tasks))
        self.assertEqual(EXPECTED_CALIBRATION_COUNT, len(self.source.calibration_tasks))
        self.assertEqual(
            EXPECTED_SOURCE_COUNTS,
            Counter(task.stratum for task in self.source.tasks),
        )
        self.assertEqual("bash-fs1-000", self.source.tasks[0].task_id)
        self.assertEqual("bash-fs1-059", self.source.tasks[59].task_id)
        self.assertEqual("bash-fs2-000", self.source.tasks[60].task_id)
        self.assertEqual("bash-fs4-026", self.source.tasks[-1].task_id)
        self.assertEqual(
            "bash-calibration-000", self.source.calibration_tasks[0].task_id
        )
        self.assertEqual(
            "bash-calibration-023", self.source.calibration_tasks[-1].task_id
        )
        expected_source_ids = tuple(
            f"bash-fs{filesystem}-{index:03d}"
            for filesystem, count in ((1, 60), (2, 53), (3, 60), (4, 27))
            for index in range(count)
        )
        self.assertEqual(
            expected_source_ids, tuple(task.task_id for task in self.source.tasks)
        )
        self.assertEqual(
            tuple(f"bash-calibration-{index:03d}" for index in range(24)),
            tuple(task.task_id for task in self.source.calibration_tasks),
        )

    def test_population_and_source_digests_are_pinned_and_deterministic(self) -> None:
        second = load_intercode_source(PROJECT_ROOT)
        self.assertEqual(
            "sha256:6c9bf55f6e6ca8e4a6c67a4f59959bad83d1eaa64f1413e9b46eb6628bf664e0",
            PUBLIC_POPULATION_SHA256,
        )
        self.assertEqual(
            "sha256:ed24cc3215ca6a34477f2cce917c1816fe92a2ff0870ddcc9580f9ea08463f1a",
            CALIBRATION_POPULATION_SHA256,
        )
        self.assertEqual(
            "sha256:b71d029f20453f96a2872b9c1a79d716f48443009acbbf916d63d0d09efc5391",
            SOURCE_CORPUS_SHA256,
        )
        self.assertEqual(PUBLIC_POPULATION_SHA256, self.source.population_sha256)
        self.assertEqual(
            CALIBRATION_POPULATION_SHA256,
            self.source.calibration_population_sha256,
        )
        self.assertEqual(SOURCE_CORPUS_SHA256, self.source.source_sha256)
        self.assertEqual(self.source.population_sha256, second.population_sha256)
        self.assertEqual(self.source.source_sha256, second.source_sha256)

    def test_public_task_repr_and_serialization_do_not_contain_gold(self) -> None:
        all_tasks = self.source.tasks + self.source.calibration_tasks
        for public_task in all_tasks:
            with self.subTest(task_id=public_task.task_id):
                reference = self.source.private_reference(public_task.task_id)
                gold = self.source.gold_for_evaluator(reference)
                public_record = public_task.to_public_record()
                serialized = json.dumps(public_record, sort_keys=True)
                dataclass_record = json.dumps(
                    dataclasses.asdict(public_task), sort_keys=True
                )
                self.assertNotIn(gold, repr(public_task))
                self.assertNotIn(gold, serialized)
                self.assertNotIn(gold, dataclass_record)
                self.assertEqual(
                    {"task_id", "query", "stratum"},
                    set(public_record),
                )

        reference = self.source.private_reference(all_tasks[0].task_id)
        self.assertNotIn("gold", repr(reference).lower())
        with self.assertRaises(TypeError):
            json.dumps(reference)

    def test_private_references_are_source_bound_and_opaque(self) -> None:
        task_id = self.source.tasks[0].task_id
        reference = self.source.private_reference(task_id)
        gold = self.source.gold_for_evaluator(reference)
        self.assertTrue(gold)
        self.assertEqual("<PrivateTaskReference opaque>", repr(reference))

        other_source = load_intercode_source(PROJECT_ROOT)
        with self.assertRaisesRegex(InterCodeSourceError, "does not belong"):
            other_source.gold_for_evaluator(reference)
        with self.assertRaisesRegex(InterCodeSourceError, "unknown task"):
            self.source.private_reference("bash-fs9-999")

    def test_calibration_and_source_have_no_exact_overlap(self) -> None:
        source_queries = {task.query for task in self.source.tasks}
        calibration_queries = {task.query for task in self.source.calibration_tasks}
        self.assertTrue(source_queries.isdisjoint(calibration_queries))

        source_pairs = {
            (task.query, self.source.gold_for_evaluator(
                self.source.private_reference(task.task_id)
            ))
            for task in self.source.tasks
        }
        calibration_pairs = {
            (task.query, self.source.gold_for_evaluator(
                self.source.private_reference(task.task_id)
            ))
            for task in self.source.calibration_tasks
        }
        self.assertTrue(source_pairs.isdisjoint(calibration_pairs))

    def test_loader_is_strictly_offline(self) -> None:
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("network access is forbidden"),
        ):
            source = load_intercode_source(PROJECT_ROOT)
        self.assertEqual(200, len(source.tasks))

    def test_missing_or_modified_vendored_file_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(PROJECT_ROOT / "vendor", root / "vendor")
            target = root / next(iter(VENDORED_FILE_SHA256))
            target.write_bytes(target.read_bytes() + b"\n")
            with self.assertRaisesRegex(InterCodeSourceError, "SHA-256 mismatch"):
                load_intercode_source(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(PROJECT_ROOT / "vendor", root / "vendor")
            target = root / next(iter(VENDORED_FILE_SHA256))
            target.unlink()
            with self.assertRaisesRegex(InterCodeSourceError, "missing vendored file"):
                load_intercode_source(root)

    def test_rejects_duplicate_keys_oversize_and_invalid_schema(self) -> None:
        with self.assertRaisesRegex(InterCodeSourceError, "duplicate JSON key"):
            _decode_task_rows(
                b'[{"query":"one","query":"two","gold":"true"}]',
                source="duplicate.json",
            )
        with self.assertRaisesRegex(InterCodeSourceError, "safety limit"):
            _decode_task_rows(
                b" " * (MAX_VENDOR_FILE_BYTES + 1),
                source="large.json",
            )
        invalid_rows = (
            b'[{"query":"q","gold":"g","unexpected":true}]',
            b'[{"query":"q"}]',
            b'[{"query":1,"gold":"g"}]',
            b'[{"query":" ","gold":"g"}]',
            b'{"query":"q","gold":"g"}',
        )
        for payload in invalid_rows:
            with self.subTest(payload=payload):
                with self.assertRaises(InterCodeSourceError):
                    _decode_task_rows(payload, source="invalid.json")

    def test_vendor_tool_and_production_pins_cannot_drift(self) -> None:
        module = importlib.import_module("tools.vendor_intercode")

        tool_pins = {asset.destination: asset.sha256 for asset in module.ASSETS}
        self.assertEqual(len(VENDORED_FILE_SHA256), len(module.ASSETS))
        self.assertEqual(dict(VENDORED_FILE_SHA256), tool_pins)
        self.assertEqual(INTERCODE_REVISION, module.INTERCODE_REVISION)
        self.assertEqual(NL2BASH_REVISION, module.NL2BASH_REVISION)
        self.assertEqual(MAX_VENDOR_FILE_BYTES, module.MAX_ASSET_BYTES)


if __name__ == "__main__":
    unittest.main()
