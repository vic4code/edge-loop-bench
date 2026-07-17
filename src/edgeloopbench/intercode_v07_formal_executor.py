"""Production composition for one model-major v0.7 formal phase.

This module is the only formal bridge from a prepared study to the episode
runner.  It resolves the exact public task, live model runtime, qualified
Docker attempt factory, host-admission hooks, fixed budget, and canonical
append-only artifact paths.  It performs no work at import time.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .docker_action_executor import (
    DockerActionExecutor,
    DockerActionExecutorBoundaryIdentity,
)
from .docker_cli import DockerCli, DockerCliBoundaryIdentity
from .interactive_controller import InteractiveBudget
from .intercode_campaign_ledger import (
    CAMPAIGN_ATTEMPT_CAP,
    CAMPAIGN_MODELS,
    CampaignAdvance,
    CampaignEpisode,
    CampaignEpisodeExecution,
    CampaignProgress,
    CampaignSpec,
    advance_campaign,
    inspect_campaign,
    load_episode_execution_envelope,
)
from .intercode_host_safety import ResidentModel, attest_docker_executable
from .intercode_source import InterCodeSource, PublicBashTask
from .intercode_v07_attempt_factory import (
    V07DockerAttemptFactory,
    build_v07_formal_docker_attempt_factory,
)
from .intercode_v07_host_policy import V07HostSafetySession
from .intercode_v07_manifest import V07PrecalibrationManifest
from .intercode_v07_runner import (
    V07EpisodeRun,
    V07_EPISODE_ATTEMPT_CAP,
    V07_LOGICAL_COMPLETION_TOKENS,
    V07_LOGICAL_PROMPT_TOKENS,
    V07_MAX_OUTPUT_TOKENS,
    V07_PER_CALL_CONTEXT_TOKENS,
    run_v07_episode,
)
from .intercode_v07_study_binding import V07BeforeNewIntent, V07PreparedStudy


V07_FORMAL_EXECUTOR_REVISION = "intercode-v0.7-formal-phase-executor-v2"
V07_FORMAL_DOCKER_CONTEXT = "desktop-linux"

_CONSTRUCTION_SEAL = object()


class V07FormalExecutorError(RuntimeError):
    """The formal phase composition is unsafe or no longer authoritative."""


@dataclass(frozen=True, slots=True)
class V07FormalCampaignRun:
    """Terminal model-major driver result; publication still requires verification."""

    progress: CampaignProgress
    advanced_episode_count: int
    advance_call_count: int
    phase_models: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.progress) is not CampaignProgress:
            raise V07FormalExecutorError("formal campaign progress type is invalid")
        if (
            type(self.advanced_episode_count) is not int
            or not 0 <= self.advanced_episode_count <= 240
            or type(self.advance_call_count) is not int
            or not self.advanced_episode_count
            <= self.advance_call_count
            <= 241
            or type(self.phase_models) is not tuple
            or self.phase_models not in ((), (CAMPAIGN_MODELS[0],), CAMPAIGN_MODELS)
        ):
            raise V07FormalExecutorError("formal campaign driver result is invalid")


V07FormalPhaseFactory = Callable[[str | None, str], "V07FormalPhaseExecutor"]


class V07FormalPhaseExecutor:
    """Callable executor for exactly one preloaded model-major phase."""

    __slots__ = (
        "_action_executor",
        "_artifact_root",
        "_controller_directory",
        "_docker_cli",
        "_envelope_directory",
        "_host_session",
        "_manifest",
        "_phase_model_id",
        "_prepared",
        "_source",
    )

    def __init__(
        self,
        *,
        prepared_study: V07PreparedStudy,
        source: InterCodeSource,
        manifest: V07PrecalibrationManifest,
        phase_model_id: str,
        host_session: V07HostSafetySession,
        docker_cli: DockerCli,
        action_executor: DockerActionExecutor,
        artifact_root: Path,
        _construction_seal: object | None = None,
    ) -> None:
        if _construction_seal is not _CONSTRUCTION_SEAL:
            raise V07FormalExecutorError(
                "formal phase executors must be builder-issued"
            )
        self._prepared = prepared_study
        self._source = source
        self._manifest = manifest
        self._phase_model_id = phase_model_id
        self._host_session = host_session
        self._docker_cli = docker_cli
        self._action_executor = action_executor
        self._artifact_root = artifact_root
        self._controller_directory = artifact_root / "controllers"
        self._envelope_directory = artifact_root / "envelopes"

    @property
    def phase_model_id(self) -> str:
        return self._phase_model_id

    @property
    def artifact_root(self) -> Path:
        return self._artifact_root

    @property
    def controller_directory(self) -> Path:
        return self._controller_directory

    @property
    def envelope_directory(self) -> Path:
        return self._envelope_directory

    def require_phase_episode(self, episode: CampaignEpisode) -> None:
        """Reject a model switch before any episode artifact can be created."""

        if type(episode) is not CampaignEpisode:
            raise V07FormalExecutorError("formal phase episode type is invalid")
        schedule = self._prepared.bound_campaign_spec.episodes
        if (
            not 1 <= episode.episode_index <= len(schedule)
            or schedule[episode.episode_index - 1] != episode
        ):
            raise V07FormalExecutorError(
                "formal phase episode differs from the bound schedule"
            )
        if episode.model_id != self._phase_model_id:
            raise V07FormalExecutorError(
                "formal phase model differs from the preloaded model"
            )

    def pending_envelope(self, episode: CampaignEpisode) -> Path | None:
        """Locate one canonical pre-existing envelope without model work."""

        self.require_phase_episode(episode)
        path = self._envelope_path(episode)
        return path if path.exists() or path.is_symlink() else None

    def __call__(self, episode: CampaignEpisode) -> CampaignEpisodeExecution:
        self.require_phase_episode(episode)
        admission = None
        try:
            task = _exact_public_task(self._source, episode)
            runtime = self._prepared.model_runtime(episode.model_id)
            gold = self._prepared.trusted_gold_for_episode(episode)
            boundary_factory = build_v07_formal_docker_attempt_factory(
                prepared_study=self._prepared,
                episode=episode,
                source=self._source,
                task=task,
                manifest=self._manifest,
                docker_cli=self._docker_cli,
                action_executor=self._action_executor,
            )
            if type(boundary_factory) is not V07DockerAttemptFactory:
                raise V07FormalExecutorError(
                    "formal Docker boundary factory authority is invalid"
                )
            admission = self._host_session.issue_episode_admission()
            run = run_v07_episode(
                episode=episode,
                task=task,
                private_gold=gold,
                model=runtime.model,
                prompt_preparer=runtime.prompt_preparer,
                boundary_factory=boundary_factory,
                budget=v07_formal_budget(),
                before_episode_admission=admission.before_episode_admission,
                after_episode_admission=admission.after_episode_admission,
                execution_authority_sha256=(
                    self._prepared.study_binding_sha256
                ),
                event_log=self._controller_path(episode),
                execution_envelope=self._envelope_path(episode),
            )
            if type(run) is not V07EpisodeRun:
                raise V07FormalExecutorError("formal episode run type is invalid")
            execution = run.execution
            reopened = load_episode_execution_envelope(
                self._envelope_path(episode),
                episode,
            )
            evidence = admission.evidence
            if (
                reopened != execution
                or execution.execution_authority_sha256
                != self._prepared.study_binding_sha256
                or execution.before_host_sample != evidence.before
                or execution.after_host_sample != evidence.after
            ):
                raise V07FormalExecutorError(
                    "formal episode evidence differs after reverification"
                )
            return execution
        except (KeyboardInterrupt, SystemExit):
            raise
        except V07FormalExecutorError:
            raise
        except Exception:
            raise V07FormalExecutorError(
                "formal episode composition failed closed"
            ) from None
        finally:
            if admission is not None:
                admission.abort()

    def _controller_path(self, episode: CampaignEpisode) -> Path:
        return self._controller_directory / (
            f"episode-{episode.episode_index:04d}.jsonl"
        )

    def _envelope_path(self, episode: CampaignEpisode) -> Path:
        return self._envelope_directory / (
            f"episode-{episode.episode_index:04d}.execution.jsonl"
        )


class _V07FormalBeforeNewIntent:
    """Compose the phase guard before the complete prepared-study recheck."""

    __slots__ = ("_binding_revalidator", "_executor")

    def __init__(
        self,
        executor: V07FormalPhaseExecutor,
        binding_revalidator: V07BeforeNewIntent,
    ) -> None:
        if type(executor) is not V07FormalPhaseExecutor or type(
            binding_revalidator
        ) is not V07BeforeNewIntent:
            raise V07FormalExecutorError(
                "formal pre-intent authorities are invalid"
            )
        self._executor = executor
        self._binding_revalidator = binding_revalidator

    def __call__(self, episode: CampaignEpisode) -> None:
        self._executor.require_phase_episode(episode)
        self._binding_revalidator(episode)


def advance_v07_formal_phase(
    *,
    executor: V07FormalPhaseExecutor,
    campaign_journal_path: Path,
    repository_root: Path,
    intervention_journal_path: Path,
) -> CampaignAdvance:
    """Advance at most one episode through the complete formal authority path."""

    if type(executor) is not V07FormalPhaseExecutor:
        raise V07FormalExecutorError("formal advance executor is invalid")
    if any(
        type(path) is not type(Path()) or not path.is_absolute()
        for path in (
            campaign_journal_path,
            repository_root,
            intervention_journal_path,
        )
    ):
        raise V07FormalExecutorError(
            "formal advance paths must be exact absolute Paths"
        )
    binding_revalidator = executor._prepared.before_new_intent_callback(
        repository_root=repository_root,
        intervention_journal_path=intervention_journal_path,
    )
    before_new_intent = _V07FormalBeforeNewIntent(
        executor,
        binding_revalidator,
    )
    return advance_campaign(
        campaign_journal_path,
        executor._prepared.bound_campaign_spec,
        executor,
        reconcile_pending=executor.pending_envelope,
        before_new_intent=before_new_intent,
    )


def run_v07_formal_campaign(
    *,
    spec: CampaignSpec,
    open_phase: V07FormalPhaseFactory,
    campaign_journal_path: Path,
    repository_root: Path,
    intervention_journal_path: Path,
) -> V07FormalCampaignRun:
    """Drive the exact 120-Qwen then 120-Phi campaign in one live process.

    ``open_phase`` owns each audited unload/load transition and must return an
    executor whose admitted resident model matches the requested target.  The
    driver never alternates models and never skips the append-only campaign
    ledger.  A reboot cannot resume this function as the episode host evidence
    is single-boot by construction.
    """

    if (
        type(spec) is not CampaignSpec
        or spec.study_binding_sha256 is None
        or len(spec.episodes) != 240
        or tuple(dict.fromkeys(item.model_id for item in spec.episodes))
        != CAMPAIGN_MODELS
        or not callable(open_phase)
    ):
        raise V07FormalExecutorError("formal campaign authority is invalid")
    paths = (campaign_journal_path, repository_root, intervention_journal_path)
    if any(
        type(path) is not type(Path()) or not path.is_absolute()
        for path in paths
    ):
        raise V07FormalExecutorError(
            "formal campaign paths must be exact absolute Paths"
        )

    progress = inspect_campaign(campaign_journal_path, spec)
    if type(progress) is not CampaignProgress or progress.invalid_episodes:
        raise V07FormalExecutorError("formal campaign progress is invalid")
    initial_completed = progress.completed_episodes
    phase_models: list[str] = []
    current_model_id: str | None = None
    executor: V07FormalPhaseExecutor | None = None
    advance_calls = 0

    while not progress.sealed:
        if advance_calls >= 241:
            raise V07FormalExecutorError("formal campaign made no terminal progress")
        next_index = min(progress.completed_episodes, len(spec.episodes) - 1)
        target_model_id = spec.episodes[next_index].model_id
        if target_model_id != current_model_id:
            if target_model_id not in CAMPAIGN_MODELS:
                raise V07FormalExecutorError("formal campaign model order drifted")
            expected_position = len(phase_models)
            if (
                expected_position >= len(CAMPAIGN_MODELS)
                or target_model_id != CAMPAIGN_MODELS[expected_position]
            ):
                raise V07FormalExecutorError("formal campaign model order drifted")
            opened = open_phase(current_model_id, target_model_id)
            if (
                type(opened) is not V07FormalPhaseExecutor
                or opened.phase_model_id != target_model_id
            ):
                raise V07FormalExecutorError(
                    "formal phase factory returned the wrong model authority"
                )
            executor = opened
            current_model_id = target_model_id
            phase_models.append(target_model_id)
        assert executor is not None
        before_completed = progress.completed_episodes
        advance = advance_v07_formal_phase(
            executor=executor,
            campaign_journal_path=campaign_journal_path,
            repository_root=repository_root,
            intervention_journal_path=intervention_journal_path,
        )
        if type(advance) is not CampaignAdvance:
            raise V07FormalExecutorError("formal campaign advance type is invalid")
        progress = advance.progress
        advance_calls += 1
        if (
            type(progress) is not CampaignProgress
            or progress.invalid_episodes
            or progress.completed_episodes < before_completed
            or progress.completed_episodes > before_completed + 1
            or (
                progress.completed_episodes == before_completed
                and not progress.sealed
            )
        ):
            raise V07FormalExecutorError("formal campaign progress is inconsistent")
        if advance.episode is not None:
            index = advance.episode.episode_index - 1
            if (
                not 0 <= index < len(spec.episodes)
                or spec.episodes[index] != advance.episode
                or advance.episode.model_id != current_model_id
            ):
                raise V07FormalExecutorError(
                    "formal campaign advanced outside the active model phase"
                )

    return V07FormalCampaignRun(
        progress=progress,
        advanced_episode_count=(
            progress.completed_episodes - initial_completed
        ),
        advance_call_count=advance_calls,
        phase_models=tuple(phase_models),
    )


def v07_formal_budget() -> InteractiveBudget:
    """Return the one exact formal budget shared by all four arms."""

    if V07_EPISODE_ATTEMPT_CAP != CAMPAIGN_ATTEMPT_CAP:
        raise RuntimeError("formal runner and campaign attempt caps differ")
    return InteractiveBudget(
        attempts=V07_EPISODE_ATTEMPT_CAP,
        prompt_tokens=V07_LOGICAL_PROMPT_TOKENS,
        completion_tokens=V07_LOGICAL_COMPLETION_TOKENS,
        model_calls=V07_EPISODE_ATTEMPT_CAP,
        environment_actions=V07_EPISODE_ATTEMPT_CAP,
        evaluator_calls=V07_EPISODE_ATTEMPT_CAP + 1,
        checkpoint_creates=V07_EPISODE_ATTEMPT_CAP,
        checkpoint_restores=V07_EPISODE_ATTEMPT_CAP,
        safety_recoveries=V07_EPISODE_ATTEMPT_CAP,
        per_call_context_tokens=V07_PER_CALL_CONTEXT_TOKENS,
        max_output_tokens=V07_MAX_OUTPUT_TOKENS,
    )


def build_v07_formal_phase_executor(
    *,
    prepared_study: V07PreparedStudy,
    source: InterCodeSource,
    manifest: V07PrecalibrationManifest,
    phase_model_id: str,
    host_session: V07HostSafetySession,
    docker_cli: DockerCli,
    action_executor: DockerActionExecutor,
    artifact_root: Path,
) -> V07FormalPhaseExecutor:
    """Build one exact, model-major formal phase executor."""

    if type(prepared_study) is not V07PreparedStudy:
        raise V07FormalExecutorError("formal executor requires prepared study")
    if type(source) is not InterCodeSource:
        raise V07FormalExecutorError("formal executor source authority is invalid")
    if type(manifest) is not V07PrecalibrationManifest:
        raise V07FormalExecutorError("formal executor manifest authority is invalid")
    if type(host_session) is not V07HostSafetySession:
        raise V07FormalExecutorError("formal executor host authority is invalid")
    if (
        type(docker_cli) is not DockerCli
        or type(action_executor) is not DockerActionExecutor
    ):
        raise V07FormalExecutorError("formal executor Docker authority is invalid")
    if phase_model_id not in CAMPAIGN_MODELS or type(phase_model_id) is not str:
        raise V07FormalExecutorError("formal phase model is invalid")
    if (
        manifest.manifest_sha256
        != prepared_study.binding.manifest_sha256
        or manifest.execution != prepared_study.execution_pins
    ):
        raise V07FormalExecutorError(
            "formal executor manifest differs from the prepared study"
        )
    runtime = prepared_study.model_runtime(phase_model_id)
    expected_model = ResidentModel(
        runtime.model_id,
        runtime.model_identity.generation.profile.model_manifest_sha256.removeprefix(
            "sha256:"
        ),
    )
    try:
        session_pins = host_session.policy_pins
        expected = host_session.expected_resources
    except Exception:
        raise V07FormalExecutorError(
            "formal host session authority is invalid"
        ) from None
    if session_pins != prepared_study.execution_pins.host_safety:
        raise V07FormalExecutorError(
            "formal host policy pins differ from the prepared study"
        )
    if expected.resident_models != (expected_model,) or expected.running_container_ids:
        raise V07FormalExecutorError(
            "formal host session differs from the preloaded phase model"
        )
    _verify_formal_docker_boundaries(
        docker_cli=docker_cli,
        action_executor=action_executor,
        prepared_study=prepared_study,
    )
    root = _absolute_private_directory(artifact_root, "formal artifact root")
    controllers = _absolute_private_directory(
        root / "controllers",
        "formal controller directory",
    )
    envelopes = _absolute_private_directory(
        root / "envelopes",
        "formal envelope directory",
    )
    if controllers.parent != root or envelopes.parent != root:
        raise V07FormalExecutorError("formal artifact layout is invalid")
    return V07FormalPhaseExecutor(
        prepared_study=prepared_study,
        source=source,
        manifest=manifest,
        phase_model_id=phase_model_id,
        host_session=host_session,
        docker_cli=docker_cli,
        action_executor=action_executor,
        artifact_root=root,
        _construction_seal=_CONSTRUCTION_SEAL,
    )


def _verify_formal_docker_boundaries(
    *,
    docker_cli: DockerCli,
    action_executor: DockerActionExecutor,
    prepared_study: V07PreparedStudy,
) -> None:
    try:
        cli_identity = docker_cli.boundary_identity
        action_identity = action_executor.boundary_identity
    except Exception:
        raise V07FormalExecutorError(
            "formal Docker boundary identity is unavailable"
        ) from None
    if type(cli_identity) is not DockerCliBoundaryIdentity or type(
        action_identity
    ) is not DockerActionExecutorBoundaryIdentity:
        raise V07FormalExecutorError(
            "formal Docker boundary identity type is invalid"
        )
    if cli_identity.expected_context != V07_FORMAL_DOCKER_CONTEXT:
        raise V07FormalExecutorError(
            "formal Docker context differs from the frozen context"
        )
    host_identity = prepared_study.execution_pins.host_safety.host_identity
    if cli_identity.endpoint_sha256 != host_identity.docker_endpoint_sha256:
        raise V07FormalExecutorError(
            "formal Docker endpoint differs from the prepared host identity"
        )
    try:
        binary_sha256 = attest_docker_executable(Path(cli_identity.docker_binary))
    except Exception:
        raise V07FormalExecutorError(
            "formal Docker binary could not be attested"
        ) from None
    if binary_sha256 != host_identity.docker_binary_sha256:
        raise V07FormalExecutorError(
            "formal Docker binary differs from the prepared host identity"
        )
    if (
        action_identity.boundary is not docker_cli
        or action_identity.expected_docker_binary != cli_identity.docker_binary
        or action_identity.expected_endpoint != cli_identity.expected_endpoint
    ):
        raise V07FormalExecutorError(
            "formal Docker boundary is split across independent objects"
        )


def _exact_public_task(
    source: InterCodeSource,
    episode: CampaignEpisode,
) -> PublicBashTask:
    matches = tuple(task for task in source.tasks if task.task_id == episode.task_id)
    if len(matches) != 1 or type(matches[0]) is not PublicBashTask:
        raise V07FormalExecutorError(
            "formal episode public task could not be resolved exactly"
        )
    return matches[0]


def _absolute_private_directory(path: Path, label: str) -> Path:
    if (
        type(path) is not type(Path())
        or not path.is_absolute()
        or Path(os.path.normpath(path)) != path
    ):
        raise V07FormalExecutorError(f"{label} must be a canonical absolute path")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise V07FormalExecutorError(f"{label} parent is unavailable") from error
    if parent != path.parent:
        raise V07FormalExecutorError(f"{label} contains a symlink component")
    created = False
    try:
        if not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o700)
            created = True
        metadata = path.lstat()
        if created:
            os.chmod(path, 0o700)
            metadata = path.lstat()
    except OSError as error:
        raise V07FormalExecutorError(f"{label} could not be prepared") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise V07FormalExecutorError(f"{label} identity is unsafe")
    return path


__all__ = (
    "V07_FORMAL_DOCKER_CONTEXT",
    "V07_FORMAL_EXECUTOR_REVISION",
    "V07FormalCampaignRun",
    "V07FormalExecutorError",
    "V07FormalPhaseFactory",
    "V07FormalPhaseExecutor",
    "advance_v07_formal_phase",
    "build_v07_formal_phase_executor",
    "run_v07_formal_campaign",
    "v07_formal_budget",
)
