"""Frozen, gold-free protocol facts for the v0.7 30-task study."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from .interactive_environment import AttemptEvaluation
from .intercode_source import InterCodeSource


V07_SOURCE_SHA256 = (
    "sha256:b71d029f20453f96a2872b9c1a79d716f48443009acbbf916d63d0d09efc5391"
)
V07_STATIC_AUDIT_SHA256 = (
    "sha256:ab8e1121971ff22426afa3394bb5469bae2ec7d3c6c45e323ecfe55237feb35e"
)
V07_SAMPLE_SALT = "edgeloopbench-v0.7-intercode-30task-v1"
V07_STRATUM_QUOTAS = ("fs1", 9), ("fs2", 8), ("fs3", 9), ("fs4", 4)
V07_STATIC_EXCLUDED_TASK_IDS = frozenset(
    {
        "bash-fs1-011",
        "bash-fs1-012",
        "bash-fs1-018",
        "bash-fs1-046",
        "bash-fs1-047",
        "bash-fs2-017",
        "bash-fs2-018",
        "bash-fs2-019",
        "bash-fs2-025",
        "bash-fs2-032",
        "bash-fs2-039",
        "bash-fs2-041",
        "bash-fs2-042",
        "bash-fs3-000",
        "bash-fs3-002",
        "bash-fs3-003",
        "bash-fs3-012",
        "bash-fs4-018",
        "bash-fs4-021",
        "bash-fs4-022",
    }
)
V07_TASK_IDS = (
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
V07_SAMPLE_MANIFEST_SHA256 = (
    "da5355df187c85b248469c6238c4f4c61dbfcca34c290e4163b55292d287fc60"
)


def build_v07_sample(source: InterCodeSource) -> tuple[str, ...]:
    """Rebuild the preregistered task sample without reading task gold."""

    if type(source) is not InterCodeSource:
        raise ValueError("v0.7 sample requires a verified InterCodeSource")
    if source.source_sha256 != V07_SOURCE_SHA256:
        raise ValueError("v0.7 source corpus identity differs from preregistration")
    if source.static_exclusion_audit_sha256 != V07_STATIC_AUDIT_SHA256:
        raise ValueError("v0.7 static audit identity differs from preregistration")

    by_stratum: dict[str, list[str]] = defaultdict(list)
    all_task_ids = {task.task_id for task in source.tasks}
    if not V07_STATIC_EXCLUDED_TASK_IDS <= all_task_ids:
        raise ValueError("v0.7 static exclusions are outside the pinned population")
    for task in source.tasks:
        if task.task_id not in V07_STATIC_EXCLUDED_TASK_IDS:
            by_stratum[task.stratum].append(task.task_id)

    source_digest = V07_SOURCE_SHA256.removeprefix("sha256:")

    def rank(task_id: str) -> tuple[str, str]:
        payload = (
            V07_SAMPLE_SALT.encode("utf-8")
            + b"\0"
            + source_digest.encode("ascii")
            + b"\0"
            + task_id.encode("ascii")
        )
        return hashlib.sha256(payload).hexdigest(), task_id

    selected: list[str] = []
    for stratum, quota in V07_STRATUM_QUOTAS:
        candidates = sorted(by_stratum[stratum], key=rank)
        if len(candidates) < quota:
            raise ValueError(f"v0.7 {stratum} eligible frame is below quota")
        selected.extend(candidates[:quota])
    frozen = tuple(selected)
    if frozen != V07_TASK_IDS:
        raise RuntimeError("v0.7 sample reconstruction differs from preregistration")
    manifest = "".join(f"{task_id}\n" for task_id in frozen).encode("ascii")
    if hashlib.sha256(manifest).hexdigest() != V07_SAMPLE_MANIFEST_SHA256:
        raise RuntimeError("v0.7 sample manifest digest is inconsistent")
    return frozen


def candidate_progress_evaluation(
    *,
    parsed_single_action: bool,
    action_admissible: bool,
    exit_code: int | None,
    state_changed: bool,
    normalized_output_nonempty: bool,
) -> AttemptEvaluation:
    """Return the candidate-only ranking signal, deliberately capped at 0.8."""

    facts = (
        parsed_single_action,
        action_admissible,
        state_changed,
        normalized_output_nonempty,
    )
    if any(type(fact) is not bool for fact in facts):
        raise ValueError("candidate progress facts must be booleans")
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise ValueError("candidate progress exit code must be an integer or null")
    if not parsed_single_action and (
        action_admissible
        or exit_code is not None
        or state_changed
        or normalized_output_nonempty
    ):
        raise ValueError("unparsed action cannot have candidate-surface facts")
    if not action_admissible and (
        exit_code is not None or state_changed or normalized_output_nonempty
    ):
        raise ValueError("inadmissible action cannot expose candidate-surface facts")
    if action_admissible and exit_code is None:
        raise ValueError("admissible action must expose an exit code")

    component_count = sum(
        (
            parsed_single_action,
            action_admissible,
            exit_code == 0,
            state_changed or normalized_output_nonempty,
        )
    )
    progress = round(0.2 * component_count, 1)
    if not 0.0 <= progress <= 0.8:  # pragma: no cover - arithmetic invariant
        raise RuntimeError("candidate progress escaped its frozen range")
    return AttemptEvaluation(reward=progress, official_success=False)
