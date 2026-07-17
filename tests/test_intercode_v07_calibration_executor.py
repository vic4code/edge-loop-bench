from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

import edgeloopbench.intercode_v07_docker_qualification as qualification_authority
from edgeloopbench.intercode_campaign_ledger import CAMPAIGN_MODELS
from edgeloopbench.intercode_source import load_intercode_source
from edgeloopbench.intercode_v07_calibration import (
    V07_CALIBRATION_TASK_IDS,
    build_v07_calibration_design,
)
from edgeloopbench.intercode_v07_calibration_executor import (
    V07CalibrationExecutionRow,
    V07CalibrationInfrastructureInvalidError,
    V07CalibrationIntegrityError,
    V07CalibrationPendingEpisodeError,
    V07CalibrationRuntime,
    execute_v07_calibration,
    v07_calibration_budget,
)
from edgeloopbench.intercode_v07_docker_qualification import (
    V07CalibrationGoldResult,
    _issue_v07_trusted_gold_material,
)
from edgeloopbench.journal import append_journal_event, inspect_journal
from edgeloopbench.model_adapter import (
    ExactPromptPreparer,
    OllamaRawModel,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
)
from tests.test_intercode_v07_calibration import host_sample
from tests.test_intercode_v07_manifest import build as build_precalibration_manifest
from tests.test_intercode_v07_runner import (
    FakeBoundaryFactory,
    FakeTransport,
    FixedTokenCounter,
    candidate,
    digest,
    generation,
)


def calibration_gold():  # type: ignore[no-untyped-def]
    materials = {
        task_id: candidate(f"gold-{index}")
        for index, task_id in enumerate(V07_CALIBRATION_TASK_IDS, 1)
    }
    sealed = {
        task_id: _issue_v07_trusted_gold_material(
            task_id=task_id,
            source_capability_sha256=digest(f"capability-{task_id}"),
            image_id=digest("calibration-image"),
            evaluator_sha256=digest("evaluator"),
            state_normalization_sha256=digest("normalizer"),
            replay_receipt_sha256=digest(f"receipt-{task_id}"),
            material=materials[task_id],
        )
        for task_id in V07_CALIBRATION_TASK_IDS
    }
    result = V07CalibrationGoldResult(
        calibration_campaign_sha256=digest("calibration-campaign"),
        trusted_gold_by_task_id=sealed,
        _construction_seal=qualification_authority._RESULT_SEAL,
    )
    return result, materials


class RuntimeFactory:
    def __init__(
        self,
        materials,  # type: ignore[no-untyped-def]
        *,
        fail_episode: int | None = None,
        infrastructure_invalid_episode: int | None = None,
    ) -> None:
        self.materials = materials
        self.fail_episode = fail_episode
        self.infrastructure_invalid_episode = infrastructure_invalid_episode
        self.rows: list[V07CalibrationExecutionRow] = []
        self.aborted_admissions: list[int] = []

    def __call__(self, row: V07CalibrationExecutionRow) -> V07CalibrationRuntime:
        self.rows.append(row)
        profile = (
            QWEN35_RAW_PROFILE
            if row.episode.model_id == QWEN35_RAW_PROFILE.model
            else PHI4_MINI_RAW_PROFILE
        )
        counter = FixedTokenCounter(profile)
        preparer = ExactPromptPreparer(profile, counter)
        timeline: list[str] = []
        transport = FakeTransport(
            [f"candidate-{row.episode.episode_index}-{attempt}" for attempt in range(1, 5)],
            timeline,
            failure=(
                RuntimeError("simulated calibration crash")
                if row.episode.episode_index == self.fail_episode
                else None
            ),
        )
        if row.episode.episode_index == self.infrastructure_invalid_episode:
            original = transport

            def mismatched_telemetry(payload: bytes) -> bytes:
                response = json.loads(original(payload))
                response["prompt_eval_count"] = 13
                return json.dumps(response, separators=(",", ":")).encode("utf-8")

            model = OllamaRawModel(generation(profile), transport=mismatched_telemetry)
        else:
            model = OllamaRawModel(generation(profile), transport=transport)
        factory = FakeBoundaryFactory(
            [self.materials[row.episode.task_id]] * row.request_cap,
            timeline,
        )
        before_ns = row.episode.episode_index * 10_000
        clock = iter((before_ns + 100, before_ns + 900)).__next__
        return V07CalibrationRuntime(
            model=model,
            prompt_preparer=preparer,
            boundary_factory=factory,
            before_episode_admission=lambda: host_sample(
                row.episode.model_id,
                monotonic_ns=before_ns,
            ),
            after_episode_admission=lambda: host_sample(
                row.episode.model_id,
                monotonic_ns=before_ns + 1_000,
            ),
            abort_episode_admission=lambda: self.aborted_admissions.append(
                row.episode.episode_index
            ),
            monotonic_ns=clock,
        )


