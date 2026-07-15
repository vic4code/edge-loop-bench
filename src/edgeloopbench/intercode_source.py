"""Strict, offline loading for the pinned InterCode Bash source population.

The public task objects in this module intentionally have no field that can
carry a gold command. Evaluator code must explicitly exchange a source-bound,
opaque reference for gold through :meth:`InterCodeSource.gold_for_evaluator`.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


INTERCODE_REVISION = "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8"
NL2BASH_REVISION = "d6b9f5bdff45621d190134e31ab63b7bf7002190"
MAX_VENDOR_FILE_BYTES = 4 * 1024 * 1024

_INTERCODE_ROOT = f"vendor/intercode/{INTERCODE_REVISION}"
_NL2BASH_ROOT = f"vendor/nl2bash/{NL2BASH_REVISION}"

# This is the complete v0.6 vendor inventory. Keeping the downloader's copy of
# these pins under test prevents a source refresh from silently changing the
# measured population.
VENDORED_FILE_SHA256: Mapping[str, str] = MappingProxyType(
    {
        f"{_INTERCODE_ROOT}/LICENSE.md": (
            "837bf0fc3fe75298e6bcca9dbb66028b449bc456e16621d7a0f65292fa037274"
        ),
        f"{_INTERCODE_ROOT}/data/nl2bash/README.md": (
            "c796ac8e6c633eceaf102e1cbfecb133e293a3ed5db620d22df3641536513667"
        ),
        f"{_INTERCODE_ROOT}/data/nl2bash/nl2bash_fs_1.json": (
            "60f88e1aacc7ebba535093f9890c5c33203f4e5f32958e0e94fbe90ec4f01c82"
        ),
        f"{_INTERCODE_ROOT}/data/nl2bash/nl2bash_fs_2.json": (
            "8f4ce24e535fab782fda607e37db2ae1d6c5f99993c638d1ac0a7e0b542f633e"
        ),
        f"{_INTERCODE_ROOT}/data/nl2bash/nl2bash_fs_3.json": (
            "a2d4ec8bc7ad69a4e2fb3eb84033994cf65ee9cfb355e3e63099df67a339b2e1"
        ),
        f"{_INTERCODE_ROOT}/data/nl2bash/nl2bash_fs_4.json": (
            "ce41b89450f87765a02a51df259ca0c1762e8249185c022adb089147e2c16200"
        ),
        f"{_INTERCODE_ROOT}/data/nl2bash/test_queries.json": (
            "d24a7a1eb61c2621c48a42f942d08f6aa02066630ab49c2a07de2530a226e0aa"
        ),
        f"{_INTERCODE_ROOT}/docker/nl2bash.Dockerfile": (
            "c8b52b44cc276921f1b139d49562152792872c7b013261b748305a78d4230189"
        ),
        f"{_INTERCODE_ROOT}/docker/docker.gitignore": (
            "5479a1cafa260c77e836e8601ba9a345d39df777dc9cb07d6a93f0ac29b69166"
        ),
        f"{_INTERCODE_ROOT}/docker/bash_scripts/setup_nl2b_fs_1.sh": (
            "02b9a2206d809a9fca03b755e61b94618248a400fd3132ac61d32b6f3009dd3f"
        ),
        f"{_INTERCODE_ROOT}/docker/bash_scripts/setup_nl2b_fs_2.sh": (
            "05c3109c4e9999e661d66c6d74137f0238b88017ec9cf884abdda0499e94ff1d"
        ),
        f"{_INTERCODE_ROOT}/docker/bash_scripts/setup_nl2b_fs_3.sh": (
            "5e8d9f832f272c31dfb73567e75d33efb970d4e4bf9a8e691582d4fa09422d09"
        ),
        f"{_INTERCODE_ROOT}/docker/bash_scripts/setup_nl2b_fs_4.sh": (
            "c5fb550aa1578fe2454e8ab06221165df90311231cb71d3d9b0ce036a8235274"
        ),
        f"{_NL2BASH_ROOT}/data/bash/LICENSE": (
            "4ac5c8b7fb1d1fccfa52916749674d67b2024c76616fed89db7f67a976056750"
        ),
    }
)

EXPECTED_SOURCE_COUNTS: Mapping[str, int] = MappingProxyType(
    {"fs1": 60, "fs2": 53, "fs3": 60, "fs4": 27}
)
EXPECTED_CALIBRATION_COUNT = 24

# Digests cover canonical UTF-8 JSON with sorted keys and compact separators.
# The population digests contain only public records. The source digest binds
# all 224 records, including evaluator-only gold, without exposing that gold.
PUBLIC_POPULATION_SHA256 = (
    "sha256:6c9bf55f6e6ca8e4a6c67a4f59959bad83d1eaa64f1413e9b46eb6628bf664e0"
)
CALIBRATION_POPULATION_SHA256 = (
    "sha256:ed24cc3215ca6a34477f2cce917c1816fe92a2ff0870ddcc9580f9ea08463f1a"
)
SOURCE_CORPUS_SHA256 = (
    "sha256:b71d029f20453f96a2872b9c1a79d716f48443009acbbf916d63d0d09efc5391"
)


class InterCodeSourceError(ValueError):
    """Raised when the vendored source fails integrity or schema checks."""


@dataclass(frozen=True, slots=True)
class PublicBashTask:
    """The complete agent-visible task record; it cannot carry gold."""

    task_id: str
    query: str
    stratum: str

    def to_public_record(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "query": self.query,
            "stratum": self.stratum,
        }


class PrivateTaskReference:
    """An identity-only capability for evaluator-side gold lookup."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<PrivateTaskReference opaque>"

    def __reduce__(self) -> Any:
        raise TypeError("private task references cannot be serialized")


