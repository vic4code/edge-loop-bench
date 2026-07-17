"""Private, bounded evaluator primitives for the pinned InterCode Bash study.

The adapted-compatible score is grounded in ``BashEnv.get_reward`` at pinned
InterCode revision ``c3e46d827cfc9d4c704ec078f7abf9f41e3191d8``:

Source: pinned ``intercode/envs/bash/bash_env.py`` in the upstream
``princeton-nlp/InterCode`` repository.  The full immutable URL is recorded as
``INTERCODE_BASH_ENV_SOURCE_URL`` below.

It preserves the upstream ``0.01 + 0.33 filesystem diff + 0.33 common-change
hash + 0.33 TF-IDF output`` arithmetic, Python rounding, status filtering, and
change-record weighting.  The unsafe upstream shell commands are represented
by separately captured, trusted evidence: parsed ``git status``-compatible
``(path, status)`` records and SHA-256 identities of the exact md5 command
outputs.  Qualification rejects paths/statuses that the pinned whitespace
parser cannot represent.  The stronger normalized filesystem snapshot is kept
separate and is used only by the strict endpoint.

Strict equality means exact equality under the frozen normalization below:
relative path, kind, permission/special mode bits, uid, gid, SHA-256 content,
symlink target, hard-link topology, and normalized output.  Volatile mtime and
ctime are intentionally excluded.  Qualification must reject tasks whose
correctness depends on metadata outside this declared surface.

No function in this module executes a command, opens a path, follows a symlink,
or returns gold-derived diagnostics.  Symlink targets are lexical paths in the
container namespace only; this core never resolves them against the host.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from edgeloopbench.interactive_environment import (
    AttemptEvaluation,
    AttemptEvaluationKind,
)


MAX_STATE_ENTRIES = 4_096
MAX_PATH_BYTES = 1_024
MAX_SYMLINK_TARGET_BYTES = 4_096
MAX_NORMALIZED_OUTPUT_BYTES = 1_048_576
MAX_SNAPSHOT_METADATA_BYTES = 4_194_304
MAX_OFFICIAL_STATUS_BYTES = 4_194_304
MAX_UID_GID = (1 << 32) - 2

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_TFIDF_TOKEN_PATTERN = re.compile(r"(?u)\b\w\w+\b")
_STATUS_TOKEN_PATTERN = re.compile(r"\S+")
_KINDS = frozenset(("file", "directory", "symlink", "absent"))
_HASHED_OFFICIAL_STATUSES = frozenset(("A", "??", "C"))

StateKind = Literal["file", "directory", "symlink", "absent"]

INTERCODE_BASH_ENV_SOURCE_URL = (
    "https://github.com/princeton-nlp/InterCode/blob/"
    "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8/"
    "intercode/envs/bash/bash_env.py"
)


class EvaluatorInputError(ValueError):
    """Raised when private evaluator input is unsafe or non-canonical."""


class CandidateObservationUnsupported(EvaluatorInputError):
    """A model-created status surface cannot be represented safely.

    Adapters must map only this typed candidate-side condition to the frozen
    zero-score failure.  Gold-side occurrences remain qualification failures.
    """


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise EvaluatorInputError(f"{field} must be a lowercase SHA-256 reference")
    return value


def _utf8_size(value: str, field: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        # UnicodeEncodeError retains the complete private input in ``args``.
        # Suppress chaining so a traceback cannot disclose gold-derived text.
        raise EvaluatorInputError(f"{field} must be valid UTF-8 text") from None


def _has_unsafe_character(value: str, *, output: bool) -> bool:
    for character in value:
        if output and character in ("\n", "\t"):
            continue
        category = unicodedata.category(character)
        if category.startswith("C") or category in ("Zl", "Zp"):
            return True
    return False


def _require_state_path(value: object, field: str = "state path") -> str:
    if not isinstance(value, str):
        raise EvaluatorInputError(f"{field} must be text")
    if not value or _utf8_size(value, field) > MAX_PATH_BYTES:
        raise EvaluatorInputError(f"{field} exceeds its safety limit")
    if (
        value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or "\\" in value
        or _has_unsafe_character(value, output=False)
    ):
        raise EvaluatorInputError(f"{field} must be a canonical relative POSIX path")
    components = value.split("/")
    if any(component in ("", ".", "..") for component in components):
        raise EvaluatorInputError(f"{field} must not contain traversal components")
    return value


def _require_symlink_target(value: object) -> str:
    field = "symlink target"
    if not isinstance(value, str):
        raise EvaluatorInputError(f"{field} must be text")
    if not value or _utf8_size(value, field) > MAX_SYMLINK_TARGET_BYTES:
        raise EvaluatorInputError(f"{field} exceeds its safety limit")
    if "\\" in value or _has_unsafe_character(value, output=False):
        raise EvaluatorInputError(f"{field} must be a canonical POSIX path")
    if value == "/":
        return value
    if value.endswith("/") or "//" in value:
        raise EvaluatorInputError(f"{field} must be a canonical POSIX path")
    components = value[1:].split("/") if value.startswith("/") else value.split("/")
    if any(component in ("", ".", "..") for component in components):
        raise EvaluatorInputError(f"{field} must not contain traversal components")
    return value


def _require_official_token(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise EvaluatorInputError(f"{field} must be text")
    if not value or _utf8_size(value, field) > MAX_PATH_BYTES:
        raise EvaluatorInputError(f"{field} exceeds its safety limit")
    if any(character.isspace() for character in value) or _has_unsafe_character(
        value,
        output=False,
    ):
        raise EvaluatorInputError(f"{field} is not a safe parser token")
    return value


def _require_official_change_path(value: object) -> str:
    # This token is evaluator data, never a path opened by this module.  In
    # particular, preserving ``->`` and quoted/traversal-looking tokens is
    # required to reproduce the pinned upstream whitespace parser's diff set.
    return _require_official_token(value, "official change path")


def _parse_upstream_status_tokens(
    status_text: object,
    *,
    candidate_side: bool,
) -> tuple[tuple[str, str], ...]:
    """Safely reproduce upstream ``set(parse_status(status))`` semantics.

    The return order is ``(path, status)`` exactly as in pinned InterCode.
    Hash collection is deliberately separate and may open only qualified,
    trusted paths in an isolated evaluator container.
    """

    error_type: type[EvaluatorInputError] = (
        CandidateObservationUnsupported if candidate_side else EvaluatorInputError
    )
    side = "candidate" if candidate_side else "gold"
    if not isinstance(status_text, str):
        raise error_type(f"{side} status must be text")
    try:
        encoded_size = _utf8_size(status_text, f"{side} status")
    except EvaluatorInputError:
        raise error_type(f"{side} status is not safe UTF-8") from None
    if encoded_size > MAX_OFFICIAL_STATUS_BYTES:
        raise error_type(f"{side} status exceeds its safety limit")
    for character in status_text:
        if character in {" ", "\t", "\r", "\n"}:
            continue
        if _has_unsafe_character(character, output=False):
            raise error_type(f"{side} status contains unsafe text")
    pairs: dict[tuple[str, str], None] = {}
    pending_status: str | None = None
    try:
        for match in _STATUS_TOKEN_PATTERN.finditer(status_text):
            token = match.group(0)
            if pending_status is None:
                pending_status = _require_official_token(
                    token,
                    "official change status",
                )
                continue
            path = _require_official_change_path(token)
            pairs[(path, pending_status)] = None
            pending_status = None
            if len(pairs) > MAX_STATE_ENTRIES:
                raise error_type(f"{side} status contains too many unique changes")
    except EvaluatorInputError:
        raise error_type(f"{side} status contains an unsafe parser token") from None
    if pending_status is not None:
        raise error_type(f"{side} status has an unpaired parser token")
    # Pinned get_reward converts parsed changes to a set before every score
    # comparison.  Dict insertion order preserves first appearance without an
    # unbounded intermediate token or pair list.
    return tuple(pairs)


def parse_candidate_status_tokens(
    status_text: object,
) -> tuple[tuple[str, str], ...]:
    """Parse model-created status with a denominator-preserving error type."""

    return _parse_upstream_status_tokens(status_text, candidate_side=True)


def parse_gold_status_tokens(
    status_text: object,
) -> tuple[tuple[str, str], ...]:
    """Parse gold status with a qualification-invalid error type."""

    return _parse_upstream_status_tokens(status_text, candidate_side=False)


def _require_mode(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluatorInputError("state mode must be an integer")
    if not 0 <= value <= 0o7777:
        raise EvaluatorInputError("state mode is outside the normalized range")
    return value


def _require_identity(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluatorInputError(f"state {field} must be an integer")
    if not 0 <= value <= MAX_UID_GID:
        raise EvaluatorInputError(f"state {field} is outside the normalized range")
    return value


def hardlink_group_sha256(paths: tuple[str, ...]) -> str:
    """Return the canonical, inode-free identity for a hard-link member set."""

    if not isinstance(paths, tuple):
        raise EvaluatorInputError("hardlink paths must be a tuple")
    if len(paths) < 2:
        raise EvaluatorInputError("hardlink groups require at least two paths")
    if len(paths) > MAX_STATE_ENTRIES:
        raise EvaluatorInputError("hardlink group contains too many paths")
    checked = tuple(_require_state_path(path, "hardlink member path") for path in paths)
    if len(set(checked)) != len(checked):
        raise EvaluatorInputError("hardlink groups must not contain duplicate paths")
    if sum(_utf8_size(path, "hardlink member path") for path in checked) > (
        MAX_SNAPSHOT_METADATA_BYTES
    ):
        raise EvaluatorInputError("hardlink group metadata exceeds its safety limit")
    canonical = tuple(sorted(checked))
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class StateEntry:
    """One changed path in a canonical snapshot relative to a frozen baseline.

    ``absent`` is a tombstone for a path present in the baseline but deleted in
    this state.  ``hardlink_group_sha256`` is derived only from the complete
    sorted member-path set.  It is never an inode number.
    """

    path: str
    kind: StateKind
    mode: int | None
    uid: int | None
    gid: int | None
    content_sha256: str | None
    symlink_target: str | None
    hardlink_group_sha256: str | None

    def __post_init__(self) -> None:
        _require_state_path(self.path)
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise EvaluatorInputError("state kind is unsupported")

        if self.kind == "absent":
            if any(
                value is not None
                for value in (
                    self.mode,
                    self.uid,
                    self.gid,
                    self.content_sha256,
                    self.symlink_target,
                    self.hardlink_group_sha256,
                )
            ):
                raise EvaluatorInputError(
                    "absent entries may only contain a path and kind"
                )
            return

        _require_mode(self.mode)
        _require_identity(self.uid, "uid")
        _require_identity(self.gid, "gid")

        if self.kind == "file":
            _require_sha256(self.content_sha256, "file content digest")
            if self.symlink_target is not None:
                raise EvaluatorInputError(
                    "file entries cannot contain a symlink target"
                )
            if self.hardlink_group_sha256 is not None:
                _require_sha256(
                    self.hardlink_group_sha256,
                    "hardlink group digest",
                )
            return

        if self.kind == "directory":
            if any(
                value is not None
                for value in (
                    self.content_sha256,
                    self.symlink_target,
                    self.hardlink_group_sha256,
                )
            ):
                raise EvaluatorInputError(
                    "directory entries cannot contain file, symlink, or hardlink data"
                )
            return

        if self.content_sha256 is not None or self.hardlink_group_sha256 is not None:
            raise EvaluatorInputError(
                "symlink entries cannot contain file or hardlink data"
            )
        _require_symlink_target(self.symlink_target)

    def __repr__(self) -> str:
        return "<StateEntry redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalStateSnapshot:
    """Bounded, path-sorted changed state on the declared observable surface."""

    entries: tuple[StateEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise EvaluatorInputError("state entries must be a tuple")
        if len(self.entries) > MAX_STATE_ENTRIES:
            raise EvaluatorInputError("too many state entries")
        if any(not isinstance(entry, StateEntry) for entry in self.entries):
            raise EvaluatorInputError(
                "state entries must contain only StateEntry values"
            )

        paths = tuple(entry.path for entry in self.entries)
        if len(set(paths)) != len(paths):
            raise EvaluatorInputError("duplicate state path")

        metadata_bytes = sum(_utf8_size(path, "state path") for path in paths)
        metadata_bytes += sum(
            _utf8_size(entry.symlink_target, "symlink target")
            for entry in self.entries
            if entry.symlink_target is not None
        )
        if metadata_bytes > MAX_SNAPSHOT_METADATA_BYTES:
            raise EvaluatorInputError(
                "state snapshot metadata exceeds its safety limit"
            )

        canonical_entries = tuple(sorted(self.entries, key=lambda entry: entry.path))
        object.__setattr__(self, "entries", canonical_entries)
        self._validate_hardlink_groups()

    def _validate_hardlink_groups(self) -> None:
        grouped: dict[str, list[StateEntry]] = defaultdict(list)
        for entry in self.entries:
            if entry.hardlink_group_sha256 is not None:
                grouped[entry.hardlink_group_sha256].append(entry)

        for group_digest, members in grouped.items():
            if len(members) < 2:
                raise EvaluatorInputError(
                    "hardlink groups require at least two members"
                )
            expected = hardlink_group_sha256(tuple(member.path for member in members))
            if group_digest != expected:
                raise EvaluatorInputError("hardlink group is not path-derived")
            stable_signatures = {
                (
                    member.kind,
                    member.mode,
                    member.uid,
                    member.gid,
                    member.content_sha256,
                    member.symlink_target,
                )
                for member in members
            }
            if len(stable_signatures) != 1:
                raise EvaluatorInputError(
                    "hardlink members must share stable metadata and content"
                )

    def __repr__(self) -> str:
        return f"<CanonicalStateSnapshot entries={len(self.entries)}>"


@dataclass(frozen=True, slots=True, repr=False)
class OfficialChange:
    """One upstream-compatible parsed status unit and private hash evidence.

    ``hash_output_sha256`` identifies the exact upstream md5/md5deep command
    output for a common ``A``/``??``/``C`` key.  Candidate-only keys may omit
    it because pinned ``get_reward`` never hashes them.  Every other status is
    retained in the diff set but excluded from the hash filter.
    """

    path: str
    status: str
    hash_output_sha256: str | None

    def __post_init__(self) -> None:
        _require_official_token(self.status, "official change status")
        _require_official_change_path(self.path)
        if self.hash_output_sha256 is not None:
            _require_sha256(
                self.hash_output_sha256,
                "official hash-output digest",
            )
        if (
            self.status not in _HASHED_OFFICIAL_STATUSES
            and self.hash_output_sha256 is not None
        ):
            raise EvaluatorInputError(
                "non-hashed official changes cannot contain hash evidence"
            )

    def __repr__(self) -> str:
        return "<OfficialChange redacted>"


@dataclass(frozen=True, slots=True, repr=False)
class OfficialChangeSnapshot:
    """Canonical status units used only by the pinned upstream reward formula."""

    changes: tuple[OfficialChange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.changes, tuple):
            raise EvaluatorInputError("official changes must be a tuple")
        if len(self.changes) > MAX_STATE_ENTRIES:
            raise EvaluatorInputError("too many official change records")
        if any(not isinstance(change, OfficialChange) for change in self.changes):
            raise EvaluatorInputError(
                "official changes must contain only OfficialChange values"
            )
        by_key: dict[tuple[str, str], OfficialChange] = {}
        for change in self.changes:
            key = (change.path, change.status)
            existing = by_key.get(key)
            if existing is not None and existing != change:
                raise EvaluatorInputError(
                    "duplicate official change key has conflicting evidence"
                )
            by_key[key] = change
        canonical = tuple(
            sorted(by_key.values(), key=lambda change: (change.path, change.status))
        )
        object.__setattr__(self, "changes", canonical)

    def __repr__(self) -> str:
        return f"<OfficialChangeSnapshot changes={len(self.changes)}>"


class PrivateAttempt:
    """Evaluator-only normalized state and output; never model-visible."""

    __slots__ = ("__official_changes", "__state", "__normalized_output")

    def __init__(
        self,
        *,
        official_changes: OfficialChangeSnapshot,
        state: CanonicalStateSnapshot,
        normalized_output: str,
    ) -> None:
        if not isinstance(official_changes, OfficialChangeSnapshot):
            raise EvaluatorInputError(
                "attempt official_changes must be an OfficialChangeSnapshot"
            )
        if not isinstance(state, CanonicalStateSnapshot):
            raise EvaluatorInputError("attempt state must be a CanonicalStateSnapshot")
        if not isinstance(normalized_output, str):
            raise EvaluatorInputError("normalized output must be text")
        if (
            _utf8_size(normalized_output, "normalized output")
            > MAX_NORMALIZED_OUTPUT_BYTES
        ):
            raise EvaluatorInputError("normalized output exceeds its safety limit")
        if "\r" in normalized_output or _has_unsafe_character(
            normalized_output,
            output=True,
        ):
            raise EvaluatorInputError("normalized output contains unsafe control text")
        object.__setattr__(self, "_PrivateAttempt__official_changes", official_changes)
        object.__setattr__(self, "_PrivateAttempt__state", state)
        object.__setattr__(self, "_PrivateAttempt__normalized_output", normalized_output)

    @property
    def official_changes(self) -> OfficialChangeSnapshot:
        return self.__official_changes

    @property
    def state(self) -> CanonicalStateSnapshot:
        return self.__state

    @property
    def normalized_output(self) -> str:
        return self.__normalized_output

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("private attempts are immutable")

    def __repr__(self) -> str:
        return "<PrivateAttempt redacted>"

    def __reduce__(self) -> object:
        raise TypeError("private attempts cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("private attempts cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("private attempts cannot be copied")

    def __deepcopy__(self, _memo: object) -> object:
        raise TypeError("private attempts cannot be copied")


def candidate_surface_failure_evaluation() -> AttemptEvaluation:
    """Return the preregistered denominator-preserving candidate fallback."""

    return AttemptEvaluation(
        reward=0.0,
        official_success=False,
        evaluation_kind=AttemptEvaluationKind.CANDIDATE_SURFACE_FAILURE,
    )


def evaluate_candidate_or_failure(
    candidate_builder: Callable[[], PrivateAttempt],
    gold: PrivateAttempt,
) -> AttemptEvaluation:
    """Build one candidate privately and preserve model-side failures.

    Only bounded evaluator-input failures raised while constructing the
    candidate are converted to the frozen score.  Host, Docker, and unexpected
    exceptions propagate as infrastructure failures.  Gold is validated before
    invoking the builder and evaluator failures after construction also
    propagate, so a corrupt reference cannot be hidden as a model failure.
    """

    if not callable(candidate_builder):
        raise EvaluatorInputError("candidate builder must be callable")
    _validate_gold_attempt(gold)
    try:
        candidate = candidate_builder()
    except CandidateObservationUnsupported:
        return candidate_surface_failure_evaluation()
    if not isinstance(candidate, PrivateAttempt):
        raise EvaluatorInputError("candidate builder must return a PrivateAttempt")
    return adapted_compatible_evaluate(candidate, gold)


def _validate_gold_attempt(gold: object) -> PrivateAttempt:
    if not isinstance(gold, PrivateAttempt):
        raise EvaluatorInputError("gold must be a PrivateAttempt")
    if any(
        change.status in _HASHED_OFFICIAL_STATUSES
        and change.hash_output_sha256 is None
        for change in gold.official_changes.changes
    ):
        raise EvaluatorInputError("gold hash evidence is incomplete")
    return gold


def _term_counts(document: str) -> Counter[str]:
    lowered = document.lower()
    return Counter(match.group(0) for match in _TFIDF_TOKEN_PATTERN.finditer(lowered))


def _tfidf_similarity(candidate_output: str, gold_output: str) -> float:
    """Reproduce sklearn ``TfidfVectorizer`` defaults for exactly two texts."""

    documents = (_term_counts(candidate_output), _term_counts(gold_output))
    vocabulary = tuple(sorted(set(documents[0]) | set(documents[1])))
    if not vocabulary:
        # Pinned upstream catches TfidfVectorizer's empty-vocabulary error and
        # falls back to exact text equality.
        return 1.0 if candidate_output == gold_output else 0.0

    vectors: list[dict[str, float]] = []
    for counts in documents:
        vector: dict[str, float] = {}
        for term in vocabulary:
            document_frequency = int(term in documents[0]) + int(term in documents[1])
            inverse_document_frequency = math.log(
                (1.0 + len(documents)) / (1.0 + document_frequency)
            ) + 1.0
            vector[term] = counts.get(term, 0) * inverse_document_frequency
        norm = math.sqrt(sum(weight * weight for weight in vector.values()))
        if norm:
            vector = {term: weight / norm for term, weight in vector.items()}
        vectors.append(vector)

    similarity = sum(
        vectors[0][term] * vectors[1][term]
        for term in vocabulary
    )
    # Floating-point accumulation can produce 1+epsilon for identical texts.
    return min(1.0, max(0.0, similarity))


def _require_attempt_pair(
    candidate: object,
    gold: object,
) -> tuple[PrivateAttempt, PrivateAttempt]:
    if not isinstance(candidate, PrivateAttempt) or not isinstance(
        gold,
        PrivateAttempt,
    ):
        raise EvaluatorInputError("candidate and gold must be PrivateAttempt values")
    return candidate, gold


def adapted_compatible_evaluate(
    candidate: PrivateAttempt,
    gold: PrivateAttempt,
) -> AttemptEvaluation:
    """Return only the adapted-compatible scalar and its exact-1.0 stop bit."""

    candidate, gold = _require_attempt_pair(candidate, gold)
    _validate_gold_attempt(gold)
    candidate_by_key = {
        (change.path, change.status): change
        for change in candidate.official_changes.changes
    }
    gold_by_key = {
        (change.path, change.status): change
        for change in gold.official_changes.changes
    }

    candidate_keys = set(candidate_by_key)
    gold_keys = set(gold_by_key)
    difference_count = len(gold_keys - candidate_keys) + len(
        candidate_keys - gold_keys
    )
    filesystem_diff_score = round(
        0.33 * (1.0 - math.erf(difference_count)),
        2,
    )

    hashed_common_keys = tuple(
        key
        for key in candidate_keys & gold_keys
        if key[1] in _HASHED_OFFICIAL_STATUSES
    )
    common_hash_score = 0.33
    if hashed_common_keys:
        correct = sum(
            candidate_by_key[key].hash_output_sha256 is not None
            and candidate_by_key[key].hash_output_sha256
            == gold_by_key[key].hash_output_sha256
            for key in hashed_common_keys
        )
        common_hash_score = round(0.33 * (correct / len(hashed_common_keys)), 2)

    output_similarity = _tfidf_similarity(
        candidate.normalized_output,
        gold.normalized_output,
    )
    output_score = round(0.33 * output_similarity, 2)
    reward = 0.01 + filesystem_diff_score + common_hash_score + output_score
    if not math.isfinite(reward) or not 0.0 <= reward <= 1.0:
        raise EvaluatorInputError(
            "adapted-compatible reward must be finite and bounded"
        )

    return AttemptEvaluation(
        reward=reward,
        official_success=reward == 1.0,
    )


def strict_exact_success(candidate: PrivateAttempt, gold: PrivateAttempt) -> bool:
    """Return exact equality on the frozen normalized state/output surface."""

    candidate, gold = _require_attempt_pair(candidate, gold)
    return bool(
        candidate.state == gold.state
        and candidate.normalized_output == gold.normalized_output
    )
