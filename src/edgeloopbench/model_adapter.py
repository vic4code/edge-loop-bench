"""Pinned prompt rendering, exact token preflight, and raw Ollama transport."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import stat
import subprocess
from copy import deepcopy
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .ollama_loopback_http import (
    OllamaLoopbackHttpError,
    parse_strict_json_object,
)


_SHA256_PREFIX = "sha256:"
_MAX_TOKENIZER_STDOUT_BYTES = 1 * 1024 * 1024
_MAX_TOKENIZER_STDERR_BYTES = 64 * 1024
_MAX_OLLAMA_RESPONSE_BYTES = 1 * 1024 * 1024
_MAX_RENDERED_PROMPT_BYTES = 64 * 1024
_MAX_TOKEN_CACHE_ENTRIES = 128
_MAX_TOKEN_CACHE_PROMPT_BYTES = 8 * 1024 * 1024

# Ollama v0.31.1 tag 710292ff4f191d8da9f6a4230804fbc693338d4a.
# Qwen renderer source:
# https://github.com/ollama/ollama/blob/710292ff4f191d8da9f6a4230804fbc693338d4a/model/renderers/qwen35.go
# The official no-think test requires the explicit empty <think> prefill:
# https://github.com/ollama/ollama/blob/710292ff4f191d8da9f6a4230804fbc693338d4a/model/renderers/qwen35_test.go#L77-L90
_QWEN35_SOURCE_SHA256 = (
    "sha256:6ca6abea759548962ea23189691c5def15cb86704c114f17182784fd159b4872"
)

# The Phi template is the immutable template layer in the official Ollama
# phi4-mini manifest. Its complete blob is addressed by this digest:
# https://ollama.com/library/phi4-mini:3.8b/blobs/813f53fdc6e5
_PHI4_MINI_TEMPLATE_SHA256 = (
    "sha256:813f53fdc6e58d35bb1c3853c93266380e9ca918a993e8eab193e8ede5d3a603"
)

# Go strings.TrimSpace uses ASCII whitespace plus the Unicode White_Space
# property. Keeping the exact set here avoids Python's additional C0 separators.
_GO_TRIM_SPACE = (
    "\t\n\v\f\r "
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_QWEN35_TURN_TEMPLATE = "<|im_start|>{role}\n{content}<|im_end|>\n"
_QWEN35_REASONING_CLOSE_TAG = "</think>"
_QWEN35_GENERATION_SUFFIX = "<|im_start|>assistant\n<think>\n\n</think>\n\n"
_PHI4_MINI_TURN_TEMPLATE = "<|{role}|>{content}<|end|>"
_PHI4_MINI_GENERATION_SUFFIX = "<|assistant|>"


def _digest(payload: bytes) -> str:
    return _SHA256_PREFIX + hashlib.sha256(payload).hexdigest()


def _validate_digest(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(_SHA256_PREFIX)
        or len(value) != len(_SHA256_PREFIX) + 64
    ):
        raise ValueError(f"{field} must be a sha256 digest")
    suffix = value[len(_SHA256_PREFIX) :]
    if any(character not in "0123456789abcdef" for character in suffix):
        raise ValueError(f"{field} must be a lowercase sha256 digest")


def _utf8(value: str, field: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} must contain valid Unicode") from error


@dataclass(frozen=True)
class TranscriptMessage:
    """One typed controller/model turn accepted by the restricted renderers."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("transcript role must be user or assistant")
        _utf8(self.content, "transcript content")
        if self.role == "user" and not self.content:
            raise ValueError("user transcript content must not be empty")