@dataclass(frozen=True, slots=True, repr=False)
class _SourceRow:
    query: str
    gold: str


class InterCodeSource:
    """A verified public population plus evaluator-owned private capabilities."""

    __slots__ = (
        "_calibration_tasks",
        "_gold_by_reference",
        "_reference_by_task_id",
        "_tasks",
        "calibration_population_sha256",
        "population_sha256",
        "source_sha256",
    )

    def __init__(
        self,
        *,
        tasks: Sequence[PublicBashTask],
        calibration_tasks: Sequence[PublicBashTask],
        gold_by_task_id: Mapping[str, str],
        population_sha256: str,
        calibration_population_sha256: str,
        source_sha256: str,
    ) -> None:
        self._tasks = tuple(tasks)
        self._calibration_tasks = tuple(calibration_tasks)
        self.population_sha256 = population_sha256
        self.calibration_population_sha256 = calibration_population_sha256
        self.source_sha256 = source_sha256

        references: dict[str, PrivateTaskReference] = {}
        private: dict[PrivateTaskReference, str] = {}
        for task_id, gold in gold_by_task_id.items():
            reference = PrivateTaskReference()
            references[task_id] = reference
            private[reference] = gold
        self._reference_by_task_id = references
        self._gold_by_reference = private

    @property
    def tasks(self) -> tuple[PublicBashTask, ...]:
        return self._tasks

    @property
    def calibration_tasks(self) -> tuple[PublicBashTask, ...]:
        return self._calibration_tasks

    def private_reference(self, task_id: str) -> PrivateTaskReference:
        """Return an opaque evaluator capability without returning gold."""

        try:
            return self._reference_by_task_id[task_id]
        except KeyError as error:
            raise InterCodeSourceError(f"unknown task ID: {task_id}") from error

    def gold_for_evaluator(self, reference: PrivateTaskReference) -> str:
        """Resolve gold only from a capability created by this source instance."""

        try:
            return self._gold_by_reference[reference]
        except (KeyError, TypeError) as error:
            raise InterCodeSourceError(
                "private task reference does not belong to this source"
            ) from error


