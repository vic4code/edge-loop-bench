"""Production composition of model-major v0.7 calibration dependencies."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from .docker_action_executor import (
    DockerActionExecutor,
    DockerActionExecutorBoundaryIdentity,
)
from .docker_cli import DockerCli, DockerCliBoundaryIdentity
from .intercode_campaign_ledger import CAMPAIGN_MODELS, CAMPAIGN_SEED, CampaignEpisode
from .intercode_host_safety import attest_docker_executable
from .intercode_source import InterCodeSource
from .intercode_v07_attempt_factory import (
    V07DockerAttemptFactory,
    build_v07_calibration_docker_attempt_factory,
)
from .intercode_v07_calibration import (
    V07_CALIBRATION_ARMS,
    V07_CALIBRATION_TASK_IDS,
)
from .intercode_v07_calibration_executor import (
    V07CalibrationExecutionRow,
    V07CalibrationRuntime,
)
from .intercode_v07_docker_qualification import V07CalibrationGoldResult
from .intercode_v07_host_policy import V07HostSafetySession
from .intercode_v07_manifest import V07PrecalibrationManifest
from .intercode_v07_runtime_factory import V07ModelRuntime, V07RuntimeSession


V07_CALIBRATION_RUNTIME_COMPOSER_REVISION = (
    "intercode-v0.7-calibration-runtime-composer-v1"
)
V07_CALIBRATION_DOCKER_CONTEXT = "desktop-linux"

_CONSTRUCTION_SEAL = object()
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


class V07CalibrationRuntimeCompositionError(RuntimeError):
    """Calibration runtime authorities are incomplete, stale, or out of order."""


V07CalibrationPhaseFactory = Callable[
    [V07ModelRuntime | None, V07ModelRuntime],
    V07HostSafetySession,
]


class V07CalibrationRuntimeComposer:
    """Issue one runtime row at a time in exact Qwen-then-Phi order."""

    __slots__ = (
        "_action_executor",
        "_active_model",
        "_active_session",
        "_calibration_gold",
        "_docker_cli",
        "_lock",
        "_manifest",
        "_next_episode_index",
        "_open_phase",
        "_runtime_session",
        "_source",
    )

    def __init__(
        self,
        *,
        runtime_session: V07RuntimeSession,
        source: InterCodeSource,
        calibration_gold: V07CalibrationGoldResult,
        manifest: V07PrecalibrationManifest,
        open_phase: V07CalibrationPhaseFactory,
        docker_cli: DockerCli,
        action_executor: DockerActionExecutor,
        _construction_seal: object | None = None,
    ) -> None:
        if _construction_seal is not _CONSTRUCTION_SEAL:
            raise V07CalibrationRuntimeCompositionError(
                "calibration runtime composers must be builder-issued"
            )
        self._runtime_session = runtime_session
        self._source = source
        self._calibration_gold = calibration_gold
        self._manifest = manifest
        self._open_phase = open_phase
        self._docker_cli = docker_cli
        self._action_executor = action_executor
        self._active_model: V07ModelRuntime | None = None
        self._active_session: V07HostSafetySession | None = None
        self._next_episode_index = 1
        self._lock = threading.Lock()

    def __call__(self, row: V07CalibrationExecutionRow) -> V07CalibrationRuntime:
        with self._lock:
            try:
                if (
                    type(row) is not V07CalibrationExecutionRow
                    or row.episode.episode_index != self._next_episode_index
                    or row.episode != _CALIBRATION_EPISODES[
                        self._next_episode_index - 1
                    ]
                ):
                    raise ValueError("calibration row order differs")
                target = self._runtime_session.model_runtime(row.episode.model_id)
                if self._active_model is None or target.model_id != self._active_model.model_id:
                    phase_index = (row.episode.episode_index - 1) // len(
                        V07_CALIBRATION_TASK_IDS
                    )
                    if target.model_id != CAMPAIGN_MODELS[phase_index]:
                        raise ValueError("calibration model phase differs")
                    phase = self._open_phase(self._active_model, target)
                    _validate_phase_session(phase, target, self._manifest)
                    self._active_model = target
                    self._active_session = phase
                assert self._active_session is not None
                boundary = build_v07_calibration_docker_attempt_factory(
                    calibration_gold=self._calibration_gold,
                    episode=row.episode,
                    source=self._source,
                    task=row.task,
                    manifest=self._manifest,
                    docker_cli=self._docker_cli,
                    action_executor=self._action_executor,
                )
                if type(boundary) is not V07DockerAttemptFactory:
                    raise ValueError("calibration attempt authority differs")
                admission = self._active_session.issue_episode_admission()
                try:
                    runtime = V07CalibrationRuntime(
                        model=target.model,
                        prompt_preparer=target.prompt_preparer,
                        boundary_factory=boundary,
                        before_episode_admission=(
                            admission.before_episode_admission
                        ),
                        after_episode_admission=admission.after_episode_admission,
                        abort_episode_admission=admission.abort,
                    )
                except BaseException:
                    admission.abort()
                    raise
                self._next_episode_index += 1
                return runtime
            except (KeyboardInterrupt, SystemExit):
                raise
            except V07CalibrationRuntimeCompositionError:
                raise
            except Exception:
                raise V07CalibrationRuntimeCompositionError(
                    "v0.7 calibration runtime composition failed closed"
                ) from None


def build_v07_calibration_runtime_composer(
    *,
    runtime_session: V07RuntimeSession,
    source: InterCodeSource,
    calibration_gold: V07CalibrationGoldResult,
    manifest: V07PrecalibrationManifest,
    open_phase: V07CalibrationPhaseFactory,
    docker_cli: DockerCli,
    action_executor: DockerActionExecutor,
) -> V07CalibrationRuntimeComposer:
    """Validate static authorities before any phase transition or admission."""

    try:
        if (
            type(runtime_session) is not V07RuntimeSession
            or type(source) is not InterCodeSource
            or type(calibration_gold) is not V07CalibrationGoldResult
            or type(manifest) is not V07PrecalibrationManifest
            or type(docker_cli) is not DockerCli
            or type(action_executor) is not DockerActionExecutor
            or not callable(open_phase)
        ):
            raise ValueError("calibration composer authority type differs")
        runtime_record = runtime_session.canonical_record()
        manifest_record = manifest.canonical_record()
        if (
            tuple(
                item["model_identity"] for item in runtime_record["models"]
            )
            != tuple(item.canonical_record() for item in manifest.models)
            or runtime_record["host_identity"]
            != manifest.host_identity.canonical_record()
            or manifest_record["manifest_sha256"] != manifest.manifest_sha256
        ):
            raise ValueError("calibration runtime session differs from manifest")
        _validate_docker_boundaries(
            docker_cli=docker_cli,
            action_executor=action_executor,
            manifest=manifest,
        )
        return V07CalibrationRuntimeComposer(
            runtime_session=runtime_session,
            source=source,
            calibration_gold=calibration_gold,
            manifest=manifest,
            open_phase=open_phase,
            docker_cli=docker_cli,
            action_executor=action_executor,
            _construction_seal=_CONSTRUCTION_SEAL,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except V07CalibrationRuntimeCompositionError:
        raise
    except Exception:
        raise V07CalibrationRuntimeCompositionError(
            "v0.7 calibration runtime composer construction failed closed"
        ) from None


def _validate_phase_session(
    session: object,
    target: V07ModelRuntime,
    manifest: V07PrecalibrationManifest,
) -> None:
    if type(session) is not V07HostSafetySession:
        raise ValueError("calibration phase session type differs")
    if (
        session.policy_pins != manifest.execution.host_safety
        or session.expected_resources.resident_models
        != (target.expected_resident_model,)
        or session.expected_resources.running_container_ids
    ):
        raise ValueError("calibration phase host authority differs")


def _validate_docker_boundaries(
    *,
    docker_cli: DockerCli,
    action_executor: DockerActionExecutor,
    manifest: V07PrecalibrationManifest,
) -> None:
    cli_identity = docker_cli.boundary_identity
    action_identity = action_executor.boundary_identity
    if (
        type(cli_identity) is not DockerCliBoundaryIdentity
        or type(action_identity) is not DockerActionExecutorBoundaryIdentity
        or cli_identity.expected_context != V07_CALIBRATION_DOCKER_CONTEXT
    ):
        raise ValueError("calibration Docker boundary identity differs")
    host = manifest.execution.host_safety.host_identity
    if (
        cli_identity.endpoint_sha256 != host.docker_endpoint_sha256
        or attest_docker_executable(Path(cli_identity.docker_binary))
        != host.docker_binary_sha256
        or action_identity.boundary is not docker_cli
        or action_identity.expected_docker_binary != cli_identity.docker_binary
        or action_identity.expected_endpoint != cli_identity.expected_endpoint
    ):
        raise ValueError("calibration Docker boundary is split or stale")


__all__ = (
    "V07_CALIBRATION_DOCKER_CONTEXT",
    "V07_CALIBRATION_RUNTIME_COMPOSER_REVISION",
    "V07CalibrationPhaseFactory",
    "V07CalibrationRuntimeComposer",
    "V07CalibrationRuntimeCompositionError",
    "build_v07_calibration_runtime_composer",
)
