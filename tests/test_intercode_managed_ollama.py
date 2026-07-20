from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from edgeloopbench.intercode_managed_ollama import (
    OLLAMA_GENERATION_ENDPOINT_SHA256,
    OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
    V07_OLLAMA_ENVIRONMENT,
    ManagedOllamaRuntimeError,
    ManagedOllamaRuntimeReceipt,
    OllamaEndpointObservation,
    _read_loopback_listener_pids,
    launch_managed_v07_ollama,
    require_live_managed_ollama_receipt,
)


def tagged(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class FakeVersionRunner:
    def __init__(
        self,
        stdout: bytes = (
            b"Warning: could not connect to a running Ollama instance\n"
            b"Warning: client version is 0.31.1\n"
        ),
        *,
        returncode: int = 0,
        stderr: bytes = b"",
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.before_return = None

    def __call__(
        self,
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((tuple(argv), dict(kwargs)))
        if self.before_return is not None:
            self.before_return()
        return subprocess.CompletedProcess(
            argv,
            self.returncode,
            self.stdout,
            self.stderr,
        )


class FakeProcess:
    def __init__(self, pid: int = 4242, *, terminate_times_out: bool = False) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminate_times_out = terminate_times_out
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self.terminate_times_out:
            self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired(("ollama", "serve"), timeout)
        return self.returncode


class FakeLauncher:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> FakeProcess:
        self.calls.append((tuple(argv), dict(kwargs)))
        return self.process


class SequenceEndpointInspector:
    def __init__(self, *observations: OllamaEndpointObservation) -> None:
        self.observations = observations
        self.calls = 0

    def __call__(self) -> OllamaEndpointObservation:
        index = min(self.calls, len(self.observations) - 1)
        self.calls += 1
        return self.observations[index]


EMPTY_ENDPOINT = OllamaEndpointObservation(False, None, ())
OWNED_ENDPOINT = OllamaEndpointObservation(True, "0.31.1", (4242,))


class ManagedOllamaRuntimeTests(unittest.TestCase):
    def test_macos_lsof_pid_output_accepts_its_mandatory_fd_record(self) -> None:
        completed = subprocess.CompletedProcess(
            ["/usr/sbin/lsof"],
            0,
            stdout=b"p4242\nf3\n",
            stderr=b"",
        )
        with patch(
            "edgeloopbench.intercode_managed_ollama.subprocess.run",
            return_value=completed,
        ):
            self.assertEqual(_read_loopback_listener_pids(), (4242,))

        for malformed in (b"p4242\nfX\n", b"p4242\nnlocalhost\n", b"f3\n"):
            with self.subTest(malformed=malformed), patch(
                "edgeloopbench.intercode_managed_ollama.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ["/usr/sbin/lsof"],
                    0,
                    stdout=malformed,
                    stderr=b"",
                ),
            ):
                self.assertEqual(_read_loopback_listener_pids(), ())

    def launch_fixture(
        self,
        binary: Path,
        *,
        process: FakeProcess | None = None,
        inspector: SequenceEndpointInspector | None = None,
        runner: FakeVersionRunner | None = None,
        inherited_environment: dict[str, str] | None = None,
    ):  # type: ignore[no-untyped-def]
        process = process or FakeProcess()
        launcher = FakeLauncher(process)
        runtime = launch_managed_v07_ollama(
            runtime_binary=binary,
            expected_runtime_binary_sha256=tagged(binary.read_bytes()),
            inherited_environment=inherited_environment
            or {
                "HOME": "/Users/tester",
                "LANG": "en_US.UTF-8",
                "PATH": "/untrusted/bin",
                "http_proxy": "http://proxy.invalid",
                "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib",
                "OLLAMA_HOST": "0.0.0.0:11434",
                "OLLAMA_KV_CACHE_TYPE": "f16",
            },
            version_runner=runner or FakeVersionRunner(),
            process_launcher=launcher,
            endpoint_inspector=inspector
            or SequenceEndpointInspector(EMPTY_ENDPOINT, OWNED_ENDPOINT),
            sleeper=lambda _seconds: None,
        )
        return runtime, launcher

    def test_launches_exact_binary_without_shell_and_with_sanitized_frozen_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned ollama")
            binary.chmod(0o755)
            runner = FakeVersionRunner()
            runtime, launcher = self.launch_fixture(binary, runner=runner)
            receipt = runtime.receipt

            version_argv, version_kwargs = runner.calls[0]
            serve_argv, serve_kwargs = launcher.calls[0]
            environment = serve_kwargs["env"]

            self.assertEqual(version_argv, (os.fspath(binary), "--version"))
            self.assertIs(version_kwargs["shell"], False)
            self.assertEqual(serve_argv, (os.fspath(binary), "serve"))
            self.assertIs(serve_kwargs["shell"], False)
            self.assertIs(serve_kwargs["start_new_session"], True)
            self.assertEqual(
                environment,
                {
                    "HOME": "/Users/tester",
                    "LANG": "en_US.UTF-8",
                    **dict(V07_OLLAMA_ENVIRONMENT),
                },
            )
            self.assertEqual(
                receipt.launch_environment_sha256,
                OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
            )
            self.assertEqual(
                receipt.generation_endpoint_sha256,
                OLLAMA_GENERATION_ENDPOINT_SHA256,
            )
            self.assertEqual(receipt.kv_cache_quantization, "q8_0")
            self.assertNotIn(directory, json.dumps(receipt.canonical_record()))
            runtime.close()

    def test_preexisting_loopback_service_fails_before_process_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned ollama")
            binary.chmod(0o755)
            launcher = FakeLauncher(FakeProcess())
            with self.assertRaisesRegex(
                ManagedOllamaRuntimeError,
                "already occupied",
            ):
                launch_managed_v07_ollama(
                    runtime_binary=binary,
                    expected_runtime_binary_sha256=tagged(binary.read_bytes()),
                    inherited_environment={"HOME": "/Users/tester"},
                    version_runner=FakeVersionRunner(),
                    process_launcher=launcher,
                    endpoint_inspector=SequenceEndpointInspector(
                        OllamaEndpointObservation(True, "0.31.1", (77,))
                    ),
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual(launcher.calls, [])

    def test_wrong_version_or_listener_owner_is_rejected_and_child_is_closed(self) -> None:
        cases = (
            ("version", OllamaEndpointObservation(True, "0.31.2", (4242,))),
            ("owner", OllamaEndpointObservation(True, "0.31.1", (9999,))),
            ("shared owner", OllamaEndpointObservation(True, "0.31.1", (4242, 9999))),
        )
        for label, observation in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "ollama"
                binary.write_bytes(b"pinned ollama")
                binary.chmod(0o755)
                process = FakeProcess()
                with self.assertRaises(ManagedOllamaRuntimeError):
                    self.launch_fixture(
                        binary,
                        process=process,
                        inspector=SequenceEndpointInspector(
                            EMPTY_ENDPOINT,
                            observation,
                        ),
                    )
                self.assertEqual(process.terminate_calls, 1)

    def test_receipt_forgery_replacement_and_live_drift_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned ollama")
            binary.chmod(0o755)
            inspector = SequenceEndpointInspector(
                EMPTY_ENDPOINT,
                OWNED_ENDPOINT,
                OllamaEndpointObservation(True, "0.31.1", (9999,)),
            )
            runtime, _launcher = self.launch_fixture(binary, inspector=inspector)
            receipt = runtime.receipt

            with self.assertRaises(ManagedOllamaRuntimeError):
                dataclasses.replace(receipt)

            forged = object.__new__(ManagedOllamaRuntimeReceipt)
            for field in dataclasses.fields(receipt):
                object.__setattr__(forged, field.name, getattr(receipt, field.name))
            with self.assertRaisesRegex(ManagedOllamaRuntimeError, "issued"):
                require_live_managed_ollama_receipt(forged)

            with self.assertRaisesRegex(ManagedOllamaRuntimeError, "ownership"):
                require_live_managed_ollama_receipt(receipt)
            runtime.close()

    def test_child_environment_drift_invalidates_the_live_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned ollama")
            binary.chmod(0o755)
            runtime, launcher = self.launch_fixture(binary)
            environment = launcher.calls[0][1]["env"]
            environment["OLLAMA_KV_CACHE_TYPE"] = "f16"

            with self.assertRaisesRegex(ManagedOllamaRuntimeError, "environment"):
                require_live_managed_ollama_receipt(runtime.receipt)
            runtime.close()

    def test_binary_replacement_during_version_probe_is_rejected_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned ollama")
            binary.chmod(0o755)
            runner = FakeVersionRunner()
            runner.before_return = lambda: binary.write_bytes(b"replacement")
            launcher = FakeLauncher(FakeProcess())

            with self.assertRaisesRegex(ManagedOllamaRuntimeError, "changed"):
                launch_managed_v07_ollama(
                    runtime_binary=binary,
                    expected_runtime_binary_sha256=tagged(b"pinned ollama"),
                    inherited_environment={"HOME": "/Users/tester"},
                    version_runner=runner,
                    process_launcher=launcher,
                    endpoint_inspector=SequenceEndpointInspector(EMPTY_ENDPOINT),
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual(launcher.calls, [])

    def test_version_probe_is_exact_bounded_and_fail_closed(self) -> None:
        runners = (
            FakeVersionRunner(b"Warning: client version is 0.31.1\n"),
            FakeVersionRunner(
                b"Warning: could not connect to a running Ollama instance\n"
                b"Warning: client version is 0.31.2\n"
            ),
            FakeVersionRunner(b"ollama version is 0.31.2\n"),
            FakeVersionRunner(returncode=1),
            FakeVersionRunner(stderr=b"warning"),
            FakeVersionRunner(b"x" * 65_537),
        )
        for runner in runners:
            with self.subTest(runner=runner), tempfile.TemporaryDirectory() as directory:
                binary = Path(directory) / "ollama"
                binary.write_bytes(b"pinned ollama")
                binary.chmod(0o755)
                launcher = FakeLauncher(FakeProcess())
                with self.assertRaises(ManagedOllamaRuntimeError):
                    launch_managed_v07_ollama(
                        runtime_binary=binary,
                        expected_runtime_binary_sha256=tagged(binary.read_bytes()),
                        inherited_environment={"HOME": "/Users/tester"},
                        version_runner=runner,
                        process_launcher=launcher,
                        endpoint_inspector=SequenceEndpointInspector(EMPTY_ENDPOINT),
                        sleeper=lambda _seconds: None,
                    )
                self.assertEqual(launcher.calls, [])

    def test_close_is_bounded_owned_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "ollama"
            binary.write_bytes(b"pinned ollama")
            binary.chmod(0o755)
            process = FakeProcess(terminate_times_out=True)
            runtime, _launcher = self.launch_fixture(binary, process=process)
            receipt = runtime.receipt

            runtime.close()
            runtime.close()

            self.assertEqual(process.terminate_calls, 1)
            self.assertEqual(process.kill_calls, 1)
            self.assertEqual(len(process.wait_calls), 2)
            with self.assertRaisesRegex(ManagedOllamaRuntimeError, "active"):
                require_live_managed_ollama_receipt(receipt)


if __name__ == "__main__":
    unittest.main()