def load_intercode_source(project_root: str | Path | None = None) -> InterCodeSource:
    """Load the exact pinned task corpus without performing network access."""

    root = (
        Path(project_root)
        if project_root is not None
        else Path(__file__).resolve().parents[2]
    )
    payloads = _verify_vendor_inventory(root)

    source_rows: list[tuple[str, int, _SourceRow]] = []
    for number in range(1, 5):
        relative = f"{_INTERCODE_ROOT}/data/nl2bash/nl2bash_fs_{number}.json"
        rows = _decode_task_rows(payloads[relative], source=relative)
        expected = EXPECTED_SOURCE_COUNTS[f"fs{number}"]
        if len(rows) != expected:
            raise InterCodeSourceError(
                f"{relative}: expected {expected} rows, got {len(rows)}"
            )
        source_rows.extend((f"fs{number}", index, row) for index, row in enumerate(rows))

    calibration_relative = f"{_INTERCODE_ROOT}/data/nl2bash/test_queries.json"
    calibration_rows = _decode_task_rows(
        payloads[calibration_relative], source=calibration_relative
    )
    if len(calibration_rows) != EXPECTED_CALIBRATION_COUNT:
        raise InterCodeSourceError(
            f"{calibration_relative}: expected {EXPECTED_CALIBRATION_COUNT} rows, "
            f"got {len(calibration_rows)}"
        )

    _validate_unique_and_disjoint(source_rows, calibration_rows)

    tasks: list[PublicBashTask] = []
    calibration_tasks: list[PublicBashTask] = []
    gold_by_task_id: dict[str, str] = {}
    private_records: list[dict[str, str]] = []
    for stratum, index, row in source_rows:
        task_id = f"bash-{stratum}-{index:03d}"
        task = PublicBashTask(task_id=task_id, query=row.query, stratum=stratum)
        tasks.append(task)
        gold_by_task_id[task_id] = row.gold
        private_records.append({**task.to_public_record(), "gold": row.gold})
    for index, row in enumerate(calibration_rows):
        task_id = f"bash-calibration-{index:03d}"
        task = PublicBashTask(
            task_id=task_id, query=row.query, stratum="calibration"
        )
        calibration_tasks.append(task)
        gold_by_task_id[task_id] = row.gold
        private_records.append({**task.to_public_record(), "gold": row.gold})

    source_counts = Counter(task.stratum for task in tasks)
    if source_counts != EXPECTED_SOURCE_COUNTS:
        raise InterCodeSourceError(
            f"source stratum counts differ from the pin: {dict(source_counts)}"
        )

    population_sha256 = _canonical_sha256(
        [task.to_public_record() for task in tasks]
    )
    calibration_sha256 = _canonical_sha256(
        [task.to_public_record() for task in calibration_tasks]
    )
    source_sha256 = _canonical_sha256(private_records)
    _require_digest("public population", population_sha256, PUBLIC_POPULATION_SHA256)
    _require_digest(
        "calibration population",
        calibration_sha256,
        CALIBRATION_POPULATION_SHA256,
    )
    _require_digest("source corpus", source_sha256, SOURCE_CORPUS_SHA256)

    return InterCodeSource(
        tasks=tasks,
        calibration_tasks=calibration_tasks,
        gold_by_task_id=gold_by_task_id,
        population_sha256=population_sha256,
        calibration_population_sha256=calibration_sha256,
        source_sha256=source_sha256,
    )


def _verify_vendor_inventory(root: Path) -> dict[str, bytes]:
    try:
        resolved_root = root.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise InterCodeSourceError(f"project root is unavailable: {root}") from error
    if not resolved_root.is_dir():
        raise InterCodeSourceError(f"project root is not a directory: {root}")

    payloads: dict[str, bytes] = {}
    for relative, expected_sha256 in VENDORED_FILE_SHA256.items():
        path = resolved_root / relative
        if path.is_symlink():
            raise InterCodeSourceError(f"vendored file must not be a symlink: {relative}")
        try:
            resolved_path = path.resolve(strict=True)
            resolved_path.relative_to(resolved_root)
        except FileNotFoundError as error:
            raise InterCodeSourceError(f"missing vendored file: {relative}") from error
        except (OSError, ValueError) as error:
            raise InterCodeSourceError(f"unsafe vendored file path: {relative}") from error
        if not resolved_path.is_file():
            raise InterCodeSourceError(f"vendored path is not a file: {relative}")
        try:
            with resolved_path.open("rb") as handle:
                payload = handle.read(MAX_VENDOR_FILE_BYTES + 1)
        except OSError as error:
            raise InterCodeSourceError(f"cannot read vendored file: {relative}") from error
        if len(payload) > MAX_VENDOR_FILE_BYTES:
            raise InterCodeSourceError(
                f"vendored file exceeds the {MAX_VENDOR_FILE_BYTES}-byte safety limit: "
                f"{relative}"
            )
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if actual_sha256 != expected_sha256:
            raise InterCodeSourceError(
                f"SHA-256 mismatch for {relative}: expected {expected_sha256}, "
                f"got {actual_sha256}"
            )
        payloads[relative] = payload
    return payloads


