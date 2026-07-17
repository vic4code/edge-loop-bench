from __future__ import annotations

import os
import unittest
import urllib.request
from unittest import mock

from edgeloopbench import ollama_loopback_http as http_module


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        final_url: str = http_module.OLLAMA_PS_URL,
    ) -> None:
        self.status = status
        self.final_url = final_url

    def geturl(self) -> str:
        return self.final_url


class OllamaLoopbackHttpTests(unittest.TestCase):
    def test_opener_ignores_proxy_environment_and_rejects_redirects(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "http_proxy": "http://proxy.invalid:8080",
                "https_proxy": "http://proxy.invalid:8080",
            },
        ):
            handlers = http_module._build_ollama_http_opener().handlers

        self.assertFalse(
            any(type(handler) is urllib.request.ProxyHandler for handler in handlers)
        )
        redirect = next(
            handler
            for handler in handlers
            if type(handler) is http_module._RejectRedirectHandler
        )
        with self.assertRaisesRegex(
            http_module.OllamaLoopbackHttpError,
            "redirect",
        ):
            redirect.redirect_request(
                urllib.request.Request(http_module.OLLAMA_PS_URL),
                None,
                302,
                "Found",
                {},
                "https://example.invalid/escape",
            )

    def test_response_requires_exact_200_and_callable_exact_final_url(self) -> None:
        http_module.require_exact_ollama_response(
            FakeResponse(),
            expected_url=http_module.OLLAMA_PS_URL,
        )

        cases = (
            FakeResponse(status=204),
            FakeResponse(final_url=http_module.OLLAMA_VERSION_URL),
            object(),
        )
        for response in cases:
            with self.subTest(response=response), self.assertRaises(
                http_module.OllamaLoopbackHttpError
            ):
                http_module.require_exact_ollama_response(
                    response,
                    expected_url=http_module.OLLAMA_PS_URL,
                )

    def test_only_fixed_urls_and_strict_json_objects_are_admitted(self) -> None:
        self.assertEqual(
            http_module.require_fixed_ollama_url(http_module.OLLAMA_PS_URL),
            http_module.OLLAMA_PS_URL,
        )
        with self.assertRaises(http_module.OllamaLoopbackHttpError):
            http_module.require_fixed_ollama_url(
                "http://127.0.0.1:11434/api/ps?redirect=1"
            )

        self.assertEqual(
            http_module.parse_strict_json_object(b'{"models":[]}'),
            {"models": []},
        )
        for payload in (
            b'{"models":[],"models":[]}',
            b'{"models":[NaN]}',
            b'{"models":[1e400]}',
            b'[]',
            '{"models":[]}'.encode("utf-16"),
            '{"models":[]}'.encode("utf-32"),
        ):
            with self.subTest(payload=payload), self.assertRaises(
                http_module.OllamaLoopbackHttpError
            ):
                http_module.parse_strict_json_object(payload)


if __name__ == "__main__":
    unittest.main()
