from __future__ import annotations

import hashlib
import json
import pickle
import unittest
from types import MappingProxyType
from types import SimpleNamespace
from unittest import mock

from edgeloopbench.docker_action_executor import (
    DockerActionLimits,
    DockerActionResult,
)
from edgeloopbench.docker_cli import (
    DockerContainer,
    DockerContainerSpec,
    DockerLimits,
    DockerTrustedState,
)
from edgeloopbench.intercode_campaign_ledger import (
    CAMPAIGN_MODELS,
    CAMPAIGN_TASK_IDS,
    CampaignEpisode,
    CampaignSpec,
)
from edgeloopbench.intercode_docker_attempt import DockerAttemptInfrastructureError
from edgeloopbench.intercode_evaluator import CanonicalStateSnapshot
from edgeloopbench.intercode_local_model import (
    OLLAMA_GENERATION_ENDPOINT_SHA256,
    OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
)
from edgeloopbench.intercode_replay_environment import (
    CandidateMaterial,
    V07_STRICT_REPLAY_EVALUATOR_SHA256,
)
from edgeloopbench.intercode_source import PublicBashTask, load_intercode_source
from edgeloopbench.intercode_v07_attempt_factory import (
    V07_ATTEMPT_RUN_ID_POLICY_REVISION,
    V07AttemptFactoryError,
    V07DockerAttemptFactory,
    build_v07_calibration_docker_attempt_factory,
    build_v07_formal_docker_attempt_factory,
    v07_attempt_run_id,
)
from edgeloopbench.intercode_v07_calibration import (
    V07_CALIBRATION_ARMS,
    V07_CALIBRATION_TASK_IDS,
)
from edgeloopbench.intercode_v07_docker_qualification import (
    V07CalibrationGoldResult,
    V07TrustedGoldMaterial,
    _RESULT_SEAL,
    _issue_v07_trusted_gold_material,
)
from edgeloopbench.intercode_v07_manifest import (
    V07_INTERVENTION_JOURNAL_REVISION,
    V07_RUN_ID_POLICY_REVISION,
    V07ExecutionPins,
    V07HostIdentityPins,
    V07HostSafetyPins,
    V07PrecalibrationManifest,
    _EXECUTION_SEAL,
    _HOST_SAFETY_SEAL,
    _digest_record,
    _execution_core_record,
)
from edgeloopbench.intercode_v07_study_binding import V07PreparedStudy


NORMALIZER_SHA256 = "sha256:" + "9" * 64
IMAGE_IDS = {
    "fs1": "sha256:" + "1" * 64,
    "fs2": "sha256:" + "2" * 64,
    "fs3": "sha256:" + "3" * 64,
    "fs4": "sha256:" + "4" * 64,
}


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
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
            writable_layer_watchdog_bytes=256 << 20,
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


