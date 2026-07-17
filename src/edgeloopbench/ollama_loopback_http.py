"""Hardened stdlib HTTP boundary for fixed local Ollama control probes.

The benchmark's measured generation transport uses a separate ``http.client``
path; only its strict JSON decoder is shared here. The HTTP opener is for the
small read-only identity probes and zero-generation load/unload control
request. It never consults proxy environment variables, never follows
redirects, and admits only the three frozen loopback endpoint URLs.
"""

from __future__ import annotations

import json
import math
import urllib.request


OLLAMA_ORIGIN = "http://127.0.0.1:11434"
OLLAMA_GENERATE_URL = OLLAMA_ORIGIN + "/api/generate"
OLLAMA_PS_URL = OLLAMA_ORIGIN + "/api/ps"
OLLAMA_VERSION_URL = OLLAMA_ORIGIN + "/api/version"
_FIXED_OLLAMA_URLS = frozenset(
    (OLLAMA_GENERATE_URL, OLLAMA_PS_URL, OLLAMA_VERSION_URL)
)


class OllamaLoopbackHttpError(ValueError):
    """A request or response crossed the frozen local HTTP contract."""


class _DuplicateKey(ValueError):
    pass


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Raise on every redirect instead of resolving a new target."""

    def redirect_request(
        self,
        request: object,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> object:
        del request, file_pointer, code, message, headers, new_url
        raise OllamaLoopbackHttpError("Ollama redirects are not admitted")


def _build_ollama_http_opener() -> urllib.request.OpenerDirector:
    """Build an opener whose handler graph never reads proxy environment."""

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirectHandler(),
    )


_OLLAMA_HTTP_OPENER = _build_ollama_http_opener()


def require_fixed_ollama_url(value: object) -> str:
    if type(value) is not str or value not in _FIXED_OLLAMA_URLS:
        raise OllamaLoopbackHttpError("Ollama URL is outside the fixed loopback set")
    return value


def open_ollama_http(request: object, timeout: float) -> object:
    """Open one exact fixed request with proxies and redirects disabled."""

    if not isinstance(request, urllib.request.Request):
        raise OllamaLoopbackHttpError("Ollama request type is invalid")
    require_fixed_ollama_url(request.full_url)
    if (
        type(timeout) not in (int, float)
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise OllamaLoopbackHttpError("Ollama request timeout is invalid")
    return _OLLAMA_HTTP_OPENER.open(request, timeout=timeout)


def require_exact_ollama_response(
    response: object,
    *,
    expected_url: str,
) -> None:
    """Require HTTP 200 and origin proof from urllib's final response URL."""

    expected = require_fixed_ollama_url(expected_url)
    try:
        status = getattr(response, "status")
        get_url = getattr(response, "geturl")
    except Exception as error:
        raise OllamaLoopbackHttpError(
            "Ollama response lacks HTTP status or final URL evidence"
        ) from error
    if status != 200:
        raise OllamaLoopbackHttpError("Ollama response status is not HTTP 200")
    if not callable(get_url):
        raise OllamaLoopbackHttpError("Ollama response final URL probe is invalid")
    try:
        final_url = get_url()
    except Exception as error:
        raise OllamaLoopbackHttpError(
            "Ollama response final URL probe failed"
        ) from error
    if type(final_url) is not str or final_url != expected:
        raise OllamaLoopbackHttpError("Ollama response final URL differs from request")


def parse_strict_json_object(payload: bytes) -> dict[str, object]:
    """Parse finite UTF-8 JSON while rejecting duplicate keys at every depth."""

    if type(payload) is not bytes:
        raise OllamaLoopbackHttpError("Ollama response is not bytes")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKey(key)
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError(value)

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(value)
        return parsed

    try:
        text = payload.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
            parse_float=parse_finite_float,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKey,
        ValueError,
    ) as error:
        raise OllamaLoopbackHttpError(
            "Ollama response is not strict finite UTF-8 JSON"
        ) from error
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise OllamaLoopbackHttpError("Ollama response must be a JSON object")
    return value


__all__ = (
    "OLLAMA_GENERATE_URL",
    "OLLAMA_ORIGIN",
    "OLLAMA_PS_URL",
    "OLLAMA_VERSION_URL",
    "OllamaLoopbackHttpError",
    "open_ollama_http",
    "parse_strict_json_object",
    "require_exact_ollama_response",
    "require_fixed_ollama_url",
)
