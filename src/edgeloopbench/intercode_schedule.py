"""Frozen, outcome-independent block order for the v0.6 Bash study."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import InitVar, dataclass

from .interactive_controller import INTERACTIVE_STRATEGIES
from .intercode_sampling import ConfirmatorySampleManifest


SCHEDULE_SCHEMA_VERSION = 1
SCHEDULE_SALT = "edgeloopbench-v0.6-intercode-block-order-v1"
BLOCK_RANKING_ALGORITHM = (
    "sha256(salt_utf8 || nul || sample_sha256_utf8 || nul || "
    "task_id_utf8 || nul || decimal_seed_utf8)"
)
LATIN_SQUARE_ALGORITHM = (
    "even-williams-n4:first-row-[0,1,3,2];subsequent-rows-add-one-modulo-four"
)
CONFIRMATORY_SEEDS = (11, 29)
INTERACTIVE_ARMS = tuple(INTERACTIVE_STRATEGIES)

_TASK_ID_PATTERN = re.compile(r"^bash-fs[1-4]-[0-9]{3}$")
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCHEDULE_CONSTRUCTION_SEAL = object()
_BASE_WILLIAMS_ROW = (0, 1, 3, 2)


@dataclass(frozen=True, slots=True)
class ConfirmatoryBlock:
    """One task/seed block and its complete within-block arm order."""

    task_id: str
    replicate_seed: int
    arm_order: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or _TASK_ID_PATTERN.fullmatch(self.task_id) is None:
            raise ValueError("confirmatory block contains an invalid task ID")
        if type(self.replicate_seed) is not int or self.replicate_seed not in CONFIRMATORY_SEEDS:
            raise ValueError("confirmatory block contains an undeclared replicate seed")
        if type(self.arm_order) is not tuple or len(self.arm_order) != 4:
            raise ValueError("confirmatory block arm order must be a four-item tuple")
        if set(self.arm_order) != set(INTERACTIVE_ARMS):
            raise ValueError("confirmatory block arm order must contain each frozen arm once")


@dataclass(frozen=True, slots=True)
class ConfirmatoryBlockSchedule:
    """Builder-sealed canonical schedule shared by every admitted model."""

    sample_sha256: str
    schedule_salt: str
    block_ranking_algorithm: str
    latin_square_algorithm: str
    replicate_seeds: tuple[int, ...]
    blocks: tuple[ConfirmatoryBlock, ...]
    schedule_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _SCHEDULE_CONSTRUCTION_SEAL:
            raise ValueError("confirmatory block schedules are builder-sealed")
        _validate_schedule(self)

    def canonical_bytes(self) -> bytes:
        _validate_schedule(self)
        record = _schedule_core_record(self)
        record["schedule_sha256"] = self.schedule_sha256
        return _canonical_json(record)


def build_confirmatory_block_schedule(
    sample: ConfirmatorySampleManifest,
) -> ConfirmatoryBlockSchedule:
    """Build the preregistered 100-block Williams schedule from a sealed sample."""

    if type(sample) is not ConfirmatorySampleManifest:
        raise ValueError("sample must be a builder-sealed confirmatory manifest")
    sample.canonical_bytes()

    ranked_keys = sorted(
        (
            (task_id, seed)
            for task_id in sample.task_ids
            for seed in CONFIRMATORY_SEEDS
        ),
        key=lambda item: (
            _block_rank(sample.sample_sha256, item[0], item[1]),
            item[0],
            item[1],
        ),
    )
    blocks = tuple(
        ConfirmatoryBlock(
            task_id=task_id,
            replicate_seed=seed,
            arm_order=_williams_row(ordinal % 4),
        )
        for ordinal, (task_id, seed) in enumerate(ranked_keys)
    )
    values: dict[str, object] = {
        "sample_sha256": sample.sample_sha256,
        "schedule_salt": SCHEDULE_SALT,
        "block_ranking_algorithm": BLOCK_RANKING_ALGORITHM,
        "latin_square_algorithm": LATIN_SQUARE_ALGORITHM,
        "replicate_seeds": CONFIRMATORY_SEEDS,
        "blocks": blocks,
    }
    digest = "sha256:" + hashlib.sha256(
        _canonical_json(_schedule_core_record_from_values(**values))
    ).hexdigest()
    return ConfirmatoryBlockSchedule(
        **values,  # type: ignore[arg-type]
        schedule_sha256=digest,
        _construction_seal=_SCHEDULE_CONSTRUCTION_SEAL,
    )


def _block_rank(sample_sha256: str, task_id: str, seed: int) -> str:
    payload = (
        SCHEDULE_SALT.encode("utf-8")
        + b"\0"
        + sample_sha256.encode("ascii")
        + b"\0"
        + task_id.encode("ascii")
        + b"\0"
        + str(seed).encode("ascii")
    )
    return hashlib.sha256(payload).hexdigest()


def _williams_row(row: int) -> tuple[str, ...]:
    if type(row) is not int or not 0 <= row < 4:
        raise ValueError("Williams-square row must be in [0, 3]")
    return tuple(INTERACTIVE_ARMS[(index + row) % 4] for index in _BASE_WILLIAMS_ROW)


def _validate_schedule(schedule: ConfirmatoryBlockSchedule) -> None:
    if type(schedule.sample_sha256) is not str or _SHA256_PATTERN.fullmatch(
        schedule.sample_sha256
    ) is None:
        raise ValueError("schedule sample_sha256 must be a lowercase SHA-256 reference")
    if schedule.schedule_salt != SCHEDULE_SALT:
        raise ValueError("confirmatory schedule salt differs from the frozen value")
    if schedule.block_ranking_algorithm != BLOCK_RANKING_ALGORITHM:
        raise ValueError("confirmatory block ranking algorithm differs from the frozen value")
    if schedule.latin_square_algorithm != LATIN_SQUARE_ALGORITHM:
        raise ValueError("confirmatory Latin-square algorithm differs from the frozen value")
    if schedule.replicate_seeds != CONFIRMATORY_SEEDS:
        raise ValueError("confirmatory replicate seeds differ from the frozen values")
    if type(schedule.blocks) is not tuple or len(schedule.blocks) != 100:
        raise ValueError("confirmatory schedule must contain exactly 100 blocks")
    if any(type(block) is not ConfirmatoryBlock for block in schedule.blocks):
        raise ValueError("confirmatory schedule contains an untyped block")

    keys = tuple((block.task_id, block.replicate_seed) for block in schedule.blocks)
    if len(set(keys)) != 100:
        raise ValueError("confirmatory schedule block keys must be unique")
    task_counts = Counter(task_id for task_id, _seed in keys)
    if len(task_counts) != 50 or set(task_counts.values()) != {2}:
        raise ValueError("confirmatory schedule must pair 50 tasks with both seeds")
    expected_keys = tuple(
        sorted(
            keys,
            key=lambda item: (
                _block_rank(schedule.sample_sha256, item[0], item[1]),
                item[0],
                item[1],
            ),
        )
    )
    if keys != expected_keys:
        raise ValueError("confirmatory block order is not the frozen hash ranking")
    for ordinal, block in enumerate(schedule.blocks):
        if block.arm_order != _williams_row(ordinal % 4):
            raise ValueError("confirmatory arm order differs from the frozen Williams square")

    expected_per_arm = Counter({arm: 25 for arm in INTERACTIVE_ARMS})
    for position in range(4):
        if Counter(block.arm_order[position] for block in schedule.blocks) != expected_per_arm:
            raise ValueError("confirmatory arm positions are not balanced")
    expected_pairs = Counter(
        {
            (left, right): 25
            for left in INTERACTIVE_ARMS
            for right in INTERACTIVE_ARMS
            if left != right
        }
    )
    observed_pairs = Counter(
        pair
        for block in schedule.blocks
        for pair in zip(block.arm_order, block.arm_order[1:])
    )
    if observed_pairs != expected_pairs:
        raise ValueError("confirmatory ordered adjacent arm pairs are not balanced")

    if type(schedule.schedule_sha256) is not str or _SHA256_PATTERN.fullmatch(
        schedule.schedule_sha256
    ) is None:
        raise ValueError("schedule_sha256 must be a lowercase SHA-256 reference")
    expected_digest = "sha256:" + hashlib.sha256(
        _canonical_json(_schedule_core_record(schedule))
    ).hexdigest()
    if schedule.schedule_sha256 != expected_digest:
        raise ValueError("schedule_sha256 differs from the canonical schedule")


def _schedule_core_record(schedule: ConfirmatoryBlockSchedule) -> dict[str, object]:
    return _schedule_core_record_from_values(
        sample_sha256=schedule.sample_sha256,
        schedule_salt=schedule.schedule_salt,
        block_ranking_algorithm=schedule.block_ranking_algorithm,
        latin_square_algorithm=schedule.latin_square_algorithm,
        replicate_seeds=schedule.replicate_seeds,
        blocks=schedule.blocks,
    )


def _schedule_core_record_from_values(
    *,
    sample_sha256: str,
    schedule_salt: str,
    block_ranking_algorithm: str,
    latin_square_algorithm: str,
    replicate_seeds: tuple[int, ...],
    blocks: tuple[ConfirmatoryBlock, ...],
) -> dict[str, object]:
    return {
        "block_ranking_algorithm": block_ranking_algorithm,
        "blocks": [
            {
                "arm_order": list(block.arm_order),
                "replicate_seed": block.replicate_seed,
                "task_id": block.task_id,
            }
            for block in blocks
        ],
        "latin_square_algorithm": latin_square_algorithm,
        "replicate_seeds": list(replicate_seeds),
        "sample_sha256": sample_sha256,
        "schedule_salt": schedule_salt,
        "schema_version": SCHEDULE_SCHEMA_VERSION,
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = (
    "BLOCK_RANKING_ALGORITHM",
    "CONFIRMATORY_SEEDS",
    "INTERACTIVE_ARMS",
    "LATIN_SQUARE_ALGORITHM",
    "SCHEDULE_SCHEMA_VERSION",
    "SCHEDULE_SALT",
    "ConfirmatoryBlock",
    "ConfirmatoryBlockSchedule",
    "build_confirmatory_block_schedule",
)
