"""Bounded production construction for v0.7 Docker attempt boundaries.

The factory is deliberately narrower than a Docker or evaluator factory.  It
retains only path-free public identities and runtime dependencies needed to
create a fresh qualified agent container.  Authority-sealed reference material
is checked at construction and then discarded; the episode runner remains the
only component that opens it inside the strict-evaluator closure.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Protocol

from .docker_action_executor import DockerActionExecutor, DockerActionLimits
from .docker_cli import DockerCli, DockerContainerSpec, DockerLimits
from .intercode_campaign_ledger import (
    CAMPAIGN_ATTEMPT_CAP,
    CAMPAIGN_EPISODE_COUNT,
    CAMPAIGN_MODELS,
    CAMPAIGN_SEED,
    CAMPAIGN_TASK_IDS,
    CampaignEpisode,
    CampaignSpec,
)
from .intercode_docker_attempt import DockerAttemptBoundary
from .intercode_source import (
    CALIBRATION_POPULATION_SHA256,
    PUBLIC_POPULATION_SHA256,
    SOURCE_CORPUS_SHA256,
    STATIC_EXCLUSION_AUDIT_SHA256,
    InterCodeSource,
    PublicBashTask,
)
from .intercode_v07_calibration import (
    V07_CALIBRATION_ARMS,
    V07_CALIBRATION_EPISODE_COUNT,
    V07_CALIBRATION_TASK_IDS,
)
from .intercode_v07_docker_qualification import (
    V07CalibrationGoldResult,
    V07TrustedGoldMaterial,
)
from .intercode_v07_manifest import (
    V07_RUN_ID_POLICY_REVISION,
    V07ExecutionPins,
    V07PrecalibrationManifest,
)
from .intercode_v07_protocol import build_v07_sample
from .intercode_v07_study_binding import V07PreparedStudy


V07_ATTEMPT_FACTORY_REVISION = "intercode-v0.7-docker-attempt-factory-v1"
V07_ATTEMPT_RUN_ID_POLICY_REVISION = (
    "intercode-v0.7-run-id-campaign-episode-attempt-role-sha256-v1"
)

_PHASES = frozenset(("calibration", "formal"))
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RUN_ID_DOMAIN = b"edgeloopbench.v0.7.production-attempt-run-id.v1\0"
_BINDING_DOMAIN = b"edgeloopbench.v0.7.production-attempt-binding.v1\0"
_FACTORY_SEAL = object()
_FORMAL_EPISODES = CampaignSpec(CAMPAIGN_TASK_IDS).episodes
_CALIBRATION_EPISODES = tuple(
    CampaignEpisode(index, model_id, task_id, arm, CAMPAIGN_SEED)
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


class V07AttemptFactoryError(ValueError):
    """One redacted rejection for an unsafe attempt-factory input or use."""

    def __init__(self) -> None:
        super().__init__("v0.7 Docker attempt factory rejected its inputs")

    def __repr__(self) -> str:
        return "<V07AttemptFactoryError redacted>"


class _DockerBoundary(Protocol):
    def list_run_containers(self, run_id: str) -> tuple[str, ...]: ...

    def create_container(self, spec: DockerContainerSpec) -> object: ...

    def start_container(self, container: object) -> object: ...

    def collect_trusted_state(self, container: object, *, profile: str) -> object: ...

    def remove_run_containers(
        self,
        run_id: str,
        identifiers: Sequence[str],
    ) -> tuple[str, ...]: ...


class _ActionExecutor(Protocol):
    def execute(self, **values: object) -> object: ...


class V07DockerAttemptFactory:
    """Create at most the arm-specific cap of fresh qualified boundaries."""

    __slots__ = (
        "_action_executor",
        "_action_limits",
        "_agent_image_id",
        "_attempt_cap",
        "_attempts_started",
        "_binding_sha256",
        "_campaign_root_sha256",
        "_docker_cli",
        "_docker_limits",
        "_episode_index",
        "_lock",
        "_phase",
        "_profile",
        "_run_id_policy_revision",
    )

    def __init__(
        self,
        *,
        phase: str,
        campaign_root_sha256: str,
        episode_index: int,
        profile: str,
        agent_image_id: str,
        docker_limits: DockerLimits,
        action_limits: DockerActionLimits,
        run_id_policy_revision: str,
        attempt_cap: int,
        binding_sha256: str,
        docker_cli: _DockerBoundary,
        action_executor: _ActionExecutor,
        _construction_seal: object | None = None,
    ) -> None:
        if _construction_seal is not _FACTORY_SEAL:
            raise V07AttemptFactoryError()
        self._phase = phase
        self._campaign_root_sha256 = campaign_root_sha256
        self._episode_index = episode_index
        self._profile = profile
        self._agent_image_id = agent_image_id
        self._docker_limits = docker_limits
        self._action_limits = action_limits
        self._run_id_policy_revision = run_id_policy_revision
        self._attempt_cap = attempt_cap
        self._attempts_started = 0
        self._binding_sha256 = binding_sha256
        self._docker_cli = docker_cli
        self._action_executor = action_executor
        self._lock = threading.Lock()

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def attempt_cap(self) -> int:
        return self._attempt_cap

    @property
    def attempts_started(self) -> int:
        with self._lock:
            return self._attempts_started

    @property
    def binding_sha256(self) -> str:
        return self._binding_sha256

    def __repr__(self) -> str:
        with self._lock:
            count = self._attempts_started
        return (
            f"<V07DockerAttemptFactory phase={self._phase} "
            f"attempts={count}/{self._attempt_cap}>"
        )

    def __reduce__(self) -> object:
        raise TypeError("v0.7 Docker attempt factories cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("v0.7 Docker attempt factories cannot be serialized")

    def __call__(self) -> DockerAttemptBoundary:
        """Consume one attempt slot before constructing its Docker boundary."""

        with self._lock:
            if self._attempts_started >= self._attempt_cap:
                raise V07AttemptFactoryError()
            attempt_index = self._attempts_started + 1
            # Consume the slot before Docker work.  A partially created resource
            # must never cause the same run ID to be reused after an exception.
            self._attempts_started = attempt_index
        run_id = v07_attempt_run_id(
            campaign_root_sha256=self._campaign_root_sha256,
            episode_index=self._episode_index,
            attempt_index=attempt_index,
            role="agent",
            run_id_policy_revision=self._run_id_policy_revision,
        )
        spec = DockerContainerSpec(
            run_id=run_id,
            role="agent",
            image=self._agent_image_id,
            limits=self._docker_limits,
            image_id=self._agent_image_id,
        )
        return DockerAttemptBoundary(
            docker_cli=self._docker_cli,  # type: ignore[arg-type]
            action_executor=self._action_executor,  # type: ignore[arg-type]
            container_spec=spec,
            profile=self._profile,
            action_limits=self._action_limits,
        )


def v07_attempt_run_id(
    *,
    campaign_root_sha256: str,
    episode_index: int,
    attempt_index: int,
    role: str,
    run_id_policy_revision: str,
) -> str:
    """Return the path-free deterministic ID for one production attempt."""

    if (
        type(campaign_root_sha256) is not str
        or _SHA256.fullmatch(campaign_root_sha256) is None
        or isinstance(episode_index, bool)
        or not isinstance(episode_index, int)
        or not 1 <= episode_index <= CAMPAIGN_EPISODE_COUNT
        or isinstance(attempt_index, bool)
        or not isinstance(attempt_index, int)
        or not 1 <= attempt_index <= CAMPAIGN_ATTEMPT_CAP
        or role != "agent"
        or run_id_policy_revision != V07_RUN_ID_POLICY_REVISION
    ):
        raise V07AttemptFactoryError()
    record = {
        "attempt_index": attempt_index,
        "campaign_root_sha256": campaign_root_sha256,
        "episode_index": episode_index,
        "parent_policy_revision": run_id_policy_revision,
        "policy_revision": V07_ATTEMPT_RUN_ID_POLICY_REVISION,
        "role": role,
    }
    hasher = hashlib.sha256(_RUN_ID_DOMAIN)
    hasher.update(_canonical_json(record))
    return "v07-" + hasher.hexdigest()[:20]


def build_v07_formal_docker_attempt_factory(
    *,
    prepared_study: V07PreparedStudy,
    episode: CampaignEpisode,
    source: InterCodeSource,
    task: PublicBashTask,
    manifest: V07PrecalibrationManifest,
    docker_cli: DockerCli,
    action_executor: DockerActionExecutor,
) -> V07DockerAttemptFactory:
    """Build one formal factory from a verifier-sealed study authority."""

    try:
        if (
            type(prepared_study) is not V07PreparedStudy
            or type(episode) is not CampaignEpisode
            or type(manifest) is not V07PrecalibrationManifest
        ):
            raise ValueError("formal authority type differs")
        prepared = prepared_study.canonical_record()
        binding = prepared.get("binding")
        bound_spec = prepared_study.bound_campaign_spec
        if (
            not isinstance(binding, Mapping)
            or binding.get("formal_campaign_sha256")
            != prepared_study.formal_campaign_sha256
            or binding.get("study_binding_sha256")
            != prepared_study.study_binding_sha256
            or binding.get("manifest_sha256") != manifest.manifest_sha256
            or prepared_study.execution_pins != manifest.execution
            or bound_spec.study_binding_sha256
            != prepared_study.study_binding_sha256
            or bound_spec.episodes[episode.episode_index - 1] != episode
        ):
            raise ValueError("prepared study binding differs")
        private_gold = prepared_study.trusted_gold_for_episode(episode)
        return _build_v07_docker_attempt_factory(
            phase="formal",
            campaign_root_sha256=prepared_study.formal_campaign_sha256,
            episode=episode,
            source=source,
            task=task,
            manifest=manifest,
            private_gold=private_gold,
            docker_cli=docker_cli,
            action_executor=action_executor,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise V07AttemptFactoryError() from None


def build_v07_calibration_docker_attempt_factory(
    *,
    calibration_gold: V07CalibrationGoldResult,
    episode: CampaignEpisode,
    source: InterCodeSource,
    task: PublicBashTask,
    manifest: V07PrecalibrationManifest,
    docker_cli: DockerCli,
    action_executor: DockerActionExecutor,
) -> V07DockerAttemptFactory:
    """Build one calibration factory from the exact sealed gold campaign."""

    try:
        if (
            type(calibration_gold) is not V07CalibrationGoldResult
            or type(episode) is not CampaignEpisode
            or type(manifest) is not V07PrecalibrationManifest
        ):
            raise ValueError("calibration authority type differs")
        campaign_root_sha256 = calibration_gold.calibration_campaign_sha256
        if _SHA256.fullmatch(campaign_root_sha256) is None:
            raise ValueError("calibration campaign root differs")
        gold = calibration_gold.trusted_gold_by_task_id
        if tuple(gold) != V07_CALIBRATION_TASK_IDS or any(
            type(value) is not V07TrustedGoldMaterial or value.task_id != task_id
            for task_id, value in gold.items()
        ):
            raise ValueError("calibration gold authority differs")
        private_gold = gold[episode.task_id]
        return _build_v07_docker_attempt_factory(
            phase="calibration",
            campaign_root_sha256=campaign_root_sha256,
            episode=episode,
            source=source,
            task=task,
            manifest=manifest,
            private_gold=private_gold,
            docker_cli=docker_cli,
            action_executor=action_executor,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise V07AttemptFactoryError() from None


def _build_v07_docker_attempt_factory(
    *,
    phase: object,
    campaign_root_sha256: object,
    episode: object,
    source: object,
    task: object,
    manifest: object,
    private_gold: object,
    docker_cli: object,
    action_executor: object,
) -> V07DockerAttemptFactory:
    if phase not in _PHASES or type(phase) is not str:
        raise ValueError("phase differs")
    if (
        type(campaign_root_sha256) is not str
        or _SHA256.fullmatch(campaign_root_sha256) is None
    ):
        raise ValueError("campaign root differs")
    if type(episode) is not CampaignEpisode:
        raise ValueError("episode type differs")
    schedule = _FORMAL_EPISODES if phase == "formal" else _CALIBRATION_EPISODES
    expected_episode_count = (
        CAMPAIGN_EPISODE_COUNT
        if phase == "formal"
        else V07_CALIBRATION_EPISODE_COUNT
    )
    if (
        len(schedule) != expected_episode_count
        or not 1 <= episode.episode_index <= len(schedule)
        or episode != schedule[episode.episode_index - 1]
    ):
        raise ValueError("episode schedule differs")

    expected_task = _exact_source_task(
        phase=phase,
        source=source,
        task_id=episode.task_id,
    )
    if type(task) is not PublicBashTask or task is not expected_task:
        raise ValueError("public task capability differs")

    if type(manifest) is not V07PrecalibrationManifest:
        raise ValueError("manifest type differs")
    manifest_record = manifest.canonical_record()
    if (
        not isinstance(manifest_record, Mapping)
        or manifest_record.get("manifest_sha256") != manifest.manifest_sha256
    ):
        raise ValueError("manifest identity differs")
    if type(manifest.execution) is not V07ExecutionPins:
        raise ValueError("execution pin type differs")
    execution = manifest.execution
    execution.canonical_record()
    if (
        execution.run_id_policy_revision != V07_RUN_ID_POLICY_REVISION
        or manifest.design.attempt_cap != CAMPAIGN_ATTEMPT_CAP
        or tuple(item.model_id for item in manifest.models) != CAMPAIGN_MODELS
        or episode.model_id not in CAMPAIGN_MODELS
        or tuple(manifest.artifacts.task_ids) != CAMPAIGN_TASK_IDS
    ):
        raise ValueError("manifest campaign identity differs")

    qualification = manifest.artifacts.qualification
    images = dict(qualification.image_id_by_stratum)
    if tuple(sorted(images)) != ("fs1", "fs2", "fs3", "fs4") or any(
        type(value) is not str or _SHA256.fullmatch(value) is None
        for value in images.values()
    ):
        raise ValueError("qualified image map differs")
    profile = task.stratum if phase == "formal" else "fs1"
    agent_image_id = images[profile]

    if type(private_gold) is not V07TrustedGoldMaterial:
        raise ValueError("trusted gold type differs")
    reference = source.private_reference(task.task_id)
    reference_task, source_capability_sha256 = source.qualification_identity(reference)
    if (
        reference_task is not task
        or private_gold.task_id != task.task_id
        or private_gold.source_capability_sha256 != source_capability_sha256
        or private_gold.image_id != agent_image_id
        or private_gold.evaluator_sha256 != qualification.evaluator_sha256
        or private_gold.state_normalization_sha256
        != qualification.state_normalization_sha256
    ):
        raise ValueError("trusted evaluator authority differs")

    for dependency, methods in (
        (
            docker_cli,
            (
                "list_run_containers",
                "create_container",
                "start_container",
                "collect_trusted_state",
                "remove_run_containers",
            ),
        ),
        (action_executor, ("execute",)),
    ):
        if any(not callable(getattr(dependency, method, None)) for method in methods):
            raise ValueError("attempt dependency differs")

    attempt_cap = 1 if episode.arm == "direct" else CAMPAIGN_ATTEMPT_CAP
    binding_sha256 = _binding_sha256(
        phase=phase,
        campaign_root_sha256=campaign_root_sha256,
        manifest_sha256=manifest.manifest_sha256,
        execution_pins_sha256=execution.execution_pins_sha256,
        episode=episode,
        profile=profile,
        agent_image_id=agent_image_id,
        evaluator_reference_image_id=private_gold.image_id,
        evaluator_sha256=private_gold.evaluator_sha256,
        state_normalization_sha256=private_gold.state_normalization_sha256,
        attempt_cap=attempt_cap,
    )
    return V07DockerAttemptFactory(
        phase=phase,
        campaign_root_sha256=campaign_root_sha256,
        episode_index=episode.episode_index,
        profile=profile,
        agent_image_id=agent_image_id,
        docker_limits=execution.docker_limits,
        action_limits=execution.docker_action_limits,
        run_id_policy_revision=execution.run_id_policy_revision,
        attempt_cap=attempt_cap,
        binding_sha256=binding_sha256,
        docker_cli=docker_cli,  # type: ignore[arg-type]
        action_executor=action_executor,  # type: ignore[arg-type]
        _construction_seal=_FACTORY_SEAL,
    )


def _exact_source_task(
    *,
    phase: str,
    source: object,
    task_id: str,
) -> PublicBashTask:
    if type(source) is not InterCodeSource:
        raise ValueError("source type differs")
    if (
        source.source_sha256 != SOURCE_CORPUS_SHA256
        or source.static_exclusion_audit_sha256 != STATIC_EXCLUSION_AUDIT_SHA256
    ):
        raise ValueError("source identity differs")
    if phase == "formal":
        if (
            source.population_sha256 != PUBLIC_POPULATION_SHA256
            or build_v07_sample(source) != CAMPAIGN_TASK_IDS
        ):
            raise ValueError("formal source differs")
        population = source.tasks
    else:
        population = source.calibration_tasks[:4]
        if (
            source.calibration_population_sha256 != CALIBRATION_POPULATION_SHA256
            or tuple(item.task_id for item in population)
            != V07_CALIBRATION_TASK_IDS
        ):
            raise ValueError("calibration source differs")
    by_id = {item.task_id: item for item in population}
    if tuple(by_id) != tuple(item.task_id for item in population):
        raise ValueError("source task IDs are not unique")
    try:
        return by_id[task_id]
    except KeyError:
        raise ValueError("task is outside the phase population") from None


def _binding_sha256(
    *,
    phase: str,
    campaign_root_sha256: str,
    manifest_sha256: str,
    execution_pins_sha256: str,
    episode: CampaignEpisode,
    profile: str,
    agent_image_id: str,
    evaluator_reference_image_id: str,
    evaluator_sha256: str,
    state_normalization_sha256: str,
    attempt_cap: int,
) -> str:
    record = {
        "agent_image_id": agent_image_id,
        "attempt_cap": attempt_cap,
        "campaign_root_sha256": campaign_root_sha256,
        "episode": {
            "arm": episode.arm,
            "episode_index": episode.episode_index,
            "model_id": episode.model_id,
            "seed": episode.seed,
            "task_id": episode.task_id,
        },
        "evaluator_reference_image_id": evaluator_reference_image_id,
        "evaluator_sha256": evaluator_sha256,
        "execution_pins_sha256": execution_pins_sha256,
        "factory_revision": V07_ATTEMPT_FACTORY_REVISION,
        "manifest_sha256": manifest_sha256,
        "phase": phase,
        "profile": profile,
        "run_id_policy_revision": V07_ATTEMPT_RUN_ID_POLICY_REVISION,
        "state_normalization_sha256": state_normalization_sha256,
    }
    hasher = hashlib.sha256(_BINDING_DOMAIN)
    hasher.update(_canonical_json(record))
    return "sha256:" + hasher.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


__all__ = (
    "V07_ATTEMPT_FACTORY_REVISION",
    "V07_ATTEMPT_RUN_ID_POLICY_REVISION",
    "V07AttemptFactoryError",
    "V07DockerAttemptFactory",
    "build_v07_calibration_docker_attempt_factory",
    "build_v07_formal_docker_attempt_factory",
    "v07_attempt_run_id",
)
