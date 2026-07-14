"""Agent-visible task snapshots, candidate patches, tests, and raw events."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from .tasks import TaskManifest


MAX_AGENT_FILE_BYTES = 256 * 1024
MAX_AGENT_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_TEST_OUTPUT_CHARACTERS = 64 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
EDIT_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["edits"],
    "additionalProperties": False,
}


class CandidatePatchError(ValueError):
    """Raised when model output is not a safe, applicable source patch."""


@dataclass(frozen=True)
class PublicTestResult:
    passed: bool
    returncode: int
    output: str


def build_agent_prompt(worktree: str | Path, task: TaskManifest) -> str:
    """Render the complete public bundle without resolving evaluation assets."""

    root = Path(worktree).resolve()
    sections = [
        "Repair the task below. Return only JSON matching the supplied edit schema.",
        "Each edit must contain an existing allowed path and the complete replacement file content.",
        f"Allowed edit patterns: {', '.join(task.allowed_paths)}",
        "",
    ]
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise CandidatePatchError(f"cannot read public file {relative}: {error}") from error
        if len(payload) > MAX_AGENT_FILE_BYTES:
            raise CandidatePatchError(f"public file exceeds snapshot limit: {relative}")
        total_bytes += len(payload)
        if total_bytes > MAX_AGENT_SNAPSHOT_BYTES:
            raise CandidatePatchError("public task snapshot exceeds safety limit")
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CandidatePatchError(f"public file is not UTF-8: {relative}") from error
        sections.extend((f"--- {relative} ---", content, ""))
    return "\n".join(sections)


def apply_candidate_patch(
    worktree: str | Path, task: TaskManifest, patch_text: str
) -> None:
    """Validate patch paths and apply a model-produced unified Git patch."""

    root = Path(worktree).resolve()
    if not patch_text.strip():
        raise CandidatePatchError("candidate patch is empty")
    encoded = patch_text.encode("utf-8")
    if len(encoded) > MAX_AGENT_SNAPSHOT_BYTES:
        raise CandidatePatchError("candidate patch exceeds safety limit")
    paths = _patch_paths(patch_text)
    if not paths:
        raise CandidatePatchError("candidate output does not contain a Git patch")
    for path in paths:
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in task.allowed_paths):
            raise CandidatePatchError(f"candidate path is not allowed: {path}")
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in task.prohibited_paths):
            raise CandidatePatchError(f"candidate path is prohibited: {path}")
    try:
        completed = subprocess.run(
            ["git", "apply", "--whitespace=error-all", "-"],
            cwd=root,
            input=patch_text,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=_sanitized_environment(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CandidatePatchError(f"cannot apply candidate patch: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise CandidatePatchError(f"candidate patch does not apply: {detail}")


def apply_candidate_edits(
    worktree: str | Path, task: TaskManifest, model_text: str
) -> None:
    """Apply schema-shaped full-file replacements inside the edit allowlist."""

    root = Path(worktree).resolve()
    try:
        raw = json.loads(model_text)
    except json.JSONDecodeError as error:
        raise CandidatePatchError(f"candidate edits are not valid JSON: {error}") from error
    if not isinstance(raw, dict) or set(raw) != {"edits"}:
        raise CandidatePatchError("candidate edits must contain only an edits array")
    edits = raw["edits"]
    if not isinstance(edits, list) or not edits:
        raise CandidatePatchError("candidate edits must be a non-empty array")
    validated: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "content"}:
            raise CandidatePatchError("each candidate edit needs path and content strings")
        path = edit["path"]
        content = edit["content"]
        if not isinstance(path, str) or not isinstance(content, str):
            raise CandidatePatchError("each candidate edit needs path and content strings")
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts or not path:
            raise CandidatePatchError("candidate edit contains an unsafe path")
        if path in seen:
            raise CandidatePatchError(f"candidate edit repeats path: {path}")
        seen.add(path)
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in task.allowed_paths):
            raise CandidatePatchError(f"candidate path is not allowed: {path}")
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in task.prohibited_paths):
            raise CandidatePatchError(f"candidate path is prohibited: {path}")
        destination = root.joinpath(*candidate.parts)
        if not destination.is_file() or destination.is_symlink():
            raise CandidatePatchError(f"candidate may replace only existing regular files: {path}")
        if len(content.encode("utf-8")) > MAX_AGENT_FILE_BYTES:
            raise CandidatePatchError(f"candidate content exceeds safety limit: {path}")
        validated.append((destination, content))
    for destination, content in validated:
        destination.write_text(content, encoding="utf-8")


def run_public_tests(worktree: str | Path, task: TaskManifest) -> PublicTestResult:
    """Run only the public command and sanitize its worktree location."""

    root = Path(worktree).resolve()
    try:
        completed = subprocess.run(
            task.public_test.command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=task.public_test.timeout_seconds,
            check=False,
            env=_sanitized_environment(),
        )
        output = f"{completed.stdout}{completed.stderr}".replace(str(root), "<worktree>")
        output = output[-MAX_TEST_OUTPUT_CHARACTERS:]
        return PublicTestResult(completed.returncode == 0, completed.returncode, output)
    except subprocess.TimeoutExpired:
        return PublicTestResult(False, 124, "public tests exceeded their timeout")
    except OSError as error:
        return PublicTestResult(False, 127, f"public tests could not start: {error}")


def append_event(path: str | Path, event: Mapping[str, object]) -> None:
    """Append one deterministic JSON event without rewriting prior records."""

    target = Path(path)
    if target.is_symlink():
        raise ValueError("event log must not be a symlink")
    try:
        line = json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"event is not valid JSON: {error}") from error
    payload = (line + "\n").encode("utf-8")
    if len(payload) > MAX_EVENT_BYTES:
        raise ValueError("event exceeds safety limit")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _patch_paths(patch_text: str) -> tuple[str, ...]:
    paths: list[str] = []
    old_headers: list[str] = []
    new_headers: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("--- "):
            old_headers.append(line[4:].split()[0])
        elif line.startswith("+++ "):
            new_headers.append(line[4:].split()[0])
        if not line.startswith("diff --git a/"):
            continue
        fields = line.split()
        if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/"):
            raise CandidatePatchError("candidate patch has a malformed diff header")
        left = fields[2][2:]
        right = fields[3][2:]
        if left != right:
            raise CandidatePatchError("candidate patch cannot rename files")
        candidate = PurePosixPath(left)
        if candidate.is_absolute() or ".." in candidate.parts or not left:
            raise CandidatePatchError("candidate patch contains an unsafe path")
        paths.append(left)
    expected_old = [f"a/{path}" for path in paths]
    expected_new = [f"b/{path}" for path in paths]
    if old_headers != expected_old or new_headers != expected_new:
        raise CandidatePatchError("candidate patch header paths do not match diff paths")
    return tuple(paths)


def _sanitized_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment.update({"LC_ALL": "C", "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"})
    return environment
