"""Crash-closed production orchestration for the frozen v0.7 calibration.

The executor owns only the exact eight-row schedule and its public evidence.
Runtime construction is injected, while the actual episode always enters the
strict calibration entrypoint in :mod:`intercode_v07_runner`.  A separately
sealed begun marker is durably created before that entrypoint; a marker without
an exact recorded row is never interpreted as permission to retry.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Protocol

from .interactive_controller import InteractiveBudget, InteractiveResult
from .intercode_campaign_ledger import (
    CAMPAIGN_MODELS,
    CAMPAIGN_SEED,
    CampaignEpisode,
    CampaignEpisodeExecution,
    load_episode_execution_envelope,
)
from .intercode_source import InterCodeSource, PublicBashTask
from .intercode_v07_calibration import (
    V07_CALIBRATION_EPISODE_COUNT,
    V07_CALIBRATION_JOURNAL_SCHEMA,
    V07CalibrationDesign,
    VerifiedV07CalibrationEvidence,
    _validate_host_pair,
    build_v07_calibration_design,
    verify_v07_calibration_evidence,
)
from .intercode_v07_docker_qualification import (
    V07CalibrationGoldResult,
    V07TrustedGoldMaterial,
)
from .intercode_v07_manifest import V07PrecalibrationManifest
from .intercode_v07_runner import (
    V07_EPISODE_ATTEMPT_CAP,
    V07_LOGICAL_COMPLETION_TOKENS,
    V07_LOGICAL_PROMPT_TOKENS,
    V07_MAX_OUTPUT_TOKENS,
    V07_PER_CALL_CONTEXT_TOKENS,
    V07AttemptBoundaryFactory,
    V07EpisodeRun,
    V07HostAdmissionHook,
    run_v07_calibration_episode,
)
from .journal import (
    JournalInspection,
    _inspect_bytes,
    append_journal_event,
    seal_journal,
)
from .model_adapter import ExactPromptPreparer, OllamaRawModel


V07_CALIBRATION_BEGUN_MARKER_SCHEMA = (
    "edgeloopbench.intercode-v0.7-calibration-begun.v2"
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CHAIN_FIELDS = frozenset(
    {"sequence", "previous_event_sha256", "event_sha256"}
)
_MAX_JOURNAL_BYTES = 2 * 1024 * 1024
_MAX_MARKER_BYTES = 64 * 1024
_MAX_CONTROLLER_BYTES = 4 * 1024 * 1024
_RESULT_FIELDS = tuple(field.name for field in fields(InteractiveResult))


class V07CalibrationExecutionError(ValueError):
    """Base error for unsafe or incomplete calibration execution."""


class V07CalibrationIntegrityError(V07CalibrationExecutionError):
    """Existing calibration state differs from the frozen protocol."""


class V07CalibrationPendingEpisodeError(V07CalibrationExecutionError):
    """An episode has durable begun evidence but no exact recorded terminal."""

    def __init__(self, episode: CampaignEpisode) -> None:
        self.episode = episode
        super().__init__(
            "v0.7 calibration episode has begun and will not be rerun automatically"
        )


class V07CalibrationInfrastructureInvalidError(V07CalibrationExecutionError):
    """An infrastructure-invalid terminal permanently halts calibration."""

    def __init__(self, episode: CampaignEpisode) -> None:
        self.episode = episode
        super().__init__(
            "v0.7 calibration contains an infrastructure-invalid terminal"
        )


@dataclass(frozen=True, slots=True)
class V07CalibrationExecutionRow:
    """One public frozen row supplied to the runtime-only factory."""

    episode: CampaignEpisode
    task: PublicBashTask
    request_cap: int
    budget: InteractiveBudget

    def __post_init__(self) -> None:
        if type(self.episode) is not CampaignEpisode:
            raise ValueError("calibration row requires an exact episode")
        if type(self.task) is not PublicBashTask or self.task.task_id != self.episode.task_id:
            raise ValueError("calibration row public task differs from its episode")
        expected_cap = 1 if self.episode.arm == "direct" else 4
        if self.request_cap != expected_cap:
            raise ValueError("calibration row request cap is not frozen")
        if (
            type(self.budget) is not InteractiveBudget
            or self.budget != v07_calibration_budget()
        ):
            raise ValueError("calibration row budget must be exact and typed")


@dataclass(frozen=True, slots=True)
class V07CalibrationRuntime:
    """Injected local runtime dependencies; no work starts in this value."""

    model: OllamaRawModel
    prompt_preparer: ExactPromptPreparer
    boundary_factory: V07AttemptBoundaryFactory
    before_episode_admission: V07HostAdmissionHook
    after_episode_admission: V07HostAdmissionHook
    abort_episode_admission: Callable[[], None]
    monotonic_ns: Callable[[], int] = time.monotonic_ns

    def __post_init__(self) -> None:
        if type(self.model) is not OllamaRawModel:
            raise ValueError("calibration runtime model must use the raw adapter")
        if type(self.prompt_preparer) is not ExactPromptPreparer:
            raise ValueError("calibration runtime prompt preparer must be exact")
        for value, label in (
            (self.boundary_factory, "attempt boundary factory"),
            (self.before_episode_admission, "before-admission hook"),
            (self.after_episode_admission, "after-admission hook"),
            (self.abort_episode_admission, "abort-admission hook"),
            (self.monotonic_ns, "monotonic clock"),
        ):
            if not callable(value):
                raise ValueError(f"calibration runtime {label} must be callable")


class V07CalibrationRuntimeFactory(Protocol):
    """Build dependencies for one row without issuing a model request."""

    def __call__(self, row: V07CalibrationExecutionRow) -> V07CalibrationRuntime: ...


@dataclass(frozen=True, slots=True)
class V07CalibrationRun:
    """Verifier-sealed evidence plus the exact artifact set that produced it."""

    evidence: VerifiedV07CalibrationEvidence
    calibration_campaign_sha256: str
    calibration_journal_path: Path
    controller_log_paths: tuple[Path, ...]
    execution_envelope_paths: tuple[Path, ...]
    begun_marker_paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        if type(self.evidence) is not VerifiedV07CalibrationEvidence:
            raise ValueError("calibration run requires verifier-sealed evidence")
        if (
            _SHA256.fullmatch(self.calibration_campaign_sha256) is None
            or self.calibration_campaign_sha256
            != self.evidence.calibration_campaign_sha256
        ):
            raise ValueError("calibration run campaign authority differs")
        for values in (
            self.controller_log_paths,
            self.execution_envelope_paths,
            self.begun_marker_paths,
        ):
            if type(values) is not tuple or len(values) != V07_CALIBRATION_EPISODE_COUNT:
                raise ValueError("calibration run artifact cardinality differs")


@dataclass(frozen=True, slots=True)
class _EpisodePaths:
    marker: Path
    controller: Path
    envelope: Path


def v07_calibration_budget() -> InteractiveBudget:
    """Return the exact shared v0.7 ceiling used for every calibration arm."""

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


def execute_v07_calibration(
    *,
    design: V07CalibrationDesign,
    source: InterCodeSource,
    calibration_gold: V07CalibrationGoldResult,
    precalibration_manifest: V07PrecalibrationManifest,
    calibration_journal_path: str | Path,
    artifact_directory: str | Path,
    runtime_factory: V07CalibrationRuntimeFactory,
) -> V07CalibrationRun:
    """Execute or safely reopen the exact eight-row frozen calibration.

    Exact recorded prefixes may continue at the first never-started row.  Any
    sidecar for an unrecorded row is durable begun evidence and stops the run;
    neither this function nor its resume path can call the runtime factory for
    that row again.
    """

    schedule, tasks, gold, precalibration_manifest_sha256 = _validate_inputs(
        design=design,
        source=source,
        calibration_gold=calibration_gold,
        precalibration_manifest=precalibration_manifest,
        runtime_factory=runtime_factory,
    )
    journal = _absolute_path(calibration_journal_path)
    calibration_campaign_sha256 = calibration_gold.calibration_campaign_sha256
    artifact_root = _absolute_path(artifact_directory)
    paths = _episode_paths(artifact_root)
    _validate_distinct_paths(journal, artifact_root, paths)

    with _calibration_lock(journal):
        _prepare_artifact_directory(artifact_root)
        if not journal.exists():
            pending = _first_existing_artifact(schedule, paths, start=0)
            if pending is not None:
                raise V07CalibrationPendingEpisodeError(pending)
            append_journal_event(
                journal,
                _declaration(
                    design,
                    precalibration_manifest_sha256,
                    calibration_campaign_sha256,
                ),
            )
            _fsync_directory(journal.parent)

        executions, sealed = _load_prefix(
            journal=journal,
            design=design,
            manifest_sha256=precalibration_manifest_sha256,
            calibration_campaign_sha256=calibration_campaign_sha256,
            schedule=schedule,
            paths=paths,
        )
        _raise_if_infrastructure_invalid(schedule, executions)
        if sealed:
            return _verify_run(
                design,
                precalibration_manifest_sha256,
                calibration_campaign_sha256,
                journal,
                paths,
            )

        next_index = len(executions)
        pending = _first_existing_artifact(schedule, paths, start=next_index)
        if pending is not None:
            raise V07CalibrationPendingEpisodeError(pending)

        prior = executions[-1] if executions else None
        for index in range(next_index, len(schedule)):
            episode = schedule[index]
            row = V07CalibrationExecutionRow(
                episode=episode,
                task=tasks[episode.task_id],
                request_cap=design.request_caps[index % len(design.request_caps)],
                budget=v07_calibration_budget(),
            )
            _write_begun_marker(
                paths[index].marker,
                episode,
                design,
                precalibration_manifest_sha256,
                calibration_campaign_sha256,
            )
            runtime = runtime_factory(row)
            if type(runtime) is not V07CalibrationRuntime:
                raise ValueError(
                    "calibration runtime factory must return the exact runtime type"
                )
            try:
                run = run_v07_calibration_episode(
                    episode=episode,
                    task=row.task,
                    private_gold=gold[episode.task_id],
                    model=runtime.model,
                    prompt_preparer=runtime.prompt_preparer,
                    boundary_factory=runtime.boundary_factory,
                    budget=row.budget,
                    before_episode_admission=runtime.before_episode_admission,
                    after_episode_admission=runtime.after_episode_admission,
                    execution_authority_sha256=precalibration_manifest_sha256,
                    event_log=paths[index].controller,
                    execution_envelope=paths[index].envelope,
                    monotonic_ns=runtime.monotonic_ns,
                )
            finally:
                runtime.abort_episode_admission()
            if type(run) is not V07EpisodeRun:
                raise V07CalibrationIntegrityError(
                    "calibration runner returned an invalid execution type"
                )
            execution = _validate_execution_artifacts(
                episode=episode,
                request_cap=row.request_cap,
                execution=run.execution,
                paths=paths[index],
                marker_event=_begun_event(
                    episode,
                    design,
                    precalibration_manifest_sha256,
                    calibration_campaign_sha256,
                ),
            )
            _validate_schedule_continuity(prior, execution)
            append_journal_event(
                journal,
                _episode_event(episode, execution),
            )
            prior = execution
            if execution.result.run_status == "infrastructure_error":
                raise V07CalibrationInfrastructureInvalidError(episode)

        seal_journal(journal)
        return _verify_run(
            design,
            precalibration_manifest_sha256,
            calibration_campaign_sha256,
            journal,
            paths,
        )


def _validate_inputs(
    *,
    design: object,
    source: object,
    calibration_gold: object,
    precalibration_manifest: object,
    runtime_factory: object,
) -> tuple[
    tuple[CampaignEpisode, ...],
    Mapping[str, PublicBashTask],
    Mapping[str, V07TrustedGoldMaterial],
    str,
]:
    if type(source) is not InterCodeSource:
        raise ValueError("calibration execution requires a verified source")
    canonical = build_v07_calibration_design(source)
    if type(design) is not V07CalibrationDesign or design != canonical:
        raise ValueError("calibration execution requires the exact sealed design")
    if type(precalibration_manifest) is not V07PrecalibrationManifest:
        raise ValueError("calibration execution requires a sealed manifest")
    precalibration_manifest.canonical_record()
    precalibration_manifest_sha256 = precalibration_manifest.manifest_sha256
    if type(calibration_gold) is not V07CalibrationGoldResult:
        raise ValueError("calibration execution requires authority-sealed gold")
    gold = calibration_gold.trusted_gold_by_task_id
    if tuple(gold) != design.task_ids or any(
        type(value) is not V07TrustedGoldMaterial or value.task_id != task_id
        for task_id, value in gold.items()
    ):
        raise ValueError("calibration gold differs from the frozen task set")
    if not callable(runtime_factory):
        raise ValueError("calibration runtime factory must be callable")
    tasks = {task.task_id: task for task in source.calibration_tasks[:4]}
    if tuple(tasks) != design.task_ids:
        raise ValueError("calibration public tasks differ from the sealed design")
    schedule = tuple(
        CampaignEpisode(index, model_id, task_id, arm, CAMPAIGN_SEED)
        for index, (model_id, task_id, arm) in enumerate(
            (
                (model_id, task_id, arm)
                for model_id in CAMPAIGN_MODELS
                for task_id, arm in zip(design.task_ids, design.arms, strict=True)
            ),
            1,
        )
    )
    if len(schedule) != V07_CALIBRATION_EPISODE_COUNT:
        raise RuntimeError("calibration schedule cardinality changed")
    return schedule, tasks, gold, precalibration_manifest_sha256


def _episode_paths(root: Path) -> tuple[_EpisodePaths, ...]:
    return tuple(
        _EpisodePaths(
            marker=root / f"calibration-{index:03d}.begun.jsonl",
            controller=root / f"calibration-{index:03d}.controller.jsonl",
            envelope=root / f"calibration-{index:03d}.execution.jsonl",
        )
        for index in range(1, V07_CALIBRATION_EPISODE_COUNT + 1)
    )


def _absolute_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def _validate_distinct_paths(
    journal: Path,
    artifact_root: Path,
    paths: tuple[_EpisodePaths, ...],
) -> None:
    all_paths = [journal]
    for item in paths:
        all_paths.extend((item.marker, item.controller, item.envelope))
    if len(set(all_paths)) != len(all_paths) or journal == artifact_root:
        raise ValueError("calibration output paths must be distinct")


def _prepare_artifact_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise V07CalibrationIntegrityError(
            "calibration artifact directory is unavailable"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise V07CalibrationIntegrityError(
            "calibration artifact directory must be a real directory"
        )


def _declaration(
    design: V07CalibrationDesign,
    manifest_sha256: str,
    calibration_campaign_sha256: str,
) -> dict[str, object]:
    return {
        "type": "calibration_declared",
        "schema": V07_CALIBRATION_JOURNAL_SCHEMA,
        "design_sha256": design.design_sha256,
        "schedule_sha256": design.schedule_sha256,
        "precalibration_manifest_sha256": manifest_sha256,
        "calibration_campaign_sha256": calibration_campaign_sha256,
        "models": list(CAMPAIGN_MODELS),
        "episode_count": V07_CALIBRATION_EPISODE_COUNT,
    }


def _begun_event(
    episode: CampaignEpisode,
    design: V07CalibrationDesign,
    manifest_sha256: str,
    calibration_campaign_sha256: str,
) -> dict[str, object]:
    return {
        "type": "calibration_episode_begun",
        "schema": V07_CALIBRATION_BEGUN_MARKER_SCHEMA,
        "design_sha256": design.design_sha256,
        "precalibration_manifest_sha256": manifest_sha256,
        "calibration_campaign_sha256": calibration_campaign_sha256,
        **_episode_identity(episode),
    }


def _write_begun_marker(
    path: Path,
    episode: CampaignEpisode,
    design: V07CalibrationDesign,
    manifest_sha256: str,
    calibration_campaign_sha256: str,
) -> None:
    _precreate_regular_file(path, "calibration begun marker")
    append_journal_event(
        path,
        _begun_event(
            episode,
            design,
            manifest_sha256,
            calibration_campaign_sha256,
        ),
    )
    seal_journal(path)
    _fsync_directory(path.parent)


def _episode_event(
    episode: CampaignEpisode,
    execution: CampaignEpisodeExecution,
) -> dict[str, object]:
    return {
        "type": "calibration_episode_recorded",
        **_episode_identity(episode),
        "result": _result_record(execution.result),
        "execution_authority_sha256": execution.execution_authority_sha256,
        "controller_log_sha256": execution.controller_log_sha256,
        "active_wall_time_ns": execution.active_wall_time_ns,
        "before_host_sample": execution.before_host_sample.to_record(),
        "after_host_sample": execution.after_host_sample.to_record(),
    }


def _episode_identity(episode: CampaignEpisode) -> dict[str, object]:
    return {
        "episode_index": episode.episode_index,
        "model_id": episode.model_id,
        "task_id": episode.task_id,
        "arm": episode.arm,
        "seed": episode.seed,
    }


def _result_record(result: InteractiveResult) -> dict[str, object]:
    return {field_name: getattr(result, field_name) for field_name in _RESULT_FIELDS}


def _load_prefix(
    *,
    journal: Path,
    design: V07CalibrationDesign,
    manifest_sha256: str,
    calibration_campaign_sha256: str,
    schedule: tuple[CampaignEpisode, ...],
    paths: tuple[_EpisodePaths, ...],
) -> tuple[tuple[CampaignEpisodeExecution, ...], bool]:
    raw, inspection = _secure_read_journal(
        journal,
        label="calibration journal",
        maximum_bytes=_MAX_JOURNAL_BYTES,
    )
    records = _decode_records(raw, "calibration journal")
    payloads = tuple(_without_chain(record) for record in records)
    if not payloads or payloads[0] != _declaration(
        design,
        manifest_sha256,
        calibration_campaign_sha256,
    ):
        raise V07CalibrationIntegrityError(
            "calibration declaration differs from the frozen design"
        )
    if inspection.sealed:
        if len(payloads) != len(schedule) + 2 or payloads[-1] != {
            "type": "journal_sealed",
            "sealed_event_count": len(schedule) + 1,
        }:
            raise V07CalibrationIntegrityError(
                "sealed calibration journal has missing or duplicate rows"
            )
        row_payloads = payloads[1:-1]
    else:
        if any(payload.get("type") == "journal_sealed" for payload in payloads):
            raise V07CalibrationIntegrityError("calibration seal is not terminal")
        row_payloads = payloads[1:]
    if len(row_payloads) > len(schedule):
        raise V07CalibrationIntegrityError("calibration journal repeats episode rows")

    executions: list[CampaignEpisodeExecution] = []
    marker_event_by_index = tuple(
        _begun_event(
            episode,
            design,
            manifest_sha256,
            calibration_campaign_sha256,
        )
        for episode in schedule
    )
    for index, payload in enumerate(row_payloads):
        episode = schedule[index]
        expected_fields = {
            "type",
            "episode_index",
            "model_id",
            "task_id",
            "arm",
            "seed",
            "result",
            "execution_authority_sha256",
            "controller_log_sha256",
            "active_wall_time_ns",
            "before_host_sample",
            "after_host_sample",
        }
        if set(payload) != expected_fields or any(
            payload.get(key) != value
            for key, value in {
                "type": "calibration_episode_recorded",
                **_episode_identity(episode),
            }.items()
        ):
            raise V07CalibrationIntegrityError(
                "calibration row is missing, duplicated, or out of order"
            )
        execution = _validate_execution_artifacts(
            episode=episode,
            request_cap=design.request_caps[index % len(design.request_caps)],
            execution=None,
            paths=paths[index],
            marker_event=marker_event_by_index[index],
        )
        if payload != _episode_event(episode, execution):
            raise V07CalibrationIntegrityError(
                "calibration row differs from its sealed execution envelope"
            )
        _validate_schedule_continuity(executions[-1] if executions else None, execution)
        executions.append(execution)
    return tuple(executions), inspection.sealed


def _validate_execution_artifacts(
    *,
    episode: CampaignEpisode,
    request_cap: int,
    execution: CampaignEpisodeExecution | None,
    paths: _EpisodePaths,
    marker_event: dict[str, object],
) -> CampaignEpisodeExecution:
    _validate_marker(paths.marker, marker_event)
    try:
        reopened = load_episode_execution_envelope(paths.envelope, episode)
    except Exception as error:
        raise V07CalibrationIntegrityError(
            "calibration execution envelope is missing or invalid"
        ) from error
    expected_authority = marker_event.get("precalibration_manifest_sha256")
    if reopened.execution_authority_sha256 != expected_authority:
        raise V07CalibrationIntegrityError(
            "calibration execution authority differs from its manifest"
        )
    if execution is not None and reopened != execution:
        raise V07CalibrationIntegrityError(
            "calibration runner result differs from its sealed envelope"
        )
    if not 1 <= reopened.result.model_calls <= request_cap:
        raise V07CalibrationIntegrityError(
            "calibration model calls exceed the exact row request cap"
        )
    raw, controller = _secure_read_journal(
        paths.controller,
        label="calibration controller journal",
        maximum_bytes=_MAX_CONTROLLER_BYTES,
    )
    if not raw or not controller.sealed:
        raise V07CalibrationIntegrityError(
            "calibration controller journal is incomplete"
        )
    controller_root = "sha256:" + controller.last_event_sha256
    if controller_root != reopened.controller_log_sha256:
        raise V07CalibrationIntegrityError(
            "calibration controller root differs from its execution envelope"
        )
    try:
        _validate_host_pair(
            reopened.before_host_sample,
            reopened.after_host_sample,
            model_id=episode.model_id,
            active_wall_time_ns=reopened.active_wall_time_ns,
        )
    except ValueError as error:
        raise V07CalibrationIntegrityError(
            "calibration host evidence is not admissible"
        ) from error
    return reopened


def _validate_marker(path: Path, expected: dict[str, object]) -> None:
    raw, inspection = _secure_read_journal(
        path,
        label="calibration begun marker",
        maximum_bytes=_MAX_MARKER_BYTES,
    )
    payloads = tuple(_without_chain(record) for record in _decode_records(raw, "marker"))
    if (
        not inspection.sealed
        or len(payloads) != 2
        or payloads[0] != expected
        or payloads[1] != {"type": "journal_sealed", "sealed_event_count": 1}
    ):
        raise V07CalibrationIntegrityError("calibration begun marker is invalid")


def _validate_schedule_continuity(
    prior: CampaignEpisodeExecution | None,
    current: CampaignEpisodeExecution,
) -> None:
    if prior is None:
        return
    if (
        current.before_host_sample.boot_time_unix_microseconds
        != prior.after_host_sample.boot_time_unix_microseconds
        or current.before_host_sample.docker_daemon
        != prior.after_host_sample.docker_daemon
        or current.before_host_sample.captured_monotonic_ns
        < prior.after_host_sample.captured_monotonic_ns
    ):
        raise V07CalibrationIntegrityError(
            "calibration host evidence changes boot, daemon, or schedule order"
        )


def _raise_if_infrastructure_invalid(
    schedule: tuple[CampaignEpisode, ...],
    executions: tuple[CampaignEpisodeExecution, ...],
) -> None:
    for index, execution in enumerate(executions):
        if execution.result.run_status == "infrastructure_error":
            raise V07CalibrationInfrastructureInvalidError(schedule[index])


def _first_existing_artifact(
    schedule: tuple[CampaignEpisode, ...],
    paths: tuple[_EpisodePaths, ...],
    *,
    start: int,
) -> CampaignEpisode | None:
    for index in range(start, len(paths)):
        if any(
            _path_exists(path)
            for path in (paths[index].marker, paths[index].controller, paths[index].envelope)
        ):
            return schedule[index]
    return None


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise V07CalibrationIntegrityError(
            "calibration artifact identity is unavailable"
        ) from error
    return True


def _verify_run(
    design: V07CalibrationDesign,
    manifest_sha256: str,
    calibration_campaign_sha256: str,
    journal: Path,
    paths: tuple[_EpisodePaths, ...],
) -> V07CalibrationRun:
    controllers = tuple(item.controller for item in paths)
    try:
        evidence = verify_v07_calibration_evidence(
            design,
            precalibration_manifest_sha256=manifest_sha256,
            calibration_campaign_sha256=calibration_campaign_sha256,
            calibration_journal_path=journal,
            controller_log_paths=controllers,
        )
    except (OSError, ValueError) as error:
        raise V07CalibrationIntegrityError(
            "calibration evidence failed independent verification"
        ) from error
    return V07CalibrationRun(
        evidence=evidence,
        calibration_campaign_sha256=calibration_campaign_sha256,
        calibration_journal_path=journal,
        controller_log_paths=controllers,
        execution_envelope_paths=tuple(item.envelope for item in paths),
        begun_marker_paths=tuple(item.marker for item in paths),
    )


def _secure_read_journal(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> tuple[bytes, JournalInspection]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise V07CalibrationIntegrityError(f"{label} requires no-follow opens")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise V07CalibrationIntegrityError(f"{label} is unavailable") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600:
            raise V07CalibrationIntegrityError(
                f"{label} must be a regular mode-0600 file"
            )
        if not 0 < before.st_size <= maximum_bytes:
            raise V07CalibrationIntegrityError(f"{label} exceeds its byte bound")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise V07CalibrationIntegrityError(f"{label} changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        try:
            named = path.lstat()
        except OSError as error:
            raise V07CalibrationIntegrityError(
                f"{label} identity changed during read"
            ) from error
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (named.st_dev, named.st_ino)
            or stat.S_ISLNK(named.st_mode)
        ):
            raise V07CalibrationIntegrityError(f"{label} changed during read")
        raw = b"".join(chunks)
        try:
            inspection = _inspect_bytes(raw)
        except ValueError as error:
            raise V07CalibrationIntegrityError(f"{label} is not a valid journal") from error
        if inspection.partial_tail is not None:
            raise V07CalibrationIntegrityError(f"{label} has a partial tail")
        return raw, inspection
    finally:
        os.close(descriptor)


def _decode_records(raw: bytes, label: str) -> tuple[dict[str, object], ...]:
    if not raw.endswith(b"\n"):
        raise V07CalibrationIntegrityError(f"{label} is not newline terminated")
    records: list[dict[str, object]] = []
    try:
        for line in raw.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise V07CalibrationIntegrityError(f"{label} record is not an object")
            records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise V07CalibrationIntegrityError(f"{label} could not be decoded") from error
    return tuple(records)


def _without_chain(record: dict[str, object]) -> dict[str, object]:
    if not _CHAIN_FIELDS <= set(record):
        raise V07CalibrationIntegrityError("calibration journal chain fields are missing")
    return {key: value for key, value in record.items() if key not in _CHAIN_FIELDS}


def _precreate_regular_file(path: Path, label: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise V07CalibrationIntegrityError(f"{label} parent is unavailable") from error
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise V07CalibrationIntegrityError(f"{label} requires no-follow creation")
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
        raise V07CalibrationIntegrityError(f"{label} already exists") from error
    except OSError as error:
        raise V07CalibrationIntegrityError(f"{label} could not be created") from error
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


@contextmanager
def _calibration_lock(journal: Path) -> Iterator[None]:
    lock_path = journal.with_name(journal.name + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise V07CalibrationIntegrityError("calibration lock parent is unavailable") from error
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise V07CalibrationIntegrityError("calibration lock requires no-follow opens")
    common_flags = os.O_RDWR | nofollow | getattr(os, "O_CLOEXEC", 0)
    created = False
    try:
        descriptor = os.open(
            os.fspath(lock_path),
            common_flags | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(os.fspath(lock_path), common_flags)
        except OSError as error:
            raise V07CalibrationIntegrityError(
                "calibration lock is unavailable"
            ) from error
    except OSError as error:
        raise V07CalibrationIntegrityError("calibration lock is unavailable") from error
    try:
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            _fsync_directory(lock_path.parent)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise V07CalibrationIntegrityError("calibration lock must use mode 0600")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_CLOEXEC", 0
    )
    try:
        descriptor = os.open(os.fspath(directory), flags)
    except OSError as error:
        raise V07CalibrationIntegrityError("calibration directory is unavailable") from error
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "V07_CALIBRATION_BEGUN_MARKER_SCHEMA",
    "V07CalibrationExecutionError",
    "V07CalibrationExecutionRow",
    "V07CalibrationInfrastructureInvalidError",
    "V07CalibrationIntegrityError",
    "V07CalibrationPendingEpisodeError",
    "V07CalibrationRun",
    "V07CalibrationRuntime",
    "V07CalibrationRuntimeFactory",
    "execute_v07_calibration",
    "v07_calibration_budget",
]
