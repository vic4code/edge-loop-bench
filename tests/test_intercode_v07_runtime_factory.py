from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from edgeloopbench import ollama_loopback_http as http_module
from edgeloopbench.intercode_host_safety import (
    DockerDaemonIdentity,
    DockerTelemetryPins,
    ResidentModel,
)
from edgeloopbench.intercode_local_model import (
    LocalModelAttestation,
    _ATTESTATION_SEAL,
)
from edgeloopbench.intercode_managed_ollama import (
    OllamaEndpointObservation,
    launch_managed_v07_ollama,
)
from edgeloopbench.intercode_v07_manifest import (
    V07HostIdentityPins,
    V07TokenizerPins,
)
from edgeloopbench.intercode_v07_runtime_factory import (
    V07ManagedResidencyBoundary,
    V07ResidencyCommand,
    V07ResidencyOperation,
    V07RuntimeFactoryError,
    V07RuntimeSession,
    V07TokenizerHelperAttestation,
    attest_v07_tokenizer_helper,
    build_v07_generation_config,
    build_v07_host_identity,
    build_v07_model_runtime,
    build_v07_runtime_session,
    issue_v07_managed_residency_boundary,
    transition_v07_model_residency,
)
from edgeloopbench.model_adapter import (
    ExactPromptPreparer,
    LlamaTokenizeCounter,
    OllamaGenerationConfig,
    OllamaRawModel,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
    RestrictedRawRenderingProfile,
    TokenCount,
)


def tagged(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class FakeVersionRunner:
    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            argv,
            0,
            (
                b"Warning: could not connect to a running Ollama instance\n"
                b"Warning: client version is 0.31.1\n"
            ),
            b"",
        )


class FakeProcess:
    pid = 4242

    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        del timeout
        assert self.returncode is not None
        return self.returncode


class FakeLauncher:
    def __init__(self) -> None:
        self.process = FakeProcess()

    def __call__(self, argv: list[str], **kwargs: object) -> FakeProcess:
        del argv, kwargs
        return self.process


class FakeEndpointInspector:
    def __call__(self) -> OllamaEndpointObservation:
        return OllamaEndpointObservation(True, "0.31.1", (4242,))


class StructuralFakeResidencyBoundary:
    def __init__(
        self,
        resident_models: tuple[ResidentModel, ...],
        runtime_receipt_sha256: str,
    ) -> None:
        self.resident_models = resident_models
        self.runtime_receipt_sha256 = runtime_receipt_sha256
        self.events: list[object] = []
        self.ignore_unload = False

    def observe(self) -> tuple[ResidentModel, ...]:
        self.events.append(("observe", self.resident_models))
        return self.resident_models

    def apply(self, command: V07ResidencyCommand) -> None:
        self.events.append(command)
        if command.operation is V07ResidencyOperation.UNLOAD:
            if not self.ignore_unload:
                self.resident_models = ()
        else:
            self.resident_models = (command.expected_resident_model,)


class FakeHttpResponse:
    def __init__(self, url: str, payload: bytes) -> None:
        self.status = 200
        self._url = url
        self._payload = payload

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int) -> bytes:
        return self._payload[:limit]

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class FakeResidencyHttp:
    def __init__(self, resident_models: tuple[ResidentModel, ...] = ()) -> None:
        self.resident_models = resident_models
        self.events: list[tuple[str, dict[str, object] | None]] = []
        self.ignore_unload = False

    def open(self, request: object, *, timeout: float) -> FakeHttpResponse:
        self.assert_request(request, timeout)
        assert isinstance(request, urllib.request.Request)
        if request.full_url == http_module.OLLAMA_PS_URL:
            self.events.append(("observe", None))
            payload = {
                "models": [item.to_record() for item in self.resident_models]
            }
            return FakeHttpResponse(
                request.full_url,
                json.dumps(payload, sort_keys=True).encode("ascii"),
            )
        assert request.full_url == http_module.OLLAMA_GENERATE_URL
        assert request.data is not None
        command = json.loads(request.data)
        self.events.append(("control", command))
        model_id = command["model"]
        profile = (
            QWEN35_RAW_PROFILE
            if model_id == QWEN35_RAW_PROFILE.model
            else PHI4_MINI_RAW_PROFILE
        )
        if command["keep_alive"] == 0:
            operation = "unload"
            if not self.ignore_unload:
                self.resident_models = ()
        else:
            operation = "load"
            self.resident_models = (
                ResidentModel(
                    model_id,
                    profile.model_manifest_sha256.removeprefix("sha256:"),
                ),
            )
        response = {
            "done": True,
            "model": model_id,
            "response": "",
        }
        if operation == "unload":
            response["done_reason"] = operation
        return FakeHttpResponse(
            request.full_url,
            json.dumps(response, sort_keys=True).encode("ascii"),
        )

    @staticmethod
    def assert_request(request: object, timeout: float) -> None:
        if not isinstance(request, urllib.request.Request):
            raise AssertionError("expected a typed urllib request")
        if timeout != 30.0:
            raise AssertionError("unexpected residency timeout")


