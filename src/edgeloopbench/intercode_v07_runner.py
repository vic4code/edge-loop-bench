"""Fail-closed composition for one v0.7 InterCode production episode.

Each strict entrypoint receives one public task and one private gold material.
Formal and calibration schedules are disjoint and cannot be cross-used.
Only the strict-evaluator closure captures the latter.  Model prompts and the
controller journal therefore have no type-level route to gold commands,
expected output, evaluator diagnostics, or evaluator filesystem paths.

This module performs no Docker, Ollama, tokenizer, or telemetry construction at
import time.  Production boundaries are injected; unit tests can exercise the
complete composition without starting a service or accessing the network.
"""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .interactive_controller import (
    InteractiveBudget,
    InteractiveTask,
    run_interactive_strategy,
)
from .intercode_campaign_ledger import (
    CAMPAIGN_MODELS,
    CAMPAIGN_SEED,
    CAMPAIGN_TASK_IDS,
    CampaignEpisode,
    CampaignEpisodeExecution,
    CampaignSpec,
    write_episode_execution_envelope,
)
from .intercode_host_safety import HostSafetySample
from .intercode_replay_environment import (
    AttemptBoundary,
    EpisodeCheckpointRegistry,
    ReplayEnvironment,
    finalize_v07_terminal,
    make_candidate_progress_evaluator,
    make_strict_evaluator,
)
from .intercode_source import PublicBashTask
from .intercode_v07_docker_qualification import (
    V07TrustedGoldMaterial,
    _open_v07_trusted_gold_material,
)
from .intercode_v07_calibration import (
    V07_CALIBRATION_ARMS,
    V07_CALIBRATION_TASK_IDS,
)
from .journal import inspect_journal
from .model_adapter import (
    ExactPromptPreparer,
    OllamaRawModel,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
)


