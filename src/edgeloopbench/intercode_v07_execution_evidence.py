"""Independent execution-envelope evidence for the v0.7 formal campaign.

The campaign ledger and controller journals are separate append-only evidence
surfaces.  Publication additionally requires reopening every per-episode
execution envelope and proving that its result, controller root, active time,
and host samples exactly equal the already verified campaign matrix.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import InitVar, asdict, dataclass
from hashlib import sha256
from pathlib import Path

from .intercode_campaign_evidence import VerifiedCampaignEvidence
from .intercode_campaign_ledger import (
    CAMPAIGN_EPISODE_COUNT,
    CampaignEpisodeExecution,
    CampaignEpisodeResult,
    CampaignExecutionEnvelopeError,
    CampaignSpec,
    load_episode_execution_envelope_at,
)


V07_EXECUTION_EVIDENCE_REVISION = "intercode-v0.7-execution-envelope-set-v1"

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_AUTHORITY = object()


class V07ExecutionEvidenceError(ValueError):
    """The formal execution-envelope set is not publication-safe."""


@dataclass(frozen=True, slots=True)
class VerifiedV07ExecutionEnvelopeSet:
    """Path-free verifier-issued identity for all 240 exact envelopes."""

    campaign_log_sha256: str
    study_binding_sha256: str
    schedule_sha256: str
    episode_log_set_sha256: str
    execution_set_sha256: str
    verified_envelope_count: int
    _authority: InitVar[object | None] = None

    def __post_init__(self, _authority: object | None) -> None:
        if _authority is not _AUTHORITY:
            raise V07ExecutionEvidenceError(
                "v0.7 execution evidence must be verifier-issued"
            )
        for value in (
            self.campaign_log_sha256,
            self.study_binding_sha256,
            self.schedule_sha256,
            self.episode_log_set_sha256,
            self.execution_set_sha256,
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise V07ExecutionEvidenceError(
                    "v0.7 execution evidence contains an invalid SHA-256"
                )
        if self.verified_envelope_count != CAMPAIGN_EPISODE_COUNT:
            raise V07ExecutionEvidenceError(
                "v0.7 execution evidence must contain exactly 240 envelopes"
            )

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__(_AUTHORITY)
        return {
            "campaign_log_sha256": self.campaign_log_sha256,
            "episode_log_set_sha256": self.episode_log_set_sha256,
            "execution_set_sha256": self.execution_set_sha256,
            "schedule_sha256": self.schedule_sha256,
            "schema": V07_EXECUTION_EVIDENCE_REVISION,
            "study_binding_sha256": self.study_binding_sha256,
            "verified_envelope_count": self.verified_envelope_count,
        }


def verify_v07_execution_envelope_set(
    envelope_directory: str | Path,
    spec: CampaignSpec,
    campaign_evidence: VerifiedCampaignEvidence,
) -> VerifiedV07ExecutionEnvelopeSet:
    """Reopen and match the exact formal envelope set to campaign evidence."""

    if type(spec) is not CampaignSpec:
        raise ValueError("v0.7 execution evidence requires exact CampaignSpec")
    if type(campaign_evidence) is not VerifiedCampaignEvidence:
        raise ValueError(
            "v0.7 execution evidence requires VerifiedCampaignEvidence"
        )
    if (
        spec.study_binding_sha256 is None
        or campaign_evidence.study_binding_sha256 != spec.study_binding_sha256
    ):
        raise V07ExecutionEvidenceError(
            "campaign study binding differs from the requested formal study"
        )
    if (
        campaign_evidence.schedule_sha256 != spec.schedule_sha256
        or campaign_evidence.verified_episode_count != CAMPAIGN_EPISODE_COUNT
        or len(campaign_evidence.matrix.episodes) != CAMPAIGN_EPISODE_COUNT
    ):
        raise V07ExecutionEvidenceError(
            "campaign authority differs from the frozen formal schedule"
        )

    directory = Path(envelope_directory)
    descriptor, identity = _open_private_directory(directory)
    expected_names = {
        f"episode-{episode.episode_index:04d}.execution.jsonl"
        for episode in spec.episodes
    }
    records: list[dict[str, object]] = []
    try:
        try:
            observed_names = set(os.listdir(descriptor))
        except OSError as error:
            raise V07ExecutionEvidenceError(
                "execution-envelope directory could not be enumerated"
            ) from error
        if observed_names != expected_names:
            raise V07ExecutionEvidenceError(
                "execution evidence requires the exact 240-envelope set"
            )

        for episode, bound in zip(
            spec.episodes,
            campaign_evidence.matrix.episodes,
            strict=True,
        ):
            if type(bound) is not CampaignEpisodeResult or bound.episode != episode:
                raise V07ExecutionEvidenceError(
                    "campaign evidence order differs from the frozen schedule"
                )
            name = f"episode-{episode.episode_index:04d}.execution.jsonl"
            try:
                observed = load_episode_execution_envelope_at(
                    descriptor,
                    name,
                    episode,
                )
            except (OSError, ValueError, CampaignExecutionEnvelopeError) as error:
                raise V07ExecutionEvidenceError(
                    "execution envelope could not be independently verified"
                ) from error
            expected = _bound_execution(bound)
            if observed != expected:
                raise V07ExecutionEvidenceError(
                    "execution envelope differs from the campaign binding"
                )
            records.append(_execution_record(episode.episode_index, observed))
        _require_same_directory(directory, identity)
    finally:
        os.close(descriptor)

    root_record = {
        "campaign_log_sha256": campaign_evidence.campaign_log_sha256,
        "episode_log_set_sha256": campaign_evidence.episode_log_set_sha256,
        "executions": records,
        "schedule_sha256": spec.schedule_sha256,
        "schema": V07_EXECUTION_EVIDENCE_REVISION,
        "study_binding_sha256": spec.study_binding_sha256,
    }
    execution_set_sha256 = "sha256:" + sha256(
        json.dumps(
            root_record,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return VerifiedV07ExecutionEnvelopeSet(
        campaign_log_sha256=campaign_evidence.campaign_log_sha256,
        study_binding_sha256=spec.study_binding_sha256,
        schedule_sha256=spec.schedule_sha256,
        episode_log_set_sha256=campaign_evidence.episode_log_set_sha256,
        execution_set_sha256=execution_set_sha256,
        verified_envelope_count=len(records),
        _authority=_AUTHORITY,
    )


def _bound_execution(bound: CampaignEpisodeResult) -> CampaignEpisodeExecution:
    return CampaignEpisodeExecution(
        result=bound.result,
        execution_authority_sha256=bound.execution_authority_sha256,
        controller_log_sha256=bound.controller_log_sha256,
        active_wall_time_ns=bound.active_wall_time_ns,
        before_host_sample=bound.before_host_sample,
        after_host_sample=bound.after_host_sample,
    )


def _execution_record(
    episode_index: int,
    execution: CampaignEpisodeExecution,
) -> dict[str, object]:
    return {
        "active_wall_time_ns": execution.active_wall_time_ns,
        "after_host_sample": execution.after_host_sample.to_record(),
        "before_host_sample": execution.before_host_sample.to_record(),
        "controller_log_sha256": execution.controller_log_sha256,
        "execution_authority_sha256": execution.execution_authority_sha256,
        "episode_index": episode_index,
        "result": asdict(execution.result),
    }


def _open_private_directory(path: Path) -> tuple[int, tuple[int, ...]]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise V07ExecutionEvidenceError(
            "execution-envelope directory is unavailable"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise V07ExecutionEvidenceError(
            "execution-envelope directory must be a non-symlink directory"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
        raise V07ExecutionEvidenceError(
            "execution-envelope directory must be owner-owned mode 0700"
        )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise V07ExecutionEvidenceError(
            "execution-envelope verification requires no-follow opens"
        )
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise V07ExecutionEvidenceError(
            "execution-envelope directory could not be opened"
        ) from error
    opened = os.fstat(descriptor)
    identity = _directory_identity(opened)
    if identity != _directory_identity(metadata):
        os.close(descriptor)
        raise V07ExecutionEvidenceError(
            "execution-envelope directory identity changed"
        )
    return descriptor, identity


def _require_same_directory(path: Path, expected: tuple[int, ...]) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise V07ExecutionEvidenceError(
            "execution-envelope directory changed during verification"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or _directory_identity(metadata) != expected:
        raise V07ExecutionEvidenceError(
            "execution-envelope directory changed during verification"
        )


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


__all__ = [
    "V07ExecutionEvidenceError",
    "V07_EXECUTION_EVIDENCE_REVISION",
    "VerifiedV07ExecutionEnvelopeSet",
    "verify_v07_execution_envelope_set",
]
