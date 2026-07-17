from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import pickle
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.docker_action_executor import (
    ActionDisposition,
    CleanupOutcome,
    DockerActionLimits,
    DockerActionResult,
)
from edgeloopbench.docker_cli import (
    DockerContainer,
    DockerContainerSpec,
    DockerLimits,
    DockerTrustedState,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    ROLE_LABEL,
    RUN_LABEL,
)
from edgeloopbench.intercode_campaign_ledger import CAMPAIGN_TASK_IDS
from edgeloopbench.intercode_v07_calibration import V07_CALIBRATION_TASK_IDS
from edgeloopbench.intercode_replay_environment import (
    V07_STRICT_REPLAY_EVALUATOR_SHA256,
)
from edgeloopbench.intercode_local_model import (
    OLLAMA_GENERATION_ENDPOINT_SHA256,
    OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
)
from edgeloopbench.intercode_source import load_intercode_source
from edgeloopbench.intercode_v07_docker_qualification import (
    V07_DOCKER_QUALIFICATION_AUTHORITY_REVISION,
    V07DockerQualificationError,
    V07DockerQualificationResult,
    V07CalibrationGoldResult,
    V07TrustedGoldMaterial,
    _open_v07_trusted_gold_material,
    run_v07_docker_calibration_gold,
    run_v07_docker_qualification,
    v07_qualification_run_id,
)
from edgeloopbench.intercode_v07_manifest import (
    V07_INTERVENTION_JOURNAL_REVISION,
    V07_RUN_ID_POLICY_REVISION,
    V07ExecutionPins,
    V07HostIdentityPins,
    V07HostSafetyPins,
    _EXECUTION_SEAL,
    _HOST_SAFETY_SEAL,
    _digest_record,
    _execution_core_record,
)
from edgeloopbench.intercode_v07_image_provenance import (
    V07_STATE_NORMALIZATION_REVISION,
    VerifiedV07ImageSet,
    _IMAGE_SET_SEAL,
    _image_set_core,
    _digest as image_set_digest,
)
from edgeloopbench.intercode_v07_qualification import (
    VerifiedV07QualificationEvidence,
)
from edgeloopbench.journal import inspect_journal


NORMALIZER_SHA256 = "sha256:" + "9" * 64
IMAGE_IDS = {
    "fs1": "sha256:" + "1" * 64,
    "fs2": "sha256:" + "2" * 64,
    "fs3": "sha256:" + "3" * 64,
    "fs4": "sha256:" + "4" * 64,
}