V07_EPISODE_ATTEMPT_CAP = 4
V07_PER_CALL_CONTEXT_TOKENS = 4_096
V07_MAX_OUTPUT_TOKENS = 512
V07_LOGICAL_PROMPT_TOKENS = (
    V07_EPISODE_ATTEMPT_CAP * (V07_PER_CALL_CONTEXT_TOKENS - 1)
)
V07_LOGICAL_COMPLETION_TOKENS = (
    V07_EPISODE_ATTEMPT_CAP * V07_MAX_OUTPUT_TOKENS
)
_V07_EPISODES = CampaignSpec(CAMPAIGN_TASK_IDS).episodes
_V07_CALIBRATION_EPISODES = tuple(
    CampaignEpisode(
        episode_index,
        model_id,
        task_id,
        arm,
        CAMPAIGN_SEED,
    )
    for episode_index, (model_id, task_id, arm) in enumerate(
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
_PROFILE_BY_MODEL = {
    QWEN35_RAW_PROFILE.model: QWEN35_RAW_PROFILE,
    PHI4_MINI_RAW_PROFILE.model: PHI4_MINI_RAW_PROFILE,
}
_SHA256_REFERENCE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class V07AttemptBoundaryFactory(Protocol):
    """Create one fresh attempt boundary, normally DockerAttemptBoundary."""

    def __call__(self) -> AttemptBoundary: ...


class V07HostAdmissionHook(Protocol):
    """Return one admitted path-free sample or raise before evidence is used."""

    def __call__(self) -> HostSafetySample: ...


@dataclass(frozen=True, slots=True)
class V07EpisodeRun:
    """One ledger-ready execution with delegated operational evidence."""

    execution: CampaignEpisodeExecution

    def __post_init__(self) -> None:
        if type(self.execution) is not CampaignEpisodeExecution:
            raise ValueError("v0.7 episode execution must use the campaign type")

    @property
    def active_wall_time_ns(self) -> int:
        return self.execution.active_wall_time_ns

    @property
    def before_host_admission(self) -> HostSafetySample:
        return self.execution.before_host_sample

    @property
    def after_host_admission(self) -> HostSafetySample:
        return self.execution.after_host_sample


def run_v07_episode(
    *,
    episode: CampaignEpisode,
    task: PublicBashTask,
    private_gold: V07TrustedGoldMaterial,
    model: OllamaRawModel,
    prompt_preparer: ExactPromptPreparer,
    boundary_factory: V07AttemptBoundaryFactory,
    budget: InteractiveBudget,
    before_episode_admission: V07HostAdmissionHook,
    after_episode_admission: V07HostAdmissionHook,
    execution_authority_sha256: str,
    event_log: str | Path,
    execution_envelope: str | Path,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> V07EpisodeRun:
    """Run exactly one episode and seal its distinct execution envelope.

    A controller/model/environment failure leaves its journal unsealed and is
    re-raised after the post-episode host hook.  A post hook failure withholds
    even an otherwise sealed controller result.  Only after controller sealing
    and both host samples does the runner create the append-only envelope that
    permits the campaign layer to reconcile its durable pending intent.
    """

    _validate_formal_inputs(
        episode=episode,
        task=task,
        private_gold=private_gold,
        model=model,
        prompt_preparer=prompt_preparer,
        boundary_factory=boundary_factory,
        budget=budget,
        before_episode_admission=before_episode_admission,
        after_episode_admission=after_episode_admission,
        execution_authority_sha256=execution_authority_sha256,
        monotonic_ns=monotonic_ns,
    )
    return _run_validated_v07_episode(
        episode=episode,
        task=task,
        private_gold=private_gold,
        model=model,
        prompt_preparer=prompt_preparer,
        boundary_factory=boundary_factory,
        budget=budget,
        before_episode_admission=before_episode_admission,
        after_episode_admission=after_episode_admission,
        execution_authority_sha256=execution_authority_sha256,
        event_log=event_log,
        execution_envelope=execution_envelope,
        monotonic_ns=monotonic_ns,
    )


def run_v07_calibration_episode(
    *,
    episode: CampaignEpisode,
    task: PublicBashTask,
    private_gold: V07TrustedGoldMaterial,
    model: OllamaRawModel,
    prompt_preparer: ExactPromptPreparer,
    boundary_factory: V07AttemptBoundaryFactory,
    budget: InteractiveBudget,
    before_episode_admission: V07HostAdmissionHook,
    after_episode_admission: V07HostAdmissionHook,
    execution_authority_sha256: str,
    event_log: str | Path,
    execution_envelope: str | Path,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> V07EpisodeRun:
    """Run one exact frozen calibration row through the production core.

    This entrypoint cannot accept a formal 30-task row.  It uses the same
    controller, private evaluator boundary, budget, controller journal, host
    interval, and execution envelope as :func:`run_v07_episode`.
    """

    _validate_calibration_inputs(
        episode=episode,
        task=task,
        private_gold=private_gold,
        model=model,
        prompt_preparer=prompt_preparer,
        boundary_factory=boundary_factory,
        budget=budget,
        before_episode_admission=before_episode_admission,
        after_episode_admission=after_episode_admission,
        execution_authority_sha256=execution_authority_sha256,
        monotonic_ns=monotonic_ns,
    )
    return _run_validated_v07_episode(
        episode=episode,
        task=task,
        private_gold=private_gold,
        model=model,
        prompt_preparer=prompt_preparer,
        boundary_factory=boundary_factory,
        budget=budget,
        before_episode_admission=before_episode_admission,
        after_episode_admission=after_episode_admission,
        execution_authority_sha256=execution_authority_sha256,
        event_log=event_log,
        execution_envelope=execution_envelope,
        monotonic_ns=monotonic_ns,
    )


def _run_validated_v07_episode(
    *,
    episode: CampaignEpisode,
    task: PublicBashTask,
    private_gold: V07TrustedGoldMaterial,
    model: OllamaRawModel,
    prompt_preparer: ExactPromptPreparer,
    boundary_factory: V07AttemptBoundaryFactory,
    budget: InteractiveBudget,
    before_episode_admission: V07HostAdmissionHook,
    after_episode_admission: V07HostAdmissionHook,
    execution_authority_sha256: str,
    event_log: str | Path,
    execution_envelope: str | Path,
    monotonic_ns: Callable[[], int],
) -> V07EpisodeRun:
    strict_gold = _open_v07_trusted_gold_material(
        private_gold,
        task_id=episode.task_id,
    )
    path = Path(event_log)
    envelope_path = Path(execution_envelope)
    _validate_execution_paths(path, envelope_path)
    _precreate_controller_journal(path)

    before = before_episode_admission()
    _require_host_sample(before, "before")
    started = _read_monotonic_ns(monotonic_ns, "episode start")
    if before.captured_monotonic_ns > started:
        raise ValueError("before-admission sample was captured after episode start")

    registry = EpisodeCheckpointRegistry()
    result = None
    episode_error: BaseException | None = None
    finished: int | None = None
    clock_error: BaseException | None = None
    after: HostSafetySample | None = None
    after_error: BaseException | None = None
    try:
        result = run_interactive_strategy(
            strategy=episode.arm,
            task=InteractiveTask(task.task_id, task.query),
            model=model,
            prompt_preparer=prompt_preparer,
            environment_factory=lambda: ReplayEnvironment(
                registry,
                boundary_factory,
            ),
            attempt_evaluate=make_candidate_progress_evaluator(registry),
            strict_evaluate=make_strict_evaluator(registry, strict_gold),
            terminal_finalize=finalize_v07_terminal,
            budget=budget,
            replicate_seed=episode.seed,
            event_log=path,
            execution_authority_sha256=execution_authority_sha256,
        )
    except BaseException as error:
        episode_error = error

    try:
        finished = _read_monotonic_ns(monotonic_ns, "episode finish")
    except BaseException as error:
        clock_error = error
    try:
        observed_after = after_episode_admission()
        _require_host_sample(observed_after, "after")
        after = observed_after
    except BaseException as error:
        after_error = error

    if episode_error is not None:
        raise episode_error
    if clock_error is not None:
        raise clock_error
    if after_error is not None:
        raise after_error
    assert result is not None and finished is not None and after is not None
    if finished < started:
        raise ValueError("v0.7 monotonic episode clock moved backwards")
    if after.captured_monotonic_ns < finished:
        raise ValueError("after-admission sample was captured before episode finish")
    if after.boot_time_unix_microseconds != before.boot_time_unix_microseconds:
        raise ValueError("v0.7 episode crossed a boot boundary")

    inspection = inspect_journal(path, require_sealed=True)
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("v0.7 controller journal must have exact mode 0600")
    execution = CampaignEpisodeExecution(
        result=result,
        execution_authority_sha256=execution_authority_sha256,
        controller_log_sha256="sha256:" + inspection.last_event_sha256,
        active_wall_time_ns=finished - started,
        before_host_sample=before,
        after_host_sample=after,
    )
    sealed_execution = write_episode_execution_envelope(
        envelope_path,
        episode,
        execution,
    )
    return V07EpisodeRun(execution=sealed_execution)


def _validate_formal_inputs(
    *,
    episode: CampaignEpisode,
    task: PublicBashTask,
    private_gold: V07TrustedGoldMaterial,
    model: OllamaRawModel,
    prompt_preparer: ExactPromptPreparer,
    boundary_factory: object,
    budget: InteractiveBudget,
    before_episode_admission: object,
    after_episode_admission: object,
    execution_authority_sha256: object,
    monotonic_ns: object,
) -> None:
    if type(episode) is not CampaignEpisode:
        raise ValueError("v0.7 episode must use the campaign episode type")
    if (
        not 1 <= episode.episode_index <= len(_V07_EPISODES)
        or episode != _V07_EPISODES[episode.episode_index - 1]
    ):
        raise ValueError("v0.7 episode differs from the frozen formal schedule")
    _validate_common_inputs(
        episode=episode,
        task=task,
        private_gold=private_gold,
        model=model,
        prompt_preparer=prompt_preparer,
        boundary_factory=boundary_factory,
        budget=budget,
        before_episode_admission=before_episode_admission,
        after_episode_admission=after_episode_admission,
        execution_authority_sha256=execution_authority_sha256,
        monotonic_ns=monotonic_ns,
    )


def _validate_calibration_inputs(
    *,
    episode: CampaignEpisode,
    task: PublicBashTask,
    private_gold: V07TrustedGoldMaterial,
    model: OllamaRawModel,
    prompt_preparer: ExactPromptPreparer,
    boundary_factory: object,
    budget: InteractiveBudget,
    before_episode_admission: object,
    after_episode_admission: object,
    execution_authority_sha256: object,
    monotonic_ns: object,
) -> None:
    if type(episode) is not CampaignEpisode:
        raise ValueError("v0.7 episode must use the campaign episode type")
    if (
        not 1 <= episode.episode_index <= len(_V07_CALIBRATION_EPISODES)
        or episode != _V07_CALIBRATION_EPISODES[episode.episode_index - 1]
    ):
        raise ValueError("v0.7 episode differs from the frozen calibration schedule")
    _validate_common_inputs(
        episode=episode,
        task=task,
        private_gold=private_gold,
        model=model,
        prompt_preparer=prompt_preparer,
        boundary_factory=boundary_factory,
        budget=budget,
        before_episode_admission=before_episode_admission,
        after_episode_admission=after_episode_admission,
        execution_authority_sha256=execution_authority_sha256,
        monotonic_ns=monotonic_ns,
    )


def _validate_common_inputs(
    *,
    episode: CampaignEpisode,
    task: PublicBashTask,
    private_gold: V07TrustedGoldMaterial,
    model: OllamaRawModel,
    prompt_preparer: ExactPromptPreparer,
    boundary_factory: object,
    budget: InteractiveBudget,
    before_episode_admission: object,
    after_episode_admission: object,
    execution_authority_sha256: object,
    monotonic_ns: object,
) -> None:
    if type(task) is not PublicBashTask or task.task_id != episode.task_id:
        raise ValueError("v0.7 public task does not match the episode")
    expected_stratum = episode.task_id.removeprefix("bash-").split("-", 1)[0]
    if task.stratum != expected_stratum:
        raise ValueError("v0.7 public task stratum differs from its task ID")
    if (
        type(private_gold) is not V07TrustedGoldMaterial
        or private_gold.task_id != episode.task_id
    ):
        raise ValueError(
            "v0.7 private gold must use task-bound trusted authority material"
        )
    if type(model) is not OllamaRawModel:
        raise ValueError("v0.7 model must use the frozen raw Ollama adapter")
    if type(prompt_preparer) is not ExactPromptPreparer:
        raise ValueError("v0.7 prompt preparer must use exact preflight")
    profile = _PROFILE_BY_MODEL.get(episode.model_id)
    if (
        profile is None
        or model.config.profile != profile
        or prompt_preparer.renderer != profile
    ):
        raise ValueError("v0.7 model, renderer, and episode identities differ")
    if type(budget) is not InteractiveBudget:
        raise ValueError("v0.7 episode budget must be exact and typed")
    if (
        type(execution_authority_sha256) is not str
        or _SHA256_REFERENCE.fullmatch(execution_authority_sha256) is None
    ):
        raise ValueError("v0.7 execution authority must be a lowercase SHA-256")
    _validate_budget(budget, model)
    for value, field in (
        (boundary_factory, "attempt boundary factory"),
        (before_episode_admission, "before-admission hook"),
        (after_episode_admission, "after-admission hook"),
        (monotonic_ns, "monotonic clock"),
    ):
        if not callable(value):
            raise ValueError(f"v0.7 {field} must be callable")


def _validate_budget(budget: InteractiveBudget, model: OllamaRawModel) -> None:
    exact_caps = {
        "attempts": V07_EPISODE_ATTEMPT_CAP,
        "prompt_tokens": V07_LOGICAL_PROMPT_TOKENS,
        "completion_tokens": V07_LOGICAL_COMPLETION_TOKENS,
        "model_calls": V07_EPISODE_ATTEMPT_CAP,
        "environment_actions": V07_EPISODE_ATTEMPT_CAP,
        "evaluator_calls": V07_EPISODE_ATTEMPT_CAP + 1,
        "checkpoint_creates": V07_EPISODE_ATTEMPT_CAP,
        "checkpoint_restores": V07_EPISODE_ATTEMPT_CAP,
        "safety_recoveries": V07_EPISODE_ATTEMPT_CAP,
        "per_call_context_tokens": V07_PER_CALL_CONTEXT_TOKENS,
        "max_output_tokens": V07_MAX_OUTPUT_TOKENS,
    }
    for field, expected in exact_caps.items():
        if getattr(budget, field) != expected:
            raise ValueError(f"v0.7 budget {field} must equal {expected}")
    if model.config.context_tokens != V07_PER_CALL_CONTEXT_TOKENS:
        raise ValueError("v0.7 budget and model context windows differ")


def _validate_execution_paths(event_log: Path, execution_envelope: Path) -> None:
    controller_path = Path(os.path.abspath(os.fspath(event_log)))
    envelope_path = Path(os.path.abspath(os.fspath(execution_envelope)))
    if controller_path == envelope_path:
        raise ValueError(
            "v0.7 controller journal and execution envelope must be distinct"
        )
    for path, label in (
        (event_log, "controller journal"),
        (execution_envelope, "execution envelope"),
    ):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError(f"v0.7 {label} target is unavailable") from error
        raise ValueError(f"v0.7 {label} must not exist before episode execution")


def _precreate_controller_journal(path: Path) -> None:
    """Durably claim a never-started controller path before host admission."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError("v0.7 controller journal parent is unavailable") from error
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError("v0.7 controller journal requires no-follow creation")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(os.fspath(path), flags, 0o600)
    except FileExistsError as error:
        raise ValueError(
            "v0.7 controller journal must not exist before episode execution"
        ) from error
    except OSError as error:
        raise ValueError("v0.7 controller journal could not be claimed") from error
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("v0.7 controller journal claim is not mode 0600")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_CLOEXEC", 0
    )
    try:
        descriptor = os.open(os.fspath(directory), flags)
    except OSError as error:
        raise ValueError("v0.7 controller journal parent is unavailable") from error
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_host_sample(value: object, phase: str) -> HostSafetySample:
    if type(value) is not HostSafetySample:
        raise ValueError(f"v0.7 {phase}-admission hook returned invalid evidence")
    return value


def _read_monotonic_ns(clock: Callable[[], int], phase: str) -> int:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"v0.7 {phase} monotonic time is invalid")
    return value
