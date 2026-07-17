from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.model_adapter import (
    ExactPromptPreparer,
    InteractiveModelRequest,
    LlamaTokenizeCounter,
    OllamaGenerationConfig,
    OllamaRawModel,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
    PreparedPrompt,
    RenderedPromptByteLimitExceeded,
    TokenCount,
    TranscriptMessage,
)


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class RecordingRunner:
    def __init__(self, token_ids: list[int]) -> None:
        self.token_ids = token_ids
        self.calls: list[tuple[list[str], bytes, dict[str, str]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        stdin = kwargs["input"]
        environment = kwargs["env"]
        assert isinstance(stdin, bytes)
        assert isinstance(environment, dict)
        self.calls.append((command, stdin, environment))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(json.dumps(self.token_ids) + "\n").encode("ascii"),
            stderr=b"",
        )


class FixedCounter:
    def __init__(self, *, count: int, model_sha256: str) -> None:
        self.count_value = count
        self.model_sha256 = model_sha256
        self.prompts: list[str] = []

    def count(self, prompt: str) -> TokenCount:
        self.prompts.append(prompt)
        ids = list(range(self.count_value))
        return TokenCount(
            count=self.count_value,
            token_ids_sha256=digest(
                json.dumps(ids, separators=(",", ":")).encode("ascii")
            ),
            tokenizer_artifact_sha256="sha256:" + "1" * 64,
            model_artifact_sha256=self.model_sha256,
        )


class ModelAdapterTests(unittest.TestCase):
    def test_renderer_profile_digest_covers_the_exact_render_contract(self) -> None:
        qwen_definition = QWEN35_RAW_PROFILE.canonical_definition
        phi_definition = PHI4_MINI_RAW_PROFILE.canonical_definition

        self.assertEqual(
            qwen_definition["render_contract"]["turn_template"],
            "<|im_start|>{role}\n{content}<|im_end|>\n",
        )
        self.assertEqual(
            qwen_definition["render_contract"]["generation_suffix"],
            "<|im_start|>assistant\n<think>\n\n</think>\n\n",
        )
        self.assertEqual(
            qwen_definition["render_contract"]["assistant_reasoning_close_tag"],
            "</think>",
        )
        self.assertEqual(
            qwen_definition["render_contract"][
                "assistant_reasoning_close_occurrence"
            ],
            "first",
        )
        self.assertEqual(
            qwen_definition["render_contract"]["content_operation_order"],
            [
                "trim_go_space",
                "assistant_remove_through_reasoning_close",
                "assistant_left_trim_newline",
            ],
        )
        self.assertEqual(
            phi_definition["render_contract"]["turn_template"],
            "<|{role}|>{content}<|end|>",
        )
        self.assertEqual(
            phi_definition["render_contract"]["generation_suffix"],
            "<|assistant|>",
        )
        self.assertNotIn("transport", qwen_definition)
        self.assertEqual(
            QWEN35_RAW_PROFILE.sha256,
            digest(
                json.dumps(
                    qwen_definition,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        )

    def test_qwen35_no_think_renderer_matches_the_pinned_official_subset(self) -> None:
        first = QWEN35_RAW_PROFILE.render(
            (TranscriptMessage("user", "  hello  "),)
        )
        multi_turn = QWEN35_RAW_PROFILE.render(
            (
                TranscriptMessage("user", "first"),
                TranscriptMessage("assistant", "<think>hidden</think>\n{\"command\":\"pwd\"}"),
                TranscriptMessage("user", "Output: /work\nReward: 0.0"),
            )
        )

        self.assertEqual(
            first,
            "<|im_start|>user\nhello<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n",
        )
        self.assertEqual(
            multi_turn,
            "<|im_start|>user\nfirst<|im_end|>\n"
            '<|im_start|>assistant\n{\"command\":\"pwd\"}<|im_end|>\n'
            "<|im_start|>user\nOutput: /work\nReward: 0.0<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n",
        )
        self.assertEqual(
            QWEN35_RAW_PROFILE.ollama_source_sha256,
            "sha256:6ca6abea759548962ea23189691c5def15cb86704c114f17182784fd159b4872",
        )

    def test_phi4_mini_renderer_matches_the_pinned_registry_template_subset(self) -> None:
        first = PHI4_MINI_RAW_PROFILE.render(
            (TranscriptMessage("user", "hello\n"),)
        )
        multi_turn = PHI4_MINI_RAW_PROFILE.render(
            (
                TranscriptMessage("user", "first"),
                TranscriptMessage("assistant", '{"command":"pwd"}'),
                TranscriptMessage("user", "Output: /work\nReward: 0.0"),
            )
        )

        self.assertEqual(first, "<|user|>hello\n<|end|><|assistant|>")
        self.assertEqual(
            multi_turn,
            '<|user|>first<|end|><|assistant|>{"command":"pwd"}<|end|>'
            "<|user|>Output: /work\nReward: 0.0<|end|><|assistant|>",
        )
        self.assertEqual(
            PHI4_MINI_RAW_PROFILE.ollama_source_sha256,
            "sha256:813f53fdc6e58d35bb1c3853c93266380e9ca918a993e8eab193e8ede5d3a603",
        )
        self.assertEqual(PHI4_MINI_RAW_PROFILE.model, "phi4-mini:3.8b")

    def test_renderers_reject_unsupported_or_non_alternating_transcripts(self) -> None:
        invalid = (
            (TranscriptMessage("assistant", "starts wrong"),),
            (
                TranscriptMessage("user", "one"),
                TranscriptMessage("user", "two"),
            ),
            (
                TranscriptMessage("user", "one"),
                TranscriptMessage("assistant", "ends wrong"),
            ),
        )
        for messages in invalid:
            with self.subTest(messages=messages):
                with self.assertRaises(ValueError):
                    QWEN35_RAW_PROFILE.render(messages)
                with self.assertRaises(ValueError):
                    PHI4_MINI_RAW_PROFILE.render(messages)

        with self.assertRaises(ValueError):
            TranscriptMessage("system", "unsupported")
        with self.assertRaises(ValueError):
            TranscriptMessage("user", "bad surrogate \ud800")

    def test_exact_preparer_binds_rendered_bytes_count_and_all_artifact_hashes(self) -> None:
        counter = FixedCounter(
            count=17,
            model_sha256=PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
        )
        preparer = ExactPromptPreparer(PHI4_MINI_RAW_PROFILE, counter)

        prepared = preparer((TranscriptMessage("user", "hello"),))

        self.assertEqual(prepared.prompt_tokens, 17)
        self.assertEqual(prepared.rendered_prompt, "<|user|>hello<|end|><|assistant|>")
        self.assertEqual(
            prepared.prompt_sha256,
            digest(prepared.rendered_prompt.encode("utf-8")),
        )
        self.assertEqual(prepared.renderer_profile_sha256, PHI4_MINI_RAW_PROFILE.sha256)
        self.assertEqual(
            prepared.model_artifact_sha256,
            PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
        )

    def test_preparer_rejects_a_tokenizer_bound_to_different_model_bytes(self) -> None:
        counter = FixedCounter(count=1, model_sha256="sha256:" + "f" * 64)
        preparer = ExactPromptPreparer(PHI4_MINI_RAW_PROFILE, counter)

        with self.assertRaises(ValueError):
            preparer((TranscriptMessage("user", "hello"),))

    def test_preparer_rejects_rendered_bytes_before_invoking_tokenizer(self) -> None:
        counter = FixedCounter(
            count=1,
            model_sha256=PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
        )
        preparer = ExactPromptPreparer(
            PHI4_MINI_RAW_PROFILE,
            counter,
            max_rendered_prompt_bytes=64,
        )

        with self.assertRaises(RenderedPromptByteLimitExceeded) as raised:
            preparer((TranscriptMessage("user", "x" * 64),))

        self.assertEqual(counter.prompts, [])
        self.assertGreater(raised.exception.observed_bytes, 64)
        self.assertEqual(raised.exception.limit_bytes, 64)
        self.assertEqual(
            raised.exception.renderer_profile_sha256,
            PHI4_MINI_RAW_PROFILE.sha256,
        )

    def test_llama_tokenize_uses_exact_pinned_flags_and_unmodified_utf8_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "llama-tokenize"
            model = root / "model.gguf"
            helper.write_bytes(b"test helper")
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
            model.write_bytes(b"test model")
            runner = RecordingRunner([1, 22, 333])
            counter = LlamaTokenizeCounter(
                helper_path=helper,
                helper_sha256=digest(helper.read_bytes()),
                model_path=model,
                model_sha256=digest(model.read_bytes()),
                run_command=runner,
            )

            result = counter.count("line 1\\n原樣 newline\n")
            cached = counter.count("line 1\\n原樣 newline\n")

        self.assertEqual(result, cached)
        self.assertEqual(result.count, 3)
        self.assertEqual(len(runner.calls), 1)
        command, stdin, environment = runner.calls[0]
        self.assertEqual(
            command,
            [
                str(helper.resolve()),
                "--model",
                str(model.resolve()),
                "--stdin",
                "--ids",
                "--no-escape",
                "--log-disable",
            ],
        )
        self.assertEqual(stdin, "line 1\\n原樣 newline\n".encode("utf-8"))
        self.assertEqual(environment, {"LANG": "C", "LC_ALL": "C"})

    def test_llama_tokenize_rejects_tampered_artifacts_and_malformed_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "llama-tokenize"
            model = root / "model.gguf"
            helper.write_bytes(b"helper")
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
            model.write_bytes(b"model")

            with self.assertRaises(ValueError):
                LlamaTokenizeCounter(
                    helper_path=helper,
                    helper_sha256="sha256:" + "0" * 64,
                    model_path=model,
                    model_sha256=digest(model.read_bytes()),
                )

            def malformed(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                return subprocess.CompletedProcess(command, 0, b'[1,true,"2"]\n', b"")

            counter = LlamaTokenizeCounter(
                helper_path=helper,
                helper_sha256=digest(helper.read_bytes()),
                model_path=model,
                model_sha256=digest(model.read_bytes()),
                run_command=malformed,
            )
            with self.assertRaises(RuntimeError):
                counter.count("hello")

    def test_llama_tokenize_rejects_artifact_drift_after_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "llama-tokenize"
            model = root / "model.gguf"
            helper.write_bytes(b"helper")
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
            model.write_bytes(b"model")
            counter = LlamaTokenizeCounter(
                helper_path=helper,
                helper_sha256=digest(helper.read_bytes()),
                model_path=model,
                model_sha256=digest(model.read_bytes()),
                run_command=RecordingRunner([1]),
            )
            model.write_bytes(b"changed model")

            with self.assertRaisesRegex(RuntimeError, "changed after verification"):
                counter.count("hello")

    def test_llama_tokenize_rejects_artifact_drift_during_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "llama-tokenize"
            model = root / "model.gguf"
            helper.write_bytes(b"helper")
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
            model.write_bytes(b"model")

            def mutate_during_call(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                model.write_bytes(b"changed during execution")
                return subprocess.CompletedProcess(command, 0, b"[1]\n", b"")

            counter = LlamaTokenizeCounter(
                helper_path=helper,
                helper_sha256=digest(helper.read_bytes()),
                model_path=model,
                model_sha256=digest(model.read_bytes()),
                run_command=mutate_during_call,
            )

            with self.assertRaisesRegex(RuntimeError, "changed after verification"):
                counter.count("hello")

    def test_llama_tokenize_cache_is_lru_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "llama-tokenize"
            model = root / "model.gguf"
            helper.write_bytes(b"helper")
            helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
            model.write_bytes(b"model")
            runner = RecordingRunner([1])
            counter = LlamaTokenizeCounter(
                helper_path=helper,
                helper_sha256=digest(helper.read_bytes()),
                model_path=model,
                model_sha256=digest(model.read_bytes()),
                max_cache_entries=2,
                max_cache_bytes=32,
                run_command=runner,
            )

            counter.count("alpha")
            counter.count("beta")
            counter.count("alpha")  # refresh alpha; beta is now least recent
            counter.count("gamma")
            counter.count("beta")

        self.assertEqual(len(runner.calls), 4)
        self.assertLessEqual(counter.cache_entry_count, 2)
        self.assertLessEqual(counter.cache_prompt_bytes, 32)

    def test_ollama_raw_model_sends_prepared_bytes_without_chat_rerendering(self) -> None:
        payloads: list[bytes] = []

        def transport(payload: bytes) -> bytes:
            payloads.append(payload)
            return json.dumps(
                {
                    "model": PHI4_MINI_RAW_PROFILE.model,
                    "response": '{"command":"pwd"}',
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 7,
                    "eval_count": 4,
                    "total_duration": 1234,
                }
            ).encode("utf-8")

        rendered = "<|user|>hello<|end|><|assistant|>"
        prepared = PreparedPrompt(
            rendered_prompt=rendered,
            prompt_tokens=7,
            prompt_sha256=digest(rendered.encode("utf-8")),
            token_ids_sha256="sha256:" + "2" * 64,
            renderer_profile_sha256=PHI4_MINI_RAW_PROFILE.sha256,
            tokenizer_artifact_sha256="sha256:" + "3" * 64,
            model_artifact_sha256=PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
        )
        config = OllamaGenerationConfig(
            profile=PHI4_MINI_RAW_PROFILE,
            runtime_version="0.31.1",
            runtime_binary_sha256="sha256:" + "4" * 64,
            context_tokens=4096,
            num_batch=128,
            num_gpu=99,
            main_gpu=0,
            use_mmap=True,
            num_thread=8,
            draft_num_predict=0,
            temperature=0.6,
            top_k=20,
            top_p=0.9,
            min_p=0.0,
            typical_p=1.0,
            repeat_last_n=64,
            repeat_penalty=1.1,
            presence_penalty=0.0,
            frequency_penalty=0.0,
            stop=("<|end|>",),
            keep_alive_seconds=-1,
            request_timeout_seconds=180.0,
        )
        model = OllamaRawModel(config, transport=transport)

        output = model(
            InteractiveModelRequest(
                prepared_prompt=prepared,
                seed=29,
                context_id="context-1",
                max_output_tokens=64,
            )
        )

        self.assertEqual(output.prompt_tokens, 7)
        self.assertEqual(output.completion_tokens, 4)
        request = json.loads(payloads[0])
        self.assertEqual(request["model"], PHI4_MINI_RAW_PROFILE.model)
        self.assertEqual(request["keep_alive"], -1)
        self.assertEqual(request["prompt"].encode("utf-8"), rendered.encode("utf-8"))
        self.assertIs(request["raw"], True)
        self.assertIs(request["think"], False)
        self.assertIs(request["truncate"], False)
        self.assertIs(request["shift"], False)
        self.assertIs(request["stream"], False)
        self.assertNotIn("messages", request)
        self.assertNotIn("template", request)
        self.assertNotIn("system", request)
        self.assertNotIn("context", request)
        self.assertEqual(
            request["options"],
            {
                "min_p": 0.0,
                "main_gpu": 0,
                "num_batch": 128,
                "num_ctx": 4096,
                "num_gpu": 99,
                "num_keep": 0,
                "num_predict": 64,
                "num_thread": 8,
                "draft_num_predict": 0,
                "frequency_penalty": 0.0,
                "presence_penalty": 0.0,
                "repeat_last_n": 64,
                "repeat_penalty": 1.1,
                "seed": 29,
                "stop": ["<|end|>"],
                "temperature": 0.6,
                "top_k": 20,
                "top_p": 0.9,
                "typical_p": 1.0,
                "use_mmap": True,
            },
        )

        duplicate_response = (
            b'{"model":"phi4-mini:3.8b","response":"{\\"command\\":\\"pwd\\"}",'
            b'"response":"{\\"command\\":\\"pwd\\"}","done":true,'
            b'"done_reason":"stop","prompt_eval_count":7,"eval_count":4,'
            b'"total_duration":1234}'
        )
        duplicate_model = OllamaRawModel(
            config,
            transport=lambda _payload: duplicate_response,
        )
        with self.assertRaisesRegex(RuntimeError, "invalid response JSON"):
            duplicate_model(
                InteractiveModelRequest(
                    prepared_prompt=prepared,
                    seed=29,
                    context_id="context-duplicate",
                    max_output_tokens=64,
                )
            )

    def test_ollama_config_digest_binds_fixed_request_and_response_contracts(self) -> None:
        config = OllamaGenerationConfig(
            profile=PHI4_MINI_RAW_PROFILE,
            runtime_version="0.31.1",
            runtime_binary_sha256="sha256:" + "4" * 64,
            context_tokens=4096,
            num_batch=128,
            num_gpu=99,
            main_gpu=0,
            use_mmap=True,
            num_thread=8,
            draft_num_predict=0,
            temperature=0.6,
            top_k=20,
            top_p=0.9,
            min_p=0.0,
            typical_p=1.0,
            repeat_last_n=64,
            repeat_penalty=1.1,
            presence_penalty=0.0,
            frequency_penalty=0.0,
            stop=("<|end|>",),
            keep_alive_seconds=-1,
            request_timeout_seconds=180.0,
        )

        self.assertEqual(
            config.canonical_definition["request_contract"],
            {
                "num_keep": 0,
                "num_predict": "request.max_output_tokens",
                "raw": True,
                "seed": "request.candidate_seed",
                "shift": False,
                "stream": False,
                "think": False,
                "truncate": False,
            },
        )
        self.assertEqual(
            config.canonical_definition["response_contract"],
            {
                "done": True,
                "done_reason": ["length", "stop"],
                "max_response_bytes": 1_048_576,
                "model": PHI4_MINI_RAW_PROFILE.model,
                "remote_fields": "absent_or_null",
            },
        )

        original_sha256 = config.sha256
        detached = config.canonical_definition
        detached["action_schema"]["properties"].clear()
        detached["response_contract"]["done_reason"].clear()
        fresh = config.canonical_definition
        self.assertIn("command", fresh["action_schema"]["properties"])
        self.assertEqual(fresh["response_contract"]["done_reason"], ["length", "stop"])
        self.assertEqual(config.sha256, original_sha256)

    def test_ollama_config_cannot_substitute_an_unrelated_model_tag(self) -> None:
        with self.assertRaises(TypeError):
            OllamaGenerationConfig(  # type: ignore[call-arg]
                profile=PHI4_MINI_RAW_PROFILE,
                model="unrelated:latest",
                runtime_version="0.31.1",
                runtime_binary_sha256="sha256:" + "4" * 64,
                context_tokens=4096,
                num_batch=128,
                num_gpu=99,
                main_gpu=0,
                use_mmap=True,
                num_thread=8,
                draft_num_predict=0,
                temperature=0.6,
                top_k=20,
                top_p=0.9,
                min_p=0.0,
                typical_p=1.0,
                repeat_last_n=64,
                repeat_penalty=1.1,
                presence_penalty=0.0,
                frequency_penalty=0.0,
                stop=("<|end|>",),
                keep_alive_seconds=-1,
                request_timeout_seconds=180.0,
            )

    def test_ollama_model_rejects_a_prompt_from_another_renderer_profile(self) -> None:
        config = OllamaGenerationConfig(
            profile=PHI4_MINI_RAW_PROFILE,
            runtime_version="0.31.1",
            runtime_binary_sha256="sha256:" + "4" * 64,
            context_tokens=4096,
            num_batch=128,
            num_gpu=99,
            main_gpu=0,
            use_mmap=True,
            num_thread=8,
            draft_num_predict=0,
            temperature=0.6,
            top_k=20,
            top_p=0.9,
            min_p=0.0,
            typical_p=1.0,
            repeat_last_n=64,
            repeat_penalty=1.1,
            presence_penalty=0.0,
            frequency_penalty=0.0,
            stop=("<|end|>",),
            keep_alive_seconds=-1,
            request_timeout_seconds=180.0,
        )
        rendered = "prompt"
        prepared = PreparedPrompt(
            rendered_prompt=rendered,
            prompt_tokens=1,
            prompt_sha256=digest(rendered.encode()),
            token_ids_sha256="sha256:" + "1" * 64,
            renderer_profile_sha256=QWEN35_RAW_PROFILE.sha256,
            tokenizer_artifact_sha256="sha256:" + "2" * 64,
            model_artifact_sha256=PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
        )

        with self.assertRaisesRegex(ValueError, "renderer profile"):
            OllamaRawModel(config, transport=lambda _payload: b"{}")(
                InteractiveModelRequest(prepared, 11, "context", 8)
            )


if __name__ == "__main__":
    unittest.main()