def launch_runtime(binary: Path):  # type: ignore[no-untyped-def]
    observations = iter(
        (
            OllamaEndpointObservation(False, None, ()),
            OllamaEndpointObservation(True, "0.31.1", (4242,)),
        )
    )
    return launch_managed_v07_ollama(
        runtime_binary=binary,
        expected_runtime_binary_sha256=tagged(binary.read_bytes()),
        inherited_environment={"HOME": "/Users/tester", "LANG": "C"},
        version_runner=FakeVersionRunner(),
        process_launcher=FakeLauncher(),
        endpoint_inspector=lambda: next(observations, FakeEndpointInspector()()),
        sleeper=lambda _seconds: None,
    )


def local_attestation(
    profile: RestrictedRawRenderingProfile,
    runtime_sha256: str,
    *,
    model: str | None = None,
) -> LocalModelAttestation:
    values: dict[str, object] = {
        "model": model or profile.model,
        "renderer_profile_sha256": profile.sha256,
        "model_manifest_sha256": profile.model_manifest_sha256,
        "model_config_sha256": tagged(f"config:{profile.model}".encode("ascii")),
        "model_artifact_sha256": profile.model_artifact_sha256,
        "model_artifact_size_bytes": (
            (3 << 30) if profile is QWEN35_RAW_PROFILE else (2 << 30)
        ),
        "model_family": (
            "qwen35" if profile is QWEN35_RAW_PROFILE else "phi4mini"
        ),
        "model_parameter_label": (
            "4B" if profile is QWEN35_RAW_PROFILE else "3.8B"
        ),
        "weight_quantization": "Q4_K_M",
        "kv_cache_quantization": "q8_0",
        "runtime_version": "0.31.1",
        "runtime_binary_sha256": runtime_sha256,
    }
    core = {
        "schema": "edgeloopbench.local-ollama-model-attestation.v1",
        **values,
    }
    attestation_sha256 = tagged(
        json.dumps(
            core,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )
    return LocalModelAttestation(
        **values,  # type: ignore[arg-type]
        attestation_sha256=attestation_sha256,
        _seal=_ATTESTATION_SEAL,
    )


def docker_fixture() -> tuple[DockerTelemetryPins, DockerDaemonIdentity]:
    pins = DockerTelemetryPins(
        endpoint="unix:///var/run/docker.sock",
        client_version="28.3.2",
        server_version="28.3.2",
        binary_sha256=tagged(b"docker"),
    )
    return pins, DockerDaemonIdentity(
        binary_sha256=pins.binary_sha256,
        endpoint_sha256=pins.endpoint_sha256,
        client_version=pins.client_version,
        server_version=pins.server_version,
    )


def write_tokenizer_provenance(
    helper: Path,
    *,
    llama_cpp_commit: str = "8c146a8366304c871efc26057cc90370ccf58dad",
    artifact_sha256: str | None = None,
) -> Path:
    provenance = {
        "artifact_sha256": artifact_sha256 or tagged(helper.read_bytes()),
        "build_recipe": {
            "cmake_definitions": [
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_SHARED_LIBS=OFF",
                "-DGGML_BACKEND_DL=OFF",
                "-DGGML_CPU_ALL_VARIANTS=OFF",
                "-DGGML_METAL=OFF",
                "-DGGML_NATIVE=OFF",
                "-DGGML_OPENMP=OFF",
                "-DLLAMA_CURL=OFF",
                "-DOLLAMA_RUNNER_DIR=",
                "-DCMAKE_OSX_ARCHITECTURES=arm64",
            ],
            "parallel_jobs": 2,
            "target": "llama-tokenize",
            "target_platform": "macos-arm64",
        },
        "llama_cpp_commit": llama_cpp_commit,
        "llama_cpp_tag": "b9840",
        "ollama_commit": "710292ff4f191d8da9f6a4230804fbc693338d4a",
        "ollama_repository": "https://github.com/ollama/ollama.git",
    }
    path = helper.with_name(helper.name + ".provenance.json")
    path.write_text(json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8")
    return path


class V07RuntimeFactoryTests(unittest.TestCase):
    @staticmethod
    def build_bundle(  # type: ignore[no-untyped-def]
        *,
        profile: RestrictedRawRenderingProfile,
        runtime,
        root: Path,
        tokenizer_attestation: V07TokenizerHelperAttestation,
    ):
        artifact = root / (profile.model.replace(":", "-") + ".gguf")
        artifact.write_bytes(("stand-in:" + profile.model).encode("ascii"))

        def verified_identity(
            path: Path, expected_sha256: str, *, executable: bool
        ) -> tuple[int, int, int, int, int]:
            del expected_sha256, executable
            metadata = path.lstat()
            return (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )

        with patch.object(
            LlamaTokenizeCounter,
            "_verify_artifact",
            side_effect=verified_identity,
        ):
            return build_v07_model_runtime(
                model_id=profile.model,
                attestation=local_attestation(
                    profile,
                    runtime.receipt.runtime_binary_sha256,
                ),
                runtime_receipt=runtime.receipt,
                tokenizer_attestation=tokenizer_attestation,
                model_artifact_path=artifact,
            )

    def test_tokenizer_helper_attestation_verifies_provenance_and_redacts_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "llama-tokenize"
            helper.write_bytes(b"pinned tokenizer")
            helper.chmod(0o755)
            provenance = write_tokenizer_provenance(helper)

            attestation = attest_v07_tokenizer_helper(
                helper_path=helper,
                provenance_path=provenance,
            )

            self.assertIs(type(attestation), V07TokenizerHelperAttestation)
            self.assertEqual(attestation.helper_sha256, tagged(helper.read_bytes()))
            self.assertEqual(
                attestation.llama_cpp_commit,
                "8c146a8366304c871efc26057cc90370ccf58dad",
            )
            public = json.dumps(attestation.canonical_record(), sort_keys=True)
            self.assertNotIn(directory, public)
            self.assertNotIn(os.fspath(helper), public)
            self.assertNotIn(os.fspath(provenance), public)

    def test_tokenizer_helper_attestation_rejects_bytes_or_provenance_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "llama-tokenize"
            helper.write_bytes(b"pinned tokenizer")
            helper.chmod(0o755)
            wrong_artifact = write_tokenizer_provenance(
                helper,
                artifact_sha256=tagged(b"different helper"),
            )
            with self.assertRaisesRegex(V07RuntimeFactoryError, "artifact"):
                attest_v07_tokenizer_helper(
                    helper_path=helper,
                    provenance_path=wrong_artifact,
                )

            wrong_commit = write_tokenizer_provenance(
                helper,
                llama_cpp_commit="0" * 40,
            )
            with self.assertRaisesRegex(V07RuntimeFactoryError, "provenance"):
                attest_v07_tokenizer_helper(
                    helper_path=helper,
                    provenance_path=wrong_commit,
                )

    def test_generation_configs_are_exact_model_specific_and_receipt_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned runtime")
            binary.chmod(0o755)
            runtime = launch_runtime(binary)
            try:
                cases = (
                    (QWEN35_RAW_PROFILE, ("<|im_end|>",)),
                    (PHI4_MINI_RAW_PROFILE, ("<|end|>",)),
                )
                for profile, stop in cases:
                    with self.subTest(model=profile.model):
                        config = build_v07_generation_config(
                            model_id=profile.model,
                            runtime_receipt=runtime.receipt,
                        )
                        self.assertIs(type(config), OllamaGenerationConfig)
                        self.assertIs(config.profile, profile)
                        self.assertEqual(config.runtime_version, "0.31.1")
                        self.assertEqual(
                            config.runtime_binary_sha256,
                            tagged(binary.read_bytes()),
                        )
                        self.assertEqual(config.context_tokens, 4_096)
                        self.assertEqual(config.num_batch, 128)
                        self.assertEqual(config.num_gpu, 99)
                        self.assertEqual(config.main_gpu, 0)
                        self.assertIs(config.use_mmap, True)
                        self.assertEqual(config.num_thread, 8)
                        self.assertEqual(config.draft_num_predict, 0)
                        self.assertEqual(config.temperature, 0.2)
                        self.assertEqual(config.top_k, 40)
                        self.assertEqual(config.top_p, 0.9)
                        self.assertEqual(config.min_p, 0.0)
                        self.assertEqual(config.typical_p, 1.0)
                        self.assertEqual(config.repeat_last_n, 64)
                        self.assertEqual(config.repeat_penalty, 1.1)
                        self.assertEqual(config.presence_penalty, 0.0)
                        self.assertEqual(config.frequency_penalty, 0.0)
                        self.assertEqual(config.stop, stop)
                        self.assertEqual(config.keep_alive_seconds, -1)
                        self.assertEqual(config.request_timeout_seconds, 120.0)
                        self.assertEqual(config.endpoint, runtime.receipt.endpoint)
            finally:
                runtime.close()

    def test_generation_rejects_unknown_model_and_closed_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned runtime")
            binary.chmod(0o755)
            runtime = launch_runtime(binary)
            with self.assertRaisesRegex(V07RuntimeFactoryError, "model"):
                build_v07_generation_config(
                    model_id="gemma3:4b",
                    runtime_receipt=runtime.receipt,
                )
            runtime.close()
            with self.assertRaisesRegex(V07RuntimeFactoryError, "managed Ollama"):
                build_v07_generation_config(
                    model_id=QWEN35_RAW_PROFILE.model,
                    runtime_receipt=runtime.receipt,
                )

    def test_model_runtime_composes_exact_types_and_path_free_public_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ollama"
            helper = root / "llama-tokenize"
            model_artifact = root / "model.gguf"
            binary.write_bytes(b"pinned runtime")
            helper.write_bytes(b"pinned tokenizer")
            model_artifact.write_bytes(b"test model stand-in")
            binary.chmod(0o755)
            helper.chmod(0o755)
            runtime = launch_runtime(binary)
            tokenizer_attestation = attest_v07_tokenizer_helper(
                helper_path=helper,
                provenance_path=write_tokenizer_provenance(helper),
            )
            attestation = local_attestation(
                QWEN35_RAW_PROFILE,
                runtime.receipt.runtime_binary_sha256,
            )
            def verified_identity(
                path: Path, expected_sha256: str, *, executable: bool
            ) -> tuple[int, int, int, int, int]:
                del expected_sha256, executable
                metadata = path.lstat()
                return (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )

            try:
                with patch.object(
                    LlamaTokenizeCounter,
                    "_verify_artifact",
                    side_effect=verified_identity,
                ):
                    bundle = build_v07_model_runtime(
                        model_id=QWEN35_RAW_PROFILE.model,
                        attestation=attestation,
                        runtime_receipt=runtime.receipt,
                        tokenizer_attestation=tokenizer_attestation,
                        model_artifact_path=model_artifact,
                    )

                self.assertIs(type(bundle.tokenizer_pins), V07TokenizerPins)
                self.assertIs(type(bundle.token_counter), LlamaTokenizeCounter)
                self.assertIs(type(bundle.prompt_preparer), ExactPromptPreparer)
                self.assertIs(type(bundle.model), OllamaRawModel)
                self.assertIs(bundle.prompt_preparer.token_counter, bundle.token_counter)
                self.assertIs(bundle.prompt_preparer.renderer, QWEN35_RAW_PROFILE)
                self.assertIs(bundle.model.config, bundle.generation)
                self.assertEqual(
                    bundle.tokenizer_pins.model_artifact_sha256,
                    attestation.model_artifact_sha256,
                )
                public = json.dumps(bundle.public_record(), sort_keys=True)
                self.assertNotIn(directory, public)
                self.assertNotIn(os.fspath(helper), public)
                self.assertNotIn(os.fspath(model_artifact), public)

                original_transport = bundle.model._transport
                bundle.model._transport = lambda _payload: b"{}"
                with self.assertRaisesRegex(V07RuntimeFactoryError, "transport"):
                    bundle.require_live()
                bundle.model._transport = original_transport

                original_runner = bundle.token_counter._run_command
                bundle.token_counter._run_command = lambda *_args, **_kwargs: None
                with self.assertRaisesRegex(
                    V07RuntimeFactoryError,
                    "command runner",
                ):
                    bundle.require_live()
                bundle.token_counter._run_command = original_runner

                original_cache = bundle.token_counter._cache
                bundle.token_counter._cache = {}  # type: ignore[assignment]
                with self.assertRaisesRegex(V07RuntimeFactoryError, "cache"):
                    bundle.require_live()
                bundle.token_counter._cache = original_cache

                original_host = original_transport.host
                original_transport.host = "example.invalid"
                with self.assertRaisesRegex(V07RuntimeFactoryError, "transport"):
                    bundle.require_live()
                original_transport.host = original_host

                replacement_artifact = root / "replacement.gguf"
                replacement_artifact.write_bytes(model_artifact.read_bytes())
                original_model_path = bundle.token_counter.model_path
                original_model_identity = bundle.token_counter._model_identity
                bundle.token_counter.model_path = replacement_artifact.resolve()
                bundle.token_counter._model_identity = verified_identity(
                    replacement_artifact,
                    bundle.token_counter.model_sha256,
                    executable=False,
                )
                with self.assertRaisesRegex(
                    V07RuntimeFactoryError,
                    "artifact boundary",
                ):
                    bundle.require_live()
                bundle.token_counter.model_path = original_model_path
                bundle.token_counter._model_identity = original_model_identity

                bundle.token_counter.count = lambda _prompt: None  # type: ignore[method-assign]
                with self.assertRaisesRegex(
                    V07RuntimeFactoryError,
                    "execution surface",
                ):
                    bundle.require_live()
                del bundle.token_counter.count

                cached_prompt = b"cached prompt"
                cached_count = TokenCount(
                    count=2,
                    token_ids_sha256=tagged(b"[1,2]"),
                    tokenizer_artifact_sha256=bundle.token_counter.helper_sha256,
                    model_artifact_sha256=bundle.token_counter.model_sha256,
                )
                cache_key = tagged(cached_prompt)
                original_cache[cache_key] = (cached_prompt, cached_count)
                bundle.token_counter._cache_prompt_bytes = len(cached_prompt)
                bundle.require_live()
                bundle.token_counter._cache_prompt_bytes = 0
                with self.assertRaisesRegex(V07RuntimeFactoryError, "cache state"):
                    bundle.require_live()
                original_cache.clear()
                bundle.require_live()
            finally:
                runtime.close()
            with self.assertRaisesRegex(V07RuntimeFactoryError, "managed Ollama"):
                bundle.require_live()

    def test_residency_transition_unloads_then_loads_exact_model_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ollama"
            helper = root / "llama-tokenize"
            binary.write_bytes(b"pinned runtime")
            helper.write_bytes(b"pinned tokenizer")
            binary.chmod(0o755)
            helper.chmod(0o755)
            runtime = launch_runtime(binary)
            tokenizer_attestation = attest_v07_tokenizer_helper(
                helper_path=helper,
                provenance_path=write_tokenizer_provenance(helper),
            )
            try:
                qwen = self.build_bundle(
                    profile=QWEN35_RAW_PROFILE,
                    runtime=runtime,
                    root=root,
                    tokenizer_attestation=tokenizer_attestation,
                )
                phi = self.build_bundle(
                    profile=PHI4_MINI_RAW_PROFILE,
                    runtime=runtime,
                    root=root,
                    tokenizer_attestation=tokenizer_attestation,
                )
                fake_http = FakeResidencyHttp()
                with patch.object(
                    http_module._OLLAMA_HTTP_OPENER,
                    "open",
                    side_effect=fake_http.open,
                ):
                    boundary = issue_v07_managed_residency_boundary(
                        runtime.receipt
                    )
                    self.assertIs(type(boundary), V07ManagedResidencyBoundary)
                    first_receipt = transition_v07_model_residency(
                        previous=None,
                        target=qwen,
                        boundary=boundary,
                    )
                    self.assertIsNone(first_receipt.previous_model_id)
                    self.assertEqual(
                        fake_http.resident_models,
                        (qwen.expected_resident_model,),
                    )
                    fake_http.events.clear()

                    receipt = transition_v07_model_residency(
                        previous=qwen,
                        target=phi,
                        boundary=boundary,
                    )

                controls = [
                    payload
                    for kind, payload in fake_http.events
                    if kind == "control"
                ]
                self.assertEqual(
                    controls,
                    [
                        {
                            "keep_alive": 0,
                            "model": qwen.model_id,
                            "prompt": "",
                            "stream": False,
                        },
                        {
                            "keep_alive": -1,
                            "model": phi.model_id,
                            "prompt": "",
                            "stream": False,
                        },
                    ],
                )
                self.assertEqual(
                    fake_http.resident_models,
                    (phi.expected_resident_model,),
                )
                self.assertEqual(receipt.previous_model_id, qwen.model_id)
                self.assertEqual(receipt.target_model_id, phi.model_id)
                self.assertEqual(
                    receipt.target_model_artifact_sha256,
                    PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
                )
                self.assertNotIn(directory, json.dumps(receipt.canonical_record()))
            finally:
                runtime.close()

    def test_residency_transition_stops_on_unexpected_or_failed_unload_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ollama"
            helper = root / "llama-tokenize"
            binary.write_bytes(b"pinned runtime")
            helper.write_bytes(b"pinned tokenizer")
            binary.chmod(0o755)
            helper.chmod(0o755)
            runtime = launch_runtime(binary)
            tokenizer_attestation = attest_v07_tokenizer_helper(
                helper_path=helper,
                provenance_path=write_tokenizer_provenance(helper),
            )
            try:
                qwen = self.build_bundle(
                    profile=QWEN35_RAW_PROFILE,
                    runtime=runtime,
                    root=root,
                    tokenizer_attestation=tokenizer_attestation,
                )
                phi = self.build_bundle(
                    profile=PHI4_MINI_RAW_PROFILE,
                    runtime=runtime,
                    root=root,
                    tokenizer_attestation=tokenizer_attestation,
                )
                unexpected_http = FakeResidencyHttp()
                with patch.object(
                    http_module._OLLAMA_HTTP_OPENER,
                    "open",
                    side_effect=unexpected_http.open,
                ):
                    unexpected = issue_v07_managed_residency_boundary(
                        runtime.receipt
                    )
                    with self.assertRaisesRegex(
                        V07RuntimeFactoryError,
                        "initial residency",
                    ):
                        transition_v07_model_residency(
                            previous=qwen,
                            target=phi,
                            boundary=unexpected,
                        )
                self.assertFalse(
                    any(kind == "control" for kind, _payload in unexpected_http.events)
                )

                stuck_http = FakeResidencyHttp((qwen.expected_resident_model,))
                stuck_http.ignore_unload = True
                with patch.object(
                    http_module._OLLAMA_HTTP_OPENER,
                    "open",
                    side_effect=stuck_http.open,
                ):
                    stuck = issue_v07_managed_residency_boundary(runtime.receipt)
                    with self.assertRaisesRegex(V07RuntimeFactoryError, "unload"):
                        transition_v07_model_residency(
                            previous=qwen,
                            target=phi,
                            boundary=stuck,
                        )
                controls = [
                    payload
                    for kind, payload in stuck_http.events
                    if kind == "control"
                ]
                self.assertEqual(
                    controls,
                    [
                        {
                            "keep_alive": 0,
                            "model": qwen.model_id,
                            "prompt": "",
                            "stream": False,
                        }
                    ],
                )

                structural_fake = StructuralFakeResidencyBoundary(
                    (qwen.expected_resident_model,),
                    runtime.receipt.receipt_sha256,
                )
                with self.assertRaisesRegex(V07RuntimeFactoryError, "issued"):
                    transition_v07_model_residency(
                        previous=qwen,
                        target=phi,
                        boundary=structural_fake,  # type: ignore[arg-type]
                    )
                self.assertEqual(structural_fake.events, [])

                issued = issue_v07_managed_residency_boundary(runtime.receipt)
                forged = object.__new__(V07ManagedResidencyBoundary)
                object.__setattr__(
                    forged,
                    "_runtime_receipt",
                    issued._runtime_receipt,
                )
                object.__setattr__(forged, "_http_open", issued._http_open)
                with self.assertRaisesRegex(V07RuntimeFactoryError, "not issued"):
                    transition_v07_model_residency(
                        previous=qwen,
                        target=phi,
                        boundary=forged,
                    )

                issued._http_open = lambda _request, _timeout: None
                with self.assertRaisesRegex(V07RuntimeFactoryError, "replaced"):
                    transition_v07_model_residency(
                        previous=qwen,
                        target=phi,
                        boundary=issued,
                    )
            finally:
                runtime.close()

    def test_runtime_session_seals_two_models_host_and_live_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ollama"
            helper = root / "llama-tokenize"
            binary.write_bytes(b"pinned runtime")
            helper.write_bytes(b"pinned tokenizer")
            binary.chmod(0o755)
            helper.chmod(0o755)
            runtime = launch_runtime(binary)
            tokenizer_attestation = attest_v07_tokenizer_helper(
                helper_path=helper,
                provenance_path=write_tokenizer_provenance(helper),
            )
            qwen = self.build_bundle(
                profile=QWEN35_RAW_PROFILE,
                runtime=runtime,
                root=root,
                tokenizer_attestation=tokenizer_attestation,
            )
            phi = self.build_bundle(
                profile=PHI4_MINI_RAW_PROFILE,
                runtime=runtime,
                root=root,
                tokenizer_attestation=tokenizer_attestation,
            )
            docker_pins, docker_daemon = docker_fixture()
            host = build_v07_host_identity(
                docker_pins=docker_pins,
                docker_daemon=docker_daemon,
                runtime_receipt=runtime.receipt,
            )
            session = build_v07_runtime_session(
                models=(phi, qwen),
                host_identity=host,
            )
            try:
                self.assertIs(type(session), V07RuntimeSession)
                self.assertEqual(
                    session.model_ids,
                    (QWEN35_RAW_PROFILE.model, PHI4_MINI_RAW_PROFILE.model),
                )
                self.assertIs(
                    session.model_runtime(QWEN35_RAW_PROFILE.model),
                    qwen,
                )
                self.assertIs(
                    session.model_runtime(PHI4_MINI_RAW_PROFILE.model),
                    phi,
                )
                session.require_live()
                public = session.canonical_record()
                self.assertEqual(public["session_sha256"], session.session_sha256)
                encoded = json.dumps(public, sort_keys=True)
                self.assertNotIn(directory, encoded)
                self.assertIn("managed_runtime", public)
                self.assertIn("host_identity", public)
                self.assertEqual(len(public["models"]), 2)
                with self.assertRaisesRegex(V07RuntimeFactoryError, "model"):
                    session.model_runtime("gemma3:4b")
            finally:
                runtime.close()
            with self.assertRaisesRegex(V07RuntimeFactoryError, "managed Ollama"):
                session.require_live()

    def test_runtime_session_rejects_incomplete_models_or_host_runtime_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ollama"
            helper = root / "llama-tokenize"
            binary.write_bytes(b"pinned runtime")
            helper.write_bytes(b"pinned tokenizer")
            binary.chmod(0o755)
            helper.chmod(0o755)
            runtime = launch_runtime(binary)
            tokenizer_attestation = attest_v07_tokenizer_helper(
                helper_path=helper,
                provenance_path=write_tokenizer_provenance(helper),
            )
            qwen = self.build_bundle(
                profile=QWEN35_RAW_PROFILE,
                runtime=runtime,
                root=root,
                tokenizer_attestation=tokenizer_attestation,
            )
            phi = self.build_bundle(
                profile=PHI4_MINI_RAW_PROFILE,
                runtime=runtime,
                root=root,
                tokenizer_attestation=tokenizer_attestation,
            )
            docker_pins, docker_daemon = docker_fixture()
            host = build_v07_host_identity(
                docker_pins=docker_pins,
                docker_daemon=docker_daemon,
                runtime_receipt=runtime.receipt,
            )
            try:
                with self.assertRaisesRegex(V07RuntimeFactoryError, "two-model"):
                    build_v07_runtime_session(models=(qwen,), host_identity=host)
                with self.assertRaisesRegex(V07RuntimeFactoryError, "two-model"):
                    build_v07_runtime_session(
                        models=(qwen, qwen),
                        host_identity=host,
                    )
                drifted_host = dataclasses.replace(
                    host,
                    ollama_runtime_binary_sha256=tagged(b"other runtime"),
                )
                with self.assertRaisesRegex(V07RuntimeFactoryError, "host"):
                    build_v07_runtime_session(
                        models=(qwen, phi),
                        host_identity=drifted_host,
                    )
            finally:
                runtime.close()

    def test_model_runtime_rejects_cross_model_and_runtime_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ollama"
            helper = root / "llama-tokenize"
            model_artifact = root / "model.gguf"
            for path, payload in (
                (binary, b"pinned runtime"),
                (helper, b"pinned tokenizer"),
                (model_artifact, b"test model stand-in"),
            ):
                path.write_bytes(payload)
            binary.chmod(0o755)
            helper.chmod(0o755)
            runtime = launch_runtime(binary)
            tokenizer_attestation = attest_v07_tokenizer_helper(
                helper_path=helper,
                provenance_path=write_tokenizer_provenance(helper),
            )
            try:
                cross_model = local_attestation(
                    PHI4_MINI_RAW_PROFILE,
                    runtime.receipt.runtime_binary_sha256,
                )
                with self.assertRaisesRegex(V07RuntimeFactoryError, "model"):
                    build_v07_model_runtime(
                        model_id=QWEN35_RAW_PROFILE.model,
                        attestation=cross_model,
                        runtime_receipt=runtime.receipt,
                        tokenizer_attestation=tokenizer_attestation,
                        model_artifact_path=model_artifact,
                    )

                wrong_runtime = local_attestation(
                    QWEN35_RAW_PROFILE,
                    tagged(b"other runtime"),
                )
                with self.assertRaisesRegex(V07RuntimeFactoryError, "runtime"):
                    build_v07_model_runtime(
                        model_id=QWEN35_RAW_PROFILE.model,
                        attestation=wrong_runtime,
                        runtime_receipt=runtime.receipt,
                        tokenizer_attestation=tokenizer_attestation,
                        model_artifact_path=model_artifact,
                    )
            finally:
                runtime.close()

    def test_model_runtime_rejects_helper_and_model_artifact_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "ollama"
            helper = root / "llama-tokenize"
            model_artifact = root / "model.gguf"
            binary.write_bytes(b"pinned runtime")
            helper.write_bytes(b"pinned tokenizer")
            model_artifact.write_bytes(b"not the attested GGUF")
            binary.chmod(0o755)
            helper.chmod(0o755)
            runtime = launch_runtime(binary)
            tokenizer_attestation = attest_v07_tokenizer_helper(
                helper_path=helper,
                provenance_path=write_tokenizer_provenance(helper),
            )
            attestation = local_attestation(
                QWEN35_RAW_PROFILE,
                runtime.receipt.runtime_binary_sha256,
            )
            try:
                helper.write_bytes(b"changed after attestation")
                with self.assertRaisesRegex(V07RuntimeFactoryError, "artifact"):
                    build_v07_model_runtime(
                        model_id=QWEN35_RAW_PROFILE.model,
                        attestation=attestation,
                        runtime_receipt=runtime.receipt,
                        tokenizer_attestation=tokenizer_attestation,
                        model_artifact_path=model_artifact,
                    )
                helper.write_bytes(b"pinned tokenizer")
                with self.assertRaisesRegex(V07RuntimeFactoryError, "artifact"):
                    build_v07_model_runtime(
                        model_id=QWEN35_RAW_PROFILE.model,
                        attestation=attestation,
                        runtime_receipt=runtime.receipt,
                        tokenizer_attestation=tokenizer_attestation,
                        model_artifact_path=model_artifact,
                    )
            finally:
                runtime.close()

    def test_host_identity_uses_exact_docker_observation_and_live_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned runtime")
            binary.chmod(0o755)
            runtime = launch_runtime(binary)
            pins, daemon = docker_fixture()
            try:
                identity = build_v07_host_identity(
                    docker_pins=pins,
                    docker_daemon=daemon,
                    runtime_receipt=runtime.receipt,
                )
                self.assertIs(type(identity), V07HostIdentityPins)
                self.assertEqual(identity.docker_binary_sha256, pins.binary_sha256)
                self.assertEqual(identity.docker_endpoint_sha256, pins.endpoint_sha256)
                self.assertEqual(
                    identity.ollama_runtime_binary_sha256,
                    runtime.receipt.runtime_binary_sha256,
                )
                self.assertNotIn(directory, json.dumps(identity.canonical_record()))
            finally:
                runtime.close()

    def test_host_identity_rejects_docker_drift_and_closed_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned runtime")
            binary.chmod(0o755)
            runtime = launch_runtime(binary)
            pins, daemon = docker_fixture()
            drifted = dataclasses.replace(
                daemon,
                server_version="28.3.3",
            )
            with self.assertRaisesRegex(V07RuntimeFactoryError, "Docker"):
                build_v07_host_identity(
                    docker_pins=pins,
                    docker_daemon=drifted,
                    runtime_receipt=runtime.receipt,
                )
            runtime.close()
            with self.assertRaisesRegex(V07RuntimeFactoryError, "managed Ollama"):
                build_v07_host_identity(
                    docker_pins=pins,
                    docker_daemon=daemon,
                    runtime_receipt=runtime.receipt,
                )


if __name__ == "__main__":
    unittest.main()