@dataclass(frozen=True)
class RestrictedRawRenderingProfile:
    """A hashed, deliberately small subset of one pinned model renderer."""

    profile_id: str
    model: str
    model_manifest_sha256: str
    model_artifact_sha256: str
    ollama_commit: str
    ollama_source_sha256: str
    algorithm_revision: str

    def __post_init__(self) -> None:
        if not self.profile_id or not self.model or not self.algorithm_revision:
            raise ValueError("raw rendering profile identifiers must not be empty")
        for field in (
            "model_manifest_sha256",
            "model_artifact_sha256",
            "ollama_source_sha256",
        ):
            _validate_digest(getattr(self, field), field)
        if len(self.ollama_commit) != 40 or any(
            character not in "0123456789abcdef" for character in self.ollama_commit
        ):
            raise ValueError("ollama_commit must be a full lowercase Git commit")

    @property
    def canonical_definition(self) -> dict[str, object]:
        """Return the complete data contract consumed by the renderer."""

        return {
            "algorithm_revision": self.algorithm_revision,
            "model": self.model,
            "model_artifact_sha256": self.model_artifact_sha256,
            "model_manifest_sha256": self.model_manifest_sha256,
            "ollama_commit": self.ollama_commit,
            "ollama_source_sha256": self.ollama_source_sha256,
            "profile_id": self.profile_id,
            "render_contract": _render_contract(self.profile_id),
        }

    @property
    def sha256(self) -> str:
        return _digest(
            json.dumps(
                self.canonical_definition,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def render(self, messages: Sequence[TranscriptMessage]) -> str:
        _validate_transcript(messages)
        if self.profile_id == "qwen35-ollama-v0.31.1-nothink-v1":
            return _render_qwen35(messages)
        if self.profile_id == "phi4-mini-template-813f53fd-nothink-v1":
            return _render_phi4_mini(messages)
        raise ValueError(f"unsupported raw rendering profile: {self.profile_id}")


QWEN35_RAW_PROFILE = RestrictedRawRenderingProfile(
    profile_id="qwen35-ollama-v0.31.1-nothink-v1",
    model="qwen3.5:4b",
    model_manifest_sha256=(
        "sha256:2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"
    ),
    model_artifact_sha256=(
        "sha256:81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490"
    ),
    ollama_commit="710292ff4f191d8da9f6a4230804fbc693338d4a",
    ollama_source_sha256=_QWEN35_SOURCE_SHA256,
    algorithm_revision="qwen35-user-assistant-subset-empty-think-v1",
)

PHI4_MINI_RAW_PROFILE = RestrictedRawRenderingProfile(
    profile_id="phi4-mini-template-813f53fd-nothink-v1",
    model="phi4-mini:3.8b",
    model_manifest_sha256=(
        "sha256:78fad5d182a7c33065e153a5f8ba210754207ba9d91973f57dffa7f487363753"
    ),
    model_artifact_sha256=(
        "sha256:3c168af1dea0a414299c7d9077e100ac763370e5a98b3c53801a958a47f0a5db"
    ),
    ollama_commit="710292ff4f191d8da9f6a4230804fbc693338d4a",
    ollama_source_sha256=_PHI4_MINI_TEMPLATE_SHA256,
    algorithm_revision="phi4-mini-user-assistant-template-subset-v1",
)


def _validate_transcript(messages: Sequence[TranscriptMessage]) -> None:
    if not messages:
        raise ValueError("transcript must contain at least one message")
    for index, message in enumerate(messages):
        if not isinstance(message, TranscriptMessage):
            raise ValueError("transcript entries must be TranscriptMessage values")
        expected = "user" if index % 2 == 0 else "assistant"
        if message.role != expected:
            raise ValueError("transcript must strictly alternate user and assistant")
    if messages[-1].role != "user":
        raise ValueError("generation transcript must end with a user message")


def _render_contract(profile_id: str) -> dict[str, object]:
    common: dict[str, object] = {
        "allowed_roles": ["user", "assistant"],
        "content_encoding": "utf-8",
        "sequence": "user-first-strict-alternation-user-last",
    }
    if profile_id == "qwen35-ollama-v0.31.1-nothink-v1":
        return {
            **common,
            "assistant_after_close_left_trim_characters": "\n",
            "assistant_reasoning_close_occurrence": "first",
            "assistant_reasoning_close_tag": _QWEN35_REASONING_CLOSE_TAG,
            "content_operation_order": [
                "trim_go_space",
                "assistant_remove_through_reasoning_close",
                "assistant_left_trim_newline",
            ],
            "content_trim_characters": _GO_TRIM_SPACE,
            "generation_suffix": _QWEN35_GENERATION_SUFFIX,
            "turn_template": _QWEN35_TURN_TEMPLATE,
        }
    if profile_id == "phi4-mini-template-813f53fd-nothink-v1":
        return {
            **common,
            "content_operation_order": ["identity"],
            "content_transform": "identity",
            "generation_suffix": _PHI4_MINI_GENERATION_SUFFIX,
            "turn_template": _PHI4_MINI_TURN_TEMPLATE,
        }
    raise ValueError(f"unsupported raw rendering profile: {profile_id}")


def _render_qwen35(messages: Sequence[TranscriptMessage]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.content.strip(_GO_TRIM_SPACE)
        if message.role == "assistant":
            closing = content.find(_QWEN35_REASONING_CLOSE_TAG)
            if closing >= 0:
                content = content[
                    closing + len(_QWEN35_REASONING_CLOSE_TAG) :
                ].lstrip("\n")
        parts.append(_QWEN35_TURN_TEMPLATE.format(role=message.role, content=content))
    parts.append(_QWEN35_GENERATION_SUFFIX)
    return "".join(parts)


def _render_phi4_mini(messages: Sequence[TranscriptMessage]) -> str:
    parts = [
        _PHI4_MINI_TURN_TEMPLATE.format(role=message.role, content=message.content)
        for message in messages
    ]
    parts.append(_PHI4_MINI_GENERATION_SUFFIX)
    return "".join(parts)


@dataclass(frozen=True)
class TokenCount:
    count: int
    token_ids_sha256: str
    tokenizer_artifact_sha256: str
    model_artifact_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count <= 0:
            raise ValueError("token count must be a positive integer")
        for field in (
            "token_ids_sha256",
            "tokenizer_artifact_sha256",
            "model_artifact_sha256",
        ):
            _validate_digest(getattr(self, field), field)


class PromptTokenCounter(Protocol):
    def count(self, prompt: str) -> TokenCount: ...


class RenderedPromptByteLimitExceeded(ValueError):
    """A rendered prompt crossed the frozen pre-tokenization safety ceiling."""

    def __init__(
        self,
        *,
        observed_bytes: int,
        limit_bytes: int,
        prompt_sha256: str,
        renderer_profile_sha256: str,
    ) -> None:
        if (
            isinstance(observed_bytes, bool)
            or not isinstance(observed_bytes, int)
            or observed_bytes <= 0
        ):
            raise ValueError("observed prompt bytes must be positive")
        if (
            isinstance(limit_bytes, bool)
            or not isinstance(limit_bytes, int)
            or limit_bytes <= 0
        ):
            raise ValueError("prompt byte limit must be positive")
        if observed_bytes <= limit_bytes:
            raise ValueError("observed prompt bytes must exceed the byte limit")
        _validate_digest(prompt_sha256, "prompt_sha256")
        _validate_digest(renderer_profile_sha256, "renderer_profile_sha256")
        self.observed_bytes = observed_bytes
        self.limit_bytes = limit_bytes
        self.prompt_sha256 = prompt_sha256
        self.renderer_profile_sha256 = renderer_profile_sha256
        super().__init__("rendered prompt exceeded the frozen byte ceiling")


@dataclass(frozen=True)
class PreparedPrompt:
    """Exact rendered bytes and their preflight tokenization evidence."""

    rendered_prompt: str
    prompt_tokens: int
    prompt_sha256: str
    token_ids_sha256: str
    renderer_profile_sha256: str
    tokenizer_artifact_sha256: str
    model_artifact_sha256: str

    def __post_init__(self) -> None:
        encoded = _utf8(self.rendered_prompt, "rendered prompt")
        if not encoded:
            raise ValueError("rendered prompt must not be empty")
        if (
            isinstance(self.prompt_tokens, bool)
            or not isinstance(self.prompt_tokens, int)
            or self.prompt_tokens <= 0
        ):
            raise ValueError("prepared prompt token count must be positive")
        for field in (
            "prompt_sha256",
            "token_ids_sha256",
            "renderer_profile_sha256",
            "tokenizer_artifact_sha256",
            "model_artifact_sha256",
        ):
            _validate_digest(getattr(self, field), field)
        if self.prompt_sha256 != _digest(encoded):
            raise ValueError("prepared prompt digest does not match rendered bytes")


class PromptPreparer(Protocol):
    def __call__(self, messages: tuple[TranscriptMessage, ...]) -> PreparedPrompt: ...


class ExactPromptPreparer:
    """Compose one frozen renderer with a tokenizer bound to the same model."""

    def __init__(
        self,
        renderer: RestrictedRawRenderingProfile,
        token_counter: PromptTokenCounter,
        *,
        max_rendered_prompt_bytes: int = _MAX_RENDERED_PROMPT_BYTES,
    ) -> None:
        if (
            isinstance(max_rendered_prompt_bytes, bool)
            or not isinstance(max_rendered_prompt_bytes, int)
            or not 0 < max_rendered_prompt_bytes <= _MAX_RENDERED_PROMPT_BYTES
        ):
            raise ValueError(
                "rendered prompt byte limit must be within the frozen maximum"
            )
        self.renderer = renderer
        self.token_counter = token_counter
        self.max_rendered_prompt_bytes = max_rendered_prompt_bytes

    def __call__(self, messages: tuple[TranscriptMessage, ...]) -> PreparedPrompt:
        rendered = self.renderer.render(messages)
        rendered_bytes = rendered.encode("utf-8")
        if len(rendered_bytes) > self.max_rendered_prompt_bytes:
            raise RenderedPromptByteLimitExceeded(
                observed_bytes=len(rendered_bytes),
                limit_bytes=self.max_rendered_prompt_bytes,
                prompt_sha256=_digest(rendered_bytes),
                renderer_profile_sha256=self.renderer.sha256,
            )
        counted = self.token_counter.count(rendered)
        if counted.model_artifact_sha256 != self.renderer.model_artifact_sha256:
            raise ValueError("renderer and tokenizer are bound to different model artifacts")
        return PreparedPrompt(
            rendered_prompt=rendered,
            prompt_tokens=counted.count,
            prompt_sha256=_digest(rendered_bytes),
            token_ids_sha256=counted.token_ids_sha256,
            renderer_profile_sha256=self.renderer.sha256,
            tokenizer_artifact_sha256=counted.tokenizer_artifact_sha256,
            model_artifact_sha256=counted.model_artifact_sha256,
        )


RunCommand = Callable[..., subprocess.CompletedProcess[bytes]]
ArtifactIdentity = tuple[int, int, int, int, int]


class LlamaTokenizeCounter:
    """Invoke the pinned patched llama-tokenize helper without inference.

    The upstream tool loads the GGUF with ``vocab_only=true`` and its defaults
    add model special tokens and parse special-token text. ``--no-escape`` is
    essential: it prevents the helper from rewriting backslash sequences that
    Ollama's raw generate request passes through unchanged.

    Source: https://github.com/ggml-org/llama.cpp/blob/8c146a8366304c871efc26057cc90370ccf58dad/tools/tokenize/tokenize.cpp#L340-L383
    """

    def __init__(
        self,
        *,
        helper_path: str | Path,
        helper_sha256: str,
        model_path: str | Path,
        model_sha256: str,
        timeout_seconds: float = 30.0,
        max_prompt_bytes: int = _MAX_RENDERED_PROMPT_BYTES,
        max_cache_entries: int = _MAX_TOKEN_CACHE_ENTRIES,
        max_cache_bytes: int = _MAX_TOKEN_CACHE_PROMPT_BYTES,
        run_command: RunCommand = subprocess.run,
    ) -> None:
        _validate_digest(helper_sha256, "helper_sha256")
        _validate_digest(model_sha256, "model_sha256")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("tokenizer timeout must be positive and finite")
        for name, value, maximum in (
            ("max_prompt_bytes", max_prompt_bytes, _MAX_RENDERED_PROMPT_BYTES),
            ("max_cache_entries", max_cache_entries, _MAX_TOKEN_CACHE_ENTRIES),
            ("max_cache_bytes", max_cache_bytes, _MAX_TOKEN_CACHE_PROMPT_BYTES),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 < value <= maximum
            ):
                raise ValueError(f"{name} must be within the frozen maximum")
        helper_input = Path(helper_path).expanduser()
        model_input = Path(model_path).expanduser()
        self.helper_sha256 = helper_sha256
        self.model_sha256 = model_sha256
        self.timeout_seconds = float(timeout_seconds)
        self.max_prompt_bytes = max_prompt_bytes
        self.max_cache_entries = max_cache_entries
        self.max_cache_bytes = max_cache_bytes
        self._run_command = run_command
        self._cache: OrderedDict[str, tuple[bytes, TokenCount]] = OrderedDict()
        self._cache_prompt_bytes = 0
        self._helper_identity = self._verify_artifact(
            helper_input, helper_sha256, executable=True
        )
        self._model_identity = self._verify_artifact(
            model_input, model_sha256, executable=False
        )
        self.helper_path = helper_input.resolve(strict=True)
        self.model_path = model_input.resolve(strict=True)

    @staticmethod
    def _verify_artifact(
        path: Path, expected_sha256: str, *, executable: bool
    ) -> ArtifactIdentity:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise ValueError("pinned artifact is not readable") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("pinned artifact must be a regular non-symlink file")
        if executable and not metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            raise ValueError("tokenizer helper must be executable")
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(block)
        except OSError as error:
            raise ValueError("pinned artifact could not be hashed") from error
        if _SHA256_PREFIX + hasher.hexdigest() != expected_sha256:
            raise ValueError("pinned artifact sha256 mismatch")
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    @staticmethod
    def _assert_unchanged(path: Path, expected: ArtifactIdentity) -> None:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise RuntimeError("pinned artifact changed after verification") from error
        observed = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        if not stat.S_ISREG(metadata.st_mode) or observed != expected:
            raise RuntimeError("pinned artifact changed after verification")

    def count(self, prompt: str) -> TokenCount:
        prompt_bytes = _utf8(prompt, "tokenizer prompt")
        if len(prompt_bytes) > self.max_prompt_bytes:
            raise ValueError("tokenizer prompt exceeded the frozen byte ceiling")
        self._assert_unchanged(self.helper_path, self._helper_identity)
        self._assert_unchanged(self.model_path, self._model_identity)
        cache_key = _digest(prompt_bytes)
        cached = self._cache.get(cache_key)
        if cached is not None:
            cached_bytes, cached_result = cached
            if cached_bytes != prompt_bytes:
                raise RuntimeError("prompt digest collision")
            self._cache.move_to_end(cache_key)
            return cached_result

        command = [
            str(self.helper_path),
            "--model",
            str(self.model_path),
            "--stdin",
            "--ids",
            "--no-escape",
            "--log-disable",
        ]
        try:
            completed = self._run_command(
                command,
                input=prompt_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_seconds,
                check=False,
                env={"LANG": "C", "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("pinned tokenizer helper failed") from error
        self._assert_unchanged(self.helper_path, self._helper_identity)
        self._assert_unchanged(self.model_path, self._model_identity)
        if completed.returncode != 0:
            raise RuntimeError("pinned tokenizer helper returned nonzero status")
        if not isinstance(completed.stdout, bytes) or not isinstance(completed.stderr, bytes):
            raise RuntimeError("pinned tokenizer helper returned non-byte output")
        if len(completed.stdout) > _MAX_TOKENIZER_STDOUT_BYTES:
            raise RuntimeError("pinned tokenizer helper output exceeded limit")
        if len(completed.stderr) > _MAX_TOKENIZER_STDERR_BYTES or completed.stderr:
            raise RuntimeError("pinned tokenizer helper wrote unexpected diagnostics")
        try:
            raw = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("pinned tokenizer helper returned invalid token JSON") from error
        if (
            not isinstance(raw, list)
            or not raw
            or any(
                isinstance(token, bool)
                or not isinstance(token, int)
                or token < 0
                or token > 0x7FFFFFFF
                for token in raw
            )
        ):
            raise RuntimeError("pinned tokenizer helper returned invalid token IDs")
        canonical_ids = json.dumps(raw, separators=(",", ":")).encode("ascii")
        result = TokenCount(
            count=len(raw),
            token_ids_sha256=_digest(canonical_ids),
            tokenizer_artifact_sha256=self.helper_sha256,
            model_artifact_sha256=self.model_sha256,
        )
        if len(prompt_bytes) <= self.max_cache_bytes:
            while self._cache and (
                len(self._cache) >= self.max_cache_entries
                or self._cache_prompt_bytes + len(prompt_bytes) > self.max_cache_bytes
            ):
                _evicted_key, (evicted_prompt, _evicted_result) = self._cache.popitem(
                    last=False
                )
                self._cache_prompt_bytes -= len(evicted_prompt)
            self._cache[cache_key] = (prompt_bytes, result)
            self._cache_prompt_bytes += len(prompt_bytes)
        return result

    @property
    def cache_entry_count(self) -> int:
        return len(self._cache)

    @property
    def cache_prompt_bytes(self) -> int:
        return self._cache_prompt_bytes


@dataclass(frozen=True)
class InteractiveModelRequest:
    prepared_prompt: PreparedPrompt
    seed: int
    context_id: str
    max_output_tokens: int

    def __post_init__(self) -> None:
        if not isinstance(self.prepared_prompt, PreparedPrompt):
            raise ValueError("interactive request must contain a prepared prompt")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("interactive request seed must be an integer")
        if not isinstance(self.context_id, str) or not self.context_id:
            raise ValueError("interactive request context_id must not be empty")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("interactive request max_output_tokens must be positive")

    @property
    def prompt(self) -> str:
        return self.prepared_prompt.rendered_prompt

    @property
    def prompt_tokens(self) -> int:
        return self.prepared_prompt.prompt_tokens


@dataclass(frozen=True)
class InteractiveModelOutput:
    text: str
    prompt_tokens: int
    completion_tokens: int
    total_duration_ns: int

    def __post_init__(self) -> None:
        _utf8(self.text, "interactive model output text")
        for field in ("prompt_tokens", "completion_tokens", "total_duration_ns"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"interactive model output {field} must be non-negative")


InteractiveModel = Callable[[InteractiveModelRequest], InteractiveModelOutput]


_ACTION_JSON_SCHEMA = {
    "additionalProperties": False,
    "properties": {"command": {"minLength": 1, "type": "string"}},
    "required": ["command"],
    "type": "object",
}


@dataclass(frozen=True)
class OllamaGenerationConfig:
    profile: RestrictedRawRenderingProfile
    runtime_version: str
    runtime_binary_sha256: str
    context_tokens: int
    num_batch: int
    num_gpu: int
    main_gpu: int
    use_mmap: bool
    num_thread: int
    draft_num_predict: int
    temperature: float
    top_k: int
    top_p: float
    min_p: float
    typical_p: float
    repeat_last_n: int
    repeat_penalty: float
    presence_penalty: float
    frequency_penalty: float
    stop: tuple[str, ...]
    keep_alive_seconds: int
    request_timeout_seconds: float
    endpoint: str = "http://127.0.0.1:11434"

    def __post_init__(self) -> None:
        if self.profile not in {QWEN35_RAW_PROFILE, PHI4_MINI_RAW_PROFILE}:
            raise ValueError("Ollama profile must be one frozen benchmark profile")
        if self.runtime_version != "0.31.1":
            raise ValueError("Ollama runtime version must match the frozen profile")
        _validate_digest(self.runtime_binary_sha256, "runtime_binary_sha256")
        if (
            isinstance(self.context_tokens, bool)
            or not isinstance(self.context_tokens, int)
            or self.context_tokens <= 1
        ):
            raise ValueError("Ollama context_tokens must be greater than one")
        for field, allow_zero in (
            ("num_batch", False),
            ("num_gpu", True),
            ("main_gpu", True),
            ("num_thread", False),
            ("draft_num_predict", True),
            ("repeat_last_n", True),
        ):
            value = getattr(self, field)
            minimum = 0 if allow_zero else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"Ollama {field} is outside the frozen domain")
        if not isinstance(self.use_mmap, bool):
            raise ValueError("Ollama use_mmap must be boolean")
        if not math.isfinite(self.temperature) or self.temperature <= 0:
            raise ValueError("Ollama temperature must be positive and finite")
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int) or self.top_k <= 0:
            raise ValueError("Ollama top_k must be positive")
        if not math.isfinite(self.top_p) or not 0 < self.top_p <= 1:
            raise ValueError("Ollama top_p must be in (0, 1]")
        if not math.isfinite(self.min_p) or not 0 <= self.min_p <= 1:
            raise ValueError("Ollama min_p must be in [0, 1]")
        if not math.isfinite(self.typical_p) or not 0 < self.typical_p <= 1:
            raise ValueError("Ollama typical_p must be in (0, 1]")
        if not math.isfinite(self.repeat_penalty) or self.repeat_penalty <= 0:
            raise ValueError("Ollama repeat_penalty must be positive and finite")
        for field in ("presence_penalty", "frequency_penalty"):
            if not math.isfinite(getattr(self, field)):
                raise ValueError(f"Ollama {field} must be finite")
        if not self.stop or any(not isinstance(item, str) or not item for item in self.stop):
            raise ValueError("Ollama stop sequences must be non-empty strings")
        if self.keep_alive_seconds != -1:
            raise ValueError("measured Ollama requests must pin keep_alive to -1")
        if (
            not math.isfinite(self.request_timeout_seconds)
            or not 0 < self.request_timeout_seconds <= 3600
        ):
            raise ValueError("Ollama request timeout must be in (0, 3600]")
        _parse_loopback_endpoint(self.endpoint)

    @property
    def model(self) -> str:
        return self.profile.model

    @property
    def model_artifact_sha256(self) -> str:
        return self.profile.model_artifact_sha256

    @property
    def canonical_definition(self) -> dict[str, object]:
        return {
            "action_schema": deepcopy(_ACTION_JSON_SCHEMA),
            "draft_num_predict": self.draft_num_predict,
            "endpoint": self.endpoint,
            "frequency_penalty": self.frequency_penalty,
            "keep_alive_seconds": self.keep_alive_seconds,
            "main_gpu": self.main_gpu,
            "min_p": self.min_p,
            "num_batch": self.num_batch,
            "num_gpu": self.num_gpu,
            "num_thread": self.num_thread,
            "presence_penalty": self.presence_penalty,
            "profile_sha256": self.profile.sha256,
            "repeat_last_n": self.repeat_last_n,
            "repeat_penalty": self.repeat_penalty,
            "request_contract": {
                "num_keep": 0,
                "num_predict": "request.max_output_tokens",
                "raw": True,
                "seed": "request.candidate_seed",
                "shift": False,
                "stream": False,
                "think": False,
                "truncate": False,
            },
            "request_timeout_seconds": self.request_timeout_seconds,
            "response_contract": {
                "done": True,
                "done_reason": ["length", "stop"],
                "max_response_bytes": _MAX_OLLAMA_RESPONSE_BYTES,
                "model": self.model,
                "remote_fields": "absent_or_null",
            },
            "runtime_binary_sha256": self.runtime_binary_sha256,
            "runtime_commit": self.profile.ollama_commit,
            "runtime_version": self.runtime_version,
            "stop": list(self.stop),
            "temperature": self.temperature,
            "top_k": self.top_k,
            "top_p": self.top_p,
            "typical_p": self.typical_p,
            "use_mmap": self.use_mmap,
            "context_tokens": self.context_tokens,
        }

    @property
    def sha256(self) -> str:
        return _digest(
            json.dumps(
                self.canonical_definition,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )


RawTransport = Callable[[bytes], bytes]


class OllamaRawModel:
    """Send a pre-rendered prompt through Ollama without any chat templating."""

    def __init__(
        self,
        config: OllamaGenerationConfig,
        *,
        transport: RawTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _LoopbackOllamaTransport(
            config.endpoint,
            timeout_seconds=config.request_timeout_seconds,
        )

    def __call__(self, request: InteractiveModelRequest) -> InteractiveModelOutput:
        if request.prepared_prompt.model_artifact_sha256 != self.config.model_artifact_sha256:
            raise ValueError("prepared prompt and Ollama model artifacts do not match")
        if request.prepared_prompt.renderer_profile_sha256 != self.config.profile.sha256:
            raise ValueError("prepared prompt and Ollama renderer profiles do not match")
        payload = {
            "format": _ACTION_JSON_SCHEMA,
            "keep_alive": self.config.keep_alive_seconds,
            "model": self.config.model,
            "options": {
                "draft_num_predict": self.config.draft_num_predict,
                "frequency_penalty": self.config.frequency_penalty,
                "main_gpu": self.config.main_gpu,
                "min_p": self.config.min_p,
                "num_batch": self.config.num_batch,
                "num_ctx": self.config.context_tokens,
                "num_gpu": self.config.num_gpu,
                "num_keep": 0,
                "num_predict": request.max_output_tokens,
                "num_thread": self.config.num_thread,
                "presence_penalty": self.config.presence_penalty,
                "repeat_last_n": self.config.repeat_last_n,
                "repeat_penalty": self.config.repeat_penalty,
                "seed": request.seed,
                "stop": list(self.config.stop),
                "temperature": self.config.temperature,
                "top_k": self.config.top_k,
                "top_p": self.config.top_p,
                "typical_p": self.config.typical_p,
                "use_mmap": self.config.use_mmap,
            },
            "prompt": request.prompt,
            "raw": True,
            "shift": False,
            "stream": False,
            "think": False,
            "truncate": False,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        response_bytes = self._transport(encoded)
        if not isinstance(response_bytes, bytes) or len(response_bytes) > _MAX_OLLAMA_RESPONSE_BYTES:
            raise RuntimeError("Ollama response is not bounded bytes")
        try:
            response = parse_strict_json_object(response_bytes)
        except OllamaLoopbackHttpError as error:
            raise RuntimeError("Ollama returned invalid response JSON") from error
        if (
            response.get("model") != self.config.model
            or response.get("done") is not True
            or response.get("done_reason") not in {"stop", "length"}
            or response.get("remote_model") is not None
            or response.get("remote_host") is not None
        ):
            raise RuntimeError("Ollama returned an incomplete or wrong-model response")
        text = response.get("response")
        prompt_tokens = response.get("prompt_eval_count")
        completion_tokens = response.get("eval_count")
        total_duration = response.get("total_duration")
        if not isinstance(text, str):
            raise RuntimeError("Ollama response text must be a string")
        for field, value in (
            ("prompt_eval_count", prompt_tokens),
            ("eval_count", completion_tokens),
            ("total_duration", total_duration),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(f"Ollama {field} must be a non-negative integer")
        return InteractiveModelOutput(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_duration_ns=total_duration,
        )


def _parse_loopback_endpoint(endpoint: str) -> tuple[str, int]:
    if not isinstance(endpoint, str):
        raise ValueError("Ollama endpoint must be a string")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ollama endpoint must be a bare loopback HTTP origin")
    try:
        port = parsed.port or 11434
    except ValueError as error:
        raise ValueError("Ollama endpoint has an invalid port") from error
    return parsed.hostname, port


class _LoopbackOllamaTransport:
    def __init__(self, endpoint: str, *, timeout_seconds: float = 300.0) -> None:
        self.host, self.port = _parse_loopback_endpoint(endpoint)
        self.timeout_seconds = timeout_seconds

    def __call__(self, payload: bytes) -> bytes:
        connection = http.client.HTTPConnection(
            self.host,
            self.port,
            timeout=self.timeout_seconds,
        )
        try:
            connection.request(
                "POST",
                "/api/generate",
                body=payload,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = response.read(_MAX_OLLAMA_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as error:
            raise RuntimeError("Ollama loopback request failed") from error
        finally:
            connection.close()
        if response.status != 200:
            raise RuntimeError(f"Ollama returned HTTP status {response.status}")
        if len(body) > _MAX_OLLAMA_RESPONSE_BYTES:
            raise RuntimeError("Ollama response exceeded byte limit")
        return body
