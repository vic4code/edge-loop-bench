"""Read-only local Ollama model and runtime attestation for v0.7.

Only the two preregistered small-model tags are addressable.  The verifier
hashes the manifest, config, model blob, and runtime through non-symlink file
descriptors, rechecks identity after reading, and publishes no local path.
Weight and KV-cache quantization remain separate fields by construction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import InitVar, dataclass
from pathlib import Path

from .intercode_managed_ollama import (
    OLLAMA_GENERATION_ENDPOINT_SHA256,
    OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
    ManagedOllamaRuntimeError,
    ManagedOllamaRuntimeReceipt,
    require_live_managed_ollama_receipt,
)
from .model_adapter import RestrictedRawRenderingProfile


_ALLOWED_MODEL_PATHS = {
    "qwen3.5:4b": ("qwen3.5", "4b"),
    "phi4-mini:3.8b": ("phi4-mini", "3.8b"),
}
_TAGGED_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_QUANTIZATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_RUNTIME_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_MAX_MANIFEST_BYTES = 1 << 20
_MAX_CONFIG_BYTES = 1 << 20
_MAX_RUNTIME_BYTES = 256 << 20
_MAX_MODEL_BYTES = 4 << 30
_ATTESTATION_SEAL = object()


class LocalModelAttestationError(RuntimeError):
    """A local artifact differs from the preregistered identity."""


class _DuplicateKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _FileEvidence:
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class LocalModelAttestation:
    model: str
    renderer_profile_sha256: str
    model_manifest_sha256: str
    model_config_sha256: str
    model_artifact_sha256: str
    model_artifact_size_bytes: int
    model_family: str
    model_parameter_label: str
    weight_quantization: str
    kv_cache_quantization: str
    runtime_version: str
    runtime_binary_sha256: str
    attestation_sha256: str
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _ATTESTATION_SEAL:
            raise LocalModelAttestationError(
                "local model attestations are verifier-sealed"
            )
        _validate_attestation(self)

    def _core_record(self) -> dict[str, object]:
        return {
            "schema": "edgeloopbench.local-ollama-model-attestation.v1",
            "model": self.model,
            "renderer_profile_sha256": self.renderer_profile_sha256,
            "model_manifest_sha256": self.model_manifest_sha256,
            "model_config_sha256": self.model_config_sha256,
            "model_artifact_sha256": self.model_artifact_sha256,
            "model_artifact_size_bytes": self.model_artifact_size_bytes,
            "model_family": self.model_family,
            "model_parameter_label": self.model_parameter_label,
            "weight_quantization": self.weight_quantization,
            "kv_cache_quantization": self.kv_cache_quantization,
            "runtime_version": self.runtime_version,
            "runtime_binary_sha256": self.runtime_binary_sha256,
        }

    def canonical_record(self) -> dict[str, object]:
        _validate_attestation(self)
        record = self._core_record()
        record["attestation_sha256"] = self.attestation_sha256
        return record


def attest_local_ollama_model(
    *,
    profile: RestrictedRawRenderingProfile,
    models_root: Path,
    runtime_binary: Path,
    runtime_receipt: ManagedOllamaRuntimeReceipt,
) -> LocalModelAttestation:
    """Attest one exact local small model without loading or generating."""

    try:
        receipt = require_live_managed_ollama_receipt(runtime_receipt)
    except ManagedOllamaRuntimeError as error:
        raise LocalModelAttestationError(
            "managed Ollama runtime receipt is invalid"
        ) from error
    if type(profile) is not RestrictedRawRenderingProfile:
        raise LocalModelAttestationError("rendering profile type is invalid")
    model_path = _ALLOWED_MODEL_PATHS.get(profile.model)
    if model_path is None:
        raise LocalModelAttestationError(
            "model tag is outside the preregistered small-model set"
        )
    _require_absolute_directory(models_root, "Ollama models root")
    if not isinstance(runtime_binary, Path) or not runtime_binary.is_absolute():
        raise LocalModelAttestationError("runtime binary must be an absolute Path")

    manifest_path = (
        models_root
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / model_path[0]
        / model_path[1]
    )
    manifest_bytes, manifest_evidence = _read_small_file(
        manifest_path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
        label="model manifest",
    )
    if manifest_evidence.sha256 != profile.model_manifest_sha256:
        raise LocalModelAttestationError("model manifest SHA-256 differs from profile")
    manifest = _parse_json_object(manifest_bytes, "model manifest")
    config_descriptor, layers = _validate_manifest_shape(manifest)

    config_digest = _require_digest(
        config_descriptor.get("digest"), "model config digest"
    )
    config_size = _require_nonnegative_int(
        config_descriptor.get("size"), "model config size"
    )
    config_path = models_root / "blobs" / config_digest.replace(":", "-")
    config_bytes, config_evidence = _read_small_file(
        config_path,
        maximum_bytes=_MAX_CONFIG_BYTES,
        label="model config",
    )
    if (
        config_evidence.sha256 != config_digest
        or config_evidence.size_bytes != config_size
    ):
        raise LocalModelAttestationError("model config identity differs from manifest")
    config = _parse_json_object(config_bytes, "model config")

    model_layers = [
        layer
        for layer in layers
        if layer.get("mediaType") == "application/vnd.ollama.image.model"
    ]
    if len(model_layers) != 1:
        raise LocalModelAttestationError("manifest must contain one model layer")
    model_layer = model_layers[0]
    model_digest = _require_digest(
        model_layer.get("digest"), "model artifact digest"
    )
    model_size = _require_nonnegative_int(
        model_layer.get("size"), "model artifact size"
    )
    if model_digest != profile.model_artifact_sha256:
        raise LocalModelAttestationError("model artifact differs from profile")
    if model_size <= 0 or model_size > _MAX_MODEL_BYTES:
        raise LocalModelAttestationError("model artifact exceeds the small-model bound")
    model_evidence = _hash_file(
        models_root / "blobs" / model_digest.replace(":", "-"),
        maximum_bytes=_MAX_MODEL_BYTES,
        label="model artifact",
    )
    if model_evidence.sha256 != model_digest or model_evidence.size_bytes != model_size:
        raise LocalModelAttestationError("model artifact identity differs from manifest")

    model_family, parameter_label, weight_quantization = _validate_config(
        config,
        layers=layers,
    )
    runtime_evidence = _hash_file(
        runtime_binary,
        maximum_bytes=_MAX_RUNTIME_BYTES,
        label="Ollama runtime",
        require_executable=True,
    )
    if runtime_evidence.sha256 != receipt.runtime_binary_sha256:
        raise LocalModelAttestationError("Ollama runtime SHA-256 differs from receipt")
    after_probe = _hash_file(
        runtime_binary,
        maximum_bytes=_MAX_RUNTIME_BYTES,
        label="Ollama runtime",
        require_executable=True,
    )
    if after_probe != runtime_evidence:
        raise LocalModelAttestationError("Ollama runtime changed during attestation")
    try:
        require_live_managed_ollama_receipt(receipt)
    except ManagedOllamaRuntimeError as error:
        raise LocalModelAttestationError(
            "managed Ollama runtime changed during model attestation"
        ) from error

    values: dict[str, object] = {
        "model": profile.model,
        "renderer_profile_sha256": profile.sha256,
        "model_manifest_sha256": manifest_evidence.sha256,
        "model_config_sha256": config_evidence.sha256,
        "model_artifact_sha256": model_evidence.sha256,
        "model_artifact_size_bytes": model_evidence.size_bytes,
        "model_family": model_family,
        "model_parameter_label": parameter_label,
        "weight_quantization": weight_quantization,
        "kv_cache_quantization": receipt.kv_cache_quantization,
        "runtime_version": receipt.runtime_version,
        "runtime_binary_sha256": runtime_evidence.sha256,
    }
    provisional = _attestation_core_record(values)
    attestation_sha256 = _digest(_canonical_json(provisional))
    return LocalModelAttestation(
        **values,  # type: ignore[arg-type]
        attestation_sha256=attestation_sha256,
        _seal=_ATTESTATION_SEAL,
    )


def _validate_manifest_shape(
    manifest: dict[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if set(manifest) != {
        "schemaVersion",
        "mediaType",
        "config",
        "layers",
    }:
        raise LocalModelAttestationError("model manifest fields are invalid")
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType")
        != "application/vnd.docker.distribution.manifest.v2+json"
    ):
        raise LocalModelAttestationError("model manifest schema is invalid")
    config = manifest.get("config")
    layers = manifest.get("layers")
    if not isinstance(config, dict) or set(config) != {"mediaType", "digest", "size"}:
        raise LocalModelAttestationError("model config descriptor is invalid")
    if config.get("mediaType") != "application/vnd.docker.container.image.v1+json":
        raise LocalModelAttestationError("model config media type is invalid")
    if (
        not isinstance(layers, list)
        or not layers
        or len(layers) > 16
        or any(not isinstance(layer, dict) for layer in layers)
    ):
        raise LocalModelAttestationError("model layer list is invalid")
    typed_layers: list[dict[str, object]] = []
    for layer in layers:
        assert isinstance(layer, dict)
        if set(layer) != {"mediaType", "digest", "size"}:
            raise LocalModelAttestationError("model layer descriptor is invalid")
        if not isinstance(layer.get("mediaType"), str):
            raise LocalModelAttestationError("model layer media type is invalid")
        _require_digest(layer.get("digest"), "model layer digest")
        _require_nonnegative_int(layer.get("size"), "model layer size")
        typed_layers.append(layer)
    if len({layer["digest"] for layer in typed_layers}) != len(typed_layers):
        raise LocalModelAttestationError("model layer digests are not unique")
    return config, typed_layers


def _validate_config(
    config: dict[str, object],
    *,
    layers: list[dict[str, object]],
) -> tuple[str, str, str]:
    required = {
        "model_format",
        "model_family",
        "model_families",
        "model_type",
        "file_type",
        "architecture",
        "os",
        "rootfs",
    }
    if not required <= set(config):
        raise LocalModelAttestationError("model config fields are incomplete")
    family = config.get("model_family")
    families = config.get("model_families")
    parameter_label = config.get("model_type")
    weight_quantization = config.get("file_type")
    if (
        config.get("model_format") != "gguf"
        or not isinstance(family, str)
        or not family
        or families != [family]
        or not isinstance(parameter_label, str)
        or not parameter_label
        or not isinstance(weight_quantization, str)
        or not _QUANTIZATION.fullmatch(weight_quantization)
    ):
        raise LocalModelAttestationError("model config metadata is invalid")
    rootfs = config.get("rootfs")
    if not isinstance(rootfs, dict) or set(rootfs) != {"type", "diff_ids"}:
        raise LocalModelAttestationError("model config rootfs is invalid")
    if rootfs.get("type") != "layers" or rootfs.get("diff_ids") != [
        layer["digest"] for layer in layers
    ]:
        raise LocalModelAttestationError("model config layer binding is invalid")
    return family, parameter_label, weight_quantization


def _read_small_file(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[bytes, _FileEvidence]:
    descriptor, before = _open_attested_file(
        path,
        maximum_bytes=maximum_bytes,
        label=label,
    )
    try:
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                raise LocalModelAttestationError(f"{label} ended unexpectedly")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise LocalModelAttestationError(f"{label} grew while reading")
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        _revalidate_file(path, before, after, label)
    finally:
        os.close(descriptor)
    return payload, _FileEvidence(_digest(payload), len(payload))


def _hash_file(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
    require_executable: bool = False,
) -> _FileEvidence:
    descriptor, before = _open_attested_file(
        path,
        maximum_bytes=maximum_bytes,
        label=label,
        require_executable=require_executable,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum_bytes:
                raise LocalModelAttestationError(f"{label} exceeds its size bound")
            digest.update(chunk)
        after = os.fstat(descriptor)
        _revalidate_file(path, before, after, label)
    finally:
        os.close(descriptor)
    if size != before.st_size:
        raise LocalModelAttestationError(f"{label} size changed while hashing")
    return _FileEvidence("sha256:" + digest.hexdigest(), size)


def _open_attested_file(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
    require_executable: bool = False,
) -> tuple[int, os.stat_result]:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise LocalModelAttestationError(f"{label} path is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise LocalModelAttestationError(f"{label} is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size < 0
            or metadata.st_size > maximum_bytes
            or metadata.st_mode & stat.S_IWOTH
            or (require_executable and not metadata.st_mode & stat.S_IXUSR)
        ):
            raise LocalModelAttestationError(f"{label} file identity is unsafe")
        link = os.stat(path, follow_symlinks=False)
        if (link.st_dev, link.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise LocalModelAttestationError(f"{label} link identity changed")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _revalidate_file(
    path: Path,
    before: os.stat_result,
    after: os.stat_result,
    label: str,
) -> None:
    identity_fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
        raise LocalModelAttestationError(f"{label} changed while reading")
    try:
        link = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise LocalModelAttestationError(f"{label} link disappeared") from error
    if (link.st_dev, link.st_ino) != (after.st_dev, after.st_ino):
        raise LocalModelAttestationError(f"{label} link identity changed")


def _require_absolute_directory(path: Path, label: str) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path.is_symlink()
        or not path.is_dir()
    ):
        raise LocalModelAttestationError(f"{label} must be an absolute directory")


def _parse_json_object(payload: bytes, label: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKey(key)
            result[key] = value
        return result

    try:
        parsed = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey) as error:
        raise LocalModelAttestationError(f"{label} JSON is invalid") from error
    if not isinstance(parsed, dict):
        raise LocalModelAttestationError(f"{label} must be a JSON object")
    return parsed


def _validate_attestation(value: LocalModelAttestation) -> None:
    if value.model not in _ALLOWED_MODEL_PATHS:
        raise LocalModelAttestationError("attested model tag is invalid")
    for field in (
        "renderer_profile_sha256",
        "model_manifest_sha256",
        "model_config_sha256",
        "model_artifact_sha256",
        "runtime_binary_sha256",
        "attestation_sha256",
    ):
        _require_digest(getattr(value, field), field)
    if (
        type(value.model_artifact_size_bytes) is not int
        or not 0 < value.model_artifact_size_bytes <= _MAX_MODEL_BYTES
    ):
        raise LocalModelAttestationError("attested model size is invalid")
    for field in ("model_family", "model_parameter_label"):
        item = getattr(value, field)
        if not isinstance(item, str) or not item or len(item.encode("utf-8")) > 64:
            raise LocalModelAttestationError(f"{field} is invalid")
    for field in ("weight_quantization", "kv_cache_quantization"):
        item = getattr(value, field)
        if not isinstance(item, str) or not _QUANTIZATION.fullmatch(item):
            raise LocalModelAttestationError(f"{field} is invalid")
    if not _RUNTIME_VERSION.fullmatch(value.runtime_version):
        raise LocalModelAttestationError("attested runtime version is invalid")
    expected = _digest(_canonical_json(value._core_record()))
    if value.attestation_sha256 != expected:
        raise LocalModelAttestationError("attestation digest is invalid")


def _attestation_core_record(values: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "edgeloopbench.local-ollama-model-attestation.v1",
        **values,
    }


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _TAGGED_DIGEST.fullmatch(value):
        raise LocalModelAttestationError(f"{field} is invalid")
    return value


def _require_nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise LocalModelAttestationError(f"{field} is invalid")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = (
    "LocalModelAttestation",
    "LocalModelAttestationError",
    "OLLAMA_GENERATION_ENDPOINT_SHA256",
    "OLLAMA_LAUNCH_ENVIRONMENT_SHA256",
    "attest_local_ollama_model",
)
