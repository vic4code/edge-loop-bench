"""Fail-closed production component assembly for the v0.7 local study.

Construction performs no Docker, Ollama generation, or network operation.  An
explicit residency transition may use only the issuer-registered fixed Ollama
loopback boundary.  The factory cross-checks trusted runtime, model, tokenizer,
and host evidence before composing the controller's exact execution objects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import threading
import urllib.error
import urllib.request
import weakref
from collections import OrderedDict
from dataclasses import InitVar, dataclass, field
from enum import Enum
from pathlib import Path

from .intercode_host_safety import (
    DockerDaemonIdentity,
    DockerTelemetryPins,
    ResidentModel,
)
from .intercode_local_model import LocalModelAttestation
from .intercode_managed_ollama import (
    ManagedOllamaRuntimeError,
    ManagedOllamaRuntimeReceipt,
    require_live_managed_ollama_receipt,
)
from .intercode_v07_manifest import (
    V07_LLAMA_CPP_COMMIT,
    V07_TOKENIZER_POLICY_REVISION,
    V07HostIdentityPins,
    V07ModelIdentityPins,
    V07TokenizerPins,
    bind_v07_model_identity,
)
from .ollama_loopback_http import (
    OLLAMA_GENERATE_URL,
    OLLAMA_PS_URL,
    OllamaLoopbackHttpError,
    open_ollama_http,
    parse_strict_json_object,
    require_exact_ollama_response,
)
from .model_adapter import (
    ExactPromptPreparer,
    LlamaTokenizeCounter,
    OllamaGenerationConfig,
    OllamaRawModel,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
    RestrictedRawRenderingProfile,
    TokenCount,
    _LoopbackOllamaTransport,
)


V07_RUNTIME_FACTORY_REVISION = "intercode-v0.7-production-runtime-factory-v2"

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONSTRUCTION_SEAL = object()
_TOKENIZER_ATTESTATION_SEAL = object()
_RESIDENCY_RECEIPT_SEAL = object()
_SESSION_SEAL = object()
_RESIDENCY_BOUNDARY_AUTHORITY = object()
_RESIDENCY_BOUNDARY_LOCK = threading.RLock()
_PROFILE_BY_MODEL = {
    QWEN35_RAW_PROFILE.model: QWEN35_RAW_PROFILE,
    PHI4_MINI_RAW_PROFILE.model: PHI4_MINI_RAW_PROFILE,
}
_STOP_BY_MODEL = {
    QWEN35_RAW_PROFILE.model: ("<|im_end|>",),
    PHI4_MINI_RAW_PROFILE.model: ("<|end|>",),
}

_CONTEXT_TOKENS = 4_096
_NUM_BATCH = 128
_NUM_GPU = 99
_MAIN_GPU = 0
_USE_MMAP = True
_NUM_THREAD = 8
_DRAFT_NUM_PREDICT = 0
_TEMPERATURE = 0.2
_TOP_K = 40
_TOP_P = 0.9
_MIN_P = 0.0
_TYPICAL_P = 1.0
_REPEAT_LAST_N = 64
_REPEAT_PENALTY = 1.1
_PRESENCE_PENALTY = 0.0
_FREQUENCY_PENALTY = 0.0
_KEEP_ALIVE_SECONDS = -1
_REQUEST_TIMEOUT_SECONDS = 120.0

_TOKENIZER_TIMEOUT_SECONDS = 30.0
_MAX_RENDERED_PROMPT_BYTES = 64 * 1_024
_MAX_TOKEN_CACHE_ENTRIES = 128
_MAX_TOKEN_CACHE_PROMPT_BYTES = 8 * 1_024 * 1_024
_MAX_TOKENIZER_HELPER_BYTES = 256 * 1_024 * 1_024
_MAX_TOKENIZER_PROVENANCE_BYTES = 64 * 1_024
_MAX_RESIDENCY_RESPONSE_BYTES = 1 * 1_024 * 1_024
_RESIDENCY_TIMEOUT_SECONDS = 30.0
_OLLAMA_SOURCE_COMMIT = "710292ff4f191d8da9f6a4230804fbc693338d4a"
_OLLAMA_REPOSITORY = "https://github.com/ollama/ollama.git"
_LLAMA_CPP_TAG = "b9840"
_TOKENIZER_CMAKE_DEFINITIONS = (
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
)

_ArtifactIdentity = tuple[int, int, int, int, int, int]
_TokenizerArtifactIdentity = tuple[int, int, int, int, int]

_TOKEN_COUNTER_FIELDS = {
    "_cache",
    "_cache_prompt_bytes",
    "_helper_identity",
    "_model_identity",
    "_run_command",
    "helper_path",
    "helper_sha256",
    "max_cache_bytes",
    "max_cache_entries",
    "max_prompt_bytes",
    "model_path",
    "model_sha256",
    "timeout_seconds",
}
_PROMPT_PREPARER_FIELDS = {
    "max_rendered_prompt_bytes",
    "renderer",
    "token_counter",
}
_RAW_MODEL_FIELDS = {"_transport", "config"}
_LOOPBACK_TRANSPORT_FIELDS = {"host", "port", "timeout_seconds"}


class V07RuntimeFactoryError(RuntimeError):
    """Required v0.7 runtime evidence or component identity did not agree."""


@dataclass(frozen=True, slots=True)
class V07TokenizerHelperAttestation:
    """Verifier-issued, path-redacted identity for the pinned helper build."""

    helper_sha256: str
    provenance_sha256: str
    llama_cpp_commit: str
    llama_cpp_tag: str
    ollama_commit: str
    build_recipe_sha256: str
    policy_revision: str
    _helper_path: Path = field(repr=False, compare=False)
    _provenance_path: Path = field(repr=False, compare=False)
    _helper_identity: _ArtifactIdentity = field(repr=False, compare=False)
    _provenance_identity: _ArtifactIdentity = field(repr=False, compare=False)
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _TOKENIZER_ATTESTATION_SEAL:
            raise V07RuntimeFactoryError(
                "tokenizer helper attestations are verifier-sealed"
            )
        for value, label in (
            (self.helper_sha256, "tokenizer helper"),
            (self.provenance_sha256, "tokenizer provenance"),
            (self.build_recipe_sha256, "tokenizer build recipe"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise V07RuntimeFactoryError(f"{label} SHA-256 is invalid")
        if (
            self.llama_cpp_commit != V07_LLAMA_CPP_COMMIT
            or self.llama_cpp_tag != _LLAMA_CPP_TAG
            or self.ollama_commit != _OLLAMA_SOURCE_COMMIT
            or self.policy_revision != V07_TOKENIZER_POLICY_REVISION
        ):
            raise V07RuntimeFactoryError("tokenizer helper provenance is not frozen v0.7")

    def require_current(self) -> None:
        """Fail if either verified local artifact changed after attestation."""

        _require_unchanged_artifact(
            self._helper_path,
            self._helper_identity,
            executable=True,
            label="tokenizer helper artifact",
        )
        _require_unchanged_artifact(
            self._provenance_path,
            self._provenance_identity,
            executable=False,
            label="tokenizer provenance artifact",
        )

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__(_TOKENIZER_ATTESTATION_SEAL)
        self.require_current()
        return {
            "build_recipe_sha256": self.build_recipe_sha256,
            "helper_sha256": self.helper_sha256,
            "llama_cpp_commit": self.llama_cpp_commit,
            "llama_cpp_tag": self.llama_cpp_tag,
            "ollama_commit": self.ollama_commit,
            "policy_revision": self.policy_revision,
            "provenance_sha256": self.provenance_sha256,
            "schema": "edgeloopbench.v07-tokenizer-helper-attestation.v1",
        }

    def _helper_path_for_runtime(self) -> Path:
        self.require_current()
        return self._helper_path


class V07ResidencyOperation(str, Enum):
    UNLOAD = "unload"
    LOAD = "load"


@dataclass(frozen=True, slots=True)
class V07ResidencyCommand:
    """One typed model-residency operation; no arbitrary HTTP surface."""

    operation: V07ResidencyOperation
    model_id: str
    model_manifest_sha256: str
    model_artifact_sha256: str
    keep_alive_seconds: int

    def __post_init__(self) -> None:
        if type(self.operation) is not V07ResidencyOperation:
            raise V07RuntimeFactoryError("residency operation is invalid")
        profile = _profile_for_model(self.model_id)
        if (
            self.model_manifest_sha256 != profile.model_manifest_sha256
            or self.model_artifact_sha256 != profile.model_artifact_sha256
        ):
            raise V07RuntimeFactoryError("residency model identity is invalid")
        expected_keep_alive = (
            0
            if self.operation is V07ResidencyOperation.UNLOAD
            else _KEEP_ALIVE_SECONDS
        )
        if (
            type(self.keep_alive_seconds) is not int
            or self.keep_alive_seconds != expected_keep_alive
        ):
            raise V07RuntimeFactoryError("residency keep-alive operation is invalid")

    @property
    def expected_resident_model(self) -> ResidentModel:
        return ResidentModel(
            self.model_id,
            self.model_manifest_sha256.removeprefix("sha256:"),
        )

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__()
        return {
            "keep_alive_seconds": self.keep_alive_seconds,
            "model_artifact_sha256": self.model_artifact_sha256,
            "model_id": self.model_id,
            "model_manifest_sha256": self.model_manifest_sha256,
            "operation": self.operation.value,
        }


class V07ManagedResidencyBoundary:
    """Issuer-registered control boundary for only the two fixed Ollama URLs."""

    __slots__ = ("_http_open", "_runtime_receipt", "__weakref__")

    def __init__(
        self,
        runtime_receipt: ManagedOllamaRuntimeReceipt,
        *,
        _authority: object,
    ) -> None:
        if _authority is not _RESIDENCY_BOUNDARY_AUTHORITY:
            raise V07RuntimeFactoryError(
                "managed residency boundaries must be issuer-created"
            )
        self._runtime_receipt = runtime_receipt
        self._http_open = open_ollama_http

    @property
    def runtime_receipt_sha256(self) -> str:
        _require_issued_residency_boundary(self, self._runtime_receipt)
        return self._runtime_receipt.receipt_sha256

    def observe(self) -> tuple[ResidentModel, ...]:
        _require_issued_residency_boundary(self, self._runtime_receipt)
        request = urllib.request.Request(
            OLLAMA_PS_URL,
            method="GET",
            headers={"Accept": "application/json"},
        )
        payload = self._request(request, expected_url=OLLAMA_PS_URL)
        try:
            parsed = parse_strict_json_object(payload)
        except OllamaLoopbackHttpError as error:
            raise V07RuntimeFactoryError(
                "managed residency observation is invalid JSON"
            ) from error
        if set(parsed) != {"models"} or type(parsed["models"]) is not list:
            raise V07RuntimeFactoryError(
                "managed residency observation shape is invalid"
            )
        raw_models = parsed["models"]
        assert isinstance(raw_models, list)
        if len(raw_models) > 1:
            raise V07RuntimeFactoryError(
                "managed residency observed more than one loaded model"
            )
        models: list[ResidentModel] = []
        try:
            for raw in raw_models:
                if type(raw) is not dict:
                    raise ValueError("resident model entry type")
                models.append(
                    ResidentModel(
                        model=raw.get("model"),  # type: ignore[arg-type]
                        digest=raw.get("digest"),  # type: ignore[arg-type]
                    )
                )
        except (TypeError, ValueError) as error:
            raise V07RuntimeFactoryError(
                "managed residency model identity is invalid"
            ) from error
        result = tuple(sorted(models))
        _require_issued_residency_boundary(self, self._runtime_receipt)
        return result

    def apply(self, command: V07ResidencyCommand) -> None:
        _require_issued_residency_boundary(self, self._runtime_receipt)
        if type(command) is not V07ResidencyCommand:
            raise V07RuntimeFactoryError("managed residency command type is invalid")
        command.__post_init__()
        body = json.dumps(
            {
                "keep_alive": command.keep_alive_seconds,
                "model": command.model_id,
                "prompt": "",
                "stream": False,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        request = urllib.request.Request(
            OLLAMA_GENERATE_URL,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        payload = self._request(request, expected_url=OLLAMA_GENERATE_URL)
        try:
            parsed = parse_strict_json_object(payload)
        except OllamaLoopbackHttpError as error:
            raise V07RuntimeFactoryError(
                "managed residency control response is invalid JSON"
            ) from error
        observed_reason = parsed.get("done_reason")
        reason_is_valid = (
            observed_reason == "unload"
            if command.operation is V07ResidencyOperation.UNLOAD
            else observed_reason in (None, "load")
        )
        if (
            parsed.get("model") != command.model_id
            or parsed.get("response") != ""
            or parsed.get("done") is not True
            or not reason_is_valid
            or parsed.get("remote_model") is not None
            or parsed.get("remote_host") is not None
        ):
            raise V07RuntimeFactoryError(
                "managed residency control response differs from command"
            )
        _require_issued_residency_boundary(self, self._runtime_receipt)

    def _request(
        self,
        request: urllib.request.Request,
        *,
        expected_url: str,
    ) -> bytes:
        try:
            with self._http_open(request, _RESIDENCY_TIMEOUT_SECONDS) as response:
                require_exact_ollama_response(response, expected_url=expected_url)
                payload = response.read(_MAX_RESIDENCY_RESPONSE_BYTES + 1)
        except (
            OllamaLoopbackHttpError,
            OSError,
            TimeoutError,
            ValueError,
            urllib.error.URLError,
        ) as error:
            raise V07RuntimeFactoryError(
                "managed residency loopback request failed"
            ) from error
        if type(payload) is not bytes or len(payload) > _MAX_RESIDENCY_RESPONSE_BYTES:
            raise V07RuntimeFactoryError(
                "managed residency loopback response exceeded its bound"
            )
        return payload


_ISSUED_RESIDENCY_BOUNDARIES: weakref.WeakSet[
    V07ManagedResidencyBoundary
] = weakref.WeakSet()


def issue_v07_managed_residency_boundary(
    runtime_receipt: ManagedOllamaRuntimeReceipt,
) -> V07ManagedResidencyBoundary:
    """Issue the exact fixed-loopback control boundary for one live runtime."""

    receipt = _require_live_receipt(runtime_receipt)
    boundary = V07ManagedResidencyBoundary(
        receipt,
        _authority=_RESIDENCY_BOUNDARY_AUTHORITY,
    )
    with _RESIDENCY_BOUNDARY_LOCK:
        _ISSUED_RESIDENCY_BOUNDARIES.add(boundary)
    _require_issued_residency_boundary(boundary, receipt)
    return boundary


@dataclass(frozen=True, slots=True)
class V07ResidencyReceipt:
    """Path-free evidence that one exact model became solely resident."""

    previous_model_id: str | None
    previous_model_manifest_sha256: str | None
    previous_model_artifact_sha256: str | None
    target_model_id: str
    target_model_manifest_sha256: str
    target_model_artifact_sha256: str
    runtime_receipt_sha256: str
    transition_sha256: str
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _RESIDENCY_RECEIPT_SEAL:
            raise V07RuntimeFactoryError("residency receipts are transition-sealed")
        target = _profile_for_model(self.target_model_id)
        if (
            self.target_model_manifest_sha256 != target.model_manifest_sha256
            or self.target_model_artifact_sha256 != target.model_artifact_sha256
        ):
            raise V07RuntimeFactoryError("residency target identity is invalid")
        previous_values = (
            self.previous_model_manifest_sha256,
            self.previous_model_artifact_sha256,
        )
        if self.previous_model_id is None:
            if previous_values != (None, None):
                raise V07RuntimeFactoryError("absent previous model carries identity")
        else:
            previous = _profile_for_model(self.previous_model_id)
            if previous_values != (
                previous.model_manifest_sha256,
                previous.model_artifact_sha256,
            ):
                raise V07RuntimeFactoryError("residency previous identity is invalid")
        for value, label in (
            (self.runtime_receipt_sha256, "residency runtime receipt"),
            (self.transition_sha256, "residency transition"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise V07RuntimeFactoryError(f"{label} SHA-256 is invalid")
        if self.transition_sha256 != _digest_canonical(self._core_record()):
            raise V07RuntimeFactoryError("residency transition digest is invalid")

    def _core_record(self) -> dict[str, object]:
        return {
            "previous": (
                None
                if self.previous_model_id is None
                else {
                    "model_artifact_sha256": self.previous_model_artifact_sha256,
                    "model_id": self.previous_model_id,
                    "model_manifest_sha256": self.previous_model_manifest_sha256,
                }
            ),
            "runtime_receipt_sha256": self.runtime_receipt_sha256,
            "schema": "edgeloopbench.v07-model-residency-transition.v1",
            "target": {
                "model_artifact_sha256": self.target_model_artifact_sha256,
                "model_id": self.target_model_id,
                "model_manifest_sha256": self.target_model_manifest_sha256,
            },
        }

    def canonical_record(self) -> dict[str, object]:
        self.__post_init__(_RESIDENCY_RECEIPT_SEAL)
        return {**self._core_record(), "transition_sha256": self.transition_sha256}


@dataclass(frozen=True, slots=True)
class V07ModelRuntime:
    """Exact live model-side composition plus its path-free public identity."""

    generation: OllamaGenerationConfig
    tokenizer_pins: V07TokenizerPins
    model_identity: V07ModelIdentityPins
    token_counter: LlamaTokenizeCounter = field(repr=False, compare=False)
    prompt_preparer: ExactPromptPreparer = field(repr=False, compare=False)
    model: OllamaRawModel = field(repr=False, compare=False)
    runtime_receipt_sha256: str
    _runtime_receipt: ManagedOllamaRuntimeReceipt = field(
        repr=False,
        compare=False,
    )
    _tokenizer_attestation: V07TokenizerHelperAttestation = field(
        repr=False,
        compare=False,
    )
    _model_transport: _LoopbackOllamaTransport = field(
        repr=False,
        compare=False,
    )
    _tokenizer_run_command: object = field(repr=False, compare=False)
    _tokenizer_cache: OrderedDict[str, tuple[bytes, TokenCount]] = field(
        repr=False,
        compare=False,
    )
    _tokenizer_helper_path: Path = field(repr=False, compare=False)
    _tokenizer_model_path: Path = field(repr=False, compare=False)
    _tokenizer_helper_identity: _TokenizerArtifactIdentity = field(
        repr=False,
        compare=False,
    )
    _tokenizer_model_identity: _TokenizerArtifactIdentity = field(
        repr=False,
        compare=False,
    )
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _CONSTRUCTION_SEAL:
            raise V07RuntimeFactoryError("v0.7 model runtimes are factory-sealed")
        self._validate_live_composition()

    @property
    def model_id(self) -> str:
        return self.generation.model

    @property
    def expected_resident_model(self) -> ResidentModel:
        return ResidentModel(
            self.model_id,
            self.generation.profile.model_manifest_sha256.removeprefix("sha256:"),
        )

    def public_record(self) -> dict[str, object]:
        """Return identities and frozen settings without local artifact paths."""

        self._validate_live_composition()
        return {
            "composition": {
                "generation_config_sha256": self.generation.sha256,
                "generation_transport": {
                    "host": "127.0.0.1",
                    "port": 11_434,
                    "timeout_seconds": self.generation.request_timeout_seconds,
                    "type": "LoopbackOllamaTransport",
                },
                "prompt_preparer": "ExactPromptPreparer",
                "raw_model": "OllamaRawModel",
                "token_counter": "LlamaTokenizeCounter",
                "tokenizer_command_runner": "python-stdlib-subprocess.run",
                "tokenizer_cache_entries": _MAX_TOKEN_CACHE_ENTRIES,
                "tokenizer_cache_prompt_bytes": _MAX_TOKEN_CACHE_PROMPT_BYTES,
                "tokenizer_max_prompt_bytes": _MAX_RENDERED_PROMPT_BYTES,
                "tokenizer_timeout_seconds": _TOKENIZER_TIMEOUT_SECONDS,
            },
            "factory_revision": V07_RUNTIME_FACTORY_REVISION,
            "model_identity": self.model_identity.canonical_record(),
            "runtime_receipt_sha256": self.runtime_receipt_sha256,
            "tokenizer_helper": self._tokenizer_attestation.canonical_record(),
            "tokenizer_identity_sha256": self.tokenizer_pins.identity_sha256,
        }

    def canonical_record(self) -> dict[str, object]:
        return self.public_record()

    def require_live(self) -> None:
        """Revalidate the live receipt and all local component bindings."""

        self._validate_live_composition()

    def _validate_live_composition(self) -> None:
        receipt = _require_live_receipt(self._runtime_receipt)
        if type(self._tokenizer_attestation) is not V07TokenizerHelperAttestation:
            raise V07RuntimeFactoryError("tokenizer attestation type drifted")
        self._tokenizer_attestation.require_current()
        if self.runtime_receipt_sha256 != receipt.receipt_sha256:
            raise V07RuntimeFactoryError("managed Ollama receipt identity drifted")
        if type(self.generation) is not OllamaGenerationConfig:
            raise V07RuntimeFactoryError("generation config type drifted")
        self.generation.__post_init__()
        if (
            self.generation.runtime_version != receipt.runtime_version
            or self.generation.runtime_binary_sha256
            != receipt.runtime_binary_sha256
            or self.generation.endpoint != receipt.endpoint
        ):
            raise V07RuntimeFactoryError("generation and managed runtime drifted")
        if type(self.tokenizer_pins) is not V07TokenizerPins:
            raise V07RuntimeFactoryError("tokenizer pin type drifted")
        self.tokenizer_pins.__post_init__()
        if type(self.model_identity) is not V07ModelIdentityPins:
            raise V07RuntimeFactoryError("model identity type drifted")
        model_record = self.model_identity.canonical_record()
        if (
            self.model_identity.generation != self.generation
            or self.model_identity.tokenizer != self.tokenizer_pins
            or self.model_identity.model_id != self.generation.model
            or model_record["local_attestation_sha256"]
            != self.model_identity.local_attestation_sha256
        ):
            raise V07RuntimeFactoryError("model identity composition drifted")
        if type(self.token_counter) is not LlamaTokenizeCounter:
            raise V07RuntimeFactoryError("token counter type drifted")
        if set(vars(self.token_counter)) != _TOKEN_COUNTER_FIELDS:
            raise V07RuntimeFactoryError("tokenizer execution surface was replaced")
        if (
            self.token_counter._run_command is not self._tokenizer_run_command
            or self._tokenizer_run_command is not subprocess.run
        ):
            raise V07RuntimeFactoryError("tokenizer command runner was replaced")
        if (
            self.token_counter._cache is not self._tokenizer_cache
            or type(self._tokenizer_cache) is not OrderedDict
        ):
            raise V07RuntimeFactoryError("tokenizer cache boundary was replaced")
        _validate_tokenizer_cache(self.token_counter, self._tokenizer_cache)
        if (
            self.token_counter.helper_path != self._tokenizer_helper_path
            or self.token_counter.model_path != self._tokenizer_model_path
            or self.token_counter._helper_identity
            != self._tokenizer_helper_identity
            or self.token_counter._model_identity != self._tokenizer_model_identity
        ):
            raise V07RuntimeFactoryError("tokenizer artifact boundary was replaced")
        try:
            LlamaTokenizeCounter._assert_unchanged(  # noqa: SLF001
                self._tokenizer_helper_path,
                self._tokenizer_helper_identity,
            )
            LlamaTokenizeCounter._assert_unchanged(  # noqa: SLF001
                self._tokenizer_model_path,
                self._tokenizer_model_identity,
            )
        except RuntimeError as error:
            raise V07RuntimeFactoryError(
                "tokenizer helper or model artifact changed"
            ) from error
        if (
            self.token_counter.helper_sha256 != self.tokenizer_pins.helper_sha256
            or self.token_counter.model_sha256
            != self.tokenizer_pins.model_artifact_sha256
            or self.token_counter.timeout_seconds != _TOKENIZER_TIMEOUT_SECONDS
            or self.token_counter.max_prompt_bytes != _MAX_RENDERED_PROMPT_BYTES
            or self.token_counter.max_cache_entries != _MAX_TOKEN_CACHE_ENTRIES
            or self.token_counter.max_cache_bytes != _MAX_TOKEN_CACHE_PROMPT_BYTES
        ):
            raise V07RuntimeFactoryError("tokenizer component identity drifted")
        if (
            type(self.prompt_preparer) is not ExactPromptPreparer
            or set(vars(self.prompt_preparer)) != _PROMPT_PREPARER_FIELDS
            or self.prompt_preparer.renderer is not self.generation.profile
            or self.prompt_preparer.token_counter is not self.token_counter
            or self.prompt_preparer.max_rendered_prompt_bytes
            != _MAX_RENDERED_PROMPT_BYTES
        ):
            raise V07RuntimeFactoryError("prompt preparer composition drifted")
        if (
            type(self.model) is not OllamaRawModel
            or set(vars(self.model)) != _RAW_MODEL_FIELDS
            or self.model.config is not self.generation
        ):
            raise V07RuntimeFactoryError("raw model composition drifted")
        if (
            self.model._transport is not self._model_transport
            or type(self._model_transport) is not _LoopbackOllamaTransport
            or set(vars(self._model_transport)) != _LOOPBACK_TRANSPORT_FIELDS
            or self._model_transport.host != "127.0.0.1"
            or self._model_transport.port != 11_434
            or self._model_transport.timeout_seconds
            != self.generation.request_timeout_seconds
        ):
            raise V07RuntimeFactoryError("raw model loopback transport was replaced")


@dataclass(frozen=True, slots=True)
class V07RuntimeSession:
    """One sealed two-model, one-host, one-managed-runtime production root."""

    host_identity: V07HostIdentityPins
    session_sha256: str
    _models: tuple[V07ModelRuntime, V07ModelRuntime] = field(
        repr=False,
        compare=False,
    )
    _runtime_receipt: ManagedOllamaRuntimeReceipt = field(
        repr=False,
        compare=False,
    )
    _seal: InitVar[object | None] = None

    def __post_init__(self, _seal: object | None) -> None:
        if _seal is not _SESSION_SEAL:
            raise V07RuntimeFactoryError("v0.7 runtime sessions are builder-sealed")
        self._validate_live_session()

    @property
    def model_ids(self) -> tuple[str, str]:
        return (self._models[0].model_id, self._models[1].model_id)

    def model_runtime(self, model_id: str) -> V07ModelRuntime:
        profile = _profile_for_model(model_id)
        runtime = self._models[0] if profile is QWEN35_RAW_PROFILE else self._models[1]
        runtime.require_live()
        return runtime

    def require_live(self) -> None:
        self._validate_live_session()

    def transition_residency(
        self,
        *,
        previous_model_id: str | None,
        target_model_id: str,
        boundary: V07ManagedResidencyBoundary,
    ) -> V07ResidencyReceipt:
        previous = (
            None
            if previous_model_id is None
            else self.model_runtime(previous_model_id)
        )
        target = self.model_runtime(target_model_id)
        return transition_v07_model_residency(
            previous=previous,
            target=target,
            boundary=boundary,
        )

    def canonical_record(self) -> dict[str, object]:
        self._validate_live_session()
        return {**self._core_record(), "session_sha256": self.session_sha256}

    def _core_record(self) -> dict[str, object]:
        return {
            "factory_revision": V07_RUNTIME_FACTORY_REVISION,
            "host_identity": self.host_identity.canonical_record(),
            "managed_runtime": self._runtime_receipt.canonical_record(),
            "models": [runtime.canonical_record() for runtime in self._models],
            "schema": "edgeloopbench.v07-runtime-session.v1",
        }

    def _validate_live_session(self) -> None:
        receipt = _require_live_receipt(self._runtime_receipt)
        if type(self.host_identity) is not V07HostIdentityPins:
            raise V07RuntimeFactoryError("runtime session host identity type is invalid")
        self.host_identity.__post_init__()
        if (
            type(self._models) is not tuple
            or len(self._models) != 2
            or any(type(runtime) is not V07ModelRuntime for runtime in self._models)
            or self.model_ids
            != (QWEN35_RAW_PROFILE.model, PHI4_MINI_RAW_PROFILE.model)
        ):
            raise V07RuntimeFactoryError(
                "runtime session requires the exact canonical two-model set"
            )
        for runtime in self._models:
            runtime.require_live()
            if runtime._runtime_receipt is not receipt:  # noqa: SLF001
                raise V07RuntimeFactoryError(
                    "runtime session models do not share one managed receipt"
                )
        if (
            self.host_identity.ollama_runtime_binary_sha256
            != receipt.runtime_binary_sha256
            or self.host_identity.ollama_server_version != receipt.runtime_version
            or self.host_identity.ollama_launch_environment_sha256
            != receipt.launch_environment_sha256
            or self.host_identity.ollama_generation_endpoint_sha256
            != receipt.generation_endpoint_sha256
        ):
            raise V07RuntimeFactoryError(
                "runtime session host identity differs from managed runtime"
            )
        if type(self.session_sha256) is not str or _SHA256.fullmatch(
            self.session_sha256
        ) is None:
            raise V07RuntimeFactoryError("runtime session SHA-256 is invalid")
        if self.session_sha256 != _digest_canonical(self._core_record()):
            raise V07RuntimeFactoryError("runtime session identity drifted")


def attest_v07_tokenizer_helper(
    *,
    helper_path: Path,
    provenance_path: Path,
) -> V07TokenizerHelperAttestation:
    """Verify exact helper bytes and the frozen build-provenance record."""

    _require_canonical_absolute_path(helper_path, "tokenizer helper")
    _require_canonical_absolute_path(provenance_path, "tokenizer provenance")
    if provenance_path != helper_path.with_name(helper_path.name + ".provenance.json"):
        raise V07RuntimeFactoryError(
            "tokenizer provenance path differs from the build-tool contract"
        )
    helper, helper_identity = _read_artifact(
        helper_path,
        maximum_bytes=_MAX_TOKENIZER_HELPER_BYTES,
        executable=True,
        label="tokenizer helper artifact",
    )
    provenance_bytes, provenance_identity = _read_artifact(
        provenance_path,
        maximum_bytes=_MAX_TOKENIZER_PROVENANCE_BYTES,
        executable=False,
        label="tokenizer provenance artifact",
    )
    helper_sha256 = _digest_bytes(helper)
    provenance = _parse_strict_json_object(
        provenance_bytes,
        "tokenizer provenance",
    )
    expected_keys = {
        "artifact_sha256",
        "build_recipe",
        "llama_cpp_commit",
        "llama_cpp_tag",
        "ollama_commit",
        "ollama_repository",
    }
    if set(provenance) != expected_keys:
        raise V07RuntimeFactoryError("tokenizer provenance shape is invalid")
    if provenance["artifact_sha256"] != helper_sha256:
        raise V07RuntimeFactoryError(
            "tokenizer provenance artifact identity differs from helper bytes"
        )
    expected_recipe = {
        "cmake_definitions": list(_TOKENIZER_CMAKE_DEFINITIONS),
        "parallel_jobs": 2,
        "target": "llama-tokenize",
        "target_platform": "macos-arm64",
    }
    comparisons = (
        (provenance["build_recipe"], expected_recipe),
        (provenance["llama_cpp_commit"], V07_LLAMA_CPP_COMMIT),
        (provenance["llama_cpp_tag"], _LLAMA_CPP_TAG),
        (provenance["ollama_commit"], _OLLAMA_SOURCE_COMMIT),
        (provenance["ollama_repository"], _OLLAMA_REPOSITORY),
    )
    if any(
        type(observed) is not type(expected) or observed != expected
        for observed, expected in comparisons
    ):
        raise V07RuntimeFactoryError("tokenizer provenance differs from frozen v0.7")
    build_recipe_sha256 = _digest_canonical(expected_recipe)
    attestation = V07TokenizerHelperAttestation(
        helper_sha256=helper_sha256,
        provenance_sha256=_digest_bytes(provenance_bytes),
        llama_cpp_commit=V07_LLAMA_CPP_COMMIT,
        llama_cpp_tag=_LLAMA_CPP_TAG,
        ollama_commit=_OLLAMA_SOURCE_COMMIT,
        build_recipe_sha256=build_recipe_sha256,
        policy_revision=V07_TOKENIZER_POLICY_REVISION,
        _helper_path=helper_path,
        _provenance_path=provenance_path,
        _helper_identity=helper_identity,
        _provenance_identity=provenance_identity,
        _seal=_TOKENIZER_ATTESTATION_SEAL,
    )
    attestation.require_current()
    return attestation


def build_v07_generation_config(
    *,
    model_id: str,
    runtime_receipt: ManagedOllamaRuntimeReceipt,
) -> OllamaGenerationConfig:
    """Build the one frozen generation config for a preregistered model."""

    receipt = _require_live_receipt(runtime_receipt)
    profile = _profile_for_model(model_id)
    try:
        config = OllamaGenerationConfig(
            profile=profile,
            runtime_version=receipt.runtime_version,
            runtime_binary_sha256=receipt.runtime_binary_sha256,
            context_tokens=_CONTEXT_TOKENS,
            num_batch=_NUM_BATCH,
            num_gpu=_NUM_GPU,
            main_gpu=_MAIN_GPU,
            use_mmap=_USE_MMAP,
            num_thread=_NUM_THREAD,
            draft_num_predict=_DRAFT_NUM_PREDICT,
            temperature=_TEMPERATURE,
            top_k=_TOP_K,
            top_p=_TOP_P,
            min_p=_MIN_P,
            typical_p=_TYPICAL_P,
            repeat_last_n=_REPEAT_LAST_N,
            repeat_penalty=_REPEAT_PENALTY,
            presence_penalty=_PRESENCE_PENALTY,
            frequency_penalty=_FREQUENCY_PENALTY,
            stop=_STOP_BY_MODEL[model_id],
            keep_alive_seconds=_KEEP_ALIVE_SECONDS,
            request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
            endpoint=receipt.endpoint,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise V07RuntimeFactoryError(
            "frozen Ollama generation config is invalid"
        ) from error
    _require_same_live_receipt(runtime_receipt, receipt.receipt_sha256)
    return config


def build_v07_model_runtime(
    *,
    model_id: str,
    attestation: LocalModelAttestation,
    runtime_receipt: ManagedOllamaRuntimeReceipt,
    tokenizer_attestation: V07TokenizerHelperAttestation,
    model_artifact_path: Path,
) -> V07ModelRuntime:
    """Compose the exact tokenizer/preparer/raw-model chain for one model."""

    receipt = _require_live_receipt(runtime_receipt)
    profile = _profile_for_model(model_id)
    if type(attestation) is not LocalModelAttestation:
        raise V07RuntimeFactoryError("model attestation type is invalid")
    try:
        attestation_record = attestation.canonical_record()
    except (TypeError, ValueError, RuntimeError) as error:
        raise V07RuntimeFactoryError("model attestation is invalid") from error
    if attestation_record["model"] != model_id:
        raise V07RuntimeFactoryError("model attestation and requested model differ")
    if (
        attestation_record["renderer_profile_sha256"] != profile.sha256
        or attestation_record["model_manifest_sha256"]
        != profile.model_manifest_sha256
        or attestation_record["model_artifact_sha256"]
        != profile.model_artifact_sha256
    ):
        raise V07RuntimeFactoryError("model attestation profile identity differs")
    if (
        attestation_record["runtime_version"] != receipt.runtime_version
        or attestation_record["runtime_binary_sha256"]
        != receipt.runtime_binary_sha256
        or attestation_record["kv_cache_quantization"]
        != receipt.kv_cache_quantization
    ):
        raise V07RuntimeFactoryError("model attestation and managed runtime differ")
    _require_canonical_absolute_path(model_artifact_path, "model artifact")
    if type(tokenizer_attestation) is not V07TokenizerHelperAttestation:
        raise V07RuntimeFactoryError(
            "tokenizer helper attestation type is invalid"
        )
    tokenizer_attestation.require_current()

    generation = build_v07_generation_config(
        model_id=model_id,
        runtime_receipt=receipt,
    )
    try:
        tokenizer_pins = V07TokenizerPins(
            helper_sha256=tokenizer_attestation.helper_sha256,
            model_artifact_sha256=attestation.model_artifact_sha256,
            llama_cpp_commit=tokenizer_attestation.llama_cpp_commit,
            policy_revision=tokenizer_attestation.policy_revision,
        )
        token_counter = LlamaTokenizeCounter(
            helper_path=tokenizer_attestation._helper_path_for_runtime(),
            helper_sha256=tokenizer_pins.helper_sha256,
            model_path=model_artifact_path,
            model_sha256=tokenizer_pins.model_artifact_sha256,
            timeout_seconds=_TOKENIZER_TIMEOUT_SECONDS,
            max_prompt_bytes=_MAX_RENDERED_PROMPT_BYTES,
            max_cache_entries=_MAX_TOKEN_CACHE_ENTRIES,
            max_cache_bytes=_MAX_TOKEN_CACHE_PROMPT_BYTES,
            run_command=subprocess.run,
        )
        prompt_preparer = ExactPromptPreparer(
            profile,
            token_counter,
            max_rendered_prompt_bytes=_MAX_RENDERED_PROMPT_BYTES,
        )
        model = OllamaRawModel(generation)
        model_identity = bind_v07_model_identity(
            attestation=attestation,
            generation=generation,
            tokenizer=tokenizer_pins,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        raise V07RuntimeFactoryError(
            "pinned tokenizer or model artifact verification failed"
        ) from error
    _require_same_live_receipt(receipt, receipt.receipt_sha256)
    return V07ModelRuntime(
        generation=generation,
        tokenizer_pins=tokenizer_pins,
        model_identity=model_identity,
        token_counter=token_counter,
        prompt_preparer=prompt_preparer,
        model=model,
        runtime_receipt_sha256=receipt.receipt_sha256,
        _runtime_receipt=receipt,
        _tokenizer_attestation=tokenizer_attestation,
        _model_transport=model._transport,
        _tokenizer_run_command=token_counter._run_command,
        _tokenizer_cache=token_counter._cache,
        _tokenizer_helper_path=token_counter.helper_path,
        _tokenizer_model_path=token_counter.model_path,
        _tokenizer_helper_identity=token_counter._helper_identity,
        _tokenizer_model_identity=token_counter._model_identity,
        _seal=_CONSTRUCTION_SEAL,
    )


def transition_v07_model_residency(
    *,
    previous: V07ModelRuntime | None,
    target: V07ModelRuntime,
    boundary: V07ManagedResidencyBoundary,
) -> V07ResidencyReceipt:
    """Unload the previous model, prove emptiness, then load one exact target."""

    if type(target) is not V07ModelRuntime:
        raise V07RuntimeFactoryError("residency target runtime type is invalid")
    target.require_live()
    if previous is not None:
        if type(previous) is not V07ModelRuntime:
            raise V07RuntimeFactoryError("residency previous runtime type is invalid")
        previous.require_live()
        if previous.model_id == target.model_id:
            raise V07RuntimeFactoryError(
                "model-major residency transition requires distinct models"
            )
        if previous._runtime_receipt is not target._runtime_receipt:  # noqa: SLF001
            raise V07RuntimeFactoryError(
                "residency runtimes do not share one managed Ollama process"
            )
    _require_issued_residency_boundary(boundary, target._runtime_receipt)

    expected_initial = () if previous is None else (previous.expected_resident_model,)
    initial = _observe_residency(boundary, "initial")
    if initial != expected_initial:
        raise V07RuntimeFactoryError(
            "initial residency differs from the exact transition expectation"
        )
    if previous is not None:
        unload = _residency_command(previous, V07ResidencyOperation.UNLOAD)
        _apply_residency(boundary, unload)
        if _observe_residency(boundary, "post-unload"):
            raise V07RuntimeFactoryError(
                "model unload did not produce an empty residency set"
            )

    load = _residency_command(target, V07ResidencyOperation.LOAD)
    _apply_residency(boundary, load)
    observed_target = _observe_residency(boundary, "post-load")
    if observed_target != (target.expected_resident_model,):
        raise V07RuntimeFactoryError(
            "model load did not produce the exact target residency"
        )
    target.require_live()
    _require_issued_residency_boundary(boundary, target._runtime_receipt)
    previous_profile = None if previous is None else previous.generation.profile
    core = {
        "previous": (
            None
            if previous_profile is None
            else {
                "model_artifact_sha256": previous_profile.model_artifact_sha256,
                "model_id": previous_profile.model,
                "model_manifest_sha256": previous_profile.model_manifest_sha256,
            }
        ),
        "runtime_receipt_sha256": target.runtime_receipt_sha256,
        "schema": "edgeloopbench.v07-model-residency-transition.v1",
        "target": {
            "model_artifact_sha256": target.generation.profile.model_artifact_sha256,
            "model_id": target.model_id,
            "model_manifest_sha256": target.generation.profile.model_manifest_sha256,
        },
    }
    return V07ResidencyReceipt(
        previous_model_id=(None if previous_profile is None else previous_profile.model),
        previous_model_manifest_sha256=(
            None
            if previous_profile is None
            else previous_profile.model_manifest_sha256
        ),
        previous_model_artifact_sha256=(
            None
            if previous_profile is None
            else previous_profile.model_artifact_sha256
        ),
        target_model_id=target.model_id,
        target_model_manifest_sha256=target.generation.profile.model_manifest_sha256,
        target_model_artifact_sha256=target.generation.profile.model_artifact_sha256,
        runtime_receipt_sha256=target.runtime_receipt_sha256,
        transition_sha256=_digest_canonical(core),
        _seal=_RESIDENCY_RECEIPT_SEAL,
    )


def build_v07_host_identity(
    *,
    docker_pins: DockerTelemetryPins,
    docker_daemon: DockerDaemonIdentity,
    runtime_receipt: ManagedOllamaRuntimeReceipt,
) -> V07HostIdentityPins:
    """Bind one exact Docker observation and one live managed Ollama receipt."""

    receipt = _require_live_receipt(runtime_receipt)
    if type(docker_pins) is not DockerTelemetryPins:
        raise V07RuntimeFactoryError("Docker telemetry pins type is invalid")
    if type(docker_daemon) is not DockerDaemonIdentity:
        raise V07RuntimeFactoryError("Docker daemon observation type is invalid")
    expected = (
        docker_pins.binary_sha256,
        docker_pins.endpoint_sha256,
        docker_pins.client_version,
        docker_pins.server_version,
    )
    observed = (
        docker_daemon.binary_sha256,
        docker_daemon.endpoint_sha256,
        docker_daemon.client_version,
        docker_daemon.server_version,
    )
    if observed != expected:
        raise V07RuntimeFactoryError(
            "Docker daemon observation differs from exact telemetry pins"
        )
    try:
        identity = V07HostIdentityPins(
            docker_binary_sha256=docker_daemon.binary_sha256,
            docker_endpoint_sha256=docker_daemon.endpoint_sha256,
            docker_client_version=docker_daemon.client_version,
            docker_server_version=docker_daemon.server_version,
            ollama_runtime_binary_sha256=receipt.runtime_binary_sha256,
            ollama_server_version=receipt.runtime_version,
            ollama_launch_environment_sha256=receipt.launch_environment_sha256,
            ollama_generation_endpoint_sha256=receipt.generation_endpoint_sha256,
        )
    except (TypeError, ValueError) as error:
        raise V07RuntimeFactoryError("v0.7 host identity is invalid") from error
    _require_same_live_receipt(receipt, receipt.receipt_sha256)
    return identity


def build_v07_runtime_session(
    *,
    models: tuple[V07ModelRuntime, ...],
    host_identity: V07HostIdentityPins,
) -> V07RuntimeSession:
    """Seal exactly both preregistered model runtimes under one live host."""

    if type(models) is not tuple or len(models) != 2 or any(
        type(runtime) is not V07ModelRuntime for runtime in models
    ):
        raise V07RuntimeFactoryError(
            "runtime session requires the exact two-model runtime tuple"
        )
    by_model: dict[str, V07ModelRuntime] = {}
    for runtime in models:
        runtime.require_live()
        if runtime.model_id in by_model:
            raise V07RuntimeFactoryError(
                "runtime session requires the exact two-model runtime set"
            )
        by_model[runtime.model_id] = runtime
    if set(by_model) != set(_PROFILE_BY_MODEL):
        raise V07RuntimeFactoryError(
            "runtime session requires the exact two-model runtime set"
        )
    ordered = (
        by_model[QWEN35_RAW_PROFILE.model],
        by_model[PHI4_MINI_RAW_PROFILE.model],
    )
    receipt = ordered[0]._runtime_receipt  # noqa: SLF001
    if ordered[1]._runtime_receipt is not receipt:  # noqa: SLF001
        raise V07RuntimeFactoryError(
            "runtime session models do not share one managed receipt"
        )
    if type(host_identity) is not V07HostIdentityPins:
        raise V07RuntimeFactoryError("runtime session host identity type is invalid")
    host_identity.__post_init__()
    provisional = {
        "factory_revision": V07_RUNTIME_FACTORY_REVISION,
        "host_identity": host_identity.canonical_record(),
        "managed_runtime": receipt.canonical_record(),
        "models": [runtime.canonical_record() for runtime in ordered],
        "schema": "edgeloopbench.v07-runtime-session.v1",
    }
    return V07RuntimeSession(
        host_identity=host_identity,
        session_sha256=_digest_canonical(provisional),
        _models=ordered,
        _runtime_receipt=receipt,
        _seal=_SESSION_SEAL,
    )


def _validate_tokenizer_cache(
    counter: LlamaTokenizeCounter,
    cache: OrderedDict[str, tuple[bytes, TokenCount]],
) -> None:
    try:
        entries = tuple(cache.items())
    except (RuntimeError, TypeError) as error:
        raise V07RuntimeFactoryError("tokenizer cache state is invalid") from error
    if len(entries) > _MAX_TOKEN_CACHE_ENTRIES:
        raise V07RuntimeFactoryError("tokenizer cache state is invalid")
    total_prompt_bytes = 0
    for cache_key, entry in entries:
        if type(entry) is not tuple or len(entry) != 2:
            raise V07RuntimeFactoryError("tokenizer cache state is invalid")
        prompt_bytes, count = entry
        if (
            type(cache_key) is not str
            or _SHA256.fullmatch(cache_key) is None
            or type(prompt_bytes) is not bytes
            or len(prompt_bytes) > _MAX_RENDERED_PROMPT_BYTES
            or cache_key != _digest_bytes(prompt_bytes)
            or type(count) is not TokenCount
        ):
            raise V07RuntimeFactoryError("tokenizer cache state is invalid")
        try:
            count.__post_init__()
        except (TypeError, ValueError) as error:
            raise V07RuntimeFactoryError("tokenizer cache state is invalid") from error
        if (
            count.tokenizer_artifact_sha256 != counter.helper_sha256
            or count.model_artifact_sha256 != counter.model_sha256
        ):
            raise V07RuntimeFactoryError("tokenizer cache state is invalid")
        total_prompt_bytes += len(prompt_bytes)
    if (
        type(counter._cache_prompt_bytes) is not int
        or counter._cache_prompt_bytes != total_prompt_bytes
        or total_prompt_bytes > _MAX_TOKEN_CACHE_PROMPT_BYTES
        or tuple(cache.items()) != entries
    ):
        raise V07RuntimeFactoryError("tokenizer cache state is invalid")


def _profile_for_model(model_id: str) -> RestrictedRawRenderingProfile:
    if type(model_id) is not str or model_id not in _PROFILE_BY_MODEL:
        raise V07RuntimeFactoryError(
            "model is outside the preregistered v0.7 small-model set"
        )
    return _PROFILE_BY_MODEL[model_id]


def _require_issued_residency_boundary(
    boundary: object,
    receipt: ManagedOllamaRuntimeReceipt,
) -> V07ManagedResidencyBoundary:
    if type(boundary) is not V07ManagedResidencyBoundary:
        raise V07RuntimeFactoryError(
            "residency boundary is not an exact issuer-issued boundary"
        )
    with _RESIDENCY_BOUNDARY_LOCK:
        if boundary not in _ISSUED_RESIDENCY_BOUNDARIES:
            raise V07RuntimeFactoryError("residency boundary was not issued")
    if boundary._runtime_receipt is not receipt:  # noqa: SLF001
        raise V07RuntimeFactoryError(
            "residency boundary receipt differs from target runtime"
        )
    if boundary._http_open is not open_ollama_http:  # noqa: SLF001
        raise V07RuntimeFactoryError(
            "residency boundary loopback transport was replaced"
        )
    _require_live_receipt(receipt)
    return boundary


def _residency_command(
    runtime: V07ModelRuntime,
    operation: V07ResidencyOperation,
) -> V07ResidencyCommand:
    profile = runtime.generation.profile
    return V07ResidencyCommand(
        operation=operation,
        model_id=profile.model,
        model_manifest_sha256=profile.model_manifest_sha256,
        model_artifact_sha256=profile.model_artifact_sha256,
        keep_alive_seconds=(
            0 if operation is V07ResidencyOperation.UNLOAD else _KEEP_ALIVE_SECONDS
        ),
    )


def _observe_residency(
    boundary: V07ManagedResidencyBoundary,
    phase: str,
) -> tuple[ResidentModel, ...]:
    try:
        observed = boundary.observe()
    except Exception as error:
        raise V07RuntimeFactoryError(
            f"{phase} residency observation failed"
        ) from error
    if type(observed) is not tuple or any(
        type(model) is not ResidentModel for model in observed
    ):
        raise V07RuntimeFactoryError(f"{phase} residency observation is invalid")
    if tuple(sorted(set(observed))) != observed or len(observed) > 1:
        raise V07RuntimeFactoryError(f"{phase} residency observation is invalid")
    return observed


def _apply_residency(
    boundary: V07ManagedResidencyBoundary,
    command: V07ResidencyCommand,
) -> None:
    try:
        result = boundary.apply(command)
    except Exception as error:
        raise V07RuntimeFactoryError(
            f"model {command.operation.value} operation failed"
        ) from error
    if result is not None:
        raise V07RuntimeFactoryError(
            f"model {command.operation.value} operation returned unexpected data"
        )


def _require_live_receipt(
    receipt: ManagedOllamaRuntimeReceipt,
) -> ManagedOllamaRuntimeReceipt:
    try:
        return require_live_managed_ollama_receipt(receipt)
    except ManagedOllamaRuntimeError as error:
        raise V07RuntimeFactoryError(
            "managed Ollama runtime receipt is not live and exact"
        ) from error


def _require_same_live_receipt(
    receipt: ManagedOllamaRuntimeReceipt,
    expected_sha256: str,
) -> None:
    observed = _require_live_receipt(receipt)
    if observed.receipt_sha256 != expected_sha256:
        raise V07RuntimeFactoryError("managed Ollama receipt identity drifted")


def _require_canonical_absolute_path(path: Path, label: str) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or Path(os.path.normpath(os.fspath(path))) != path
        or "\x00" in os.fspath(path)
    ):
        raise V07RuntimeFactoryError(f"{label} path is not canonical and absolute")


def _read_artifact(
    path: Path,
    *,
    maximum_bytes: int,
    executable: bool,
    label: str,
) -> tuple[bytes, _ArtifactIdentity]:
    try:
        before = path.lstat()
    except OSError as error:
        raise V07RuntimeFactoryError(f"{label} is not readable") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise V07RuntimeFactoryError(f"{label} must be a regular non-symlink file")
    if executable and not before.st_mode & (
        stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    ):
        raise V07RuntimeFactoryError(f"{label} must be executable")
    if before.st_size <= 0 or before.st_size > maximum_bytes:
        raise V07RuntimeFactoryError(f"{label} size is outside its frozen bound")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            payload = handle.read(maximum_bytes + 1)
        after = path.lstat()
    except OSError as error:
        raise V07RuntimeFactoryError(f"{label} could not be read") from error
    before_identity = _artifact_identity(before)
    if (
        _artifact_identity(opened) != before_identity
        or _artifact_identity(after) != before_identity
        or len(payload) != before.st_size
        or len(payload) > maximum_bytes
    ):
        raise V07RuntimeFactoryError(f"{label} changed while being verified")
    return payload, before_identity


def _require_unchanged_artifact(
    path: Path,
    expected: _ArtifactIdentity,
    *,
    executable: bool,
    label: str,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise V07RuntimeFactoryError(f"{label} is not readable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or _artifact_identity(metadata) != expected
        or (
            executable
            and not metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        )
    ):
        raise V07RuntimeFactoryError(f"{label} changed after verification")


def _artifact_identity(metadata: os.stat_result) -> _ArtifactIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _parse_strict_json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        text = payload.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise V07RuntimeFactoryError(f"{label} is not strict JSON") from error
    if type(parsed) is not dict:
        raise V07RuntimeFactoryError(f"{label} must be a JSON object")
    return parsed


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest_canonical(value: object) -> str:
    return _digest_bytes(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )
