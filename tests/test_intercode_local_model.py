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
from edgeloopbench.model_adapter import QWEN35_RAW_PROFILE


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

    def attest(self, runner: FakeRunner):
        return attest_local_ollama_model(
            profile=self.profile,
            models_root=self.models,
            runtime_binary=self.binary,
            runtime_version="0.31.1",
            runtime_binary_sha256=tagged(self.binary.read_bytes()),
            kv_cache_quantization="q8_0",
            runner=runner,
        )


class LocalModelAttestationTests(unittest.TestCase):
    def test_attests_manifest_config_blob_runtime_and_separate_quantization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LocalModelFixture(Path(directory))
            runner = FakeRunner((0, b"ollama version is 0.31.1\n", b""))
            attestation = fixture.attest(runner)

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
                with self.assertRaises(LocalModelAttestationError):
                    fixture.attest(FakeRunner((0, b"ollama version is 0.31.1\n", b"")))

    def test_symlinked_manifest_blob_or_runtime_is_rejected(self) -> None:
        for target in ("manifest", "model", "runtime"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                fixture = LocalModelFixture(Path(directory))
                if target == "manifest":
                    path = fixture.manifest
                elif target == "model":
                    path = fixture.blobs / fixture.model_digest.replace(":", "-")
                else:
                    path = fixture.binary
                replacement = path.with_name(path.name + ".real")
                path.rename(replacement)
                path.symlink_to(replacement)
                with self.assertRaises(LocalModelAttestationError):
                    fixture.attest(FakeRunner((0, b"ollama version is 0.31.1\n", b"")))

    def test_runtime_is_rechecked_after_the_version_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LocalModelFixture(Path(directory))
            runner = FakeRunner((0, b"ollama version is 0.31.1\n", b""))
            runner.before_return = lambda: fixture.binary.write_bytes(b"changed runtime")
            with self.assertRaises(LocalModelAttestationError):
                fixture.attest(runner)

    def test_runtime_probe_is_bounded_exact_and_fail_closed(self) -> None:
        responses = (
            (1, b"", b"failed"),
            (0, b"ollama version is 0.31.2\n", b""),
            (0, b"x" * 65_537, b""),
            (0, b"ollama version is 0.31.1\n", b"warning"),
        )
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                fixture = LocalModelFixture(Path(directory))
                with self.assertRaises(LocalModelAttestationError):
                    fixture.attest(FakeRunner(response))

    def test_only_safe_registry_tags_and_quantization_labels_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = LocalModelFixture(Path(directory))
            runner = FakeRunner((0, b"ollama version is 0.31.1\n", b""))
            unsafe = dataclasses.replace(fixture.profile, model="../escape:4b")
            with self.assertRaises(LocalModelAttestationError):
                attest_local_ollama_model(
                    profile=unsafe,
                    models_root=fixture.models,
                    runtime_binary=fixture.binary,
                    runtime_version="0.31.1",
                    runtime_binary_sha256=tagged(fixture.binary.read_bytes()),
                    kv_cache_quantization="q8_0",
                    runner=runner,
                )
            with self.assertRaises(LocalModelAttestationError):
                attest_local_ollama_model(
                    profile=fixture.profile,
                    models_root=fixture.models,
                    runtime_binary=fixture.binary,
                    runtime_version="0.31.1",
                    runtime_binary_sha256=tagged(fixture.binary.read_bytes()),
                    kv_cache_quantization="q8 0",
                    runner=runner,
                )


if __name__ == "__main__":
    unittest.main()
