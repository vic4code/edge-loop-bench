"""Publication evidence gate for the bounded v0.7 InterCode campaign.

The campaign ledger is orchestration evidence, not publication authority.  This
module reopens every separately bound controller journal, verifies its sealed
root and event-derived accounting, and returns a path-free type that downstream
analysis can require explicitly.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .interactive_controller import (
    INTERACTIVE_CONTROLLER_REVISION,
    InteractiveResult,
    candidate_seed,
)
from .intercode_campaign_ledger import (
    CAMPAIGN_ARMS,
    CAMPAIGN_ATTEMPT_CAP,
    CAMPAIGN_EPISODE_COUNT,
    CAMPAIGN_MODELS,
    CampaignEpisode,
    CampaignEpisodeResult,
    CampaignMatrix,
    CampaignSpec,
    load_complete_campaign_matrix,
)
from .intercode_host_safety import (
    DockerDaemonIdentity,
    HostSafetySample,
    ResidentModel,
    parse_host_safety_sample,
)
from .intercode_replay_environment import V07_STRICT_REPLAY_EVALUATOR_SHA256
from .journal import JournalError, inspect_journal
from .model_adapter import PHI4_MINI_RAW_PROFILE, QWEN35_RAW_PROFILE


EVIDENCE_SCHEMA_REVISION = "intercode-v0.7-campaign-evidence-v6"
MAX_EPISODE_LOG_BYTES = 4 * 1024 * 1024
MAX_EPISODE_RECORDS = 128
MAX_CAMPAIGN_LOG_BYTES = 16 * 1024 * 1024
MAX_CAMPAIGN_RECORDS = CAMPAIGN_EPISODE_COUNT * 2 + 2
_MAX_LOGICAL_PROMPT_TOKENS = 16_380
_MAX_LOGICAL_COMPLETION_TOKENS = 2_048
_MAX_PER_CALL_CONTEXT_TOKENS = 4_096
_MAX_OUTPUT_TOKENS = 512
_MAX_EVALUATOR_CALLS = 5
_MAX_CAMPAIGN_ACTIVE_WALL_TIME_NS = 18 * 60 * 60 * 1_000_000_000
_ADMISSION_FREE_PERCENT_MINIMUM = 25
_RUNNING_FREE_PERCENT_MINIMUM = 12
_ADMISSION_DISK_FREE_BYTES_MINIMUM = 32 << 30
_RUNNING_DISK_FREE_BYTES_MINIMUM = 24 << 30
_MAX_EPISODE_SWAP_GROWTH_BYTES = 512 << 20
_BUDGET_EXHAUSTED_STOP_REASONS = frozenset(
    {
        "action_pipeline_budget_exhausted",
        "attempt_budget_exhausted",
        "checkpoint_restore_budget_exhausted",
        "logical_completion_token_budget_exhausted",
        "logical_prompt_token_budget_exhausted",
        "per_call_context_token_budget_exhausted",
        "rendered_prompt_byte_budget_exhausted",
    }
)
_INFRASTRUCTURE_STOP_REASONS = frozenset(
    {
        "generation_telemetry_budget_violation",
        "prompt_token_telemetry_mismatch",
    }
)

_SHA256_REFERENCE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]")
_CHAIN_FIELDS = frozenset(
    {"sequence", "previous_event_sha256", "event_sha256"}
)
_COMMON_FIELDS = frozenset(
    {
        "type",
        "task_id",
        "strategy",
        "replicate_seed",
        "execution_authority_sha256",
    }
)
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "action",
        "command",
        "evaluator_path",
        "expected",
        "expected_output",
        "gold",
        "gold_command",
        "host_path",
        "model_text",
        "observation",
        "output",
        "query",
        "raw_model_text",
        "raw_prompt",
        "raw_text",
        "response",
        "stderr",
        "stdout",
        "text",
    }
)
_STATIC_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "controller_started": frozenset({"controller_revision"}),
    "model_preflighted": frozenset(
        {
            "attempt",
            "prompt_sha256",
            "prompt_tokens",
            "token_ids_sha256",
            "renderer_profile_sha256",
            "tokenizer_artifact_sha256",
            "model_artifact_sha256",
        }
    ),
    "model_requested": frozenset(
        {
            "attempt",
            "prompt_sha256",
            "logical_model_calls_after",
            "logical_prompt_tokens_after",
            "candidate_seed",
            "context_sha256",
            "max_output_tokens",
        }
    ),
    "model_completed": frozenset(
        {
            "attempt",
            "response_sha256",
            "prompt_tokens",
            "completion_tokens",
            "total_duration_ns",
        }
    ),
    "action_rejected": frozenset({"attempt", "reason"}),
    "environment_create_requested": frozenset({"attempt", "scope"}),
    "environment_created": frozenset({"attempt", "scope"}),
    "action_requested": frozenset({"attempt", "action_sha256"}),
    "action_completed": frozenset(
        {
            "attempt",
            "action_sha256",
            "output_sha256",
            "state_sha256",
            "exit_code",
            "admissible",
            "state_changed",
            "policy_failure",
            "safety_recovery_performed",
        }
    ),
    "safety_recovery_completed": frozenset(
        {
            "attempt",
            "state_sha256",
            "recovery_evidence_sha256",
            "replayed_environment_actions",
        }
    ),
    "attempt_defaulted": frozenset(
        {
            "attempt",
            "reward",
            "official_success",
            "evaluation_kind",
            "policy_failure",
        }
    ),
    "checkpoint_create_requested": frozenset({"attempt"}),
    "checkpoint_created": frozenset(
        {"attempt", "state_sha256", "replay_depth"}
    ),
    "attempt_evaluation_requested": frozenset({"attempt", "state_sha256"}),
    "attempt_evaluated": frozenset(
        {"attempt", "reward", "official_success", "evaluation_kind"}
    ),
    "checkpoint_restore_requested": frozenset(
        {"attempt", "target_attempt", "state_sha256", "replay_depth"}
    ),
    "checkpoint_restored": frozenset(
        {
            "attempt",
            "target_attempt",
            "state_sha256",
            "replay_depth",
            "replayed_environment_actions",
        }
    ),
    "environment_close_requested": frozenset({"scope"}),
    "environment_closed": frozenset({"scope"}),
    "strict_evaluation_planned": frozenset(
        {"selected_attempt", "state_sha256"}
    ),
    "strict_evaluation_defaulted": frozenset(
        {"selected_attempt", "reason", "strict_success"}
    ),
    "terminal_finalization_requested": frozenset(
        {
            "selected_attempt",
            "evaluation_kind",
            "aborted",
            "remaining_evaluator_calls",
        }
    ),
    "terminal_finalized": frozenset(
        {"strict_evaluator_calls", "posthoc_evaluator_calls"}
    ),
    "strict_evaluation_completed": frozenset(
        {"strict_success", "evaluator_sha256"}
    ),
    "controller_stopped": frozenset(
        {"stop_reason", "selected_attempt", "official_success"}
    ),
}
_MODEL_REJECTION_FIELDS: dict[str, frozenset[str]] = {
    "rendered_prompt_byte_budget": frozenset(
        {
            "attempt",
            "reason",
            "prompt_sha256",
            "renderer_profile_sha256",
            "observed_prompt_bytes",
            "prompt_byte_limit",
        }
    ),
    "prompt_budget": frozenset(
        {
            "attempt",
            "reason",
            "prompt_sha256",
            "prompt_tokens",
            "remaining_prompt_tokens",
        }
    ),
    "per_call_context_budget": frozenset(
        {
            "attempt",
            "reason",
            "prompt_sha256",
            "prompt_tokens",
            "remaining_context_tokens",
        }
    ),
}
_INFRASTRUCTURE_INVALID_FIELDS: dict[str, frozenset[str]] = {
    "prompt_token_telemetry_mismatch": frozenset(
        {
            "attempt",
            "reason",
            "preflight_prompt_tokens",
            "telemetry_prompt_tokens",
        }
    ),
    "generation_telemetry_budget_violation": frozenset(
        {
            "attempt",
            "reason",
            "allowed_completion_tokens",
            "telemetry_completion_tokens",
            "allowed_context_tokens",
            "telemetry_context_tokens",
        }
    ),
}
_POLICY_FAILURES = frozenset(
    {
        "timeout",
        "output_overflow",
        "invalid_text",
        "residual_process",
        "container_terminated",
        "writable_layer_overflow",
    }
)
_CANDIDATE_PROGRESS_VALUES = frozenset({0.0, 0.2, 0.4, 0.6, 0.8})
_VERIFICATION_AUTHORITY = object()


class CampaignEvidenceError(ValueError):
    """A complete campaign is not safe to treat as publication evidence."""


@dataclass(frozen=True)
class _CandidateOneEvidence:
    prompt_sha256: str
    prompt_tokens: int
    token_ids_sha256: str
    candidate_seed: int
    max_output_tokens: int
    response_sha256: str
    completion_tokens: int
    progress_reward: float | None
    progress_evaluation_kind: str | None
    outcome_kind: str
    action_sha256: str | None
    output_sha256: str | None
    state_sha256: str | None
    exit_code: int | None
    admissible: bool | None
    state_changed: bool | None
    policy_failure: str | None
    safety_recovery_performed: bool | None


@dataclass(frozen=True, init=False)
class VerifiedCampaignEvidence:
    """Path-free evidence rows available only from the complete gate."""

    matrix: CampaignMatrix
    campaign_log_sha256: str
    study_binding_sha256: str
    schedule_sha256: str
    episode_log_set_sha256: str
    tokenizer_artifacts_by_model: tuple[tuple[str, str], ...]
    verified_episode_count: int

    def __init__(
        self,
        matrix: CampaignMatrix,
        campaign_log_sha256: str,
        study_binding_sha256: str,
        schedule_sha256: str,
        episode_log_set_sha256: str,
        tokenizer_artifacts_by_model: tuple[tuple[str, str], ...],
        verified_episode_count: int,
        *,
        _authority: object,
    ) -> None:
        if _authority is not _VERIFICATION_AUTHORITY:
            raise TypeError(
                "VerifiedCampaignEvidence must be created by verify_campaign_evidence"
            )
        if any(
            type(value) is not str or _SHA256_REFERENCE.fullmatch(value) is None
            for value in (
                campaign_log_sha256,
                study_binding_sha256,
                schedule_sha256,
                episode_log_set_sha256,
            )
        ):
            raise CampaignEvidenceError(
                "verified campaign evidence contains an invalid SHA-256"
            )
        object.__setattr__(self, "matrix", matrix)
        object.__setattr__(self, "campaign_log_sha256", campaign_log_sha256)
        object.__setattr__(self, "study_binding_sha256", study_binding_sha256)
        object.__setattr__(self, "schedule_sha256", schedule_sha256)
        object.__setattr__(self, "episode_log_set_sha256", episode_log_set_sha256)
        expected_tokenizers = tuple(
            (model_id, artifact_sha256)
            for model_id, artifact_sha256 in tokenizer_artifacts_by_model
        )
        if (
            tuple(model_id for model_id, _artifact in expected_tokenizers)
            != CAMPAIGN_MODELS
            or any(
                type(artifact) is not str
                or _SHA256_REFERENCE.fullmatch(artifact) is None
                for _model_id, artifact in expected_tokenizers
            )
        ):
            raise CampaignEvidenceError(
                "verified campaign tokenizer artifact set is invalid"
            )
        object.__setattr__(
            self,
            "tokenizer_artifacts_by_model",
            expected_tokenizers,
        )
        object.__setattr__(self, "verified_episode_count", verified_episode_count)

    @property
    def total_model_calls(self) -> int:
        return self.matrix.total_model_calls

    @property
    def total_logical_prompt_tokens(self) -> int:
        return self.matrix.total_logical_prompt_tokens

    @property
    def total_logical_completion_tokens(self) -> int:
        return self.matrix.total_logical_completion_tokens

    @property
    def total_environment_actions(self) -> int:
        return self.matrix.total_environment_actions

    @property
    def total_replayed_environment_actions(self) -> int:
        return self.matrix.total_replayed_environment_actions

    @property
    def total_physical_environment_actions(self) -> int:
        return self.matrix.total_physical_environment_actions

    @property
    def total_human_prompts(self) -> int:
        return self.matrix.total_human_prompts

    @property
    def total_active_wall_time_ns(self) -> int:
        return self.matrix.total_active_wall_time_ns


def verify_campaign_evidence(
    campaign_journal_path: str | Path,
    episode_log_directory: str | Path,
    spec: CampaignSpec,
) -> VerifiedCampaignEvidence:
    """Authenticate the complete 240-log campaign for downstream analysis."""

    if type(spec) is not CampaignSpec or spec.study_binding_sha256 is None:
        raise ValueError("campaign evidence requires an exact bound CampaignSpec")
    campaign_path = Path(campaign_journal_path)
    campaign_log_sha256 = _inspect_campaign_journal(campaign_path)
    try:
        matrix = load_complete_campaign_matrix(campaign_path, spec)
    except (OSError, ValueError, JournalError) as error:
        raise CampaignEvidenceError(
            "campaign matrix is not complete, sealed, and valid"
        ) from error
    if type(matrix) is not CampaignMatrix or len(matrix.episodes) != CAMPAIGN_EPISODE_COUNT:
        raise CampaignEvidenceError("campaign matrix cardinality is invalid")
    if _inspect_campaign_journal(campaign_path) != campaign_log_sha256:
        raise CampaignEvidenceError("campaign journal changed while loading its matrix")

    directory = Path(episode_log_directory)
    directory_descriptor = _open_episode_directory(directory)
    roots: list[dict[str, object]] = []
    tokenizer_by_model: dict[str, str] = {}
    candidate_one_by_pair: dict[tuple[str, str, int], _CandidateOneEvidence] = {}
    arms_by_pair: dict[tuple[str, str, int], set[str]] = {}
    active_wall_time_ns = 0
    campaign_boot_time: int | None = None
    prior_after_monotonic_ns: int | None = None
    campaign_docker_daemon: DockerDaemonIdentity | None = None
    try:
        expected_names = {
            f"episode-{episode.episode_index:04d}.jsonl"
            for episode in spec.episodes
        }
        try:
            observed_names = set(os.listdir(directory_descriptor))
        except OSError as error:
            raise CampaignEvidenceError(
                "private episode directory could not be enumerated"
            ) from error
        if observed_names != expected_names:
            raise CampaignEvidenceError(
                "private episode directory differs from the exact 240-log set"
            )

        for expected_episode, bound in zip(spec.episodes, matrix.episodes, strict=True):
            if bound.episode != expected_episode:
                raise CampaignEvidenceError("campaign matrix episode order is invalid")
            if bound.execution_authority_sha256 != spec.study_binding_sha256:
                raise CampaignEvidenceError(
                    "campaign execution authority differs from the study binding"
                )
            _validate_host_evidence(bound)
            before_boot = bound.before_host_sample.boot_time_unix_microseconds
            if campaign_boot_time is None:
                campaign_boot_time = before_boot
            elif before_boot != campaign_boot_time:
                raise CampaignEvidenceError(
                    "campaign host evidence crosses a boot boundary"
                )
            if (
                prior_after_monotonic_ns is not None
                and bound.before_host_sample.captured_monotonic_ns
                < prior_after_monotonic_ns
            ):
                raise CampaignEvidenceError(
                    "campaign host samples are not in schedule order"
                )
            prior_after_monotonic_ns = (
                bound.after_host_sample.captured_monotonic_ns
            )
            if campaign_docker_daemon is None:
                campaign_docker_daemon = bound.before_host_sample.docker_daemon
            elif bound.before_host_sample.docker_daemon != campaign_docker_daemon:
                raise CampaignEvidenceError(
                    "campaign Docker daemon identity changed between episodes"
                )
            active_wall_time_ns += bound.active_wall_time_ns
            name = f"episode-{expected_episode.episode_index:04d}.jsonl"
            records, observed_root = _read_sealed_episode_log(
                directory,
                directory_descriptor,
                name,
            )
            if observed_root != bound.controller_log_sha256:
                raise CampaignEvidenceError(
                    "episode controller-log root differs from the campaign binding"
                )
            candidate_one = _verify_episode_records(
                records,
                bound,
                tokenizer_by_model,
            )
            pair_key = (
                expected_episode.model_id,
                expected_episode.task_id,
                expected_episode.seed,
            )
            prior_candidate = candidate_one_by_pair.setdefault(pair_key, candidate_one)
            if prior_candidate != candidate_one:
                raise CampaignEvidenceError(
                    "cross-arm candidate-1 evidence differs within a causal pair"
                )
            pair_arms = arms_by_pair.setdefault(pair_key, set())
            if expected_episode.arm in pair_arms:
                raise CampaignEvidenceError("causal pair repeats one campaign arm")
            pair_arms.add(expected_episode.arm)
            roots.append(
                {
                    "episode_index": expected_episode.episode_index,
                    "controller_log_sha256": observed_root,
                }
            )
    finally:
        os.close(directory_descriptor)

    if len(candidate_one_by_pair) * len(CAMPAIGN_ARMS) != CAMPAIGN_EPISODE_COUNT or any(
        arms != set(CAMPAIGN_ARMS) for arms in arms_by_pair.values()
    ):
        raise CampaignEvidenceError("campaign causal pairing is incomplete")
    if active_wall_time_ns != matrix.total_active_wall_time_ns:
        raise CampaignEvidenceError("campaign active wall-time aggregate is inconsistent")
    if active_wall_time_ns > _MAX_CAMPAIGN_ACTIVE_WALL_TIME_NS:
        raise CampaignEvidenceError("campaign exceeds the frozen 18-hour active-time gate")

    tokenizer_artifacts = tuple(
        (model_id, tokenizer_by_model[model_id]) for model_id in CAMPAIGN_MODELS
    )
    root_payload = json.dumps(
        {
            "campaign_log_sha256": campaign_log_sha256,
            "episode_logs": roots,
            "schedule_sha256": spec.schedule_sha256,
            "schema_revision": EVIDENCE_SCHEMA_REVISION,
            "study_binding_sha256": spec.study_binding_sha256,
            "tokenizer_artifacts_by_model": [
                {"model_id": model_id, "tokenizer_artifact_sha256": artifact}
                for model_id, artifact in tokenizer_artifacts
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return VerifiedCampaignEvidence(
        matrix,
        campaign_log_sha256,
        spec.study_binding_sha256,
        spec.schedule_sha256,
        "sha256:" + sha256(root_payload).hexdigest(),
        tokenizer_artifacts,
        len(roots),
        _authority=_VERIFICATION_AUTHORITY,
    )


def _inspect_campaign_journal(path: Path) -> str:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CampaignEvidenceError("platform lacks no-follow file opens")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            raise CampaignEvidenceError(
                "campaign journal must be a regular non-symlink file"
            ) from error
        raise CampaignEvidenceError(
            "campaign journal could not be securely opened"
        ) from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CampaignEvidenceError(
                "campaign journal must be a regular non-symlink file"
            )
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise CampaignEvidenceError("campaign journal must have exact mode 0600")
        if not 0 < before.st_size <= MAX_CAMPAIGN_LOG_BYTES:
            raise CampaignEvidenceError("campaign journal exceeds its bounded size")
        try:
            inspection = inspect_journal(path, require_sealed=True)
        except (OSError, JournalError, ValueError) as error:
            raise CampaignEvidenceError(
                "campaign journal is not a valid sealed journal"
            ) from error
        try:
            named = path.lstat()
        except OSError as error:
            raise CampaignEvidenceError(
                "campaign journal identity could not be reopened"
            ) from error
        if stat.S_ISLNK(named.st_mode) or (
            named.st_dev,
            named.st_ino,
            named.st_size,
        ) != (before.st_dev, before.st_ino, before.st_size):
            raise CampaignEvidenceError("campaign journal identity changed")
        raw = _read_bounded(descriptor, before.st_size)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ):
            raise CampaignEvidenceError("campaign journal changed during verification")
    finally:
        os.close(descriptor)
    if (
        inspection.partial_tail is not None
        or inspection.file_byte_length != len(raw)
        or inspection.record_count != MAX_CAMPAIGN_RECORDS
    ):
        raise CampaignEvidenceError("campaign journal record set is not exact")
    return "sha256:" + inspection.last_event_sha256


def _open_episode_directory(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CampaignEvidenceError("platform lacks no-follow directory opens")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise CampaignEvidenceError(
                "private episode-log location must be a non-symlink directory"
            )
        descriptor = os.open(os.fspath(path), flags)
    except CampaignEvidenceError:
        raise
    except OSError as error:
        raise CampaignEvidenceError(
            "private episode-log directory could not be securely opened"
        ) from error
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (
        opened.st_dev,
        opened.st_ino,
    ) != (metadata.st_dev, metadata.st_ino):
        os.close(descriptor)
        raise CampaignEvidenceError("private episode-log directory identity changed")
    return descriptor


def _read_sealed_episode_log(
    directory: Path,
    directory_descriptor: int,
    name: str,
) -> tuple[tuple[dict[str, object], ...], str]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:  # pragma: no cover - checked by directory opener
        raise CampaignEvidenceError("platform lacks no-follow file opens")
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            raise CampaignEvidenceError(
                "episode log must be a regular non-symlink file"
            ) from error
        raise CampaignEvidenceError("episode log could not be securely opened") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CampaignEvidenceError(
                "episode log must be a regular non-symlink file"
            )
        if stat.S_IMODE(before.st_mode) != 0o600:
            raise CampaignEvidenceError("episode log must have exact mode 0600")
        if not 0 < before.st_size <= MAX_EPISODE_LOG_BYTES:
            raise CampaignEvidenceError("episode log exceeds its bounded size")
        try:
            inspection = inspect_journal(directory / name, require_sealed=True)
        except (OSError, JournalError, ValueError) as error:
            raise CampaignEvidenceError(
                "episode log is not a valid sealed journal"
            ) from error
        try:
            named = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise CampaignEvidenceError("episode log identity could not be reopened") from error
        if (
            named.st_dev,
            named.st_ino,
            named.st_size,
        ) != (before.st_dev, before.st_ino, before.st_size):
            raise CampaignEvidenceError("episode log identity changed during inspection")
        try:
            path_named = (directory / name).lstat()
        except OSError as error:
            raise CampaignEvidenceError(
                "episode log path identity could not be reopened"
            ) from error
        if stat.S_ISLNK(path_named.st_mode) or (
            path_named.st_dev,
            path_named.st_ino,
            path_named.st_size,
        ) != (before.st_dev, before.st_ino, before.st_size):
            raise CampaignEvidenceError("episode log path identity changed")
        raw = _read_bounded(descriptor, before.st_size)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ):
            raise CampaignEvidenceError("episode log changed during verification")
    finally:
        os.close(descriptor)

    if (
        inspection.partial_tail is not None
        or inspection.file_byte_length != len(raw)
        or inspection.record_count > MAX_EPISODE_RECORDS
    ):
        raise CampaignEvidenceError("episode log records exceed the frozen bounds")
    records = _decode_records(raw)
    if len(records) != inspection.record_count:
        raise CampaignEvidenceError("episode log changed after journal inspection")
    return records, "sha256:" + inspection.last_event_sha256


def _read_bounded(descriptor: int, expected_size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = expected_size
    while remaining:
        try:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
        except InterruptedError:
            continue
        if not chunk:
            raise CampaignEvidenceError("episode log ended during verification")
        chunks.append(chunk)
        remaining -= len(chunk)
    try:
        extra = os.read(descriptor, 1)
    except InterruptedError:
        extra = os.read(descriptor, 1)
    if extra:
        raise CampaignEvidenceError("episode log grew during verification")
    return b"".join(chunks)


def _decode_records(raw: bytes) -> tuple[dict[str, object], ...]:
    if not raw.endswith(b"\n"):
        raise CampaignEvidenceError("episode log is not newline terminated")
    records: list[dict[str, object]] = []
    try:
        for line in raw.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise CampaignEvidenceError("episode journal record is not an object")
            records.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise CampaignEvidenceError("episode journal record could not be decoded") from error
    return tuple(records)


def _verify_episode_records(
    records: tuple[dict[str, object], ...],
    bound: CampaignEpisodeResult,
    tokenizer_by_model: dict[str, str],
) -> _CandidateOneEvidence:
    if not 4 <= len(records) <= MAX_EPISODE_RECORDS:
        raise CampaignEvidenceError("episode journal record count is invalid")
    payloads = tuple(_without_chain(record) for record in records)
    if payloads[-1] != {
        "type": "journal_sealed",
        "sealed_event_count": len(payloads) - 1,
    }:
        raise CampaignEvidenceError("episode journal seal is not exact and terminal")
    events = payloads[:-1]
    if events[0].get("type") != "controller_started":
        raise CampaignEvidenceError("controller_started must be the first episode event")
    if events[-1].get("type") != "controller_stopped":
        raise CampaignEvidenceError("controller_stopped must immediately precede the seal")
    if sum(event.get("type") == "controller_started" for event in events) != 1:
        raise CampaignEvidenceError("episode must contain one controller_started event")
    if sum(event.get("type") == "controller_stopped" for event in events) != 1:
        raise CampaignEvidenceError("episode must contain one controller_stopped event")

    expected_identity = {
        "task_id": bound.episode.task_id,
        "strategy": bound.episode.arm,
        "replicate_seed": bound.episode.seed,
        "execution_authority_sha256": bound.execution_authority_sha256,
    }
    positions: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for position, event in enumerate(events):
        _reject_forbidden_material(event)
        for key, expected in expected_identity.items():
            if event.get(key) != expected:
                raise CampaignEvidenceError("episode event identity differs from schedule")
        _validate_event_schema(event)
        _validate_common_scalars(event)
        event_type = event["type"]
        assert isinstance(event_type, str)
        positions.setdefault(event_type, []).append((position, event))
        if "official_success" in event and event["official_success"] is not False:
            raise CampaignEvidenceError("official_success must remain false in v0.7")

    if positions.get("infrastructure_invalid"):
        raise CampaignEvidenceError(
            "publication campaign contains infrastructure-invalid evidence"
        )

    started = positions["controller_started"][0][1]
    if started["controller_revision"] != INTERACTIVE_CONTROLLER_REVISION:
        raise CampaignEvidenceError("controller revision differs from v0.7")
    profile = _profile_for(bound.episode)
    preflights = positions.get("model_preflighted", [])
    if not preflights:
        raise CampaignEvidenceError("episode lacks model preflight evidence")
    for _position, preflight in preflights:
        if preflight["renderer_profile_sha256"] != profile.sha256:
            raise CampaignEvidenceError("model renderer profile differs from v0.7")
        if preflight["model_artifact_sha256"] != profile.model_artifact_sha256:
            raise CampaignEvidenceError("model artifact profile differs from v0.7")
        tokenizer = _require_sha256(
            preflight["tokenizer_artifact_sha256"],
            "tokenizer artifact",
        )
        prior = tokenizer_by_model.setdefault(bound.episode.model_id, tokenizer)
        if prior != tokenizer:
            raise CampaignEvidenceError("tokenizer artifact changed within one model")

    counters = _derive_counters(positions, bound.episode)
    _compare_result(counters, bound.result)
    _validate_model_flow(positions, bound.episode)
    _validate_action_flow(positions, bound.episode)
    _validate_terminal_flow(positions, bound.result)
    return _candidate_one_evidence(positions)


def _candidate_one_evidence(
    positions: dict[str, list[tuple[int, dict[str, object]]]],
) -> _CandidateOneEvidence:
    preflights = _unique_attempt_events(
        positions.get("model_preflighted", []), "model preflight"
    )
    requests = _unique_attempt_events(
        positions.get("model_requested", []), "model request"
    )
    completions = _unique_attempt_events(
        positions.get("model_completed", []), "model completion"
    )
    if 1 not in preflights or 1 not in requests or 1 not in completions:
        raise CampaignEvidenceError("episode lacks complete candidate-1 model evidence")
    _preflight_position, preflight = preflights[1]
    _request_position, request = requests[1]
    _completion_position, completion = completions[1]
    rejected = _unique_attempt_events(
        positions.get("action_rejected", []), "action rejection"
    )
    actions = _unique_attempt_events(
        positions.get("action_completed", []), "action completion"
    )
    if (1 in rejected) == (1 in actions):
        raise CampaignEvidenceError(
            "candidate-1 must have exactly one parser-failure or action outcome"
        )
    if 1 in rejected:
        if rejected[1][1]["reason"] != "parser_failure":
            raise CampaignEvidenceError("candidate-1 parser failure type is invalid")
        return _CandidateOneEvidence(
            prompt_sha256=_require_sha256(preflight["prompt_sha256"], "prompt"),
            prompt_tokens=_positive_integer(
                preflight["prompt_tokens"], "prompt tokens"
            ),
            token_ids_sha256=_require_sha256(
                preflight["token_ids_sha256"], "token IDs"
            ),
            candidate_seed=int(request["candidate_seed"]),
            max_output_tokens=_positive_integer(
                request["max_output_tokens"], "max output tokens"
            ),
            response_sha256=_require_sha256(
                completion["response_sha256"], "model response"
            ),
            completion_tokens=_require_nonnegative_integer(
                completion["completion_tokens"], "completion tokens"
            ),
            progress_reward=None,
            progress_evaluation_kind=None,
            outcome_kind="parser_failure",
            action_sha256=None,
            output_sha256=None,
            state_sha256=None,
            exit_code=None,
            admissible=None,
            state_changed=None,
            policy_failure=None,
            safety_recovery_performed=None,
        )
    action = actions[1][1]
    evaluated = _unique_attempt_events(
        positions.get("attempt_evaluated", []), "attempt evaluation"
    )
    defaulted = _unique_attempt_events(
        positions.get("attempt_defaulted", []), "attempt default"
    )
    if (1 in evaluated) == (1 in defaulted):
        raise CampaignEvidenceError(
            "candidate-1 action lacks exactly one progress outcome"
        )
    progress = evaluated.get(1, defaulted.get(1))
    assert progress is not None
    progress_event = progress[1]
    return _CandidateOneEvidence(
        prompt_sha256=_require_sha256(preflight["prompt_sha256"], "prompt"),
        prompt_tokens=_positive_integer(
            preflight["prompt_tokens"], "prompt tokens"
        ),
        token_ids_sha256=_require_sha256(
            preflight["token_ids_sha256"], "token IDs"
        ),
        candidate_seed=int(request["candidate_seed"]),
        max_output_tokens=_positive_integer(
            request["max_output_tokens"], "max output tokens"
        ),
        response_sha256=_require_sha256(
            completion["response_sha256"], "model response"
        ),
        completion_tokens=_require_nonnegative_integer(
            completion["completion_tokens"], "completion tokens"
        ),
        progress_reward=float(progress_event["reward"]),
        progress_evaluation_kind=str(progress_event["evaluation_kind"]),
        outcome_kind="action_completed",
        action_sha256=_require_sha256(action["action_sha256"], "action"),
        output_sha256=_require_sha256(action["output_sha256"], "action output"),
        state_sha256=_require_sha256(action["state_sha256"], "action state"),
        exit_code=action["exit_code"],  # type: ignore[arg-type]
        admissible=action["admissible"],  # type: ignore[arg-type]
        state_changed=action["state_changed"],  # type: ignore[arg-type]
        policy_failure=action["policy_failure"],  # type: ignore[arg-type]
        safety_recovery_performed=action[  # type: ignore[arg-type]
            "safety_recovery_performed"
        ],
    )


def _profile_for(episode: CampaignEpisode):  # type: ignore[no-untyped-def]
    if episode.model_id == QWEN35_RAW_PROFILE.model:
        return QWEN35_RAW_PROFILE
    if episode.model_id == PHI4_MINI_RAW_PROFILE.model:
        return PHI4_MINI_RAW_PROFILE
    raise CampaignEvidenceError("campaign episode model is not frozen")


def _validate_host_evidence(bound: CampaignEpisodeResult) -> None:
    if (
        isinstance(bound.active_wall_time_ns, bool)
        or not isinstance(bound.active_wall_time_ns, int)
        or bound.active_wall_time_ns <= 0
        or type(bound.before_host_sample) is not HostSafetySample
        or type(bound.after_host_sample) is not HostSafetySample
    ):
        raise CampaignEvidenceError(
            "campaign episode lacks exact timing or host-sample evidence"
        )
    before = bound.before_host_sample
    after = bound.after_host_sample
    for sample in (before, after):
        try:
            reparsed = parse_host_safety_sample(sample.to_record())
        except ValueError as error:  # pragma: no cover - loader already checks this
            raise CampaignEvidenceError(
                "campaign host sample is not canonical"
            ) from error
        if type(reparsed) is not HostSafetySample or reparsed != sample:
            raise CampaignEvidenceError("campaign host sample is not canonical")
    if (
        before.boot_time_unix_microseconds
        != after.boot_time_unix_microseconds
        or before.captured_monotonic_ns > after.captured_monotonic_ns
        or after.captured_monotonic_ns - before.captured_monotonic_ns
        < bound.active_wall_time_ns
    ):
        raise CampaignEvidenceError(
            "campaign host samples do not enclose active wall time"
        )

    profile = _profile_for(bound.episode)
    expected_models = (
        ResidentModel(
            bound.episode.model_id,
            profile.model_manifest_sha256.removeprefix("sha256:"),
        ),
    )
    if (
        before.resident_models != expected_models
        or after.resident_models != expected_models
        or before.running_container_ids
        or after.running_container_ids
    ):
        raise CampaignEvidenceError(
            "campaign host resources differ from the exact episode expectation"
        )
    if (
        before.docker_daemon is None
        or after.docker_daemon is None
        or before.docker_daemon != after.docker_daemon
    ):
        raise CampaignEvidenceError(
            "campaign Docker daemon identity is absent or changed"
        )

    if (
        not before.on_ac_power
        or before.low_power_mode_enabled
        or before.vm_pressure_level != 1
        or before.free_memory_percent < _ADMISSION_FREE_PERCENT_MINIMUM
        or before.disk_free_bytes < _ADMISSION_DISK_FREE_BYTES_MINIMUM
        or before.thermal_warning
        or before.performance_warning
    ):
        raise CampaignEvidenceError(
            "campaign before sample fails frozen generic host safety"
        )
    if (
        not after.on_ac_power
        or after.low_power_mode_enabled
        or after.vm_pressure_level != 1
        or after.free_memory_percent < _RUNNING_FREE_PERCENT_MINIMUM
        or after.disk_free_bytes < _RUNNING_DISK_FREE_BYTES_MINIMUM
        or after.thermal_warning
        or after.performance_warning
        or after.swap_used_bytes - before.swap_used_bytes
        > _MAX_EPISODE_SWAP_GROWTH_BYTES
    ):
        raise CampaignEvidenceError(
            "campaign after sample fails frozen generic host safety"
        )


def _validate_event_schema(event: dict[str, object]) -> None:
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise CampaignEvidenceError("episode event type is invalid")
    if event_type == "model_request_rejected":
        extras = _MODEL_REJECTION_FIELDS.get(event.get("reason"))  # type: ignore[arg-type]
    elif event_type == "infrastructure_invalid":
        extras = _INFRASTRUCTURE_INVALID_FIELDS.get(event.get("reason"))  # type: ignore[arg-type]
    else:
        extras = _STATIC_EVENT_FIELDS.get(event_type)
    if extras is None:
        raise CampaignEvidenceError("episode event type or reason is not frozen")
    if set(event) != _COMMON_FIELDS | extras:
        raise CampaignEvidenceError("episode event fields differ from the frozen schema")


def _validate_common_scalars(event: dict[str, object]) -> None:
    if not isinstance(event["task_id"], str) or not event["task_id"]:
        raise CampaignEvidenceError("episode task identity is invalid")
    if not isinstance(event["strategy"], str) or not event["strategy"]:
        raise CampaignEvidenceError("episode strategy identity is invalid")
    if isinstance(event["replicate_seed"], bool) or not isinstance(
        event["replicate_seed"], int
    ):
        raise CampaignEvidenceError("episode seed identity is invalid")
    for key, value in event.items():
        if key.endswith("_sha256"):
            _require_sha256(value, key)
    if "attempt" in event:
        _require_attempt(event["attempt"])
    if "selected_attempt" in event and event["selected_attempt"] is not None:
        _require_attempt(event["selected_attempt"])
    if "target_attempt" in event:
        _require_attempt(event["target_attempt"])
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "logical_model_calls_after",
        "logical_prompt_tokens_after",
        "max_output_tokens",
        "total_duration_ns",
        "observed_prompt_bytes",
        "prompt_byte_limit",
        "remaining_prompt_tokens",
        "remaining_context_tokens",
        "preflight_prompt_tokens",
        "telemetry_prompt_tokens",
        "allowed_completion_tokens",
        "telemetry_completion_tokens",
        "allowed_context_tokens",
        "telemetry_context_tokens",
        "remaining_evaluator_calls",
        "replay_depth",
        "replayed_environment_actions",
        "strict_evaluator_calls",
        "posthoc_evaluator_calls",
    ):
        if key in event:
            _require_nonnegative_integer(event[key], key)
    for key in (
        "admissible",
        "state_changed",
        "safety_recovery_performed",
        "strict_success",
        "aborted",
        "official_success",
    ):
        if key in event and type(event[key]) is not bool:
            raise CampaignEvidenceError(f"episode {key} must be boolean")
    if "exit_code" in event and event["exit_code"] is not None and (
        isinstance(event["exit_code"], bool)
        or not isinstance(event["exit_code"], int)
    ):
        raise CampaignEvidenceError("episode exit_code must be an integer or null")
    if "reward" in event:
        reward = event["reward"]
        if isinstance(reward, bool) or not isinstance(reward, (int, float)):
            raise CampaignEvidenceError("episode reward must be numeric")
        if not math.isfinite(float(reward)):
            raise CampaignEvidenceError("episode reward must be finite")


def _derive_counters(
    positions: dict[str, list[tuple[int, dict[str, object]]]],
    episode: CampaignEpisode,
) -> dict[str, int]:
    requested = [event for _position, event in positions.get("model_requested", [])]
    completed = [event for _position, event in positions.get("model_completed", [])]
    actions = positions.get("action_completed", [])
    strict_final = positions.get("terminal_finalized", [])
    if len(strict_final) != 1:
        raise CampaignEvidenceError("episode must contain one terminal_finalized event")
    terminal = strict_final[0][1]
    model_calls = len(requested)
    if not 1 <= model_calls <= CAMPAIGN_ATTEMPT_CAP:
        raise CampaignEvidenceError("episode model-call count is outside the arm cap")
    if len(completed) != model_calls:
        raise CampaignEvidenceError("episode model request/completion counts differ")
    prompt_tokens = sum(
        _positive_integer(event["prompt_tokens"], "prompt tokens")
        for event in completed
    )
    completion_tokens = sum(
        _require_nonnegative_integer(event["completion_tokens"], "completion tokens")
        for event in completed
    )
    evaluator_calls = (
        len(positions.get("attempt_evaluated", []))
        + _require_nonnegative_integer(
            terminal["strict_evaluator_calls"], "strict evaluator calls"
        )
        + _require_nonnegative_integer(
            terminal["posthoc_evaluator_calls"], "posthoc evaluator calls"
        )
    )
    initial = int(model_calls > 0)
    independent = (
        model_calls - 1 if episode.arm == "independent_verified_sampling" else 0
    )
    feedback = (
        model_calls - 1
        if episode.arm in {"raw_feedback_loop", "engineered_loop"}
        else 0
    )
    replayed_environment_actions = sum(
        _positive_integer(
            event["replay_depth"],
            "checkpoint replay depth",
        )
        for _position, event in positions.get("checkpoint_restored", [])
    ) + sum(
        _require_nonnegative_integer(
            event["replayed_environment_actions"],
            "safety recovery replayed environment actions",
        )
        for _position, event in positions.get("safety_recovery_completed", [])
    )
    return {
        "attempts": model_calls,
        "model_calls": model_calls,
        "logical_prompt_tokens": prompt_tokens,
        "logical_completion_tokens": completion_tokens,
        "environment_actions": len(actions),
        "replayed_environment_actions": replayed_environment_actions,
        "evaluator_calls": evaluator_calls,
        "checkpoint_creates": len(positions.get("checkpoint_created", [])),
        "checkpoint_restores": len(positions.get("checkpoint_restored", [])),
        "safety_recoveries": len(positions.get("safety_recovery_completed", [])),
        "parser_failures": len(positions.get("action_rejected", [])),
        "initial_prompts": initial,
        "independent_sample_prompts": independent,
        "feedback_followups": feedback,
        "human_prompts": 0,
    }


def _compare_result(counters: dict[str, int], result: InteractiveResult) -> None:
    if type(result) is not InteractiveResult:
        raise CampaignEvidenceError("campaign result type is invalid")
    if result.official_success is not False:
        raise CampaignEvidenceError("official_success must remain false in v0.7")
    expected_run_status = _run_status_for_stop_reason(result.stop_reason)
    if result.run_status != expected_run_status:
        raise CampaignEvidenceError(
            "campaign run status is not derived from its stop reason"
        )
    for field, observed in counters.items():
        if getattr(result, field) != observed:
            raise CampaignEvidenceError(
                f"event-derived {field} counter differs from InteractiveResult"
            )
    if (
        result.logical_prompt_tokens > _MAX_LOGICAL_PROMPT_TOKENS
        or result.logical_completion_tokens > _MAX_LOGICAL_COMPLETION_TOKENS
        or result.model_calls > CAMPAIGN_ATTEMPT_CAP
        or result.environment_actions > CAMPAIGN_ATTEMPT_CAP
        or result.replayed_environment_actions
        > result.environment_actions * (result.environment_actions - 1) // 2
        or result.evaluator_calls > _MAX_EVALUATOR_CALLS
        or result.checkpoint_creates > CAMPAIGN_ATTEMPT_CAP
        or result.checkpoint_restores > CAMPAIGN_ATTEMPT_CAP
        or result.safety_recoveries > CAMPAIGN_ATTEMPT_CAP
    ):
        raise CampaignEvidenceError("episode result exceeds a frozen v0.7 budget")


def _run_status_for_stop_reason(stop_reason: str) -> str:
    if stop_reason in _INFRASTRUCTURE_STOP_REASONS:
        return "infrastructure_error"
    if stop_reason in _BUDGET_EXHAUSTED_STOP_REASONS:
        return "budget_exhausted"
    return "completed"


def _validate_model_flow(
    positions: dict[str, list[tuple[int, dict[str, object]]]],
    episode: CampaignEpisode,
) -> None:
    preflight_by_attempt = _unique_attempt_events(
        positions.get("model_preflighted", []), "model preflight"
    )
    requested = positions.get("model_requested", [])
    completed_by_attempt = _unique_attempt_events(
        positions.get("model_completed", []), "model completion"
    )
    cumulative_prompt_tokens = 0
    for ordinal, (request_position, request) in enumerate(requested, 1):
        attempt = _require_attempt(request["attempt"])
        if attempt != ordinal:
            raise CampaignEvidenceError("model attempts are not contiguous")
        preflight_position, preflight = preflight_by_attempt.get(
            attempt, (-1, {})
        )
        completion_position, completed = completed_by_attempt.get(
            attempt, (-1, {})
        )
        if not preflight or not completed or not (
            preflight_position < request_position < completion_position
        ):
            raise CampaignEvidenceError("model preflight/request/completion order is invalid")
        if request["prompt_sha256"] != preflight["prompt_sha256"]:
            raise CampaignEvidenceError("model request prompt differs from preflight")
        if completed["prompt_tokens"] != preflight["prompt_tokens"]:
            raise CampaignEvidenceError("model completion prompt tokens differ from preflight")
        prompt_tokens = _positive_integer(
            completed["prompt_tokens"], "model prompt tokens"
        )
        completion_tokens = _require_nonnegative_integer(
            completed["completion_tokens"], "model completion tokens"
        )
        if prompt_tokens + completion_tokens > _MAX_PER_CALL_CONTEXT_TOKENS:
            raise CampaignEvidenceError("model completion exceeds the context budget")
        if (
            _positive_integer(request["max_output_tokens"], "max output tokens")
            > _MAX_OUTPUT_TOKENS
        ):
            raise CampaignEvidenceError("model request exceeds the output-token budget")
        cumulative_prompt_tokens += _positive_integer(
            preflight["prompt_tokens"], "preflight prompt tokens"
        )
        if request["logical_model_calls_after"] != ordinal or request[
            "logical_prompt_tokens_after"
        ] != cumulative_prompt_tokens:
            raise CampaignEvidenceError("model logical counters are not cumulative")
        if request["candidate_seed"] != candidate_seed(episode.seed, attempt):
            raise CampaignEvidenceError("model candidate seed differs from frozen schedule")
    if set(completed_by_attempt) != set(range(1, len(requested) + 1)):
        raise CampaignEvidenceError("model completion attempts differ from requests")
    if not set(range(1, len(requested) + 1)) <= set(preflight_by_attempt):
        raise CampaignEvidenceError("model request lacks a matching preflight")
    rejected = positions.get("model_request_rejected", [])
    if len(rejected) > 1:
        raise CampaignEvidenceError("episode contains multiple rejected model requests")
    if rejected:
        reject_position, rejection = rejected[0]
        attempt = _require_attempt(rejection["attempt"])
        if attempt != len(requested) + 1:
            raise CampaignEvidenceError("rejected model request attempt is out of order")
        if rejection["reason"] == "rendered_prompt_byte_budget":
            if rejection["renderer_profile_sha256"] != _profile_for(episode).sha256:
                raise CampaignEvidenceError("rejected prompt renderer differs from v0.7")
        else:
            preflight_position, preflight = preflight_by_attempt.get(
                attempt, (-1, {})
            )
            if not preflight or preflight_position >= reject_position:
                raise CampaignEvidenceError("rejected model request lacks preflight")
            if rejection["prompt_sha256"] != preflight["prompt_sha256"]:
                raise CampaignEvidenceError("rejected model request prompt changed")
    expected_preflight_attempts = set(range(1, len(requested) + 1))
    if rejected and rejected[0][1]["reason"] != "rendered_prompt_byte_budget":
        expected_preflight_attempts.add(_require_attempt(rejected[0][1]["attempt"]))
    if set(preflight_by_attempt) != expected_preflight_attempts:
        raise CampaignEvidenceError("episode has an unbound model preflight")


def _validate_action_flow(
    positions: dict[str, list[tuple[int, dict[str, object]]]],
    episode: CampaignEpisode,
) -> None:
    requested = _unique_attempt_events(
        positions.get("action_requested", []), "action request"
    )
    completed = _unique_attempt_events(
        positions.get("action_completed", []), "action completion"
    )
    model_completed = _unique_attempt_events(
        positions.get("model_completed", []), "model completion"
    )
    model_preflighted = _unique_attempt_events(
        positions.get("model_preflighted", []), "model preflight"
    )
    parser_failures = _unique_attempt_events(
        positions.get("action_rejected", []), "action rejection"
    )
    for attempt, (failure_position, failure) in parser_failures.items():
        model_position, _model = model_completed.get(attempt, (-1, {}))
        if (
            failure["reason"] != "parser_failure"
            or model_position < 0
            or model_position >= failure_position
            or attempt in requested
            or attempt in completed
        ):
            raise CampaignEvidenceError("parser-failure evidence is inconsistent")
    if set(requested) != set(completed):
        raise CampaignEvidenceError("action request/completion attempts differ")
    inadmissible: set[int] = set()
    for attempt, (request_position, request) in requested.items():
        completion_position, completion = completed[attempt]
        model_position, _model = model_completed.get(attempt, (-1, {}))
        if not model_position < request_position < completion_position:
            raise CampaignEvidenceError("model/action event order is invalid")
        if request["action_sha256"] != completion["action_sha256"]:
            raise CampaignEvidenceError("action digest changed during execution")
        admissible = completion["admissible"]
        recovery = completion["safety_recovery_performed"]
        failure = completion["policy_failure"]
        if admissible is True:
            if recovery is not False or failure is not None:
                raise CampaignEvidenceError("admissible action has policy-recovery data")
        else:
            if recovery is not True or failure not in _POLICY_FAILURES:
                raise CampaignEvidenceError("inadmissible action lacks frozen recovery data")
            inadmissible.add(attempt)

    recoveries = _unique_attempt_events(
        positions.get("safety_recovery_completed", []), "safety recovery"
    )
    defaults = _unique_attempt_events(
        positions.get("attempt_defaulted", []), "attempt default"
    )
    if set(recoveries) != inadmissible or set(defaults) != inadmissible:
        raise CampaignEvidenceError("policy-failure recovery accounting is inconsistent")
    for attempt in inadmissible:
        completion_position, completion = completed[attempt]
        recovery_position, recovery = recoveries[attempt]
        default_position, default = defaults[attempt]
        if not completion_position < recovery_position < default_position:
            raise CampaignEvidenceError("policy-failure recovery order is invalid")
        next_preflight = model_preflighted.get(attempt + 1)
        if next_preflight is not None and default_position >= next_preflight[0]:
            raise CampaignEvidenceError(
                "policy recovery completed after the next model preflight"
            )
        if recovery["state_sha256"] != completion["state_sha256"]:
            raise CampaignEvidenceError("safety recovery state differs from action state")
        if (
            default["reward"] != 0.0
            or default["official_success"] is not False
            or default["evaluation_kind"] != "action_policy_failure"
            or default["policy_failure"] != completion["policy_failure"]
        ):
            raise CampaignEvidenceError("policy-failure default differs from v0.7")

    checkpoint_requested = _unique_attempt_events(
        positions.get("checkpoint_create_requested", []), "checkpoint request"
    )
    checkpoints = _unique_attempt_events(
        positions.get("checkpoint_created", []), "checkpoint creation"
    )
    eval_requested = _unique_attempt_events(
        positions.get("attempt_evaluation_requested", []), "evaluation request"
    )
    evaluated = _unique_attempt_events(
        positions.get("attempt_evaluated", []), "attempt evaluation"
    )
    admissible = set(completed) - inadmissible
    if not (
        set(checkpoint_requested)
        == set(checkpoints)
        == set(eval_requested)
        == set(evaluated)
        == admissible
    ):
        raise CampaignEvidenceError("checkpoint/evaluator accounting is inconsistent")
    for attempt in admissible:
        action_position, action = completed[attempt]
        request_position, _checkpoint_request = checkpoint_requested[attempt]
        checkpoint_position, checkpoint = checkpoints[attempt]
        eval_request_position, eval_request = eval_requested[attempt]
        eval_position, evaluation = evaluated[attempt]
        if not (
            action_position
            < request_position
            < checkpoint_position
            < eval_request_position
            < eval_position
        ):
            raise CampaignEvidenceError("checkpoint/evaluator event order is invalid")
        if not (
            action["state_sha256"]
            == checkpoint["state_sha256"]
            == eval_request["state_sha256"]
        ):
            raise CampaignEvidenceError("checkpoint/evaluator state digest changed")
        if (
            float(evaluation["reward"]) not in _CANDIDATE_PROGRESS_VALUES
            or evaluation["evaluation_kind"] != "evaluator_derived"
            or evaluation["official_success"] is not False
        ):
            raise CampaignEvidenceError("attempt evaluation differs from v0.7 progress policy")

    restore_requested = _unique_attempt_events(
        positions.get("checkpoint_restore_requested", []),
        "checkpoint restore request",
    )
    restored = _unique_attempt_events(
        positions.get("checkpoint_restored", []),
        "checkpoint restore completion",
    )
    if set(restore_requested) != set(restored):
        raise CampaignEvidenceError("checkpoint restore request/completion counts differ")
    if restore_requested and episode.arm != "engineered_loop":
        raise CampaignEvidenceError("checkpoint restore occurred outside engineered loop")
    if not set(restored) <= set(checkpoints):
        raise CampaignEvidenceError(
            "checkpoint restore attempt lacks a current checkpoint"
        )

    replay_depth_by_attempt: dict[int, int] = {}
    current_replay_depth = 0
    engineered_best_attempt: int | None = None
    engineered_best_reward: float | None = None
    for attempt in sorted(checkpoints):
        checkpoint_position, checkpoint = checkpoints[attempt]
        observed_depth = _positive_integer(
            checkpoint["replay_depth"], "checkpoint replay depth"
        )
        expected_depth = (
            1
            if episode.arm == "independent_verified_sampling"
            else current_replay_depth + 1
        )
        if observed_depth != expected_depth:
            raise CampaignEvidenceError(
                "checkpoint replay depth differs from the action topology"
            )
        replay_depth_by_attempt[attempt] = observed_depth
        if episode.arm != "independent_verified_sampling":
            current_replay_depth = observed_depth
        expected_restore_target: int | None = None
        if episode.arm == "engineered_loop":
            candidate_reward = float(evaluated[attempt][1]["reward"])
            expects_restore = (
                engineered_best_reward is not None
                and candidate_reward < engineered_best_reward
            )
            if expects_restore:
                if engineered_best_attempt is None:  # pragma: no cover - invariant
                    raise CampaignEvidenceError(
                        "engineered best checkpoint accounting is inconsistent"
                    )
                expected_restore_target = engineered_best_attempt
                if attempt not in restored:
                    raise CampaignEvidenceError(
                        "checkpoint restore differs from the frozen best policy"
                    )
            else:
                if attempt in restored:
                    raise CampaignEvidenceError(
                        "checkpoint restore differs from the frozen best policy"
                    )
                engineered_best_attempt = attempt
                engineered_best_reward = candidate_reward
        if attempt not in restored:
            continue

        request_position, request = restore_requested[attempt]
        restore_position, restore = restored[attempt]
        evaluation_position = evaluated[attempt][0]
        target_attempt = _require_attempt(request["target_attempt"])
        if target_attempt >= attempt or target_attempt not in replay_depth_by_attempt:
            raise CampaignEvidenceError(
                "checkpoint restore target is not a prior checkpoint"
            )
        if target_attempt != expected_restore_target:
            raise CampaignEvidenceError(
                "checkpoint restore target differs from the frozen best policy"
            )
        target_position, target = checkpoints[target_attempt]
        target_depth = replay_depth_by_attempt[target_attempt]
        if not (
            target_position
            < checkpoint_position
            < evaluation_position
            < request_position
            < restore_position
        ):
            raise CampaignEvidenceError("checkpoint restore event order is invalid")
        next_preflight = model_preflighted.get(attempt + 1)
        if next_preflight is not None and restore_position >= next_preflight[0]:
            raise CampaignEvidenceError(
                "checkpoint restore completed after the next model preflight"
            )
        if not (
            request["state_sha256"] == target["state_sha256"]
            and request["replay_depth"] == target_depth
            and restore["target_attempt"] == target_attempt
            and restore["state_sha256"] == target["state_sha256"]
            and restore["replay_depth"] == target_depth
            and restore["replayed_environment_actions"] == target_depth
        ):
            raise CampaignEvidenceError(
                "checkpoint restore identity or replay depth is inconsistent"
            )
        current_replay_depth = target_depth

    current_replay_depth = 0
    for attempt in sorted(completed):
        if attempt in inadmissible:
            expected_recovery_replay = (
                0
                if episode.arm == "independent_verified_sampling"
                else current_replay_depth
            )
            observed_recovery_replay = _require_nonnegative_integer(
                recoveries[attempt][1]["replayed_environment_actions"],
                "safety recovery replayed environment actions",
            )
            if observed_recovery_replay != expected_recovery_replay:
                raise CampaignEvidenceError(
                    "safety recovery replay count differs from action topology"
                )
            continue
        checkpoint_depth = replay_depth_by_attempt[attempt]
        if episode.arm == "independent_verified_sampling":
            current_replay_depth = 0
            continue
        current_replay_depth = checkpoint_depth
        if attempt in restored:
            target_attempt = _require_attempt(
                restore_requested[attempt][1]["target_attempt"]
            )
            current_replay_depth = replay_depth_by_attempt[target_attempt]

    if episode.arm == "engineered_loop":
        terminal_requests = positions.get("terminal_finalization_requested", [])
        if (
            len(terminal_requests) == 1
            and terminal_requests[0][1]["selected_attempt"]
            != engineered_best_attempt
        ):
            raise CampaignEvidenceError(
                "terminal selection differs from the frozen best policy"
            )

    _validate_environment_lifecycle(positions, episode)


def _validate_environment_lifecycle(
    positions: dict[str, list[tuple[int, dict[str, object]]]],
    episode: CampaignEpisode,
) -> None:
    create_requests = positions.get("environment_create_requested", [])
    creates = positions.get("environment_created", [])
    close_requests = positions.get("environment_close_requested", [])
    closes = positions.get("environment_closed", [])
    if len(create_requests) != len(creates) or len(close_requests) != len(closes):
        raise CampaignEvidenceError("environment lifecycle counts differ")
    if len(creates) != len(closes):
        raise CampaignEvidenceError("environment lifecycle is not fully closed")
    created_scopes: dict[str, int] = {}
    for (request_position, request), (create_position, created) in zip(
        create_requests, creates, strict=True
    ):
        if not request_position < create_position or (
            request["attempt"], request["scope"]
        ) != (created["attempt"], created["scope"]):
            raise CampaignEvidenceError("environment creation evidence is inconsistent")
        attempt = _require_attempt(created["attempt"])
        if created["scope"] == "episode":
            normalized_scope = "episode"
        elif created["scope"] == "attempt":
            normalized_scope = f"attempt-{attempt}"
        else:
            raise CampaignEvidenceError("environment creation scope is invalid")
        if normalized_scope in created_scopes:
            raise CampaignEvidenceError("environment scope was created more than once")
        created_scopes[normalized_scope] = create_position
    closed_scopes: set[str] = set()
    close_request_positions: dict[str, int] = {}
    for (request_position, request), (close_position, closed) in zip(
        close_requests, closes, strict=True
    ):
        if not request_position < close_position or request["scope"] != closed["scope"]:
            raise CampaignEvidenceError("environment closure evidence is inconsistent")
        scope = closed["scope"]
        if not isinstance(scope, str) or scope not in created_scopes:
            raise CampaignEvidenceError("environment closure scope is invalid")
        if scope in closed_scopes or created_scopes[scope] >= request_position:
            raise CampaignEvidenceError("environment closure order is invalid")
        closed_scopes.add(scope)
        close_request_positions[scope] = request_position
    if closed_scopes != set(created_scopes):
        raise CampaignEvidenceError("environment lifecycle scope set differs")

    action_requests = _unique_attempt_events(
        positions.get("action_requested", []), "environment action request"
    )
    action_completions = _unique_attempt_events(
        positions.get("action_completed", []), "environment action completion"
    )
    if set(action_requests) != set(action_completions):
        raise CampaignEvidenceError("environment action topology is incomplete")
    action_attempts = set(action_completions)
    if episode.arm == "independent_verified_sampling":
        expected_scopes = {f"attempt-{attempt}" for attempt in action_attempts}
        if set(created_scopes) != expected_scopes:
            raise CampaignEvidenceError(
                "independent sampling environment scope topology differs"
            )
        for attempt in action_attempts:
            scope = f"attempt-{attempt}"
            request_position = action_requests[attempt][0]
            completion_position = action_completions[attempt][0]
            if not (
                created_scopes[scope]
                < request_position
                < completion_position
                < close_request_positions[scope]
            ):
                raise CampaignEvidenceError(
                    "independent sampling environment action order differs"
                )
        return

    expected_scopes = {"episode"} if action_attempts else set()
    if set(created_scopes) != expected_scopes:
        raise CampaignEvidenceError("shared episode environment topology differs")
    if action_attempts:
        first_request = min(action_requests[attempt][0] for attempt in action_attempts)
        last_completion = max(
            action_completions[attempt][0] for attempt in action_attempts
        )
        if not (
            created_scopes["episode"]
            < first_request
            <= last_completion
            < close_request_positions["episode"]
        ):
            raise CampaignEvidenceError("shared episode environment action order differs")


def _validate_terminal_flow(
    positions: dict[str, list[tuple[int, dict[str, object]]]],
    result: InteractiveResult,
) -> None:
    requested = positions.get("terminal_finalization_requested", [])
    finalized = positions.get("terminal_finalized", [])
    stopped = positions.get("controller_stopped", [])
    if len(requested) != 1 or len(finalized) != 1 or len(stopped) != 1:
        raise CampaignEvidenceError("terminal controller event cardinality is invalid")
    request_position, request = requested[0]
    finalized_position, final = finalized[0]
    stop_position, stop = stopped[0]
    if not request_position < finalized_position < stop_position:
        raise CampaignEvidenceError("terminal controller event order is invalid")
    if request["aborted"] is not False:
        raise CampaignEvidenceError("publication campaign contains an aborted episode")
    if stop["official_success"] is not False or result.official_success is not False:
        raise CampaignEvidenceError("official_success must remain false in v0.7")
    if stop["stop_reason"] != result.stop_reason:
        raise CampaignEvidenceError("controller stop reason differs from InteractiveResult")

    planned = positions.get("strict_evaluation_planned", [])
    defaulted = positions.get("strict_evaluation_defaulted", [])
    completed = positions.get("strict_evaluation_completed", [])
    strict_calls = _require_nonnegative_integer(
        final["strict_evaluator_calls"], "strict evaluator calls"
    )
    posthoc_calls = _require_nonnegative_integer(
        final["posthoc_evaluator_calls"], "posthoc evaluator calls"
    )
    if posthoc_calls != 0 or strict_calls not in {0, 1}:
        raise CampaignEvidenceError("terminal evaluator counts differ from v0.7 policy")
    if len(completed) != strict_calls or len(planned) != strict_calls:
        raise CampaignEvidenceError("strict evaluation event count differs from terminal value")
    if planned and defaulted:
        raise CampaignEvidenceError("strict evaluation cannot be both planned and defaulted")
    if completed:
        strict_position, strict = completed[0]
        planned_position, plan = planned[0]
        if not (
            planned_position
            < request_position
            < finalized_position
            < strict_position
            < stop_position
        ):
            raise CampaignEvidenceError("strict evaluation event order is invalid")
        if strict["evaluator_sha256"] != V07_STRICT_REPLAY_EVALUATOR_SHA256:
            raise CampaignEvidenceError("strict evaluator SHA differs from v0.7 policy")
        if strict["strict_success"] is not result.strict_success:
            raise CampaignEvidenceError("strict event value differs from InteractiveResult")
        if request["evaluation_kind"] != "evaluator_derived" or request[
            "selected_attempt"
        ] != plan["selected_attempt"]:
            raise CampaignEvidenceError("strict selection provenance is inconsistent")
        selected_attempt = _require_attempt(plan["selected_attempt"])
        checkpoints = _unique_attempt_events(
            positions.get("checkpoint_created", []), "checkpoint creation"
        )
        if selected_attempt not in checkpoints or plan["state_sha256"] != checkpoints[
            selected_attempt
        ][1]["state_sha256"]:
            raise CampaignEvidenceError("strict selection state lacks checkpoint evidence")
    elif result.strict_success:
        raise CampaignEvidenceError("strict success lacks strict evaluator evidence")
    if defaulted:
        default_position, default = defaulted[0]
        if (
            default["strict_success"] is not False
            or default["reason"]
            not in {"candidate_surface_failure", "action_policy_failure"}
            or not default_position < request_position
            or request["selected_attempt"] != default["selected_attempt"]
            or request["evaluation_kind"] != default["reason"]
        ):
            raise CampaignEvidenceError("default strict value must be false")
    elif not completed and (
        request["selected_attempt"] is not None
        or request["evaluation_kind"] is not None
    ):
        raise CampaignEvidenceError("unevaluated terminal selection lacks default evidence")
    if request["selected_attempt"] != stop["selected_attempt"]:
        raise CampaignEvidenceError("terminal selected attempt changed before stop")


def _unique_attempt_events(
    positioned: list[tuple[int, dict[str, object]]],
    label: str,
) -> dict[int, tuple[int, dict[str, object]]]:
    result: dict[int, tuple[int, dict[str, object]]] = {}
    for position, event in positioned:
        attempt = _require_attempt(event["attempt"])
        if attempt in result:
            raise CampaignEvidenceError(f"episode repeats {label} attempt")
        result[attempt] = (position, event)
    return result


def _reject_forbidden_material(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = key.lower()
            if (
                lowered in _FORBIDDEN_FIELD_NAMES
                or lowered.endswith("_path")
                or lowered.startswith("expected_")
                or "gold" in lowered
                or lowered.startswith("raw_")
            ):
                raise CampaignEvidenceError(
                    "episode journal contains a forbidden raw or private field"
                )
            _reject_forbidden_material(child)
        return
    if isinstance(value, list):
        for child in value:
            _reject_forbidden_material(child)
        return
    if isinstance(value, str) and (
        value.startswith(("/", "~/", "~\\"))
        or _WINDOWS_ABSOLUTE_PATH.match(value) is not None
    ):
        raise CampaignEvidenceError("episode journal contains a host absolute path")


def _without_chain(record: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in record.items() if key not in _CHAIN_FIELDS}


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_REFERENCE.fullmatch(value) is None:
        raise CampaignEvidenceError(f"episode {field} is not a lowercase SHA-256 reference")
    return value


def _require_attempt(value: object) -> int:
    attempt = _positive_integer(value, "attempt")
    if attempt > CAMPAIGN_ATTEMPT_CAP:
        raise CampaignEvidenceError("episode attempt exceeds the frozen cap")
    return attempt


def _positive_integer(value: object, field: str) -> int:
    observed = _require_nonnegative_integer(value, field)
    if observed == 0:
        raise CampaignEvidenceError(f"episode {field} must be positive")
    return observed


def _require_nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CampaignEvidenceError(f"episode {field} must be a non-negative integer")
    return value
