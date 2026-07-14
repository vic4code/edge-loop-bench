from __future__ import annotations

import unittest

from edgeloopbench.ollama import (
    OllamaClient,
    OllamaError,
    OllamaGenerateRequest,
)


class OllamaClientTests(unittest.TestCase):
    def test_generate_uses_fixed_non_streaming_request_and_records_usage(self) -> None:
        calls: list[tuple[str, dict[str, object], float]] = []

        def transport(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            calls.append((url, payload, timeout))
            return {
                "model": "qwen3.5:4b",
                "response": "fixed",
                "thinking": "checked the requested literal",
                "done": True,
                "done_reason": "stop",
                "total_duration": 2_000_000_000,
                "load_duration": 500_000_000,
                "prompt_eval_count": 40,
                "prompt_eval_duration": 400_000_000,
                "eval_count": 20,
                "eval_duration": 1_000_000_000,
            }

        client = OllamaClient("http://127.0.0.1:11434", transport=transport)
        response = client.generate(
            OllamaGenerateRequest(
                model="qwen3.5:4b",
                prompt="Return exactly: fixed",
                context_window=4096,
                max_output_tokens=64,
                thinking=False,
                seed=11,
            )
        )

        self.assertEqual(response.text, "fixed")
        self.assertEqual(response.thinking, "checked the requested literal")
        self.assertEqual(response.prompt_tokens, 40)
        self.assertEqual(response.completion_tokens, 20)
        self.assertEqual(response.decode_tokens_per_second, 20.0)
        self.assertEqual(calls[0][0], "http://127.0.0.1:11434/api/generate")
        self.assertEqual(
            calls[0][1],
            {
                "model": "qwen3.5:4b",
                "prompt": "Return exactly: fixed",
                "stream": False,
                "think": False,
                "options": {
                    "num_ctx": 4096,
                    "num_predict": 64,
                    "seed": 11,
                    "temperature": 0.0,
                },
            },
        )

    def test_rejects_remote_endpoint(self) -> None:
        with self.assertRaisesRegex(OllamaError, "loopback"):
            OllamaClient("https://models.example.com")

    def test_rejects_incomplete_generation_response(self) -> None:
        client = OllamaClient(
            "http://localhost:11434",
            transport=lambda _url, _payload, _timeout: {
                "response": "partial",
                "done": False,
                "prompt_eval_count": 1,
                "eval_count": 1,
                "total_duration": 1,
                "load_duration": 0,
                "prompt_eval_duration": 1,
                "eval_duration": 1,
            },
        )

        with self.assertRaisesRegex(OllamaError, "not complete"):
            client.generate(
                OllamaGenerateRequest(
                    model="qwen3.5:4b",
                    prompt="test",
                    context_window=4096,
                    max_output_tokens=8,
                    thinking=False,
                )
            )

    def test_rejects_invalid_usage_telemetry(self) -> None:
        client = OllamaClient(
            "http://localhost:11434",
            transport=lambda _url, _payload, _timeout: {
                "response": "bad",
                "done": True,
                "prompt_eval_count": -1,
                "eval_count": 1,
                "total_duration": 1,
                "load_duration": 0,
                "prompt_eval_duration": 1,
                "eval_duration": 1,
            },
        )

        with self.assertRaisesRegex(OllamaError, "prompt_eval_count"):
            client.generate(
                OllamaGenerateRequest(
                    model="qwen3.5:4b",
                    prompt="test",
                    context_window=4096,
                    max_output_tokens=8,
                    thinking=False,
                )
            )

    def test_rejects_non_string_thinking_output(self) -> None:
        client = OllamaClient(
            "http://localhost:11434",
            transport=lambda _url, _payload, _timeout: {
                "response": "fixed",
                "thinking": ["not", "text"],
                "done": True,
                "prompt_eval_count": 1,
                "eval_count": 1,
                "total_duration": 1,
                "load_duration": 0,
                "prompt_eval_duration": 1,
                "eval_duration": 1,
            },
        )

        with self.assertRaisesRegex(OllamaError, "response.thinking"):
            client.generate(
                OllamaGenerateRequest(
                    model="qwen3.5:4b",
                    prompt="test",
                    context_window=4096,
                    max_output_tokens=8,
                    thinking=True,
                )
            )


if __name__ == "__main__":
    unittest.main()
