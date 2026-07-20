"""Owned, attested Ollama process boundary for the v0.7 local study."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import threading
import time
import urllib.request
import weakref
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .ollama_loopback_http import (
    OLLAMA_GENERATE_URL,
    OLLAMA_ORIGIN,
    OLLAMA_VERSION_URL,
    open_ollama_http,
    parse_strict_json_object,
    require_exact_ollama_response,
)


_V07_OLLAMA_VERSION = "0.31.1"
_V07_OLLAMA_KV_CACHE = "q8_0"
_RECEIPT_SCHEMA = "edgeloopbench.managed-ollama-runtime-receipt.v1"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_MAX_RUNTIME_BYTES = 256 << 20
_MAX_VERSION_OUTPUT_BYTES = 65_536
_VERSION_TIMEOUT_SECONDS = 5.0
_STARTUP_TIMEOUT_SECONDS = 15.0
_STARTUP_POLL_SECONDS = 0.05
_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_KILL_TIMEOUT_SECONDS = 2.0
_MAX_HTTP_RESPONSE_BYTES = 4_096
_INHERITED_ENVIRONMENT_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
)
_OLLAMA_ENVIRONMENT = {
    "OLLAMA_CONTEXT_LENGTH": "4096",
    "OLLAMA_FLASH_ATTENTION": "1",
    "OLLAMA_HOST": "127.0.0.1:11434",
    "OLLAMA_KEEP_ALIVE": "-1",
    "OLLAMA_KV_CACHE_TYPE": _V07_OLLAMA_KV_CACHE,
    "OLLAMA_MAX_LOADED_MODELS": "1",
    "OLLAMA_NO_CLOUD": "1",
    "OLLAMA_NUM_PARALLEL": "1",
}
V07_OLLAMA_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    _OLLAMA_ENVIRONMENT
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest_record(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


OLLAMA_LAUNCH_ENVIRONMENT_SHA256 = _digest_record(_OLLAMA_ENVIRONMENT)
OLLAMA_GENERATION_ENDPOINT_SHA256 = "sha256:" + hashlib.sha256(
    OLLAMA_GENERATE_URL.encode("ascii")
).hexdigest()

_RECEIPT_AUTHORITY = object()
_REGISTRY_LOCK = threading.RLock()


class ManagedOllamaRuntimeError(RuntimeError):
    """The managed runtime no longer satisfies its trust boundary."""


class ManagedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float) -> int: ...


class VersionRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]: ...


class ProcessLauncher(Protocol):
    def __call__(self, argv: list[str], **kwargs: object) -> ManagedProcess: ...


EndpointInspector = Callable[[], "OllamaEndpointObservation"]
Sleeper = Callable[[float], None]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class OllamaEndpointObservation:
    occupied: bool
    server_version: str | None
    listener_pids: tuple[int, ...]

    def __post_init__(self) -> None:
        if type(self.occupied) is not bool:
            raise ManagedOllamaRuntimeError("endpoint occupation flag is invalid")
        if self.server_version is not None and (
            type(self.server_version) is not str
            or _VERSION.fullmatch(self.server_version) is None
        ):
            raise ManagedOllamaRuntimeError("endpoint server version is invalid")
        if (
            type(self.listener_pids) is not tuple
            or any(type(pid) is not int or pid <= 1 for pid in self.listener_pids)
            or tuple(sorted(set(self.listener_pids))) != self.listener_pids
        ):
            raise ManagedOllamaRuntimeError("endpoint listener PID evidence is invalid")
        if not self.occupied and (
            self.server_version is not None or self.listener_pids
        ):
            raise ManagedOllamaRuntimeError("unoccupied endpoint carries live evidence")


@dataclass(frozen=True, slots=True)
class _RuntimeFileIdentity:
    sha256: str
    size_bytes: int
    device: int
    inode: int
    mode: int
    modified_ns: int


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ManagedOllamaRuntimeReceipt:
    process_id: int
    runtime_version: str
    runtime_binary_sha256: str
    endpoint: str
    generation_endpoint_sha256: str
    launch_environment_sha256: str
    process_environment_sha256: str
    kv_cache_quantization: str
    receipt_sha256: str
    _authority: InitVar[object | None] = None

    def __post_init__(self, _authority: object | None) -> None:
        if _authority is not _RECEIPT_AUTHORITY:
            raise ManagedOllamaRuntimeError(
                "managed Ollama receipts must be launcher-issued"
            )
        _validate_receipt_structure(self)

    def _core_record(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "generation_endpoint_sha256": self.generation_endpoint_sha256,
            "kv_cache_quantization": self.kv_cache_quantization,
            "launch_environment_sha256": self.launch_environment_sha256,
            "process_environment_sha256": self.process_environment_sha256,
            "process_id": self.process_id,
            "runtime_binary_sha256": self.runtime_binary_sha256,
            "runtime_version": self.runtime_version,
            "schema": _RECEIPT_SCHEMA,
        }

    def canonical_record(self) -> dict[str, object]:
        _validate_issued_receipt(self, require_active=False)
        return {**self._core_record(), "receipt_sha256": self.receipt_sha256}


_ISSUED_RECEIPTS: weakref.WeakSet[ManagedOllamaRuntimeReceipt] = weakref.WeakSet()
_ACTIVE_RECEIPTS: weakref.WeakSet[ManagedOllamaRuntimeReceipt] = weakref.WeakSet()
_RECEIPT_MANAGERS: weakref.WeakKeyDictionary[
    ManagedOllamaRuntimeReceipt,
    ManagedOllamaRuntime,
] = weakref.WeakKeyDictionary()


class ManagedOllamaRuntime:
    """Own one admitted Ollama child and its live receipt."""

    __slots__ = (
        "_closed",
        "_endpoint_inspector",
        "_environment",
        "_file_identity",
        "_lock",
        "_process",
        "_receipt",
        "_runtime_binary",
        "__weakref__",
    )

    def __init__(
        self,
        *,
        process: ManagedProcess,
        runtime_binary: Path,
        file_identity: _RuntimeFileIdentity,
        environment: dict[str, str],
        endpoint_inspector: EndpointInspector,
        receipt: ManagedOllamaRuntimeReceipt,
        _authority: object,
    ) -> None:
        if _authority is not _RECEIPT_AUTHORITY:
            raise ManagedOllamaRuntimeError("managed runtime construction is private")
        self._process = process
        self._runtime_binary = runtime_binary
        self._file_identity = file_identity
        self._environment = environment
        self._endpoint_inspector = endpoint_inspector
        self._receipt = receipt
        self._closed = False
        self._lock = threading.RLock()

    @property
    def receipt(self) -> ManagedOllamaRuntimeReceipt:
        return self._receipt

    def __enter__(self) -> ManagedOllamaRuntime:
        require_live_managed_ollama_receipt(self._receipt)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _assert_live(self, receipt: ManagedOllamaRuntimeReceipt) -> None:
        with self._lock:
            if self._closed:
                raise ManagedOllamaRuntimeError("managed Ollama receipt is not active")
            if receipt is not self._receipt:
                raise ManagedOllamaRuntimeError("managed Ollama receipt owner differs")
            _validate_receipt_structure(receipt)
            if (
                type(self._process.pid) is not int
                or self._process.pid != receipt.process_id
                or self._process.poll() is not None
            ):
                raise ManagedOllamaRuntimeError("managed Ollama child is not live")
            if _digest_record(self._environment) != receipt.process_environment_sha256:
                raise ManagedOllamaRuntimeError(
                    "managed Ollama child environment drifted"
                )
            _validate_sanitized_environment(self._environment)
            observation = _inspect(self._endpoint_inspector)
            if observation.server_version != receipt.runtime_version:
                raise ManagedOllamaRuntimeError("managed Ollama endpoint version drifted")
            if observation.listener_pids != (receipt.process_id,):
                raise ManagedOllamaRuntimeError("managed Ollama endpoint ownership drifted")
            if not observation.occupied:
                raise ManagedOllamaRuntimeError("managed Ollama endpoint is not serving")
            current = _hash_runtime_binary(self._runtime_binary)
            if current != self._file_identity:
                raise ManagedOllamaRuntimeError("managed Ollama runtime binary changed")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with _REGISTRY_LOCK:
                _ACTIVE_RECEIPTS.discard(self._receipt)
            _close_owned_process(self._process, self._receipt.process_id)


def launch_managed_v07_ollama(
    *,
    runtime_binary: Path,
    expected_runtime_binary_sha256: str,
    inherited_environment: Mapping[str, str] | None = None,
    version_runner: VersionRunner = subprocess.run,
    process_launcher: ProcessLauncher = subprocess.Popen,
    endpoint_inspector: EndpointInspector | None = None,
    sleeper: Sleeper = time.sleep,
    monotonic_clock: MonotonicClock = time.monotonic,
) -> ManagedOllamaRuntime:
    """Launch and admit the exact owned v0.7 Ollama server."""

    _require_sha256(expected_runtime_binary_sha256, "runtime binary SHA-256")
    if not callable(version_runner) or not callable(process_launcher):
        raise ManagedOllamaRuntimeError("managed Ollama process boundary is invalid")
    if not callable(sleeper) or not callable(monotonic_clock):
        raise ManagedOllamaRuntimeError("managed Ollama clock boundary is invalid")
    inspector = endpoint_inspector or _inspect_loopback_endpoint
    if not callable(inspector):
        raise ManagedOllamaRuntimeError("managed Ollama endpoint inspector is invalid")

    initial = _hash_runtime_binary(runtime_binary)
    if initial.sha256 != expected_runtime_binary_sha256:
        raise ManagedOllamaRuntimeError("managed Ollama runtime SHA-256 differs")
    if _inspect(inspector).occupied:
        raise ManagedOllamaRuntimeError(
            "127.0.0.1:11434 is already occupied; refusing adoption"
        )

    environment = _build_sanitized_environment(
        os.environ if inherited_environment is None else inherited_environment
    )
    process_environment_sha256 = _digest_record(environment)
    _probe_exact_version(
        runtime_binary,
        environment=environment,
        runner=version_runner,
    )
    after_version = _hash_runtime_binary(runtime_binary)
    if after_version != initial:
        raise ManagedOllamaRuntimeError(
            "managed Ollama runtime binary changed during version probe"
        )

    try:
        process = process_launcher(
            [os.fspath(runtime_binary), "serve"],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ManagedOllamaRuntimeError("managed Ollama launch failed") from error

    process_id = _require_process(process)
    try:
        if _digest_record(environment) != process_environment_sha256:
            raise ManagedOllamaRuntimeError(
                "managed Ollama child environment changed during launch"
            )
        deadline = monotonic_clock() + _STARTUP_TIMEOUT_SECONDS
        while True:
            if process.poll() is not None:
                raise ManagedOllamaRuntimeError(
                    "managed Ollama child exited before admission"
                )
            observation = _inspect(inspector)
            if observation.occupied:
                if observation.server_version != _V07_OLLAMA_VERSION:
                    raise ManagedOllamaRuntimeError(
                        "managed Ollama endpoint version differs"
                    )
                if observation.listener_pids != (process_id,):
                    raise ManagedOllamaRuntimeError(
                        "managed Ollama endpoint owner differs"
                    )
                break
            if monotonic_clock() >= deadline:
                raise ManagedOllamaRuntimeError(
                    "managed Ollama endpoint readiness timed out"
                )
            sleeper(_STARTUP_POLL_SECONDS)

        after_admission = _hash_runtime_binary(runtime_binary)
        if after_admission != initial:
            raise ManagedOllamaRuntimeError(
                "managed Ollama runtime binary changed during startup"
            )
        core = {
            "endpoint": OLLAMA_ORIGIN,
            "generation_endpoint_sha256": OLLAMA_GENERATION_ENDPOINT_SHA256,
            "kv_cache_quantization": _V07_OLLAMA_KV_CACHE,
            "launch_environment_sha256": OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
            "process_environment_sha256": process_environment_sha256,
            "process_id": process_id,
            "runtime_binary_sha256": initial.sha256,
            "runtime_version": _V07_OLLAMA_VERSION,
            "schema": _RECEIPT_SCHEMA,
        }
        receipt = ManagedOllamaRuntimeReceipt(
            process_id=process_id,
            runtime_version=_V07_OLLAMA_VERSION,
            runtime_binary_sha256=initial.sha256,
            endpoint=OLLAMA_ORIGIN,
            generation_endpoint_sha256=OLLAMA_GENERATION_ENDPOINT_SHA256,
            launch_environment_sha256=OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
            process_environment_sha256=process_environment_sha256,
            kv_cache_quantization=_V07_OLLAMA_KV_CACHE,
            receipt_sha256=_digest_record(core),
            _authority=_RECEIPT_AUTHORITY,
        )
        runtime = ManagedOllamaRuntime(
            process=process,
            runtime_binary=runtime_binary,
            file_identity=initial,
            environment=environment,
            endpoint_inspector=inspector,
            receipt=receipt,
            _authority=_RECEIPT_AUTHORITY,
        )
        with _REGISTRY_LOCK:
            _ISSUED_RECEIPTS.add(receipt)
            _ACTIVE_RECEIPTS.add(receipt)
            _RECEIPT_MANAGERS[receipt] = runtime
        return runtime
    except BaseException:
        _close_owned_process(process, process_id)
        raise


def require_live_managed_ollama_receipt(
    receipt: ManagedOllamaRuntimeReceipt,
) -> ManagedOllamaRuntimeReceipt:
    """Require an issued receipt whose exact child still owns the endpoint."""

    _validate_issued_receipt(receipt, require_active=True)
    with _REGISTRY_LOCK:
        manager = _RECEIPT_MANAGERS.get(receipt)
    if manager is None:
        raise ManagedOllamaRuntimeError("managed Ollama receipt owner is unavailable")
    manager._assert_live(receipt)
    return receipt


def _validate_issued_receipt(
    receipt: ManagedOllamaRuntimeReceipt,
    *,
    require_active: bool,
) -> None:
    if type(receipt) is not ManagedOllamaRuntimeReceipt:
        raise ManagedOllamaRuntimeError("managed Ollama receipt type is invalid")
    _validate_receipt_structure(receipt)
    with _REGISTRY_LOCK:
        if receipt not in _ISSUED_RECEIPTS:
            raise ManagedOllamaRuntimeError("managed Ollama receipt was not issued")
        if require_active and receipt not in _ACTIVE_RECEIPTS:
            raise ManagedOllamaRuntimeError("managed Ollama receipt is not active")


def _validate_receipt_structure(receipt: ManagedOllamaRuntimeReceipt) -> None:
    if type(receipt.process_id) is not int or receipt.process_id <= 1:
        raise ManagedOllamaRuntimeError("managed Ollama receipt PID is invalid")
    if receipt.runtime_version != _V07_OLLAMA_VERSION:
        raise ManagedOllamaRuntimeError("managed Ollama receipt version is invalid")
    _require_sha256(receipt.runtime_binary_sha256, "receipt runtime binary")
    if receipt.endpoint != OLLAMA_ORIGIN:
        raise ManagedOllamaRuntimeError("managed Ollama receipt endpoint is invalid")
    if receipt.generation_endpoint_sha256 != OLLAMA_GENERATION_ENDPOINT_SHA256:
        raise ManagedOllamaRuntimeError(
            "managed Ollama receipt generation endpoint is invalid"
        )
    if receipt.launch_environment_sha256 != OLLAMA_LAUNCH_ENVIRONMENT_SHA256:
        raise ManagedOllamaRuntimeError(
            "managed Ollama receipt launch environment is invalid"
        )
    _require_sha256(receipt.process_environment_sha256, "receipt child environment")
    if receipt.kv_cache_quantization != _V07_OLLAMA_KV_CACHE:
        raise ManagedOllamaRuntimeError("managed Ollama receipt KV cache is invalid")
    _require_sha256(receipt.receipt_sha256, "managed Ollama receipt")
    if receipt.receipt_sha256 != _digest_record(receipt._core_record()):
        raise ManagedOllamaRuntimeError("managed Ollama receipt digest is invalid")


def _build_sanitized_environment(
    inherited: Mapping[str, str],
) -> dict[str, str]:
    if not isinstance(inherited, Mapping):
        raise ManagedOllamaRuntimeError("inherited environment is invalid")
    environment: dict[str, str] = {}
    for key in _INHERITED_ENVIRONMENT_KEYS:
        if key not in inherited:
            continue
        value = inherited[key]
        if type(value) is not str or not value or "\0" in value:
            raise ManagedOllamaRuntimeError(
                f"inherited environment {key} is invalid"
            )
        if len(value.encode("utf-8")) > 4_096:
            raise ManagedOllamaRuntimeError(
                f"inherited environment {key} exceeds its bound"
            )
        if key in {"HOME", "TMPDIR"} and not Path(value).is_absolute():
            raise ManagedOllamaRuntimeError(
                f"inherited environment {key} must be absolute"
            )
        environment[key] = value
    if "HOME" not in environment:
        raise ManagedOllamaRuntimeError("sanitized environment requires HOME")
    environment.update(_OLLAMA_ENVIRONMENT)
    _validate_sanitized_environment(environment)
    return environment


def _validate_sanitized_environment(environment: Mapping[str, str]) -> None:
    expected_keys = set(_OLLAMA_ENVIRONMENT) | {
        key for key in _INHERITED_ENVIRONMENT_KEYS if key in environment
    }
    if set(environment) != expected_keys:
        raise ManagedOllamaRuntimeError(
            "managed Ollama child environment contains an unapproved key"
        )
    if any(environment.get(key) != value for key, value in _OLLAMA_ENVIRONMENT.items()):
        raise ManagedOllamaRuntimeError(
            "managed Ollama child environment differs from frozen policy"
        )
    if "HOME" not in environment:
        raise ManagedOllamaRuntimeError("managed Ollama child environment lacks HOME")
    for key, value in environment.items():
        if type(key) is not str or type(value) is not str or not value or "\0" in value:
            raise ManagedOllamaRuntimeError(
                "managed Ollama child environment value is invalid"
            )


def _probe_exact_version(
    runtime_binary: Path,
    *,
    environment: dict[str, str],
    runner: VersionRunner,
) -> None:
    try:
        completed = runner(
            [os.fspath(runtime_binary), "--version"],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=_VERSION_TIMEOUT_SECONDS,
            env=dict(environment),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ManagedOllamaRuntimeError("managed Ollama version probe failed") from error
    if type(completed.returncode) is not int or completed.returncode != 0:
        raise ManagedOllamaRuntimeError("managed Ollama version probe returned non-zero")
    if type(completed.stdout) is not bytes or type(completed.stderr) is not bytes:
        raise ManagedOllamaRuntimeError("managed Ollama version probe is not bytes")
    if (
        len(completed.stdout) > _MAX_VERSION_OUTPUT_BYTES
        or len(completed.stderr) > _MAX_VERSION_OUTPUT_BYTES
    ):
        raise ManagedOllamaRuntimeError("managed Ollama version output is too large")
    # Ollama 0.31.1 consults the local server from ``--version``.  This
    # boundary has just proved that no server owns the endpoint, so the pinned
    # Homebrew binary emits this exact client-version form.  Accepting the
    # older server-present form here would contradict the empty-endpoint
    # admission that precedes this probe.
    expected = (
        "Warning: could not connect to a running Ollama instance\n"
        f"Warning: client version is {_V07_OLLAMA_VERSION}\n"
    ).encode("ascii")
    if completed.stdout != expected or completed.stderr:
        raise ManagedOllamaRuntimeError("managed Ollama version differs from v0.7")


def _require_process(process: object) -> int:
    process_id = getattr(process, "pid", None)
    if type(process_id) is not int or process_id <= 1:
        raise ManagedOllamaRuntimeError("managed Ollama child PID is invalid")
    for operation in ("poll", "terminate", "kill", "wait"):
        if not callable(getattr(process, operation, None)):
            raise ManagedOllamaRuntimeError(
                "managed Ollama child process interface is invalid"
            )
    return process_id


def _close_owned_process(process: ManagedProcess, expected_process_id: int) -> None:
    if type(getattr(process, "pid", None)) is not int:
        return
    if process.pid != expected_process_id:
        return
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_KILL_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return


def _inspect(inspector: EndpointInspector) -> OllamaEndpointObservation:
    try:
        observation = inspector()
    except (OSError, subprocess.SubprocessError) as error:
        raise ManagedOllamaRuntimeError(
            "managed Ollama endpoint inspection failed"
        ) from error
    if type(observation) is not OllamaEndpointObservation:
        raise ManagedOllamaRuntimeError(
            "managed Ollama endpoint inspector returned an invalid type"
        )
    observation.__post_init__()
    return observation


def _hash_runtime_binary(path: Path) -> _RuntimeFileIdentity:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise ManagedOllamaRuntimeError(
            "managed Ollama runtime must be an absolute non-symlink Path"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(os.fspath(path), flags)
    except OSError as error:
        raise ManagedOllamaRuntimeError("managed Ollama runtime is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > _MAX_RUNTIME_BYTES
            or not before.st_mode & stat.S_IXUSR
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ManagedOllamaRuntimeError(
                "managed Ollama runtime file identity is unsafe"
            )
        link = os.stat(path, follow_symlinks=False)
        if (link.st_dev, link.st_ino) != (before.st_dev, before.st_ino):
            raise ManagedOllamaRuntimeError(
                "managed Ollama runtime link identity changed"
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_RUNTIME_BYTES:
                raise ManagedOllamaRuntimeError(
                    "managed Ollama runtime exceeds its size bound"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
        if size != before.st_size or any(
            getattr(before, field) != getattr(after, field) for field in fields
        ):
            raise ManagedOllamaRuntimeError(
                "managed Ollama runtime changed while hashing"
            )
        link_after = os.stat(path, follow_symlinks=False)
        if (link_after.st_dev, link_after.st_ino) != (after.st_dev, after.st_ino):
            raise ManagedOllamaRuntimeError(
                "managed Ollama runtime link identity changed"
            )
        return _RuntimeFileIdentity(
            "sha256:" + digest.hexdigest(),
            size,
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_mtime_ns,
        )
    finally:
        os.close(descriptor)


def _inspect_loopback_endpoint() -> OllamaEndpointObservation:
    occupied = _loopback_port_is_open()
    if not occupied:
        return OllamaEndpointObservation(False, None, ())
    return OllamaEndpointObservation(
        True,
        _read_loopback_server_version(),
        _read_loopback_listener_pids(),
    )


def _loopback_port_is_open() -> bool:
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        connection.settimeout(0.25)
        return connection.connect_ex(("127.0.0.1", 11434)) == 0
    finally:
        connection.close()


def _read_loopback_server_version() -> str | None:
    request = urllib.request.Request(
        OLLAMA_VERSION_URL,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        response = open_ollama_http(request, timeout=0.5)
        with response:
            require_exact_ollama_response(
                response,
                expected_url=OLLAMA_VERSION_URL,
            )
            payload = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
        if type(payload) is not bytes or len(payload) > _MAX_HTTP_RESPONSE_BYTES:
            return None
        record = parse_strict_json_object(payload)
    except (OSError, ValueError):
        return None
    if set(record) != {"version"}:
        return None
    version = record.get("version")
    if type(version) is not str or _VERSION.fullmatch(version) is None:
        return None
    return version


def _read_loopback_listener_pids() -> tuple[int, ...]:
    candidates = (Path("/usr/sbin/lsof"), Path("/usr/bin/lsof"))
    executable = next(
        (
            candidate
            for candidate in candidates
            if candidate.is_file()
            and not candidate.is_symlink()
            and os.access(candidate, os.X_OK)
        ),
        None,
    )
    if executable is None:
        return ()
    try:
        completed = subprocess.run(
            [
                os.fspath(executable),
                "-nP",
                "-a",
                "-iTCP@127.0.0.1:11434",
                "-sTCP:LISTEN",
                "-Fp",
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=2.0,
            env={"LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if (
        completed.returncode != 0
        or type(completed.stdout) is not bytes
        or type(completed.stderr) is not bytes
        or completed.stderr
        or len(completed.stdout) > 65_536
    ):
        return ()
    pids: set[int] = set()
    for line in completed.stdout.splitlines():
        if line.startswith(b"f") and line[1:].isdigit():
            continue
        if not line.startswith(b"p") or not line[1:].isdigit():
            return ()
        pid = int(line[1:])
        if pid <= 1:
            return ()
        pids.add(pid)
    return tuple(sorted(pids))


def _require_sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ManagedOllamaRuntimeError(f"{field} is invalid")
    return value


__all__ = (
    "ManagedOllamaRuntime",
    "ManagedOllamaRuntimeError",
    "ManagedOllamaRuntimeReceipt",
    "OLLAMA_GENERATION_ENDPOINT_SHA256",
    "OLLAMA_LAUNCH_ENVIRONMENT_SHA256",
    "OllamaEndpointObservation",
    "V07_OLLAMA_ENVIRONMENT",
    "launch_managed_v07_ollama",
    "require_live_managed_ollama_receipt",
)