def digest(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def image_set(
    *,
    images: dict[str, str] | None = None,
    build_plan_sha256: str | None = None,
    source_inventory_sha256: str | None = None,
) -> VerifiedV07ImageSet:
    values = {
        "source_inventory_sha256": (
            source_inventory_sha256 or digest("source-inventory")
        ),
        "build_plan_sha256": build_plan_sha256 or digest("build-plan"),
        "build_manifest_sha256": digest("build-manifest"),
        "build_verification_sha256": digest("build-verification"),
        "image_id_by_stratum": dict(images or IMAGE_IDS),
        "state_normalization_revision": V07_STATE_NORMALIZATION_REVISION,
        "state_normalization_source_sha256": digest("normalizer-source"),
        "state_normalization_sha256": NORMALIZER_SHA256,
    }
    return VerifiedV07ImageSet(
        **values,  # type: ignore[arg-type]
        image_set_sha256=image_set_digest(
            _image_set_core(**values)  # type: ignore[arg-type]
        ),
        _construction_seal=_IMAGE_SET_SEAL,
    )


def canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def execution_pins() -> V07ExecutionPins:
    host_identity = V07HostIdentityPins(
        docker_binary_sha256=digest("docker-binary"),
        docker_endpoint_sha256=digest("docker-endpoint"),
        docker_client_version="27.3.1",
        docker_server_version="27.3.1",
        ollama_runtime_binary_sha256=digest("ollama-binary"),
        ollama_server_version="0.31.1",
        ollama_launch_environment_sha256=OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
        ollama_generation_endpoint_sha256=OLLAMA_GENERATION_ENDPOINT_SHA256,
    )
    host_safety = V07HostSafetyPins(
        host_identity=host_identity,
        policy_source_sha256=digest("host-policy"),
        telemetry_collector_source_sha256=digest("host-collector"),
        _construction_seal=_HOST_SAFETY_SEAL,
    )
    values = {
        "source_inventory_sha256": digest("source-inventory"),
        "docker_limits": DockerLimits(
            memory_bytes=512 << 20,
            memory_swap_bytes=512 << 20,
            storage_bytes=256 << 20,
            nano_cpus=1_000_000_000,
            pids_limit=64,
            nofile_soft=1024,
            nofile_hard=1024,
            nproc_soft=64,
            nproc_hard=64,
        ),
        "docker_action_limits": DockerActionLimits(
            deadline_seconds=10.0,
            private_stream_limit_bytes=4096,
            observation_limit_bytes=2048,
            read_chunk_bytes=4096,
            io_queue_chunks=8,
        ),
        "host_safety": host_safety,
        "run_id_policy_revision": V07_RUN_ID_POLICY_REVISION,
        "intervention_journal_revision": V07_INTERVENTION_JOURNAL_REVISION,
        "qualification_replay_actions": 60,
        "calibration_model_prompts": 26,
        "confirmatory_model_prompts": 780,
    }
    return V07ExecutionPins(
        **values,  # type: ignore[arg-type]
        execution_pins_sha256=_digest_record(
            _execution_core_record(**values)  # type: ignore[arg-type]
        ),
        _construction_seal=_EXECUTION_SEAL,
    )


def state_entry(label: str) -> dict[str, object]:
    path = f"testbed/{label}.txt"
    encoded = path.encode("utf-8")
    return {
        "content_sha256": digest(f"content-{label}"),
        "gid": 65532,
        "hardlink_group_sha256": None,
        "mode": 0o640,
        "path": path,
        "path_bytes_b64": base64.b64encode(encoded).decode("ascii"),
        "size_bytes": len(encoded),
        "symlink_target": None,
        "symlink_target_bytes_b64": None,
        "type": "file",
        "uid": 65532,
    }


def trusted_state(label: str, profile: str) -> DockerTrustedState:
    entries = [] if label.startswith("initial-") else [state_entry(label)]
    profile_sha256 = digest(f"profile-{profile}")
    audit_sha256 = digest(f"audit-{label}")
    state_sha256 = digest(
        canonical(
            {
                "entries": entries,
                "profile_sha256": profile_sha256,
                "schema": "edgeloopbench.filesystem-state.v1",
                "writable_surface_audit_sha256": audit_sha256,
            }
        )
    )
    payload = {
        "common_roots": ["home/agent", "tmp"],
        "dynamic_root_policy": "non_baseline_top_level",
        "entries": entries,
        "entry_count": len(entries),
        "policy_sha256": digest("policy"),
        "profile": profile,
        "profile_sha256": profile_sha256,
        "root_baseline_sha256": digest("root"),
        "schema": "edgeloopbench.filesystem-state.v1",
        "state_sha256": state_sha256,
        "strict_surface": {"failures": [], "status": "representable"},
        "task_roots": ["testbed"],
        "total_file_bytes": sum(int(item["size_bytes"]) for item in entries),
        "writable_surface_audit_sha256": audit_sha256,
    }
    return DockerTrustedState(
        canonical_json=canonical(payload),
        state_sha256=state_sha256,
        profile=profile,
        profile_sha256=profile_sha256,
        policy_sha256=digest("policy"),
        root_baseline_sha256=digest("root"),
        writable_surface_audit_sha256=audit_sha256,
        collector_source_sha256=digest("collector"),
        strict_representable=True,
        strict_failures=(),
    )


class FakeQualificationDocker:
    def __init__(
        self,
        *,
        preexisting: bool = False,
        refuse_cleanup: bool = False,
        disagree_replays: bool = False,
        task_ids: tuple[str, ...] = CAMPAIGN_TASK_IDS,
    ) -> None:
        self.preexisting = preexisting
        self.refuse_cleanup = refuse_cleanup
        self.disagree_replays = disagree_replays
        self.task_ids = task_ids
        self.specs: list[DockerContainerSpec] = []
        self.containers: dict[str, DockerContainer] = {}
        self.task_by_container: dict[str, tuple[str, int]] = {}
        self.collect_count: dict[str, int] = {}
        self.removals: list[tuple[str, tuple[str, ...]]] = []
        self._first_list = True

    def list_run_containers(self, run_id: str) -> tuple[str, ...]:
        if self.preexisting and self._first_list:
            self._first_list = False
            return ("f" * 64,)
        self._first_list = False
        return tuple(
            sorted(
                identifier
                for identifier, container in self.containers.items()
                if container.spec.run_id == run_id
            )
        )

    def create_container(self, spec: DockerContainerSpec) -> DockerContainer:
        replay_position = len(self.specs)
        task_id = self.task_ids[replay_position // 2]
        replay_index = replay_position % 2 + 1
        identifier = hashlib.sha256(
            f"container-{replay_position}".encode("ascii")
        ).hexdigest()
        name = f"elb-{spec.run_id}-agent-{identifier[:16]}"
        labels = {
            MANAGED_LABEL: "v0.6",
            RUN_LABEL: spec.run_id,
            ROLE_LABEL: "agent",
            INSTANCE_LABEL: name,
        }
        container = DockerContainer(
            identifier=identifier,
            name=name,
            image_id=spec.image_id,
            labels=tuple(sorted(labels.items())),
            spec=spec,
        )
        self.specs.append(spec)
        self.containers[identifier] = container
        self.task_by_container[identifier] = (task_id, replay_index)
        self.collect_count[identifier] = 0
        return container

    def start_container(self, container: DockerContainer) -> DockerContainer:
        return container

    def collect_trusted_state(
        self,
        container: DockerContainer,
        *,
        profile: str,
    ) -> DockerTrustedState:
        task_id, replay_index = self.task_by_container[container.identifier]
        count = self.collect_count[container.identifier]
        self.collect_count[container.identifier] = count + 1
        if count == 0:
            label = f"initial-{task_id}"
        else:
            disagreement = f"-r{replay_index}" if self.disagree_replays else ""
            label = f"final-{task_id}{disagreement}"
        return trusted_state(label, profile)

    def remove_run_containers(
        self,
        run_id: str,
        identifiers: tuple[str, ...],
    ) -> tuple[str, ...]:
        self.removals.append((run_id, identifiers))
        if self.refuse_cleanup:
            return ()
        for identifier in identifiers:
            self.containers.pop(identifier, None)
        return identifiers


class FakeQualificationExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.actions: list[str] = []
        self.limits: list[DockerActionLimits] = []

    def execute(
        self,
        *,
        container: DockerContainer,
        action: str,
        cwd: str,
        limits: DockerActionLimits,
    ) -> DockerActionResult:
        del container
        if cwd != "/":
            raise AssertionError("qualification cwd drifted")
        self.actions.append(action)
        self.limits.append(limits)
        if self.fail:
            raise RuntimeError("/Users/private/SECRET_GOLD executor failure")
        return DockerActionResult(
            disposition=ActionDisposition.EXECUTED,
            infrastructure_failure=None,
            policy_failure=None,
            failure_stage=None,
            cleanup_outcome=CleanupOutcome.NOT_REQUIRED,
            exit_code=0,
            action_started=True,
            observation="ok\n",
            private_stdout=b"ok\n",
            private_stderr=b"",
            stdout_bytes_observed=3,
            stderr_bytes_observed=0,
            observation_truncated=False,
            elapsed_seconds=0.1,
        )


class InterCodeV07DockerQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = load_intercode_source()
        cls.execution = execution_pins()

    def test_runs_exact_30_by_2_matrix_and_issues_only_sealed_gold(self) -> None:
        docker = FakeQualificationDocker()
        executor = FakeQualificationExecutor()
        first_reference = self.source.private_reference(CAMPAIGN_TASK_IDS[0])
        first_gold = self.source.gold_for_evaluator(first_reference)

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "qualification.jsonl"
            result = run_v07_docker_qualification(
                source=self.source,
                journal_path=journal,
                image_set=image_set(),
                evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                execution_pins=self.execution,
                docker_cli=docker,  # type: ignore[arg-type]
                action_executor=executor,  # type: ignore[arg-type]
            )
            journal_bytes = journal.read_bytes()
            inspection = inspect_journal(journal, require_sealed=True)

        self.assertIs(type(result), V07DockerQualificationResult)
        self.assertIs(type(result.evidence), VerifiedV07QualificationEvidence)
        result.evidence.require_admitted()
        self.assertEqual(inspection.record_count, 63)
        self.assertEqual(tuple(result.trusted_gold_by_task_id), CAMPAIGN_TASK_IDS)
        self.assertEqual(len(docker.specs), 60)
        self.assertEqual(len(executor.actions), 60)
        self.assertEqual(len(docker.removals), 60)
        self.assertTrue(
            all(spec.limits == self.execution.docker_limits for spec in docker.specs)
        )
        self.assertTrue(
            all(
                limits == self.execution.docker_action_limits
                for limits in executor.limits
            )
        )
        self.assertEqual(len({spec.run_id for spec in docker.specs}), 60)
        self.assertNotIn(first_gold.encode("utf-8"), journal_bytes)
        self.assertNotIn(b"/Users/", journal_bytes)

        for task_id, gold in result.trusted_gold_by_task_id.items():
            self.assertIs(type(gold), V07TrustedGoldMaterial)
            self.assertEqual(gold.task_id, task_id)
            self.assertEqual(gold.image_id, IMAGE_IDS[task_id[5:8]])
            self.assertNotIn(first_gold, repr(gold))
            with self.assertRaises(TypeError):
                copy.copy(gold)
            with self.assertRaises(TypeError):
                pickle.dumps(gold)
            with self.assertRaises(TypeError):
                json.dumps(gold)

        opened = _open_v07_trusted_gold_material(
            result.trusted_gold_by_task_id[CAMPAIGN_TASK_IDS[0]],
            task_id=CAMPAIGN_TASK_IDS[0],
        )
        self.assertEqual(opened.normalized_stdout, "ok\n")
        with self.assertRaises(V07DockerQualificationError):
            _open_v07_trusted_gold_material(
                result.trusted_gold_by_task_id[CAMPAIGN_TASK_IDS[0]],
                task_id=CAMPAIGN_TASK_IDS[1],
            )

    def test_production_api_accepts_only_sealed_build_and_normalizer_provenance(self) -> None:
        authority = image_set()
        docker = FakeQualificationDocker()
        executor = FakeQualificationExecutor()
        with tempfile.TemporaryDirectory() as directory:
            result = run_v07_docker_qualification(
                source=self.source,
                journal_path=Path(directory) / "qualification.jsonl",
                image_set=authority,
                evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                execution_pins=self.execution,
                docker_cli=docker,  # type: ignore[arg-type]
                action_executor=executor,  # type: ignore[arg-type]
            )

        evidence = result.evidence
        self.assertEqual(evidence.build_plan_sha256, authority.build_plan_sha256)
        self.assertEqual(
            evidence.build_manifest_sha256,
            authority.build_manifest_sha256,
        )
        self.assertEqual(evidence.image_set_sha256, authority.image_set_sha256)
        self.assertEqual(
            evidence.state_normalization_source_sha256,
            authority.state_normalization_source_sha256,
        )
        self.assertEqual(
            evidence.state_normalization_sha256,
            authority.state_normalization_sha256,
        )
        parameters = inspect.signature(run_v07_docker_qualification).parameters
        self.assertNotIn("image_id_by_stratum", parameters)
        self.assertNotIn("state_normalization_sha256", parameters)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(V07DockerQualificationError):
                run_v07_docker_qualification(
                    source=self.source,
                    journal_path=Path(directory) / "raw.jsonl",
                    image_set=IMAGE_IDS,  # type: ignore[arg-type]
                    evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                    execution_pins=self.execution,
                    docker_cli=FakeQualificationDocker(),  # type: ignore[arg-type]
                    action_executor=FakeQualificationExecutor(),  # type: ignore[arg-type]
                )

    def test_run_ids_are_revision_pinned_deterministic_and_match_matrix(self) -> None:
        docker = FakeQualificationDocker()
        executor = FakeQualificationExecutor()
        with tempfile.TemporaryDirectory() as directory:
            result = run_v07_docker_qualification(
                source=self.source,
                journal_path=Path(directory) / "qualification.jsonl",
                image_set=image_set(),
                evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                execution_pins=self.execution,
                docker_cli=docker,  # type: ignore[arg-type]
                action_executor=executor,  # type: ignore[arg-type]
            )

        expected = [
            v07_qualification_run_id(
                qualification_campaign_sha256=(
                    result.qualification_campaign_sha256
                ),
                episode_index=episode_index,
                run_id_policy_revision=V07_RUN_ID_POLICY_REVISION,
            )
            for episode_index in range(1, 61)
        ]
        self.assertEqual(
            tuple(spec.run_id for spec in docker.specs),
            tuple(expected),
        )
        self.assertEqual(
            V07_DOCKER_QUALIFICATION_AUTHORITY_REVISION,
            "intercode-v0.7-docker-qualification-authority-v1",
        )
        self.assertTrue(all(len(run_id) == 24 for run_id in expected))

    def test_calibration_gold_uses_exact_four_fs1_tasks_without_public_claim(self) -> None:
        docker = FakeQualificationDocker(task_ids=V07_CALIBRATION_TASK_IDS)
        executor = FakeQualificationExecutor()

        result = run_v07_docker_calibration_gold(
            source=self.source,
            image_set=image_set(),
            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
            execution_pins=self.execution,
            docker_cli=docker,  # type: ignore[arg-type]
            action_executor=executor,  # type: ignore[arg-type]
        )

        self.assertIs(type(result), V07CalibrationGoldResult)
        self.assertEqual(
            tuple(result.trusted_gold_by_task_id),
            V07_CALIBRATION_TASK_IDS,
        )
        self.assertEqual(len(docker.specs), 8)
        self.assertEqual(len(executor.actions), 8)
        self.assertTrue(
            all(spec.image_id == IMAGE_IDS["fs1"] for spec in docker.specs)
        )
        self.assertFalse(hasattr(result, "evidence"))
        first = result.trusted_gold_by_task_id[V07_CALIBRATION_TASK_IDS[0]]
        self.assertEqual(first.image_id, IMAGE_IDS["fs1"])
        for wrong_id in (CAMPAIGN_TASK_IDS[0], "bash-calibration-004"):
            with self.subTest(wrong_id=wrong_id):
                with self.assertRaises(V07DockerQualificationError):
                    _open_v07_trusted_gold_material(first, task_id=wrong_id)

    def test_invalid_inputs_or_preexisting_resources_fail_before_mutation(self) -> None:
        invalid_normalizer = image_set()
        object.__setattr__(
            invalid_normalizer,
            "state_normalization_sha256",
            "not-a-digest",
        )
        cases = (
            ({"image_set": {**IMAGE_IDS, "fs1": "mutable"}}, False),
            ({"image_set": invalid_normalizer}, False),
            ({}, True),
        )
        for changes, preexisting in cases:
            with self.subTest(changes=tuple(changes), preexisting=preexisting):
                docker = FakeQualificationDocker(preexisting=preexisting)
                executor = FakeQualificationExecutor()
                with tempfile.TemporaryDirectory() as directory:
                    journal = Path(directory) / "qualification.jsonl"
                    arguments = {
                        "source": self.source,
                        "journal_path": journal,
                        "image_set": image_set(),
                        "evaluator_sha256": V07_STRICT_REPLAY_EVALUATOR_SHA256,
                        "execution_pins": self.execution,
                        "docker_cli": docker,
                        "action_executor": executor,
                    }
                    arguments.update(changes)
                    with self.assertRaises(V07DockerQualificationError) as caught:
                        run_v07_docker_qualification(**arguments)  # type: ignore[arg-type]
                    self.assertEqual(
                        str(caught.exception),
                        "v0.7 Docker qualification failed",
                    )
                    self.assertEqual(
                        repr(caught.exception),
                        "<V07DockerQualificationError redacted>",
                    )
                    self.assertFalse(journal.exists())
                    self.assertEqual(docker.specs, [])
                    self.assertEqual(executor.actions, [])

    def test_executor_cleanup_and_duplicate_replay_faults_are_redacted(self) -> None:
        cases = (
            (FakeQualificationDocker(), FakeQualificationExecutor(fail=True)),
            (
                FakeQualificationDocker(refuse_cleanup=True),
                FakeQualificationExecutor(),
            ),
            (
                FakeQualificationDocker(disagree_replays=True),
                FakeQualificationExecutor(),
            ),
        )
        first_gold = self.source.gold_for_evaluator(
            self.source.private_reference(CAMPAIGN_TASK_IDS[0])
        )
        for docker, executor in cases:
            with self.subTest(
                refuse_cleanup=docker.refuse_cleanup,
                disagree=docker.disagree_replays,
                executor_failure=executor.fail,
            ):
                with tempfile.TemporaryDirectory() as directory:
                    journal = Path(directory) / "qualification.jsonl"
                    with self.assertRaises(V07DockerQualificationError) as caught:
                        run_v07_docker_qualification(
                            source=self.source,
                            journal_path=journal,
                            image_set=image_set(),
                            evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
                            execution_pins=self.execution,
                            docker_cli=docker,  # type: ignore[arg-type]
                            action_executor=executor,  # type: ignore[arg-type]
                        )
                    rendered = f"{caught.exception!s} {caught.exception!r}"
                    for secret in (first_gold, "/Users/", "SECRET_GOLD"):
                        self.assertNotIn(secret, rendered)
                    self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
