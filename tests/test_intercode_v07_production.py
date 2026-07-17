from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edgeloopbench.intercode_v07_production import (
    V07ProductionConfig,
    V07ProductionError,
    V07ProductionPreflight,
    execute_v07_production,
    inspect_v07_production_preflight,
)


class _Disk:
    f_frsize = 4096
    f_bavail = 9_000_000


class V07ProductionPreflightTests(unittest.TestCase):
    def test_execute_requires_tokenizer_before_creating_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executables = []
            for name in ("docker", "ollama"):
                path = root / name
                path.write_bytes(name.encode("ascii"))
                path.chmod(0o755)
                executables.append(path)
            models = root / "models"
            models.mkdir()
            config = V07ProductionConfig(
                repository_root=root,
                artifact_root=root / "run",
                docker_binary=executables[0],
                docker_endpoint="unix:///tmp/docker.sock",
                docker_data_path=root,
                ollama_binary=executables[1],
                ollama_models_root=models,
                tokenizer_helper=root / "llama-tokenize",
            )
            allowed = V07ProductionPreflight(
                vm_pressure_level=1,
                free_memory_percent=41,
                disk_free_bytes=40 << 30,
                reasons=(),
            )

            with mock.patch(
                "edgeloopbench.intercode_v07_production."
                "inspect_v07_production_preflight",
                return_value=allowed,
            ):
                with self.assertRaises(V07ProductionError):
                    execute_v07_production(config)

            self.assertFalse(config.artifact_root.exists())

    def test_preflight_does_not_require_tokenizer_provisioning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executables = []
            for name in ("docker", "ollama"):
                path = root / name
                path.write_bytes(name.encode("ascii"))
                path.chmod(0o755)
                executables.append(path)
            models = root / "models"
            models.mkdir()
            tokenizer = root / "build" / "artifacts" / "llama-tokenize"
            config = V07ProductionConfig(
                repository_root=root,
                artifact_root=root / "run",
                docker_binary=executables[0],
                docker_endpoint="unix:///tmp/docker.sock",
                docker_data_path=root,
                ollama_binary=executables[1],
                ollama_models_root=models,
                tokenizer_helper=tokenizer,
            )

            def runner(argv, **_kwargs):  # type: ignore[no-untyped-def]
                output = (
                    b"1\n"
                    if argv[-1] == "kern.memorystatus_vm_pressure_level"
                    else b"System-wide memory free percentage: 41%\n"
                )
                return subprocess.CompletedProcess(argv, 0, output, b"")

            snapshot = inspect_v07_production_preflight(
                config,
                runner=runner,
                statvfs=lambda _path: _Disk(),
            )

            self.assertTrue(snapshot.allowed)
            self.assertFalse(tokenizer.exists())
            self.assertFalse(config.artifact_root.exists())

    def test_preflight_is_read_only_and_reports_the_exact_frozen_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = []
            for name in ("docker", "ollama", "llama-tokenize"):
                path = root / name
                path.write_bytes(name.encode("ascii"))
                path.chmod(0o755)
                inputs.append(path)
            models = root / "models"
            models.mkdir()
            config = V07ProductionConfig(
                repository_root=root,
                artifact_root=root / "run",
                docker_binary=inputs[0],
                docker_endpoint="unix:///tmp/docker.sock",
                docker_data_path=root,
                ollama_binary=inputs[1],
                ollama_models_root=models,
                tokenizer_helper=inputs[2],
            )
            calls: list[tuple[str, ...]] = []

            def runner(argv, **_kwargs):  # type: ignore[no-untyped-def]
                calls.append(tuple(argv))
                if argv[-1] == "kern.memorystatus_vm_pressure_level":
                    output = b"1\n"
                else:
                    output = (
                        b"The system has 17179869184 (1048576 pages with a "
                        b"page size of 16384).\n"
                        b"System-wide memory free percentage: 41%\n"
                    )
                return subprocess.CompletedProcess(argv, 0, output, b"")

            snapshot = inspect_v07_production_preflight(
                config,
                runner=runner,
                statvfs=lambda _path: _Disk(),
            )

            def invalid_geometry_runner(
                argv, **_kwargs
            ):  # type: ignore[no-untyped-def]
                output = (
                    b"1\n"
                    if argv[-1] == "kern.memorystatus_vm_pressure_level"
                    else (
                        b"The system has 17179869185 (1048576 pages with a "
                        b"page size of 16384).\n"
                        b"System-wide memory free percentage: 41%\n"
                    )
                )
                return subprocess.CompletedProcess(argv, 0, output, b"")

            with self.assertRaisesRegex(V07ProductionError, "geometry"):
                inspect_v07_production_preflight(
                    config,
                    runner=invalid_geometry_runner,
                    statvfs=lambda _path: _Disk(),
                )

        self.assertTrue(snapshot.allowed)
        self.assertEqual(snapshot.vm_pressure_level, 1)
        self.assertEqual(snapshot.free_memory_percent, 41)
        self.assertGreaterEqual(snapshot.disk_free_bytes, 32 << 30)
        self.assertEqual(len(calls), 2)
        self.assertFalse(config.artifact_root.exists())

    def test_warning_pressure_is_a_hard_stop_even_with_disk_and_free_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inputs = []
            for name in ("docker", "ollama", "llama-tokenize"):
                path = root / name
                path.write_bytes(name.encode("ascii"))
                path.chmod(0o755)
                inputs.append(path)
            models = root / "models"
            models.mkdir()
            config = V07ProductionConfig(
                repository_root=root,
                artifact_root=root / "run",
                docker_binary=inputs[0],
                docker_endpoint="unix:///tmp/docker.sock",
                docker_data_path=root,
                ollama_binary=inputs[1],
                ollama_models_root=models,
                tokenizer_helper=inputs[2],
            )

            def runner(argv, **_kwargs):  # type: ignore[no-untyped-def]
                output = (
                    b"2\n"
                    if argv[-1] == "kern.memorystatus_vm_pressure_level"
                    else b"System-wide memory free percentage: 50%\n"
                )
                return subprocess.CompletedProcess(argv, 0, output, b"")

            snapshot = inspect_v07_production_preflight(
                config,
                runner=runner,
                statvfs=lambda _path: _Disk(),
            )

        self.assertFalse(snapshot.allowed)
        self.assertEqual(snapshot.reasons, ("vm_pressure",))


if __name__ == "__main__":
    unittest.main()