def _decode_task_rows(payload: bytes, *, source: str) -> tuple[_SourceRow, ...]:
    """Decode a task file with bounded input and duplicate-key rejection."""

    if len(payload) > MAX_VENDOR_FILE_BYTES:
        raise InterCodeSourceError(
            f"{source}: exceeds the {MAX_VENDOR_FILE_BYTES}-byte safety limit"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InterCodeSourceError(f"{source}: is not valid UTF-8") from error
    try:
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_object_keys)
    except InterCodeSourceError:
        raise
    except (json.JSONDecodeError, ValueError) as error:
        raise InterCodeSourceError(f"{source}: invalid JSON: {error}") from error
    if not isinstance(raw, list):
        raise InterCodeSourceError(f"{source}: top level must be a JSON array")

    rows: list[_SourceRow] = []
    for index, item in enumerate(raw):
        label = f"{source}[{index}]"
        if not isinstance(item, dict):
            raise InterCodeSourceError(f"{label}: row must be an object")
        if set(item) != {"query", "gold"}:
            raise InterCodeSourceError(
                f"{label}: row keys must be exactly 'query' and 'gold'"
            )
        query = item["query"]
        gold = item["gold"]
        if not isinstance(query, str) or not query.strip():
            raise InterCodeSourceError(f"{label}.query must be a non-empty string")
        if not isinstance(gold, str) or not gold.strip():
            raise InterCodeSourceError(f"{label}.gold must be a non-empty string")
        if "\x00" in query or "\x00" in gold:
            raise InterCodeSourceError(f"{label}: NUL bytes are forbidden")
        rows.append(_SourceRow(query=query, gold=gold))
    return tuple(rows)


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InterCodeSourceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_unique_and_disjoint(
    source_rows: Sequence[tuple[str, int, _SourceRow]],
    calibration_rows: Sequence[_SourceRow],
) -> None:
    source_values = [row for _, _, row in source_rows]
    source_queries = [row.query for row in source_values]
    source_pairs = [(row.query, row.gold) for row in source_values]
    calibration_queries = [row.query for row in calibration_rows]
    calibration_pairs = [(row.query, row.gold) for row in calibration_rows]

    _require_unique(source_queries, "source queries")
    _require_unique(source_pairs, "source query/gold pairs")
    _require_unique(calibration_queries, "calibration queries")
    _require_unique(calibration_pairs, "calibration query/gold pairs")
    if set(source_queries).intersection(calibration_queries):
        raise InterCodeSourceError(
            "calibration and source populations have exact query overlap"
        )
    if set(source_pairs).intersection(calibration_pairs):
        raise InterCodeSourceError(
            "calibration and source populations have exact query/gold overlap"
        )


def _require_unique(values: Sequence[Any], label: str) -> None:
    if len(set(values)) != len(values):
        raise InterCodeSourceError(f"{label} must be unique")


def _canonical_sha256(records: Sequence[Mapping[str, str]]) -> str:
    payload = json.dumps(
        list(records), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _require_digest(label: str, actual: str, expected: str) -> None:
    if actual != expected:
        raise InterCodeSourceError(
            f"{label} digest mismatch: expected {expected}, got {actual}"
        )