class InterCodeV07CalibrationExecutorTests(unittest.TestCase):
    def inputs(self):  # type: ignore[no-untyped-def]
        source = load_intercode_source()
        design = build_v07_calibration_design(source)
        gold, materials = calibration_gold()
        manifest = build_precalibration_manifest()
        return source, design, gold, materials, manifest

    def test_executes_exact_eight_rows_and_returns_verifier_sealed_evidence(self) -> None:
        source, design, gold, materials, manifest = self.inputs()
        runtime = RuntimeFactory(materials)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = execute_v07_calibration(
                design=design,
                source=source,
                calibration_gold=gold,
                precalibration_manifest=manifest,
                calibration_journal_path=root / "calibration.jsonl",
                artifact_directory=root / "episodes",
                runtime_factory=runtime,
            )
            journal_inspection = inspect_journal(
                run.calibration_journal_path,
                require_sealed=True,
            )
            journal_mode = stat.S_IMODE(run.calibration_journal_path.stat().st_mode)
            marker_modes = tuple(
                stat.S_IMODE(path.stat().st_mode) for path in run.begun_marker_paths
            )

            calls_before_resume = len(runtime.rows)
            resumed = execute_v07_calibration(
                design=design,
                source=source,
                calibration_gold=gold,
                precalibration_manifest=manifest,
                calibration_journal_path=root / "calibration.jsonl",
                artifact_directory=root / "episodes",
                runtime_factory=runtime,
            )

        self.assertEqual(run.evidence.episode_count, 8)
        self.assertEqual(
            run.calibration_campaign_sha256,
            gold.calibration_campaign_sha256,
        )
        self.assertEqual(len(run.controller_log_paths), 8)
        self.assertEqual(len(runtime.rows), 8)
        self.assertEqual(runtime.aborted_admissions, list(range(1, 9)))
        self.assertEqual(calls_before_resume, len(runtime.rows))
        self.assertEqual(resumed.evidence.evidence_sha256, run.evidence.evidence_sha256)
        self.assertEqual(
            tuple(row.request_cap for row in runtime.rows),
            (1, 4, 4, 4, 1, 4, 4, 4),
        )
        self.assertTrue(all(row.budget == v07_calibration_budget() for row in runtime.rows))
        self.assertEqual(
            tuple(row.episode.model_id for row in runtime.rows[:4]),
            (CAMPAIGN_MODELS[0],) * 4,
        )
        self.assertTrue(journal_inspection.sealed)
        self.assertEqual(journal_mode, 0o600)
        self.assertEqual(marker_modes, (0o600,) * 8)

    def test_crashed_begun_episode_is_never_automatically_reissued(self) -> None:
        source, design, gold, materials, manifest = self.inputs()
        crashing = RuntimeFactory(materials, fail_episode=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = {
                "design": design,
                "source": source,
                "calibration_gold": gold,
                "precalibration_manifest": manifest,
                "calibration_journal_path": root / "calibration.jsonl",
                "artifact_directory": root / "episodes",
            }
            with self.assertRaisesRegex(RuntimeError, "simulated calibration crash"):
                execute_v07_calibration(runtime_factory=crashing, **arguments)
            self.assertEqual(len(crashing.rows), 1)
            self.assertEqual(crashing.aborted_admissions, [1])
            self.assertFalse(inspect_journal(root / "calibration.jsonl").sealed)

            forbidden = RuntimeFactory(materials)
            with self.assertRaises(V07CalibrationPendingEpisodeError):
                execute_v07_calibration(runtime_factory=forbidden, **arguments)

        self.assertEqual(forbidden.rows, [])

    def test_begun_marker_is_sealed_before_runtime_factory_can_attempt_work(self) -> None:
        source, design, gold, materials, manifest = self.inputs()
        observations: list[tuple[bool, int]] = []
        attempted_model_requests: list[int] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "episodes" / "calibration-001.begun.jsonl"
            arguments = {
                "design": design,
                "source": source,
                "calibration_gold": gold,
                "precalibration_manifest": manifest,
                "calibration_journal_path": root / "calibration.jsonl",
                "artifact_directory": root / "episodes",
            }

            def hostile_factory(row: V07CalibrationExecutionRow) -> V07CalibrationRuntime:
                inspection = inspect_journal(marker, require_sealed=True)
                observations.append(
                    (inspection.sealed, stat.S_IMODE(marker.stat().st_mode))
                )
                attempted_model_requests.append(row.episode.episode_index)
                raise RuntimeError("simulated forbidden factory model request")

            with self.assertRaisesRegex(
                RuntimeError,
                "simulated forbidden factory model request",
            ):
                execute_v07_calibration(
                    runtime_factory=hostile_factory,
                    **arguments,
                )

            safe_factory = RuntimeFactory(materials)
            with self.assertRaises(V07CalibrationPendingEpisodeError):
                execute_v07_calibration(
                    runtime_factory=safe_factory,
                    **arguments,
                )

        self.assertEqual(observations, [(True, 0o600)])
        self.assertEqual(attempted_model_requests, [1])
        self.assertEqual(safe_factory.rows, [])

    def test_resume_rejects_different_calibration_campaign_before_runtime(self) -> None:
        source, design, gold, materials, manifest = self.inputs()
        crashing = RuntimeFactory(materials, fail_episode=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = {
                "design": design,
                "source": source,
                "precalibration_manifest": manifest,
                "calibration_journal_path": root / "calibration.jsonl",
                "artifact_directory": root / "episodes",
            }
            with self.assertRaisesRegex(RuntimeError, "simulated calibration crash"):
                execute_v07_calibration(
                    calibration_gold=gold,
                    runtime_factory=crashing,
                    **arguments,
                )

            other_gold = V07CalibrationGoldResult(
                calibration_campaign_sha256=digest("other-calibration-campaign"),
                trusted_gold_by_task_id=gold.trusted_gold_by_task_id,
                _construction_seal=qualification_authority._RESULT_SEAL,
            )
            forbidden = RuntimeFactory(materials)
            with self.assertRaisesRegex(
                V07CalibrationIntegrityError,
                "calibration declaration differs",
            ):
                execute_v07_calibration(
                    calibration_gold=other_gold,
                    runtime_factory=forbidden,
                    **arguments,
                )

        self.assertEqual(forbidden.rows, [])

    def test_infrastructure_invalid_row_is_recorded_then_halts_every_future_row(self) -> None:
        source, design, gold, materials, manifest = self.inputs()
        invalid = RuntimeFactory(materials, infrastructure_invalid_episode=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = {
                "design": design,
                "source": source,
                "calibration_gold": gold,
                "precalibration_manifest": manifest,
                "calibration_journal_path": root / "calibration.jsonl",
                "artifact_directory": root / "episodes",
            }
            with self.assertRaises(V07CalibrationInfrastructureInvalidError):
                execute_v07_calibration(runtime_factory=invalid, **arguments)
            self.assertEqual(len(invalid.rows), 1)
            self.assertEqual(inspect_journal(root / "calibration.jsonl").record_count, 2)

            forbidden = RuntimeFactory(materials)
            with self.assertRaises(V07CalibrationInfrastructureInvalidError):
                execute_v07_calibration(runtime_factory=forbidden, **arguments)

        self.assertEqual(forbidden.rows, [])

    def test_wrong_gold_or_unexpected_future_artifact_fails_before_runtime(self) -> None:
        source, design, gold, materials, manifest = self.inputs()
        runtime = RuntimeFactory(materials)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "sealed manifest"):
                execute_v07_calibration(
                    design=design,
                    source=source,
                    calibration_gold=gold,
                    precalibration_manifest=manifest.manifest_sha256,  # type: ignore[arg-type]
                    calibration_journal_path=root / "calibration.jsonl",
                    artifact_directory=root / "episodes",
                    runtime_factory=runtime,
                )
            with self.assertRaises(ValueError):
                execute_v07_calibration(
                    design=design,
                    source=source,
                    calibration_gold={},  # type: ignore[arg-type]
                    precalibration_manifest=manifest,
                    calibration_journal_path=root / "calibration.jsonl",
                    artifact_directory=root / "episodes",
                    runtime_factory=runtime,
                )
            self.assertFalse((root / "calibration.jsonl").exists())

            future = root / "episodes" / "calibration-002.begun.jsonl"
            future.parent.mkdir(parents=True)
            future.touch(mode=0o600)
            with self.assertRaises(V07CalibrationPendingEpisodeError):
                execute_v07_calibration(
                    design=design,
                    source=source,
                    calibration_gold=gold,
                    precalibration_manifest=manifest,
                    calibration_journal_path=root / "calibration.jsonl",
                    artifact_directory=root / "episodes",
                    runtime_factory=runtime,
                )

        self.assertEqual(runtime.rows, [])

    def test_duplicate_journal_row_or_missing_completed_artifact_fails_closed(self) -> None:
        source, design, gold, materials, manifest = self.inputs()
        completed_runtime = RuntimeFactory(materials)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = {
                "design": design,
                "source": source,
                "calibration_gold": gold,
                "precalibration_manifest": manifest,
                "artifact_directory": root / "episodes",
            }
            run = execute_v07_calibration(
                calibration_journal_path=root / "calibration.jsonl",
                runtime_factory=completed_runtime,
                **common,
            )
            records = tuple(
                json.loads(line)
                for line in run.calibration_journal_path.read_text().splitlines()
            )
            chain = {"sequence", "previous_event_sha256", "event_sha256"}
            declaration = {
                key: value for key, value in records[0].items() if key not in chain
            }
            first_row = {
                key: value for key, value in records[1].items() if key not in chain
            }
            duplicate = root / "duplicate.jsonl"
            append_journal_event(duplicate, declaration)
            append_journal_event(duplicate, first_row)
            append_journal_event(duplicate, first_row)
            forbidden = RuntimeFactory(materials)
            with self.assertRaises(V07CalibrationIntegrityError):
                execute_v07_calibration(
                    calibration_journal_path=duplicate,
                    runtime_factory=forbidden,
                    **common,
                )

            run.execution_envelope_paths[0].unlink()
            with self.assertRaises(V07CalibrationIntegrityError):
                execute_v07_calibration(
                    calibration_journal_path=root / "calibration.jsonl",
                    runtime_factory=forbidden,
                    **common,
                )

        self.assertEqual(forbidden.rows, [])

    def test_preexisting_wrong_mode_lock_is_rejected_without_chmod_or_runtime(self) -> None:
        source, design, gold, materials, manifest = self.inputs()
        runtime = RuntimeFactory(materials)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "calibration.jsonl.lock"
            lock.touch(mode=0o600)
            lock.chmod(0o640)
            with self.assertRaises(V07CalibrationIntegrityError):
                execute_v07_calibration(
                    design=design,
                    source=source,
                    calibration_gold=gold,
                    precalibration_manifest=manifest,
                    calibration_journal_path=root / "calibration.jsonl",
                    artifact_directory=root / "episodes",
                    runtime_factory=runtime,
                )
            observed_mode = stat.S_IMODE(lock.stat().st_mode)

        self.assertEqual(observed_mode, 0o640)
        self.assertEqual(runtime.rows, [])


if __name__ == "__main__":
    unittest.main()
