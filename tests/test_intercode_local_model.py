from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.intercode_local_model import (
    LocalModelAttestationError,
    attest_local_ollama_model,
)
from edgeloopbench.intercode_managed_ollama import (
    OllamaEndpointObservation,
    launch_managed_v07_ollama,
)
from edgeloopbench.model_adapter import QWEN35_RAW_PROFILE


NO_SERVER_VERSION_OUTPUT = (
    b"Warning: could not connect to a running Ollama instance\n"
    b"Warning: client version is 0.31.1\n"
)


def tagged(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class FakeRunner:
    def __init__(self, response: tuple[int, bytes, bytes]) -> None:
        self.response = response
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.before_return = None

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((tuple(argv), dict(kwargs)))
        if self.before_return is not None:
            self.before_return()
        returncode, stdout, stderr = self.response
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


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
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> OllamaEndpointObservation:
        self.calls += 1
        if self.calls == 1:
            return OllamaEndpointObservation(False, None, ())
        return OllamaEndpointObservation(True, "0.31.1", (4242,))


class LocalModelFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.models = root / "models"
        self.blobs = self.models / "blobs"
        self.manifest = (
            self.models
            / "manifests"
            / "registry.ollama.ai"
            / "library"
            / "qwen3.5"
            / "4b"
        )
        self.blobs.mkdir(parents=True)
        self.manifest.parent.mkdir(parents=True)

        self.model_bytes = b"small fake GGUF bytes"
        self.model_digest = tagged(self.model_bytes)
        self.config_bytes = json.dumps(
            {
                "architecture": "arm64",
                "file_type": "Q4_K_M",
                "model_family": "qwen35",
                "model_families": ["qwen35"],
                "model_format": "gguf",
                "model_type": "4.7B",
                "os": "linux",
                "rootfs": {
                    "diff_ids": [self.model_digest],
                    "type": "layers",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.config_digest = tagged(self.config_bytes)
        manifest_record = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "digest": self.config_digest,
                "size": len(self.config_bytes),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.ollama.image.model",
                    "digest": self.model_digest,
                    "size": len(self.model_bytes),
                }
            ],
        }
        self.manifest_bytes = json.dumps(
            manifest_record, sort_keys=True, separators=(",", ":")
        ).encode()
        self.manifest.write_bytes(self.manifest_bytes)
        (self.blobs / self.config_digest.replace(":", "-")).write_bytes(
            self.config_bytes
        )
        (self.blobs / self.model_digest.replace(":", "-")).write_bytes(
            self.model_bytes
        )

        self.binary = root / "ollama"
        self.binary.write_bytes(b"fake pinned runtime")
        self.binary.chmod(0o755)
        self.profile = dataclasses.replace(
            QWEN35_RAW_PROFILE,
            model_manifest_sha256=tagged(self.manifest_bytes),
            model_artifact_sha256=self.model_digest,
        )

    def launch_runtime(self, runner: FakeRunner | None = None):  # type: ignore[no-untyped-def]
        return launch_managed_v07_ollama(
            runtime_binary=self.binary,
            expected_runtime_binary_sha256=tagged(self.binary.read_bytes()),
            inherited_environment={"HOME": "/Users/tester"},
            version_runner=runner
            or FakeRunner((0, NO_SERVER_VERSION_OUTPUT, b"")),
            process_launcher=FakeLauncher(),
            endpoint_inspector=FakeEndpointInspector(),
            sleeper=lambda _seconds: None,
        )

    def attest(self, runtime):  # type: ignore[no-untyped-def]
        return attest_local_ollama_model(
            profile=self.profile,
            models_root=self.models,
            runtime_binary=self.binary,
            runtime_receipt=runtime.receipt,
        )


class LocalModelAttestationTests(unittest.TestCase):
    def test_attests_manifest_config_blob_runtime_and_separate_quantization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LocalModelFixture(Path(directory))
            runner = FakeRunner((0, NO_SERVER_VERSION_OUTPUT, b""))
            runtime = fixture.launch_runtime(runner)
            attestation = fixture.attest(runtime)
            runtime.close()

        self.assertEqual(attestation.model, "qwen3.5:4b")
        self.assertEqual(attestation.model_manifest_sha256, tagged(fixture.manifest_bytes))
        self.assertEqual(attestation.model_artifact_sha256, fixture.model_digest)
        self.assertEqual(attestation.model_artifact_size_bytes, len(fixture.model_bytes))
        self.assertEqual(attestation.weight_quantization, "Q4_K_M")
        self.assertEqual(attestation.kv_cache_quantization, "q8_0")
        self.assertNotEqual(
            attestation.weight_quantization, attestation.kv_cache_quantization
        )
        record = attestation.canonical_record()
        self.assertEqual(record["attestation_sha256"], attestation.attestation_sha256)
        self.assertNotIn(directory, json.dumps(record, sort_keys=True))

        self.assertEqual(runner.calls[0][0], (os.fspath(fixture.binary), "--version"))
        kwargs = runner.calls[0][1]
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["capture_output"], True)
        self.assertIs(kwargs["check"], False)
        self.assertEqual(kwargs["timeout"], 5.0)

    def test_manifest_config_and_model_bytes_are_all_independently_verified(self) -> None:
        mutations = ("manifest", "config", "model")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture = LocalModelFixture(Path(directory))
                if mutation == "manifest":
                    fixture.manifest.write_bytes(fixture.manifest_bytes + b"\n")
                elif mutation == "config":
                    path = fixture.blobs / fixture.config_digest.replace(":", "-")
                    path.write_bytes(fixture.config_bytes + b"x")
                else:
                    path = fixture.blobs / fixture.model_digest.replace(":", "-")
                    path.write_bytes(fixture.model_bytes + b"x")
                runtime = fixture.launch_runtime()
                with self.assertRaises(LocalModelAttestationError):
                    fixture.attest(runtime)
                runtime.close()

    def test_symlinked_manifest_blob_or_runtime_is_rejected(self) -> None:
        for target in ("manifest", "model"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                fixture = LocalModelFixture(Path(directory))
                if target == "manifest":
                    path = fixture.manifest
                elif target == "model":
                    path = fixture.blobs / fixture.model_digest.replace(":", "-")
                replacement = path.with_name(path.name + ".real")
                path.rename(replacement)
                path.symlink_to(replacement)
                runtime = fixture.launch_runtime()
                with self.assertRaises(LocalModelAttestationError):
                    fixture.attest(runtime)
                runtime.close()

    def test_runtime_is_rechecked_against_the_live_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LocalModelFixture(Path(directory))
            runtime = fixture.launch_runtime()
            fixture.binary.write_bytes(b"changed runtime")
            with self.assertRaises(LocalModelAttestationError):
                fixture.attest(runtime)
            runtime.close()

    def test_closed_runtime_receipt_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LocalModelFixture(Path(directory))
            runtime = fixture.launch_runtime()
            runtime.close()
            with self.assertRaises(LocalModelAttestationError):
                fixture.attest(runtime)

    def test_only_safe_registry_tags_and_quantization_labels_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LocalModelFixture(Path(directory))
            runtime = fixture.launch_runtime()
            unsafe = dataclasses.replace(fixture.profile, model="../escape:4b")
            with self.assertRaises(LocalModelAttestationError):
                attest_local_ollama_model(
                    profile=unsafe,
                    models_root=fixture.models,
                    runtime_binary=fixture.binary,
                    runtime_receipt=runtime.receipt,
                )
            runtime.close()


if __name__ == "__main__":
    unittest.main()