def trusted_state(profile: str) -> DockerTrustedState:
    profile_sha256 = digest(f"profile-{profile}")
    audit_sha256 = digest(f"audit-{profile}")
    state_sha256 = digest(
        canonical(
            {
                "entries": [],
                "profile_sha256": profile_sha256,
                "schema": "edgeloopbench.filesystem-state.v1",
                "writable_surface_audit_sha256": audit_sha256,
            }
        )
    )
    payload = {
        "common_roots": ["home/agent", "tmp"],
        "dynamic_root_policy": "non_baseline_top_level",
        "entries": [],
        "entry_count": 0,
        "policy_sha256": digest("policy"),
        "profile": profile,
        "profile_sha256": profile_sha256,
        "root_baseline_sha256": digest("root"),
        "schema": "edgeloopbench.filesystem-state.v1",
        "state_sha256": state_sha256,
        "strict_surface": {"failures": [], "status": "representable"},
        "task_roots": ["testbed"],
        "total_file_bytes": 0,
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


class FakeDocker:
    def __init__(self) -> None:
        self.specs: list[DockerContainerSpec] = []
        self.containers: dict[str, DockerContainer] = {}

    def list_run_containers(self, run_id: str) -> tuple[str, ...]:
        return tuple(
            identifier
            for identifier, container in self.containers.items()
            if container.spec.run_id == run_id
        )

    def create_container(self, spec: DockerContainerSpec) -> DockerContainer:
        identifier = hashlib.sha256(
            f"container-{len(self.specs)}".encode("ascii")
        ).hexdigest()
        container = DockerContainer(
            identifier=identifier,
            name=f"elb-{spec.run_id}-agent-{identifier[:16]}",
            image_id=spec.image_id,
            labels=(),
            spec=spec,
        )
        self.specs.append(spec)
        self.containers[identifier] = container
        return container

    def start_container(self, container: DockerContainer) -> DockerContainer:
        return container

    def collect_trusted_state(
        self,
        container: DockerContainer,
        *,
        profile: str,
    ) -> DockerTrustedState:
        del container
        return trusted_state(profile)

    def remove_run_containers(
        self,
        run_id: str,
        identifiers: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(
            self.containers[identifier].spec.run_id != run_id
            for identifier in identifiers
        ):
            return ()
        for identifier in identifiers:
            self.containers.pop(identifier)
        return identifiers


class FakeExecutor:
    def execute(
        self,
        *,
        container: DockerContainer,
        action: str,
        cwd: str,
        limits: DockerActionLimits,
    ) -> DockerActionResult:
        del container, action, cwd, limits
        raise AssertionError("factory construction must not execute an action")


class FirstRunOccupiedDocker(FakeDocker):
    def __init__(self) -> None:
        super().__init__()
        self.first_list = True

    def list_run_containers(self, run_id: str) -> tuple[str, ...]:
        if self.first_list:
            self.first_list = False
            return ("e" * 64,)
        return super().list_run_containers(run_id)


def gold_for(
    source: object,
    task: PublicBashTask,
    *,
    task_id: str | None = None,
    image_id: str | None = None,
    evaluator_sha256: str = V07_STRICT_REPLAY_EVALUATOR_SHA256,
    capability_sha256: str | None = None,
) -> V07TrustedGoldMaterial:
    reference = source.private_reference(task.task_id)  # type: ignore[attr-defined]
    _task, exact_capability = source.qualification_identity(  # type: ignore[attr-defined]
        reference
    )
    material = CandidateMaterial(
        state=CanonicalStateSnapshot(()),
        collector_state_sha256=digest("gold-state"),
        exit_code=0,
        normalized_stdout="ok\n",
        normalized_stderr="",
        agent_observation="ok\n",
        state_changed=True,
    )
    return _issue_v07_trusted_gold_material(
        task_id=task_id or task.task_id,
        source_capability_sha256=capability_sha256 or exact_capability,
        image_id=(
            image_id
            or IMAGE_IDS["fs1" if task.stratum == "calibration" else task.stratum]
        ),
        evaluator_sha256=evaluator_sha256,
        state_normalization_sha256=NORMALIZER_SHA256,
        replay_receipt_sha256=digest(f"receipt-{task.task_id}"),
        material=material,
    )


def fake_manifest(execution: V07ExecutionPins) -> V07PrecalibrationManifest:
    qualification = SimpleNamespace(
        image_id_by_stratum=IMAGE_IDS,
        evaluator_sha256=V07_STRICT_REPLAY_EVALUATOR_SHA256,
        state_normalization_sha256=NORMALIZER_SHA256,
    )
    artifacts = SimpleNamespace(
        task_ids=CAMPAIGN_TASK_IDS,
        qualification=qualification,
    )
    manifest = object.__new__(V07PrecalibrationManifest)
    object.__setattr__(manifest, "artifacts", artifacts)
    object.__setattr__(
        manifest,
        "models",
        tuple(SimpleNamespace(model_id=model_id) for model_id in CAMPAIGN_MODELS),
    )
    object.__setattr__(manifest, "host_identity", execution.host_safety.host_identity)
    object.__setattr__(manifest, "execution", execution)
    object.__setattr__(manifest, "budgets", SimpleNamespace())
    object.__setattr__(manifest, "design", SimpleNamespace(attempt_cap=4))
    object.__setattr__(manifest, "manifest_sha256", digest("manifest"))
    return manifest


class InterCodeV07AttemptFactoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = load_intercode_source()
        cls.execution = execution_pins()
        cls.manifest = fake_manifest(cls.execution)
        cls.formal_episodes = CampaignSpec(CAMPAIGN_TASK_IDS).episodes
        cls.calibration_episodes = tuple(
            CampaignEpisode(index, model_id, task_id, arm, 11)
            for index, (model_id, task_id, arm) in enumerate(
                (
                    (model_id, task_id, arm)
                    for model_id in CAMPAIGN_MODELS
                    for task_id, arm in zip(
                        V07_CALIBRATION_TASK_IDS,
                        V07_CALIBRATION_ARMS,
                        strict=True,
                    )
                ),
                1,
            )
        )

    def build_factory(
        self,
        *,
        phase: str = "formal",
        campaign_root_sha256: str | None = None,
        episode: CampaignEpisode | None = None,
        task: PublicBashTask | None = None,
        private_gold: V07TrustedGoldMaterial | None = None,
        docker: FakeDocker | None = None,
    ) -> tuple[V07DockerAttemptFactory, FakeDocker]:
        selected_episode = episode or self.formal_episodes[1]
        population = (
            self.source.tasks
            if phase == "formal"
            else self.source.calibration_tasks[:4]
        )
        selected_task = task or next(
            item for item in population if item.task_id == selected_episode.task_id
        )
        selected_gold = private_gold or gold_for(self.source, selected_task)
        selected_docker = docker or FakeDocker()
        campaign_root = campaign_root_sha256 or digest(f"{phase}-campaign")
        with mock.patch.object(
            V07PrecalibrationManifest,
            "canonical_record",
            return_value={"manifest_sha256": self.manifest.manifest_sha256},
        ):
            if phase == "formal":
                prepared_study = object.__new__(V07PreparedStudy)
                study_binding_sha256 = digest("study-binding")
                with (
                    mock.patch.object(
                        V07PreparedStudy,
                        "canonical_record",
                        return_value={
                            "binding": {
                                "formal_campaign_sha256": campaign_root,
                                "manifest_sha256": self.manifest.manifest_sha256,
                                "study_binding_sha256": study_binding_sha256,
                            }
                        },
                    ),
                    mock.patch.object(
                        V07PreparedStudy,
                        "formal_campaign_sha256",
                        new_callable=mock.PropertyMock,
                        return_value=campaign_root,
                    ),
                    mock.patch.object(
                        V07PreparedStudy,
                        "execution_pins",
                        new_callable=mock.PropertyMock,
                        return_value=self.execution,
                    ),
                    mock.patch.object(
                        V07PreparedStudy,
                        "study_binding_sha256",
                        new_callable=mock.PropertyMock,
                        return_value=study_binding_sha256,
                    ),
                    mock.patch.object(
                        V07PreparedStudy,
                        "bound_campaign_spec",
                        new_callable=mock.PropertyMock,
                        return_value=CampaignSpec(CAMPAIGN_TASK_IDS).bind(
                            study_binding_sha256
                        ),
                    ),
                    mock.patch.object(
                        V07PreparedStudy,
                        "trusted_gold_for_episode",
                        return_value=selected_gold,
                    ),
                ):
                    factory = build_v07_formal_docker_attempt_factory(
                        prepared_study=prepared_study,
                        episode=selected_episode,
                        source=self.source,
                        task=selected_task,
                        manifest=self.manifest,
                        docker_cli=selected_docker,  # type: ignore[arg-type]
                        action_executor=FakeExecutor(),  # type: ignore[arg-type]
                    )
            else:
                calibration_gold = {
                    item.task_id: gold_for(self.source, item)
                    for item in self.source.calibration_tasks[:4]
                }
                if selected_task.task_id in calibration_gold:
                    calibration_gold[selected_task.task_id] = selected_gold
                authority = V07CalibrationGoldResult(
                    calibration_campaign_sha256=campaign_root,
                    trusted_gold_by_task_id=MappingProxyType(calibration_gold),
                    _construction_seal=_RESULT_SEAL,
                )
                factory = build_v07_calibration_docker_attempt_factory(
                    calibration_gold=authority,
                    episode=selected_episode,
                    source=self.source,
                    task=selected_task,
                    manifest=self.manifest,
                    docker_cli=selected_docker,  # type: ignore[arg-type]
                    action_executor=FakeExecutor(),  # type: ignore[arg-type]
                )
        return factory, selected_docker

    def test_run_id_is_deterministic_path_free_and_binds_every_input(self) -> None:
        campaign = digest("campaign")
        arguments = {
            "campaign_root_sha256": campaign,
            "episode_index": 7,
            "attempt_index": 3,
            "role": "agent",
            "run_id_policy_revision": V07_RUN_ID_POLICY_REVISION,
        }

        first = v07_attempt_run_id(**arguments)
        second = v07_attempt_run_id(**arguments)

        self.assertEqual(first, second)
        self.assertRegex(first, r"^v07-[0-9a-f]{20}$")
        self.assertNotIn("/", first)
        variants = {
            v07_attempt_run_id(
                **{**arguments, "campaign_root_sha256": digest("other")}
            ),
            v07_attempt_run_id(**{**arguments, "episode_index": 8}),
            v07_attempt_run_id(**{**arguments, "attempt_index": 4}),
        }
        self.assertEqual(len(variants), 3)
        self.assertNotIn(first, variants)
        self.assertEqual(
            V07_ATTEMPT_RUN_ID_POLICY_REVISION,
            "intercode-v0.7-run-id-campaign-episode-attempt-role-sha256-v1",
        )
        with self.assertRaises(V07AttemptFactoryError):
            v07_attempt_run_id(**{**arguments, "role": "evaluator"})
        with self.assertRaises(V07AttemptFactoryError):
            v07_attempt_run_id(
                **{**arguments, "run_id_policy_revision": "unreviewed-v2"}
            )

    def test_non_direct_factory_constructs_four_fresh_exact_boundaries(self) -> None:
        episode = self.formal_episodes[1]
        self.assertNotEqual(episode.arm, "direct")
        factory, docker = self.build_factory(episode=episode)

        for _index in range(4):
            boundary = factory()
            boundary.close()

        self.assertEqual(factory.attempt_cap, 4)
        self.assertEqual(factory.attempts_started, 4)
        self.assertRegex(factory.binding_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(len(docker.specs), 4)
        self.assertEqual(
            [spec.run_id for spec in docker.specs],
            [
                v07_attempt_run_id(
                    campaign_root_sha256=digest("formal-campaign"),
                    episode_index=episode.episode_index,
                    attempt_index=index,
                    role="agent",
                    run_id_policy_revision=V07_RUN_ID_POLICY_REVISION,
                )
                for index in range(1, 5)
            ],
        )
        for spec in docker.specs:
            self.assertEqual(spec.role, "agent")
            self.assertEqual(spec.image, IMAGE_IDS[episode.task_id[5:8]])
            self.assertEqual(spec.image_id, spec.image)
            self.assertEqual(spec.limits, self.execution.docker_limits)
        with self.assertRaises(V07AttemptFactoryError):
            factory()
        self.assertEqual(len(docker.specs), 4)

    def test_direct_factory_enforces_the_arm_specific_single_attempt(self) -> None:
        direct = self.formal_episodes[0]
        self.assertEqual(direct.arm, "direct")
        factory, docker = self.build_factory(episode=direct)

        boundary = factory()
        boundary.close()

        self.assertEqual(factory.attempt_cap, 1)
        with self.assertRaises(V07AttemptFactoryError):
            factory()
        self.assertEqual(len(docker.specs), 1)

    def test_failed_construction_consumes_its_run_id_before_any_retry(self) -> None:
        episode = self.formal_episodes[1]
        docker = FirstRunOccupiedDocker()
        factory, _docker = self.build_factory(episode=episode, docker=docker)

        with self.assertRaises(DockerAttemptInfrastructureError):
            factory()
        boundary = factory()
        boundary.close()

        self.assertEqual(factory.attempts_started, 2)
        self.assertEqual(len(docker.specs), 1)
        self.assertEqual(
            docker.specs[0].run_id,
            v07_attempt_run_id(
                campaign_root_sha256=digest("formal-campaign"),
                episode_index=episode.episode_index,
                attempt_index=2,
                role="agent",
                run_id_policy_revision=V07_RUN_ID_POLICY_REVISION,
            ),
        )

    def test_calibration_schedule_uses_the_same_factory_contract(self) -> None:
        episode = self.calibration_episodes[1]
        task = self.source.calibration_tasks[1]

        factory, docker = self.build_factory(
            phase="calibration",
            episode=episode,
            task=task,
        )
        boundary = factory()
        boundary.close()

        self.assertEqual(factory.phase, "calibration")
        self.assertEqual(docker.specs[0].image_id, IMAGE_IDS["fs1"])

    def test_rejects_cross_phase_task_model_and_campaign_mismatches(self) -> None:
        formal = self.formal_episodes[1]
        calibration_task = self.source.calibration_tasks[1]
        cases = (
            {
                "phase": "calibration",
                "episode": formal,
                "task": next(
                    task for task in self.source.tasks if task.task_id == formal.task_id
                ),
            },
            {
                "phase": "formal",
                "episode": self.calibration_episodes[1],
                "task": calibration_task,
            },
            {
                "phase": "formal",
                "episode": CampaignEpisode(
                    formal.episode_index,
                    CAMPAIGN_MODELS[1],
                    formal.task_id,
                    formal.arm,
                    formal.seed,
                ),
                "task": next(
                    task for task in self.source.tasks if task.task_id == formal.task_id
                ),
            },
            {"campaign_root_sha256": "sha256:" + "A" * 64},
        )
        for values in cases:
            with self.subTest(values=tuple(values)):
                with self.assertRaises(V07AttemptFactoryError):
                    self.build_factory(**values)  # type: ignore[arg-type]

        first, _docker = self.build_factory(campaign_root_sha256=digest("one"))
        second, _docker = self.build_factory(campaign_root_sha256=digest("two"))
        self.assertNotEqual(first.binding_sha256, second.binding_sha256)

    def test_rejects_forged_public_task_and_gold_authority_mismatches(self) -> None:
        episode = self.formal_episodes[1]
        exact_task = next(
            task for task in self.source.tasks if task.task_id == episode.task_id
        )
        forged_equal_task = PublicBashTask(
            exact_task.task_id,
            exact_task.query,
            exact_task.stratum,
        )
        wrong_image_gold = gold_for(
            self.source,
            exact_task,
            image_id=digest("unqualified-image"),
        )
        wrong_evaluator_gold = gold_for(
            self.source,
            exact_task,
            evaluator_sha256=digest("wrong-evaluator"),
        )
        wrong_capability_gold = gold_for(
            self.source,
            exact_task,
            capability_sha256=digest("wrong-capability"),
        )
        for values in (
            {"task": forged_equal_task},
            {"private_gold": wrong_image_gold},
            {"private_gold": wrong_evaluator_gold},
            {"private_gold": wrong_capability_gold},
        ):
            with self.subTest(values=tuple(values)):
                with self.assertRaises(V07AttemptFactoryError):
                    self.build_factory(
                        episode=episode,
                        **values,  # type: ignore[arg-type]
                    )

    def test_factory_does_not_retain_or_render_private_evaluator_material(self) -> None:
        episode = self.formal_episodes[1]
        task = next(
            item for item in self.source.tasks if item.task_id == episode.task_id
        )
        private_gold = gold_for(self.source, task)

        factory, _docker = self.build_factory(
            episode=episode,
            task=task,
            private_gold=private_gold,
        )

        retained = [getattr(factory, slot) for slot in factory.__slots__]
        self.assertFalse(
            any(
                isinstance(value, (V07TrustedGoldMaterial, CandidateMaterial))
                for value in retained
            )
        )
        rendered = repr(factory)
        for private in (
            task.query,
            private_gold.replay_receipt_sha256,
            digest("formal-campaign"),
            "/Users/",
        ):
            self.assertNotIn(private, rendered)
        with self.assertRaises(TypeError):
            pickle.dumps(factory)


if __name__ == "__main__":
    unittest.main()
