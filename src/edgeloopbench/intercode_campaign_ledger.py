"""Crash-safe, append-only orchestration for the bounded 30-task campaign.

This module owns only campaign ordering and accounting. The typed executor
callback is the sole boundary to model and environment work; task text,
evaluator material, and filesystem paths are never accepted as event fields.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from .interactive_controller import (
    INTERACTIVE_CONTROLLER_REVISION,
    INTERACTIVE_STRATEGIES,
    InteractiveResult,
)
from .intercode_host_safety import HostSafetySample, parse_host_safety_sample
from .journal import (
    GENESIS_EVENT_SHA256,
    JournalPartialTailError,
    SEALED_EVENT_TYPE,
    append_journal_event,
    canonical_event_bytes,
    inspect_journal,
    seal_journal,
)
from .model_adapter import PHI4_MINI_RAW_PROFILE, QWEN35_RAW_PROFILE


CAMPAIGN_SCHEMA_REVISION = "intercode-30task-campaign-ledger-v4"
CAMPAIGN_MODELS = ("qwen3.5:4b", "phi4-mini:3.8b")
CAMPAIGN_ARMS = (
    "direct",
    "independent_verified_sampling",
    "raw_feedback_loop",
    "engineered_loop",
)
CAMPAIGN_SEED = 11
CAMPAIGN_ATTEMPT_CAP = 4
CAMPAIGN_TASK_COUNT = 30
CAMPAIGN_EPISODE_COUNT = 240
CAMPAIGN_ACTIVE_TIME_LIMIT_NS = 18 * 60 * 60 * 1_000_000_000
CAMPAIGN_SOURCE_CORPUS_SHA256 = (
    "sha256:b71d029f20453f96a2872b9c1a79d716f48443009acbbf916d63d0d09efc5391"
)
CAMPAIGN_STATIC_AUDIT_SHA256 = (
    "sha256:ab8e1121971ff22426afa3394bb5469bae2ec7d3c6c45e323ecfe55237feb35e"
)
CAMPAIGN_PROGRESS_REVISION = "candidate-only-progress-capped-0.8-v1"
CAMPAIGN_STRICT_EVALUATOR_REVISION = "strict-state-output-equality-v1"
CAMPAIGN_EPISODE_LOG_REVISION = "sealed-interactive-controller-journal-v2"
CAMPAIGN_EXECUTION_ENVELOPE_REVISION = "intercode-v0.7-execution-envelope-v2"
CAMPAIGN_TASK_IDS = (
    "bash-fs1-032",
    "bash-fs1-008",
    "bash-fs1-023",
    "bash-fs1-013",
    "bash-fs1-048",
    "bash-fs1-051",
    "bash-fs1-054",
    "bash-fs1-057",
    "bash-fs1-055",
    "bash-fs2-044",
    "bash-fs2-028",
    "bash-fs2-046",
    "bash-fs2-035",
    "bash-fs2-024",
    "bash-fs2-004",
    "bash-fs2-009",
    "bash-fs2-034",
    "bash-fs3-013",
    "bash-fs3-054",
    "bash-fs3-025",
    "bash-fs3-005",
    "bash-fs3-037",
    "bash-fs3-036",
    "bash-fs3-050",
    "bash-fs3-052",
    "bash-fs3-006",
    "bash-fs4-020",
    "bash-fs4-000",
    "bash-fs4-010",
    "bash-fs4-024",
)
CAMPAIGN_TASK_MANIFEST_SHA256 = (
    "da5355df187c85b248469c6238c4f4c61dbfcca34c290e4163b55292d287fc60"
)
CAMPAIGN_WILLIAMS_ORDER = (
    (
        "direct",
        "independent_verified_sampling",
        "engineered_loop",
        "raw_feedback_loop",
    ),
    (
        "independent_verified_sampling",
        "raw_feedback_loop",
        "direct",
        "engineered_loop",
    ),
    (
        "raw_feedback_loop",
        "engineered_loop",
        "independent_verified_sampling",
        "direct",
    ),
    (
        "engineered_loop",
        "direct",
        "raw_feedback_loop",
        "independent_verified_sampling",
    ),
)

_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256_REFERENCE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BARE_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CHAIN_FIELDS = frozenset(
    {"sequence", "previous_event_sha256", "event_sha256"}
)
_RESULT_FIELDS = (
    "run_status",
    "official_success",
    "strict_success",
    "stop_reason",
    "attempts",
    "model_calls",
    "logical_prompt_tokens",
    "logical_completion_tokens",
    "environment_actions",
    "evaluator_calls",
    "checkpoint_creates",
    "checkpoint_restores",
    "safety_recoveries",
    "parser_failures",
    "initial_prompts",
    "independent_sample_prompts",
    "feedback_followups",
    "human_prompts",
)
_RESULT_BOOLEAN_FIELDS = ("official_success", "strict_success")
_RESULT_INTEGER_FIELDS = _RESULT_FIELDS[4:]
_RUN_STATUSES = frozenset(
    {"completed", "budget_exhausted", "infrastructure_error"}
)
_STOP_REASONS = frozenset(
    {
        "action_pipeline_budget_exhausted",
        "attempt_budget_exhausted",
        "checkpoint_restore_budget_exhausted",
        "direct_action_policy_failure",
        "direct_complete",
        "direct_parser_failure",
        "generation_telemetry_budget_violation",
        "logical_completion_token_budget_exhausted",
        "logical_prompt_token_budget_exhausted",
        "no_progress_guard",
        "per_call_context_token_budget_exhausted",
        "prompt_token_telemetry_mismatch",
        "rendered_prompt_byte_budget_exhausted",
    }
)
_INTERRUPTED = "interrupted"
_INFRASTRUCTURE_ERROR = "infrastructure_error"
_MAX_EXECUTION_ENVELOPE_BYTES = 2 * 1024 * 1024


class CampaignError(ValueError):
    """Base class for campaign contract and state failures."""


class CampaignIntegrityError(CampaignError):
    """Raised when an existing campaign does not match its declaration."""


class CampaignExecutionEnvelopeError(CampaignIntegrityError):
    """A sealed per-episode execution envelope is absent or unverifiable."""


class CampaignActiveTimeLimitError(CampaignError):
    """The cumulative completed-episode active-time cap forbids a new intent."""

    def __init__(self, cumulative_active_wall_time_ns: int) -> None:
        self.cumulative_active_wall_time_ns = cumulative_active_wall_time_ns
        self.limit_ns = CAMPAIGN_ACTIVE_TIME_LIMIT_NS
        super().__init__(
            "campaign cumulative active wall time reached the 18-hour limit"
        )


class CampaignInfrastructureInvalidError(CampaignError):
    """A prior infrastructure-invalid terminal forbids every future advance."""

    def __init__(self, episode: CampaignEpisode) -> None:
        self.episode = episode
        super().__init__(
            "campaign contains an infrastructure-invalid terminal and is halted"
        )


class CampaignMatrixError(CampaignError):
    """Raised when the orchestration matrix is invalid or incomplete."""


class CampaignPendingEpisodeError(CampaignError):
    """A durable intent requires exact external resource reconciliation."""

    def __init__(self, episode: CampaignEpisode) -> None:
        self.episode = episode
        super().__init__(
            "campaign has a pending episode; exact reconciliation is required"
        )


@dataclass(frozen=True)
class CampaignEpisode:
    episode_index: int
    model_id: str
    task_id: str
    arm: str
    seed: int

    @property
    def identity(self) -> tuple[int, str, str, str, int]:
        return (
            self.episode_index,
            self.model_id,
            self.task_id,
            self.arm,
            self.seed,
        )


@dataclass(frozen=True)
class CampaignSpec:
    """The complete public schedule declaration for one bounded campaign."""

    task_ids: tuple[str, ...]
    study_binding_sha256: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.task_ids, (str, bytes)):
            raise ValueError("campaign must declare exactly 30 task IDs")
        task_ids = tuple(self.task_ids)
        if len(task_ids) != CAMPAIGN_TASK_COUNT:
            raise ValueError("campaign must declare exactly 30 task IDs")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("campaign task IDs must be unique")
        for task_id in task_ids:
            if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
                raise ValueError("campaign task ID must be a path-free public identifier")
        if task_ids != CAMPAIGN_TASK_IDS:
            raise ValueError("campaign task IDs differ from the frozen 30-task manifest")
        manifest_payload = "".join(f"{task_id}\n" for task_id in task_ids).encode(
            "ascii"
        )
        if sha256(manifest_payload).hexdigest() != CAMPAIGN_TASK_MANIFEST_SHA256:
            raise RuntimeError("frozen campaign task-manifest digest is inconsistent")
        if CAMPAIGN_ARMS != INTERACTIVE_STRATEGIES:
            raise RuntimeError("campaign arms differ from the frozen interactive controller")
        if any(
            len(row) != len(CAMPAIGN_ARMS) or set(row) != set(CAMPAIGN_ARMS)
            for row in CAMPAIGN_WILLIAMS_ORDER
        ):
            raise RuntimeError("campaign Williams arm order is invalid")
        profile_models = (QWEN35_RAW_PROFILE.model, PHI4_MINI_RAW_PROFILE.model)
        if CAMPAIGN_MODELS != profile_models:
            raise RuntimeError("campaign models differ from the frozen rendering profiles")
        if self.study_binding_sha256 is not None and (
            type(self.study_binding_sha256) is not str
            or _SHA256_REFERENCE.fullmatch(self.study_binding_sha256) is None
        ):
            raise ValueError("campaign study binding must be a lowercase SHA-256")
        object.__setattr__(self, "task_ids", task_ids)

    def bind(self, study_binding_sha256: str) -> CampaignSpec:
        """Return the exact run-specific schedule bound to one prepared study."""

        if (
            type(study_binding_sha256) is not str
            or _SHA256_REFERENCE.fullmatch(study_binding_sha256) is None
        ):
            raise ValueError("campaign study binding must be a lowercase SHA-256")
        if (
            self.study_binding_sha256 is not None
            and self.study_binding_sha256 != study_binding_sha256
        ):
            raise ValueError("campaign schedule is already bound to another study")
        if self.study_binding_sha256 == study_binding_sha256:
            return self
        return CampaignSpec(self.task_ids, study_binding_sha256)

    @property
    def episodes(self) -> tuple[CampaignEpisode, ...]:
        episodes: list[CampaignEpisode] = []
        index = 1
        for model_id in CAMPAIGN_MODELS:
            for task_index, task_id in enumerate(self.task_ids):
                arm_order = CAMPAIGN_WILLIAMS_ORDER[
                    task_index % len(CAMPAIGN_WILLIAMS_ORDER)
                ]
                for arm in arm_order:
                    episodes.append(
                        CampaignEpisode(index, model_id, task_id, arm, CAMPAIGN_SEED)
                    )
                    index += 1
        if len(episodes) != CAMPAIGN_EPISODE_COUNT:  # pragma: no cover
            raise RuntimeError("campaign episode cardinality invariant failed")
        return tuple(episodes)

    @property
    def schedule_sha256(self) -> str:
        payload = json.dumps(
            {
                "arms": list(CAMPAIGN_ARMS),
                "arm_order_rows": [list(row) for row in CAMPAIGN_WILLIAMS_ORDER],
                "active_time_limit_ns": CAMPAIGN_ACTIVE_TIME_LIMIT_NS,
                "attempt_cap": CAMPAIGN_ATTEMPT_CAP,
                "controller_revision": INTERACTIVE_CONTROLLER_REVISION,
                "episode_log_revision": CAMPAIGN_EPISODE_LOG_REVISION,
                "execution_envelope_revision": (
                    CAMPAIGN_EXECUTION_ENVELOPE_REVISION
                ),
                "model_ids": list(CAMPAIGN_MODELS),
                "progress_revision": CAMPAIGN_PROGRESS_REVISION,
                "schema_revision": CAMPAIGN_SCHEMA_REVISION,
                "seed": CAMPAIGN_SEED,
                "source_corpus_sha256": CAMPAIGN_SOURCE_CORPUS_SHA256,
                "static_audit_sha256": CAMPAIGN_STATIC_AUDIT_SHA256,
                "strict_evaluator_revision": CAMPAIGN_STRICT_EVALUATOR_REVISION,
                "task_ids": list(self.task_ids),
                "task_manifest_sha256": CAMPAIGN_TASK_MANIFEST_SHA256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + sha256(payload).hexdigest()


@dataclass(frozen=True)
class CampaignEpisodeExecution:
    """Exact result plus bound controller, timing, and host-safety evidence.

    The campaign ledger binds this root but does not authenticate the source
    journal. A later evidence gate must reopen that journal, require its seal,
    and match this digest before any publication decision.
    """

    result: InteractiveResult
    execution_authority_sha256: str
    controller_log_sha256: str
    active_wall_time_ns: int
    before_host_sample: HostSafetySample
    after_host_sample: HostSafetySample

    def __post_init__(self) -> None:
        if type(self.result) is not InteractiveResult:
            raise CampaignError("episode execution must contain an exact InteractiveResult")
        if _SHA256_REFERENCE.fullmatch(self.execution_authority_sha256) is None:
            raise CampaignError(
                "episode execution authority must be a lowercase SHA-256 reference"
            )
        if _SHA256_REFERENCE.fullmatch(self.controller_log_sha256) is None:
            raise CampaignError("controller log root must be a lowercase SHA-256 reference")
        if (
            isinstance(self.active_wall_time_ns, bool)
            or not isinstance(self.active_wall_time_ns, int)
            or self.active_wall_time_ns <= 0
        ):
            raise CampaignError("episode active wall time must be positive nanoseconds")
        if type(self.before_host_sample) is not HostSafetySample or type(
            self.after_host_sample
        ) is not HostSafetySample:
            raise CampaignError("episode host evidence must use exact samples")
        if (
            self.before_host_sample.boot_time_unix_microseconds
            != self.after_host_sample.boot_time_unix_microseconds
        ):
            raise CampaignError("episode host samples cross a boot boundary")
        observed_interval = (
            self.after_host_sample.captured_monotonic_ns
            - self.before_host_sample.captured_monotonic_ns
        )
        if observed_interval < self.active_wall_time_ns:
            raise CampaignError("episode host samples do not enclose active wall time")


def write_episode_execution_envelope(
    envelope_path: str | Path,
    episode: CampaignEpisode,
    execution: CampaignEpisodeExecution,
) -> CampaignEpisodeExecution:
    """Create, seal, and independently reverify one new execution envelope."""

    if type(episode) is not CampaignEpisode:
        raise CampaignExecutionEnvelopeError(
            "execution envelope requires an exact campaign episode"
        )
    if type(execution) is not CampaignEpisodeExecution:
        raise CampaignExecutionEnvelopeError(
            "execution envelope requires an exact episode execution"
        )
    event = {
        "type": "episode_execution_enveloped",
        "schema_revision": CAMPAIGN_EXECUTION_ENVELOPE_REVISION,
        **_episode_fields(episode),
        "execution_authority_sha256": execution.execution_authority_sha256,
        "result": _serialize_result(execution.result, episode),
        "controller_log_sha256": execution.controller_log_sha256,
        "active_wall_time_ns": execution.active_wall_time_ns,
        "before_host_sample": execution.before_host_sample.to_record(),
        "after_host_sample": execution.after_host_sample.to_record(),
    }
    path = Path(envelope_path)
    _precreate_execution_envelope(path)
    append_journal_event(path, event)
    seal_journal(path)
    observed = load_episode_execution_envelope(path, episode)
    if observed != execution:
        raise CampaignExecutionEnvelopeError(
            "execution envelope differs after independent reverification"
        )
    return observed


def load_episode_execution_envelope(
    envelope_path: str | Path,
    episode: CampaignEpisode,
) -> CampaignEpisodeExecution:
    """Strictly reopen one sealed envelope and return its exact typed payload."""

    if type(episode) is not CampaignEpisode:
        raise CampaignExecutionEnvelopeError(
            "execution envelope requires an exact campaign episode"
        )
    payload = _read_secure_execution_envelope(Path(envelope_path))
    return _parse_episode_execution_envelope(payload, episode)


def load_episode_execution_envelope_at(
    directory_descriptor: int,
    filename: str,
    episode: CampaignEpisode,
) -> CampaignEpisodeExecution:
    """Reopen one envelope relative to an already verified directory fd."""

    if type(episode) is not CampaignEpisode:
        raise CampaignExecutionEnvelopeError(
            "execution envelope requires an exact campaign episode"
        )
    payload = _read_secure_execution_envelope_at(
        directory_descriptor,
        filename,
    )
    return _parse_episode_execution_envelope(payload, episode)


def _parse_episode_execution_envelope(
    payload: bytes,
    episode: CampaignEpisode,
) -> CampaignEpisodeExecution:
    records = _decode_execution_envelope(payload)
    if len(records) != 2 or records[-1].get("type") != SEALED_EVENT_TYPE:
        raise CampaignExecutionEnvelopeError(
            "execution envelope is not exactly and terminally sealed"
        )
    record = _without_chain(records[0])
    identity = _episode_fields(episode)
    expected_fields = {
        "type",
        "schema_revision",
        *identity,
        "execution_authority_sha256",
        "result",
        "controller_log_sha256",
        "active_wall_time_ns",
        "before_host_sample",
        "after_host_sample",
    }
    if set(record) != expected_fields:
        raise CampaignExecutionEnvelopeError(
            "execution envelope fields differ from the frozen schema"
        )
    if (
        record.get("type") != "episode_execution_enveloped"
        or record.get("schema_revision")
        != CAMPAIGN_EXECUTION_ENVELOPE_REVISION
        or any(record.get(key) != value for key, value in identity.items())
    ):
        raise CampaignExecutionEnvelopeError(
            "execution envelope episode identity differs from the pending intent"
        )
    try:
        result = _parse_result(record.get("result"), episode)
        execution_authority_sha256 = _parse_execution_authority_sha256(
            record.get("execution_authority_sha256")
        )
        controller_log_sha256 = _parse_controller_log_sha256(
            record.get("controller_log_sha256")
        )
        active_wall_time_ns = _parse_active_wall_time_ns(
            record.get("active_wall_time_ns")
        )
        before_host_sample = _parse_host_sample(
            record.get("before_host_sample"),
            "before",
        )
        after_host_sample = _parse_host_sample(
            record.get("after_host_sample"),
            "after",
        )
        _validate_host_interval(
            active_wall_time_ns,
            before_host_sample,
            after_host_sample,
        )
        return CampaignEpisodeExecution(
            result=result,
            execution_authority_sha256=execution_authority_sha256,
            controller_log_sha256=controller_log_sha256,
            active_wall_time_ns=active_wall_time_ns,
            before_host_sample=before_host_sample,
            after_host_sample=after_host_sample,
        )
    except (CampaignError, CampaignIntegrityError) as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope payload is invalid"
        ) from error


class CampaignEpisodeExecutor(Protocol):
    """Typed model/environment boundary for exactly one declared episode."""

    def __call__(self, episode: CampaignEpisode) -> CampaignEpisodeExecution: ...


class CampaignPendingReconciler(Protocol):
    """Locate an existing envelope; this boundary must never issue a model call."""

    def __call__(self, episode: CampaignEpisode) -> str | Path | None: ...


class CampaignBeforeNewIntent(Protocol):
    """Revalidate external authority before creating durable new work."""

    def __call__(self, episode: CampaignEpisode) -> None: ...


@dataclass(frozen=True)
class CampaignProgress:
    declared_episodes: int
    completed_episodes: int
    invalid_episodes: int
    pending_episodes: int
    unstarted_episodes: int
    sealed: bool


@dataclass(frozen=True)
class CampaignAdvance:
    action: str
    episode: CampaignEpisode | None
    progress: CampaignProgress


@dataclass(frozen=True)
class CampaignEpisodeResult:
    episode: CampaignEpisode
    result: InteractiveResult
    execution_authority_sha256: str
    controller_log_sha256: str
    active_wall_time_ns: int
    before_host_sample: HostSafetySample
    after_host_sample: HostSafetySample


@dataclass(frozen=True)
class CampaignMatrix:
    """Complete orchestration rows, not independently verified evidence."""

    episodes: tuple[CampaignEpisodeResult, ...]

    @property
    def limitation(self) -> str:
        return (
            "This matrix is not publication authority; a separate gate must "
            "verify every bound controller log is sealed and provenance-valid."
        )

    @property
    def total_model_calls(self) -> int:
        return sum(item.result.model_calls for item in self.episodes)

    @property
    def total_logical_prompt_tokens(self) -> int:
        return sum(item.result.logical_prompt_tokens for item in self.episodes)

    @property
    def total_logical_completion_tokens(self) -> int:
        return sum(item.result.logical_completion_tokens for item in self.episodes)

    @property
    def total_human_prompts(self) -> int:
        return sum(item.result.human_prompts for item in self.episodes)

    @property
    def total_active_wall_time_ns(self) -> int:
        return sum(item.active_wall_time_ns for item in self.episodes)


@dataclass(frozen=True)
class _CampaignState:
    initialized: bool
    completed: tuple[CampaignEpisodeResult, ...]
    invalid: tuple[CampaignEpisode, ...]
    pending: CampaignEpisode | None
    terminal_count: int
    sealed: bool

    def progress(self) -> CampaignProgress:
        pending_count = int(self.pending is not None)
        return CampaignProgress(
            declared_episodes=CAMPAIGN_EPISODE_COUNT,
            completed_episodes=len(self.completed),
            invalid_episodes=len(self.invalid),
            pending_episodes=pending_count,
            unstarted_episodes=(
                CAMPAIGN_EPISODE_COUNT - self.terminal_count - pending_count
            ),
            sealed=self.sealed,
        )


def advance_campaign(
    journal_path: str | Path,
    spec: CampaignSpec,
    execute: CampaignEpisodeExecutor,
    *,
    reconcile_pending: CampaignPendingReconciler | None = None,
    before_new_intent: CampaignBeforeNewIntent | None = None,
) -> CampaignAdvance:
    """Advance no more than one new episode in declared schedule order.

    ``before_new_intent`` runs inside the campaign lock before either the first
    declaration or a new intent is written.  The intent itself is fsynced by
    :func:`append_journal_event` before the executor is entered.  An executor
    exception or process exit therefore leaves a durable pending intent that
    halts every later invocation.
    """

    _validate_spec_and_executor(
        spec,
        execute,
        reconcile_pending,
        before_new_intent,
    )
    path = Path(journal_path)
    with _campaign_lock(path):
        state = _read_state(path, spec)
        if state.invalid:
            raise CampaignInfrastructureInvalidError(state.invalid[0])
        if state.sealed:
            return CampaignAdvance("campaign_complete", None, state.progress())
        if state.pending is not None:
            pending = state.pending
            if reconcile_pending is None:
                raise CampaignPendingEpisodeError(pending)
            try:
                envelope_path = reconcile_pending(pending)
                if envelope_path is None:
                    raise CampaignExecutionEnvelopeError(
                        "pending reconciler found no execution envelope"
                    )
                execution = load_episode_execution_envelope(
                    envelope_path,
                    pending,
                )
                _require_execution_authority(spec, execution)
            except CampaignPendingEpisodeError:
                raise
            except Exception as error:
                raise CampaignPendingEpisodeError(pending) from error
            _append_execution_terminal(path, spec, pending, execution)
            state = _read_state(path, spec)
            return CampaignAdvance("episode_reconciled", pending, state.progress())
        cumulative_active_wall_time_ns = sum(
            item.active_wall_time_ns for item in state.completed
        )
        if cumulative_active_wall_time_ns >= CAMPAIGN_ACTIVE_TIME_LIMIT_NS:
            raise CampaignActiveTimeLimitError(cumulative_active_wall_time_ns)
        if state.terminal_count == CAMPAIGN_EPISODE_COUNT:
            state = _seal_if_terminal(path, spec, state)
            return CampaignAdvance("campaign_sealed", None, state.progress())

        episode = spec.episodes[state.terminal_count]
        if before_new_intent is not None:
            before_new_intent(episode)
        if not state.initialized:
            append_journal_event(path, _declaration_event(spec))
            _fsync_parent_directory(path)
            state = _read_state(path, spec)
        append_journal_event(
            path,
            {"type": "episode_intent", **_episode_fields(episode)},
        )
        execution = execute(episode)
        if type(execution) is not CampaignEpisodeExecution:
            raise CampaignError(
                "campaign executor must return an exact CampaignEpisodeExecution"
            )
        action = _append_execution_terminal(path, spec, episode, execution)
        state = _read_state(path, spec)
        state = _seal_if_terminal(path, spec, state)
        return CampaignAdvance(action, episode, state.progress())


def inspect_campaign(
    journal_path: str | Path, spec: CampaignSpec
) -> CampaignProgress:
    """Validate and return campaign progress without changing the journal."""

    _require_bound_campaign_spec(spec)
    path = Path(journal_path)
    with _campaign_lock(path):
        return _read_state(path, spec).progress()


def load_complete_campaign_matrix(
    journal_path: str | Path, spec: CampaignSpec
) -> CampaignMatrix:
    """Load a complete orchestration matrix, never a publication decision.

    The caller must separately verify each bound controller-log root against a
    sealed provenance-valid episode journal before using these rows as
    experimental evidence.
    """

    _require_bound_campaign_spec(spec)
    path = Path(journal_path)
    with _campaign_lock(path):
        state = _read_state(path, spec)
        if state.invalid:
            raise CampaignMatrixError("campaign matrix contains invalid episodes")
        if (
            not state.sealed
            or state.pending is not None
            or state.terminal_count != CAMPAIGN_EPISODE_COUNT
            or len(state.completed) != CAMPAIGN_EPISODE_COUNT
        ):
            raise CampaignMatrixError("campaign matrix is incomplete or unsealed")
        return CampaignMatrix(state.completed)


def _validate_spec_and_executor(
    spec: CampaignSpec,
    execute: CampaignEpisodeExecutor,
    reconcile_pending: CampaignPendingReconciler | None,
    before_new_intent: CampaignBeforeNewIntent | None,
) -> None:
    _require_bound_campaign_spec(spec)
    if not callable(execute):
        raise ValueError("campaign executor must be callable")
    if reconcile_pending is not None and not callable(reconcile_pending):
        raise ValueError("campaign pending reconciler must be callable")
    if before_new_intent is not None and not callable(before_new_intent):
        raise ValueError("campaign pre-intent revalidator must be callable")


def _append_execution_terminal(
    path: Path,
    spec: CampaignSpec,
    episode: CampaignEpisode,
    execution: CampaignEpisodeExecution,
) -> str:
    _require_execution_authority(spec, execution)
    result = execution.result
    result_record = _serialize_result(result, episode)
    evidence = {
        "result": result_record,
        "execution_authority_sha256": execution.execution_authority_sha256,
        "controller_log_sha256": execution.controller_log_sha256,
        "active_wall_time_ns": execution.active_wall_time_ns,
        "before_host_sample": execution.before_host_sample.to_record(),
        "after_host_sample": execution.after_host_sample.to_record(),
    }
    if result.run_status == _INFRASTRUCTURE_ERROR:
        action = "episode_invalid"
        event = {
            "type": action,
            **_episode_fields(episode),
            "reason": _INFRASTRUCTURE_ERROR,
            **evidence,
        }
    else:
        action = "episode_completed"
        event = {
            "type": action,
            **_episode_fields(episode),
            **evidence,
        }
    append_journal_event(path, event)
    return action


def _require_execution_authority(
    spec: CampaignSpec,
    execution: CampaignEpisodeExecution,
) -> None:
    if execution.execution_authority_sha256 != spec.study_binding_sha256:
        raise CampaignExecutionEnvelopeError(
            "episode execution authority differs from the campaign study binding"
        )


def _declaration_event(spec: CampaignSpec) -> dict[str, object]:
    _require_bound_campaign_spec(spec)
    return {
        "type": "campaign_declared",
        "study_binding_sha256": spec.study_binding_sha256,
        "schema_revision": CAMPAIGN_SCHEMA_REVISION,
        "controller_revision": INTERACTIVE_CONTROLLER_REVISION,
        "episode_log_revision": CAMPAIGN_EPISODE_LOG_REVISION,
        "execution_envelope_revision": CAMPAIGN_EXECUTION_ENVELOPE_REVISION,
        "model_ids": list(CAMPAIGN_MODELS),
        "task_ids": list(spec.task_ids),
        "arms": list(CAMPAIGN_ARMS),
        "arm_order_rows": [list(row) for row in CAMPAIGN_WILLIAMS_ORDER],
        "active_time_limit_ns": CAMPAIGN_ACTIVE_TIME_LIMIT_NS,
        "attempt_cap": CAMPAIGN_ATTEMPT_CAP,
        "progress_revision": CAMPAIGN_PROGRESS_REVISION,
        "seed": CAMPAIGN_SEED,
        "source_corpus_sha256": CAMPAIGN_SOURCE_CORPUS_SHA256,
        "static_audit_sha256": CAMPAIGN_STATIC_AUDIT_SHA256,
        "strict_evaluator_revision": CAMPAIGN_STRICT_EVALUATOR_REVISION,
        "episode_count": CAMPAIGN_EPISODE_COUNT,
        "schedule_sha256": spec.schedule_sha256,
        "task_manifest_sha256": CAMPAIGN_TASK_MANIFEST_SHA256,
    }


def _require_bound_campaign_spec(spec: object) -> CampaignSpec:
    if type(spec) is not CampaignSpec:
        raise ValueError("campaign spec must be a CampaignSpec")
    if spec.study_binding_sha256 is None:
        raise CampaignError(
            "campaign execution requires a verifier-issued study binding"
        )
    return spec


def _episode_fields(episode: CampaignEpisode) -> dict[str, object]:
    return {
        "episode_index": episode.episode_index,
        "model_id": episode.model_id,
        "task_id": episode.task_id,
        "arm": episode.arm,
        "seed": episode.seed,
    }


def _serialize_result(
    result: InteractiveResult, episode: CampaignEpisode
) -> dict[str, object]:
    if type(result) is not InteractiveResult:
        raise CampaignError("campaign executor must return an exact InteractiveResult")
    observed_fields = tuple(field.name for field in fields(result))
    if observed_fields != _RESULT_FIELDS:
        raise CampaignError("InteractiveResult schema differs from the campaign pin")
    if result.run_status not in _RUN_STATUSES:
        raise CampaignError("InteractiveResult run_status is not frozen")
    if result.stop_reason not in _STOP_REASONS:
        raise CampaignError("InteractiveResult stop_reason is not frozen")
    for field_name in _RESULT_BOOLEAN_FIELDS:
        if type(getattr(result, field_name)) is not bool:
            raise CampaignError(f"InteractiveResult {field_name} must be boolean")
    for field_name in _RESULT_INTEGER_FIELDS:
        value = getattr(result, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CampaignError(
                f"InteractiveResult {field_name} must be a non-negative integer"
            )
    automatic_prompts = (
        result.initial_prompts
        + result.independent_sample_prompts
        + result.feedback_followups
    )
    if automatic_prompts != result.model_calls:
        raise CampaignError(
            "InteractiveResult automatic prompt accounting must equal model_calls"
        )
    if result.attempts != result.model_calls:
        raise CampaignError("InteractiveResult attempts must equal model_calls")
    if result.human_prompts != 0:
        raise CampaignError("InteractiveResult human_prompts must be zero")
    if result.official_success:
        raise CampaignError(
            "InteractiveResult official_success must remain false in v0.7"
        )
    if not 1 <= result.model_calls <= CAMPAIGN_ATTEMPT_CAP:
        raise CampaignError("InteractiveResult model_calls exceed the frozen arm cap")
    if episode.arm == "direct":
        if (
            result.model_calls != 1
            or result.initial_prompts != 1
            or result.independent_sample_prompts != 0
            or result.feedback_followups != 0
        ):
            raise CampaignError(
                "Direct requires one initial prompt and no follow-up prompt"
            )
    elif episode.arm == "independent_verified_sampling":
        if (
            result.initial_prompts != 1
            or result.independent_sample_prompts != result.model_calls - 1
            or result.feedback_followups != 0
        ):
            raise CampaignError(
                "Independent requires one initial prompt and only independent samples"
            )
    elif episode.arm in {"raw_feedback_loop", "engineered_loop"}:
        if (
            result.initial_prompts != 1
            or result.independent_sample_prompts != 0
            or result.feedback_followups != result.model_calls - 1
        ):
            raise CampaignError(
                "feedback arms require one initial prompt and only feedback follow-ups"
            )
    else:  # pragma: no cover - CampaignEpisode is always schedule-owned
        raise CampaignError("campaign episode arm is not frozen")
    return {field_name: getattr(result, field_name) for field_name in _RESULT_FIELDS}


def _read_state(path: Path, spec: CampaignSpec) -> _CampaignState:
    records = _read_records(path)
    if not records:
        return _CampaignState(False, (), (), None, 0, False)
    declaration = _without_chain(records[0])
    if declaration != _declaration_event(spec):
        raise CampaignIntegrityError("campaign declaration differs from the requested spec")

    completed: list[CampaignEpisodeResult] = []
    invalid: list[CampaignEpisode] = []
    pending: CampaignEpisode | None = None
    terminal_count = 0
    sealed = False
    schedule = spec.episodes
    for position, record in enumerate(records[1:], 1):
        payload = _without_chain(record)
        event_type = payload.get("type")
        if event_type == "journal_sealed":
            if position != len(records) - 1:
                raise CampaignIntegrityError("campaign seal is not terminal")
            if pending is not None or terminal_count != CAMPAIGN_EPISODE_COUNT:
                raise CampaignIntegrityError(
                    "campaign was sealed before every episode became terminal"
                )
            if set(payload) != {"type", "sealed_event_count"}:
                raise CampaignIntegrityError("campaign seal has unexpected fields")
            sealed = True
            continue
        if sealed:
            raise CampaignIntegrityError("campaign has an event after its seal")
        if pending is None:
            if event_type != "episode_intent":
                raise CampaignIntegrityError("campaign expected the next episode intent")
            if terminal_count >= CAMPAIGN_EPISODE_COUNT:
                raise CampaignIntegrityError("campaign contains an undeclared episode")
            expected = schedule[terminal_count]
            if payload != {"type": "episode_intent", **_episode_fields(expected)}:
                raise CampaignIntegrityError("campaign episode intent is out of order")
            pending = expected
            continue

        expected_identity = _episode_fields(pending)
        if event_type == "episode_completed":
            expected_fields = {
                "type",
                *expected_identity,
                "result",
                "execution_authority_sha256",
                "controller_log_sha256",
                "active_wall_time_ns",
                "before_host_sample",
                "after_host_sample",
            }
            if set(payload) != expected_fields or any(
                payload.get(key) != value for key, value in expected_identity.items()
            ):
                raise CampaignIntegrityError("campaign completion identity is invalid")
            controller_log_sha256 = _parse_controller_log_sha256(
                payload.get("controller_log_sha256")
            )
            execution_authority_sha256 = _parse_execution_authority_sha256(
                payload.get("execution_authority_sha256")
            )
            if execution_authority_sha256 != spec.study_binding_sha256:
                raise CampaignIntegrityError(
                    "campaign execution authority differs from its declaration"
                )
            active_wall_time_ns = _parse_active_wall_time_ns(
                payload.get("active_wall_time_ns")
            )
            before_host_sample = _parse_host_sample(
                payload.get("before_host_sample"), "before"
            )
            after_host_sample = _parse_host_sample(
                payload.get("after_host_sample"), "after"
            )
            _validate_host_interval(
                active_wall_time_ns,
                before_host_sample,
                after_host_sample,
            )
            result = _parse_result(payload.get("result"), pending)
            if result.run_status == _INFRASTRUCTURE_ERROR:
                raise CampaignIntegrityError(
                    "infrastructure result must be an invalid terminal"
                )
            completed.append(
                CampaignEpisodeResult(
                    pending,
                    result,
                    execution_authority_sha256,
                    controller_log_sha256,
                    active_wall_time_ns,
                    before_host_sample,
                    after_host_sample,
                )
            )
        elif event_type == "episode_invalid":
            reason = payload.get("reason")
            if reason == _INTERRUPTED:
                expected_fields = {"type", *expected_identity, "reason"}
                if set(payload) != expected_fields:
                    raise CampaignIntegrityError(
                        "interrupted episode has unexpected fields"
                    )
            elif reason == _INFRASTRUCTURE_ERROR:
                expected_fields = {
                    "type",
                    *expected_identity,
                    "reason",
                    "result",
                    "execution_authority_sha256",
                    "controller_log_sha256",
                    "active_wall_time_ns",
                    "before_host_sample",
                    "after_host_sample",
                }
                if set(payload) != expected_fields:
                    raise CampaignIntegrityError(
                        "infrastructure-invalid episode has unexpected fields"
                    )
                _parse_controller_log_sha256(payload.get("controller_log_sha256"))
                execution_authority_sha256 = _parse_execution_authority_sha256(
                    payload.get("execution_authority_sha256")
                )
                if execution_authority_sha256 != spec.study_binding_sha256:
                    raise CampaignIntegrityError(
                        "campaign execution authority differs from its declaration"
                    )
                active_wall_time_ns = _parse_active_wall_time_ns(
                    payload.get("active_wall_time_ns")
                )
                before_host_sample = _parse_host_sample(
                    payload.get("before_host_sample"), "before"
                )
                after_host_sample = _parse_host_sample(
                    payload.get("after_host_sample"), "after"
                )
                _validate_host_interval(
                    active_wall_time_ns,
                    before_host_sample,
                    after_host_sample,
                )
                result = _parse_result(payload.get("result"), pending)
                if result.run_status != _INFRASTRUCTURE_ERROR:
                    raise CampaignIntegrityError(
                        "infrastructure-invalid episode has a contradictory result"
                    )
            else:
                raise CampaignIntegrityError("campaign invalid reason is not frozen")
            if any(payload.get(key) != value for key, value in expected_identity.items()):
                raise CampaignIntegrityError("campaign invalid identity is invalid")
            invalid.append(pending)
        else:
            raise CampaignIntegrityError("campaign intent lacks a valid terminal event")
        pending = None
        terminal_count += 1

    return _CampaignState(
        True,
        tuple(completed),
        tuple(invalid),
        pending,
        terminal_count,
        sealed,
    )


def _parse_result(value: object, episode: CampaignEpisode) -> InteractiveResult:
    if not isinstance(value, Mapping) or set(value) != set(_RESULT_FIELDS):
        raise CampaignIntegrityError("campaign result fields differ from the frozen schema")
    try:
        result = InteractiveResult(**dict(value))
        _serialize_result(result, episode)
    except (TypeError, CampaignError) as error:
        raise CampaignIntegrityError("campaign result is invalid") from error
    return result


def _parse_controller_log_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_REFERENCE.fullmatch(value) is None:
        raise CampaignIntegrityError("campaign controller-log root is invalid")
    return value


def _parse_execution_authority_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_REFERENCE.fullmatch(value) is None:
        raise CampaignIntegrityError("campaign execution authority is invalid")
    return value


def _parse_active_wall_time_ns(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CampaignIntegrityError("campaign active wall time is invalid")
    return value


def _parse_host_sample(value: object, phase: str) -> HostSafetySample:
    try:
        return parse_host_safety_sample(value)
    except ValueError as error:
        raise CampaignIntegrityError(
            f"campaign {phase} host-safety sample is invalid"
        ) from error


def _validate_host_interval(
    active_wall_time_ns: int,
    before: HostSafetySample,
    after: HostSafetySample,
) -> None:
    if before.boot_time_unix_microseconds != after.boot_time_unix_microseconds:
        raise CampaignIntegrityError("campaign host samples cross a boot boundary")
    if (
        after.captured_monotonic_ns - before.captured_monotonic_ns
        < active_wall_time_ns
    ):
        raise CampaignIntegrityError(
            "campaign host samples do not enclose active wall time"
        )


def _seal_if_terminal(
    path: Path, spec: CampaignSpec, state: _CampaignState
) -> _CampaignState:
    if (
        not state.sealed
        and state.pending is None
        and state.terminal_count == CAMPAIGN_EPISODE_COUNT
    ):
        seal_journal(path)
        return _read_state(path, spec)
    return state


def _without_chain(record: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in record.items() if key not in _CHAIN_FIELDS}


def _read_records(path: Path) -> tuple[dict[str, object], ...]:
    inspection = inspect_journal(path)
    if inspection.partial_tail is not None:
        raise JournalPartialTailError(
            "campaign journal has a partial tail requiring explicit recovery"
        )
    if inspection.record_count == 0:
        return ()
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
        records = tuple(json.loads(line) for line in raw_lines)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CampaignIntegrityError("campaign journal could not be read") from error
    if len(records) != inspection.record_count or any(
        not isinstance(record, dict) for record in records
    ):
        raise CampaignIntegrityError("campaign journal changed during inspection")
    return records


class _DuplicateEnvelopeKey(ValueError):
    pass


def _precreate_execution_envelope(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_metadata = path.parent.lstat()
    except OSError as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope parent is unavailable"
        ) from error
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(
        parent_metadata.st_mode
    ):
        raise CampaignExecutionEnvelopeError(
            "execution envelope parent must be a real directory"
        )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CampaignExecutionEnvelopeError(
            "execution envelope requires no-follow opens"
        )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(os.fspath(path), flags, 0o600)
    except FileExistsError:
        raise CampaignExecutionEnvelopeError(
            "execution envelope already exists and will not be overwritten"
        ) from None
    except OSError as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope could not be created safely"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CampaignExecutionEnvelopeError(
                "execution envelope is not a regular file"
            )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_parent_directory(path)


def _read_secure_execution_envelope(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope is unavailable"
        ) from error
    _validate_execution_envelope_metadata(before)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CampaignExecutionEnvelopeError(
            "execution envelope requires no-follow opens"
        )
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope could not be opened safely"
        ) from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        opened = os.fstat(descriptor)
        _validate_execution_envelope_metadata(opened)
        if _execution_envelope_identity(opened) != _execution_envelope_identity(before):
            raise CampaignExecutionEnvelopeError(
                "execution envelope identity changed before read"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_EXECUTION_ENVELOPE_BYTES:
                raise CampaignExecutionEnvelopeError(
                    "execution envelope exceeds its byte bound"
                )
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        try:
            after = path.lstat()
        except OSError as error:
            raise CampaignExecutionEnvelopeError(
                "execution envelope path changed during read"
            ) from error
        _validate_execution_envelope_metadata(finished)
        _validate_execution_envelope_metadata(after)
        identity = _execution_envelope_identity(opened)
        if (
            _execution_envelope_identity(finished) != identity
            or _execution_envelope_identity(after) != identity
            or finished.st_size != size
        ):
            raise CampaignExecutionEnvelopeError(
                "execution envelope changed during read"
            )
        return b"".join(chunks)
    except CampaignExecutionEnvelopeError:
        raise
    except OSError as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope read failed"
        ) from error
    finally:
        os.close(descriptor)


def _read_secure_execution_envelope_at(
    directory_descriptor: int,
    filename: str,
) -> bytes:
    if (
        type(directory_descriptor) is not int
        or directory_descriptor < 0
        or type(filename) is not str
        or not filename
        or len(filename.encode("utf-8")) > 255
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise CampaignExecutionEnvelopeError(
            "execution envelope descriptor or name is invalid"
        )
    try:
        directory_metadata = os.fstat(directory_descriptor)
        before = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope is unavailable"
        ) from error
    if not stat.S_ISDIR(directory_metadata.st_mode):
        raise CampaignExecutionEnvelopeError(
            "execution envelope descriptor must name a directory"
        )
    _validate_execution_envelope_metadata(before)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CampaignExecutionEnvelopeError(
            "execution envelope requires no-follow opens"
        )
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(
            filename,
            flags,
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope could not be opened safely"
        ) from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        opened = os.fstat(descriptor)
        _validate_execution_envelope_metadata(opened)
        if _execution_envelope_identity(opened) != _execution_envelope_identity(before):
            raise CampaignExecutionEnvelopeError(
                "execution envelope identity changed before read"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_EXECUTION_ENVELOPE_BYTES:
                raise CampaignExecutionEnvelopeError(
                    "execution envelope exceeds its byte bound"
                )
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        try:
            after = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise CampaignExecutionEnvelopeError(
                "execution envelope path changed during read"
            ) from error
        _validate_execution_envelope_metadata(finished)
        _validate_execution_envelope_metadata(after)
        identity = _execution_envelope_identity(opened)
        if (
            _execution_envelope_identity(finished) != identity
            or _execution_envelope_identity(after) != identity
            or finished.st_size != size
        ):
            raise CampaignExecutionEnvelopeError(
                "execution envelope changed during read"
            )
        return b"".join(chunks)
    except CampaignExecutionEnvelopeError:
        raise
    except OSError as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope read failed"
        ) from error
    finally:
        os.close(descriptor)


def _validate_execution_envelope_metadata(metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise CampaignExecutionEnvelopeError(
            "execution envelope must be a non-symlink regular file"
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise CampaignExecutionEnvelopeError(
            "execution envelope must be a regular file"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise CampaignExecutionEnvelopeError(
            "execution envelope must use exact owner mode 0600"
        )
    if metadata.st_uid != os.getuid():
        raise CampaignExecutionEnvelopeError(
            "execution envelope must be owner-owned"
        )


def _execution_envelope_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _decode_execution_envelope(payload: bytes) -> tuple[dict[str, object], ...]:
    if not payload or not payload.endswith(b"\n"):
        raise CampaignExecutionEnvelopeError(
            "execution envelope is incomplete or unsealed"
        )
    records: list[dict[str, object]] = []
    expected_sequence = 1
    previous_sha256 = GENESIS_EVENT_SHA256
    sealed = False
    for line in payload.splitlines():
        try:
            record = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_unique_envelope_object,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            _DuplicateEnvelopeKey,
        ) as error:
            raise CampaignExecutionEnvelopeError(
                "execution envelope contains invalid JSON"
            ) from error
        if not isinstance(record, dict):
            raise CampaignExecutionEnvelopeError(
                "execution envelope record must be an object"
            )
        if _canonical_envelope_record_bytes(record) != line:
            raise CampaignExecutionEnvelopeError(
                "execution envelope record is not canonical"
            )
        if record.get("sequence") != expected_sequence:
            raise CampaignExecutionEnvelopeError(
                "execution envelope sequence is invalid"
            )
        if record.get("previous_event_sha256") != previous_sha256:
            raise CampaignExecutionEnvelopeError(
                "execution envelope previous hash is invalid"
            )
        event_sha256 = record.get("event_sha256")
        if type(event_sha256) is not str or _BARE_SHA256.fullmatch(event_sha256) is None:
            raise CampaignExecutionEnvelopeError(
                "execution envelope event hash is invalid"
            )
        if event_sha256 != sha256(canonical_event_bytes(record)).hexdigest():
            raise CampaignExecutionEnvelopeError(
                "execution envelope event hash does not match"
            )
        if sealed:
            raise CampaignExecutionEnvelopeError(
                "execution envelope has a record after its seal"
            )
        if record.get("type") == SEALED_EVENT_TYPE:
            if record.get("sealed_event_count") != expected_sequence - 1:
                raise CampaignExecutionEnvelopeError(
                    "execution envelope seal count is invalid"
                )
            sealed = True
        previous_sha256 = event_sha256
        expected_sequence += 1
        records.append(record)
    if not sealed:
        raise CampaignExecutionEnvelopeError(
            "execution envelope is not sealed"
        )
    return tuple(records)


def _unique_envelope_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    record: dict[str, object] = {}
    for key, value in pairs:
        if key in record:
            raise _DuplicateEnvelopeKey(key)
        record[key] = value
    return record


def _canonical_envelope_record_bytes(record: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise CampaignExecutionEnvelopeError(
            "execution envelope record is not canonical JSON"
        ) from error


def _fsync_parent_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CampaignError("platform does not support no-follow directory opens")
    try:
        descriptor = os.open(os.fspath(path.parent), flags | nofollow)
    except OSError as error:
        raise CampaignError("campaign journal parent could not be opened") from error
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise CampaignError("campaign journal parent is not a directory")
        os.fsync(descriptor)
    except OSError as error:
        raise CampaignError("campaign journal parent could not be synchronized") from error
    finally:
        os.close(descriptor)


@contextmanager
def _campaign_lock(path: Path) -> Iterator[None]:
    """Serialize campaign decisions without putting host paths in evidence."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CampaignError("campaign journal parent could not be prepared") from error
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise CampaignError("platform does not support no-follow campaign locks")
    lock_path = path.with_name(path.name + ".lock")
    try:
        descriptor = os.open(
            os.fspath(lock_path),
            os.O_RDWR | os.O_CREAT | nofollow | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            raise CampaignError("campaign lock target is not a regular file") from error
        raise CampaignError("campaign lock could not be opened") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CampaignError("campaign lock target is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)
