"""Minimal Ollama generation adapter with validated usage telemetry."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
Transport = Callable[[str, dict[str, object], float], Mapping[str, Any]]


class OllamaError(RuntimeError):
    """Raised when the Ollama boundary is unsafe or returns invalid data."""


@dataclass(frozen=True)
class OllamaGenerateRequest:
    model: str
    prompt: str
    context_window: int
    max_output_tokens: int
    seed: int = 0
    temperature: float = 0.0

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must not be empty")
        if not self.prompt:
            raise ValueError("prompt must not be empty")
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if not math.isfinite(self.temperature) or self.temperature < 0:
            raise ValueError("temperature must be finite and nonnegative")


@dataclass(frozen=True)
class OllamaGenerateResponse:
    model: str
    text: str
    done_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    total_duration_ns: int
    load_duration_ns: int
    prompt_eval_duration_ns: int
    eval_duration_ns: int

    @property
    def decode_tokens_per_second(self) -> float | None:
        if self.eval_duration_ns == 0:
            return None
        return self.completion_tokens / (self.eval_duration_ns / 1_000_000_000)


class OllamaClient:
    """Call one loopback Ollama endpoint through a stable request contract."""

    def __init__(
        self,
        endpoint: str,
        *,
        transport: Transport | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.endpoint = _validate_loopback_endpoint(endpoint)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise OllamaError("timeout_seconds must be finite and positive")
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _http_transport

    def generate(self, request: OllamaGenerateRequest) -> OllamaGenerateResponse:
        payload: dict[str, object] = {
            "model": request.model,
            "prompt": request.prompt,
            "stream": False,
            "options": {
                "num_ctx": request.context_window,
                "num_predict": request.max_output_tokens,
                "seed": request.seed,
                "temperature": request.temperature,
            },
        }
        raw = self.transport(
            f"{self.endpoint}/api/generate", payload, self.timeout_seconds
        )
        return _parse_generate_response(raw, expected_model=request.model)


def _validate_loopback_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise OllamaError("Ollama endpoint must be a plain loopback HTTP origin")
    try:
        is_loopback = ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = parsed.hostname.lower() == "localhost"
    if not is_loopback:
        raise OllamaError("Ollama endpoint must use a loopback host")
    try:
        port = parsed.port
    except ValueError as error:
        raise OllamaError("Ollama endpoint has an invalid port") from error
    if port is None:
        raise OllamaError("Ollama endpoint must declare a port")
    return endpoint.rstrip("/")


def _parse_generate_response(
    raw: Mapping[str, Any], *, expected_model: str
) -> OllamaGenerateResponse:
    if not isinstance(raw, Mapping):
        raise OllamaError("Ollama response must be a JSON object")
    if raw.get("done") is not True:
        raise OllamaError("Ollama generation response is not complete")
    text = raw.get("response")
    if not isinstance(text, str):
        raise OllamaError("Ollama response.response must be a string")
    model = raw.get("model", expected_model)
    if not isinstance(model, str) or model != expected_model:
        raise OllamaError("Ollama response model does not match the request")
    done_reason = raw.get("done_reason")
    if done_reason is not None and not isinstance(done_reason, str):
        raise OllamaError("Ollama response.done_reason must be a string or null")
    return OllamaGenerateResponse(
        model=model,
        text=text,
        done_reason=done_reason,
        prompt_tokens=_nonnegative_integer(raw, "prompt_eval_count"),
        completion_tokens=_nonnegative_integer(raw, "eval_count"),
        total_duration_ns=_nonnegative_integer(raw, "total_duration"),
        load_duration_ns=_nonnegative_integer(raw, "load_duration"),
        prompt_eval_duration_ns=_nonnegative_integer(raw, "prompt_eval_duration"),
        eval_duration_ns=_nonnegative_integer(raw, "eval_duration"),
    )


def _nonnegative_integer(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OllamaError(f"Ollama response.{key} must be a nonnegative integer")
    return value


def _http_transport(
    url: str, payload: dict[str, object], timeout: float
) -> Mapping[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_body = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise OllamaError(f"Ollama request failed: {error}") from error
    if len(raw_body) > MAX_RESPONSE_BYTES:
        raise OllamaError("Ollama response exceeds the safety limit")
    try:
        decoded = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OllamaError("Ollama response is not valid UTF-8 JSON") from error
    if not isinstance(decoded, Mapping):
        raise OllamaError("Ollama response must be a JSON object")
    return decoded
