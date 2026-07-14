"""Public task-manifest parsing without loading evaluator assets."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


TASK_SCHEMA_VERSION = 1
MAX_TASK_MANIFEST_BYTES = 256 * 1024
CATEGORIES = frozenset({"localized", "cross-file", "diagnosis", "adversarial"})
SOURCE_TYPES = frozenset(
    {"generated_mutation", "reconstructed_bug", "verifier_adversarial"}
)
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$", re.IGNORECASE)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$", re.IGNORECASE)


class TaskManifestError(ValueError):
    """Raised when a public task manifest violates its contract."""


@dataclass(frozen=True)
class TestCommand:
    command: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class HiddenEvaluation:
    evaluator_id: str
    timeout_seconds: int


@dataclass(frozen=True)
class TaskManifest:
    schema_version: int
    id: str
    language: str
    category: str
    source_type: str
    license: str
    initial_commit: str
    gold_patch_sha256: str
    allowed_paths: tuple[str, ...]
    prohibited_paths: tuple[str, ...]
    public_test: TestCommand
    hidden_evaluation: HiddenEvaluation


def load_task_manifest(path: str | Path) -> TaskManifest:
    """Load a public task manifest without resolving evaluator identifiers."""

    source = Path(path)
    try:
        payload = source.read_bytes()
    except FileNotFoundError as error:
        raise TaskManifestError(f"task manifest not found: {source}") from error
    except OSError as error:
        raise TaskManifestError(f"cannot read task manifest {source}: {error}") from error
    if len(payload) > MAX_TASK_MANIFEST_BYTES:
        raise TaskManifestError(
            f"task manifest {source} exceeds the {MAX_TASK_MANIFEST_BYTES}-byte safety limit"
        )
    try:
        raw = tomllib.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise TaskManifestError(f"task manifest is not valid UTF-8: {source}") from error
    except (tomllib.TOMLDecodeError, ValueError) as error:
        raise TaskManifestError(f"invalid TOML in task manifest {source}: {error}") from error
    return validate_task_manifest(raw, source=str(source))


def validate_task_manifest(
    raw: Mapping[str, Any], *, source: str = "<mapping>"
) -> TaskManifest:
    """Validate the agent-visible portion of a task contract."""

    if not isinstance(raw, Mapping):
        raise TaskManifestError(f"{source}: top level must be a table")
    _reject_unknown(
        raw,
        {
            "schema_version",
            "id",
            "language",
            "category",
            "source_type",
            "license",
            "initial_commit",
            "gold_patch_sha256",
            "allowed_paths",
            "prohibited_paths",
            "public_test",
            "hidden_evaluation",
        },
        source,
    )

    schema_version = _integer(raw, "schema_version", source)
    if schema_version != TASK_SCHEMA_VERSION:
        raise TaskManifestError(
            f"{source}.schema_version must be {TASK_SCHEMA_VERSION}, got {schema_version}"
        )
    task_id = _identifier(raw, "id", source)
    language = _string(raw, "language", source)
    if language != "python":
        raise TaskManifestError(f"{source}.language must be 'python'")
    category = _choice(raw, "category", CATEGORIES, source)
    source_type = _choice(raw, "source_type", SOURCE_TYPES, source)
    license_name = _string(raw, "license", source)
    initial_commit = _string(raw, "initial_commit", source)
    if not COMMIT_PATTERN.fullmatch(initial_commit):
        raise TaskManifestError(f"{source}.initial_commit must be a full Git commit hash")
    gold_patch_sha256 = _string(raw, "gold_patch_sha256", source)
    if not SHA256_PATTERN.fullmatch(gold_patch_sha256):
        raise TaskManifestError(
            f"{source}.gold_patch_sha256 must be a SHA-256 digest"
        )

    allowed_paths = _path_patterns(raw, "allowed_paths", source)
    prohibited_paths = _path_patterns(raw, "prohibited_paths", source)
    public_test = _test_command(_table(raw, "public_test", source), source)
    hidden_evaluation = _hidden_evaluation(
        _table(raw, "hidden_evaluation", source), source
    )
    return TaskManifest(
        schema_version=schema_version,
        id=task_id,
        language=language,
        category=category,
        source_type=source_type,
        license=license_name,
        initial_commit=initial_commit.lower(),
        gold_patch_sha256=gold_patch_sha256.lower(),
        allowed_paths=allowed_paths,
        prohibited_paths=prohibited_paths,
        public_test=public_test,
        hidden_evaluation=hidden_evaluation,
    )


def _test_command(raw: Mapping[str, Any], source: str) -> TestCommand:
    field = f"{source}.public_test"
    _reject_unknown(raw, {"command", "timeout_seconds"}, field)
    command = _string_list(raw, "command", field)
    if Path(command[0]).name not in {"python", "python3"}:
        raise TaskManifestError(
            f"{field}.command executable must be an allowlisted Python interpreter"
        )
    if command[1:3] != ("-m", "unittest"):
        raise TaskManifestError(
            f"{field}.command must invoke the allowlisted unittest module"
        )
    return TestCommand(
        command=command,
        timeout_seconds=_positive_integer(raw, "timeout_seconds", field),
    )


def _hidden_evaluation(raw: Mapping[str, Any], source: str) -> HiddenEvaluation:
    field = f"{source}.hidden_evaluation"
    _reject_unknown(raw, {"evaluator_id", "timeout_seconds"}, field)
    return HiddenEvaluation(
        evaluator_id=_identifier(raw, "evaluator_id", field),
        timeout_seconds=_positive_integer(raw, "timeout_seconds", field),
    )


def _path_patterns(
    raw: Mapping[str, Any], key: str, source: str
) -> tuple[str, ...]:
    patterns = _string_list(raw, key, source)
    for pattern in patterns:
        path = PurePosixPath(pattern)
        if path.is_absolute() or ".." in path.parts or "\\" in pattern:
            raise TaskManifestError(
                f"{source}.{key} entries must be relative worktree patterns"
            )
    return patterns


def _choice(
    raw: Mapping[str, Any], key: str, choices: frozenset[str], source: str
) -> str:
    value = _string(raw, key, source)
    if value not in choices:
        raise TaskManifestError(
            f"{source}.{key} must be one of {sorted(choices)}, got {value!r}"
        )
    return value


def _identifier(raw: Mapping[str, Any], key: str, source: str) -> str:
    value = _string(raw, key, source)
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise TaskManifestError(f"{source}.{key} must be a stable identifier")
    return value


def _table(raw: Mapping[str, Any], key: str, source: str) -> Mapping[str, Any]:
    value = raw.get(key)
    if not isinstance(value, Mapping):
        raise TaskManifestError(f"{source}.{key} must be a table")
    return value


def _string(raw: Mapping[str, Any], key: str, source: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise TaskManifestError(f"{source}.{key} must be a non-empty string")
    return value


def _integer(raw: Mapping[str, Any], key: str, source: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskManifestError(f"{source}.{key} must be an integer")
    return value


def _positive_integer(raw: Mapping[str, Any], key: str, source: str) -> int:
    value = _integer(raw, key, source)
    if value <= 0:
        raise TaskManifestError(f"{source}.{key} must be positive")
    return value


def _string_list(
    raw: Mapping[str, Any], key: str, source: str
) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise TaskManifestError(f"{source}.{key} must be a non-empty array")
    if any(not isinstance(item, str) or not item for item in value):
        raise TaskManifestError(f"{source}.{key} entries must be non-empty strings")
    if len(set(value)) != len(value):
        raise TaskManifestError(f"{source}.{key} entries must be unique")
    return tuple(value)


def _reject_unknown(
    raw: Mapping[str, Any], allowed: set[str], source: str
) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise TaskManifestError(f"{source}: unknown fields: {', '.join(unknown)}")
