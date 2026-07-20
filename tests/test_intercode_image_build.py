from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import shutil
import subprocess
import stat
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import edgeloopbench.intercode_image_build as image_build_module
from edgeloopbench.intercode_gate_manifest import HostSafetyPins
from edgeloopbench.intercode_host_safety import (
    DockerTelemetryPins,
    HostSafetyPolicy,
    HostTelemetryCollector,
)
from edgeloopbench.intercode_image_build import (
    DOCKERFILE_AGENT_SHA256,
    DOCKERIGNORE_SHA256,
    InterCodeImageBuildError,
    InterCodeImageBuildRequest,
    create_intercode_image_build_plan,
    execute_intercode_image_build,
    main,
)
from edgeloopbench.intercode_local_model import (
    OLLAMA_GENERATION_ENDPOINT_SHA256,
    OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
)


ROOT = Path(__file__).resolve().parents[1]
REVISION = "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8"


def tagged(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def replace_regular_file(path: Path, payload: bytes = b'{"replacement":true}\n') -> None:
    path.unlink()
    path.write_bytes(payload)
    path.chmod(0o600)


def replace_private_parent(manifest: Path) -> None:
    original = manifest.parent.with_name(manifest.parent.name + "-original")
    manifest.parent.rename(original)
    manifest.parent.mkdir(mode=0o700)
    manifest.write_bytes(b'{"replacement":true}\n')
    manifest.chmod(0o600)


class PlanFixture:
    def __init__(self, root: Path) -> None:
        root = root.resolve()
        self.repo = root / "repo"
        (self.repo / "docker").mkdir(parents=True)
        shutil.copytree(ROOT / "docker/intercode", self.repo / "docker/intercode")
        upstream = self.repo / "vendor/intercode" / REVISION / "docker"
        upstream.mkdir(parents=True)
        shutil.copy2(
            ROOT / "vendor/intercode" / REVISION / "docker/docker.gitignore",
            upstream / "docker.gitignore",
        )
        shutil.copy2(ROOT / ".dockerignore", self.repo / ".dockerignore")
        self.binary = root / "docker-real"
        self.binary.write_bytes(b"fixed fake Docker client for offline tests\n")
        self.binary.chmod(0o700)
        self.binary_sha256 = tagged(self.binary.read_bytes())
        self.pins = DockerTelemetryPins(
            endpoint="unix:///tmp/edgeloop-test-docker.sock",
            client_version="27.3.1",
            server_version="27.3.1",
            binary_sha256=self.binary_sha256,
        )

    def request(self) -> InterCodeImageBuildRequest:
        return InterCodeImageBuildRequest(
            repo_root=self.repo,
            docker_binary=self.binary,
            docker_pins=self.pins,
        )

    def admission(
        self,
        *,
        running_containers: tuple[str, ...] = (),
        resident_models: bool = False,
    ) -> tuple[HostTelemetryCollector, HostSafetyPolicy, "TelemetryRunner"]:
        runner = TelemetryRunner(
            self,
            running_containers=running_containers,
        )
        models = []
        if resident_models:
            models.append(
                {
                    "model": "qwen3.5:4b",
                    "digest": "b" * 64,
                }
            )
        collector = HostTelemetryCollector(
            docker_binary=self.binary,
            docker_pins=self.pins,
            docker_data_path=self.repo,
            environment={},
            runner=runner,
            urlopen=FakeUrlOpen(json.dumps({"models": models}).encode()),
            statvfs=lambda _path: FakeStatVfs(),
            time_ns=lambda: 1_800_000_000_000_000_000,
            monotonic_ns=lambda: 55_000_000_000,
        )
        policy_pins = HostSafetyPins(
            policy_sha256=tagged(b"host-policy"),
            telemetry_collector_sha256=tagged(b"host-collector"),
            docker_binary_sha256=self.binary_sha256,
            docker_endpoint_sha256=self.pins.endpoint_sha256,
            docker_client_version=self.pins.client_version,
            docker_server_version=self.pins.server_version,
            ollama_runtime_binary_sha256=tagged(b"ollama-runtime"),
            ollama_server_version="0.31.1",
            ollama_launch_environment_sha256=OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
            ollama_generation_endpoint_sha256=OLLAMA_GENERATION_ENDPOINT_SHA256,
        )
        return collector, HostSafetyPolicy(policy_pins), runner


@dataclass
class FakeStatVfs:
    f_bavail: int = 20_000_000
    f_frsize: int = 4096


class FakeUrlOpen:
    class Response:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self) -> "FakeUrlOpen.Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return self.payload[:limit]

        def geturl(self) -> str:
            return "http://127.0.0.1:11434/api/ps"

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __call__(self, request: object, timeout: float) -> "FakeUrlOpen.Response":
        del request, timeout
        return self.Response(self.payload)


class TelemetryRunner:
    def __init__(
        self,
        fixture: PlanFixture,
        *,
        running_containers: tuple[str, ...] = (),
    ) -> None:
        self.fixture = fixture
        self.running_containers = running_containers
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(tuple(argv))
        key = tuple(argv)
        outputs = {
            ("/usr/bin/pmset", "-g", "batt"): (
                b"Now drawing from 'AC Power'\n",
                b"",
            ),
            ("/usr/bin/pmset", "-g", "custom"): (
                b"Battery Power:\n lowpowermode 1\nAC Power:\n lowpowermode 0\n",
                b"",
            ),
            ("/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"): (
                b"1\n",
                b"",
            ),
            ("/usr/sbin/sysctl", "-n", "vm.swapusage"): (
                b"total = 5120.00M  used = 10.00M  free = 5110.00M  (encrypted)\n",
                b"",
            ),
            ("/usr/bin/memory_pressure", "-Q"): (
                b"System-wide memory free percentage: 47%\n",
                b"",
            ),
            ("/usr/bin/pmset", "-g", "therm"): (
                b"No thermal warning level has been recorded\n"
                b"No performance warning level has been recorded\n",
                b"",
            ),
            ("/usr/sbin/sysctl", "-n", "kern.boottime"): (
                b"{ sec = 1784098183, usec = 710968 } Wed Jul 15 14:49:43 2026\n",
                b"",
            ),
        }
        if key in outputs:
            stdout, stderr = outputs[key]
        elif key[-3:] == ("version", "--format", "{{json .}}"):
            stdout = json.dumps(
                {
                    "Client": {"Version": self.fixture.pins.client_version},
                    "Server": {"Version": self.fixture.pins.server_version},
                },
                separators=(",", ":"),
            ).encode()
            stderr = b""
        elif key[-6:] == (
            "container",
            "ls",
            "--quiet",
            "--no-trunc",
            "--filter",
            "status=running",
        ):
            stdout = "".join(value + "\n" for value in self.running_containers).encode()
            stderr = b""
        else:
            raise AssertionError(f"unexpected telemetry argv: {key!r}")
        self.assert_safe_kwargs(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout, stderr)

    @staticmethod
    def assert_safe_kwargs(kwargs: dict[str, object]) -> None:
        if kwargs.get("shell") is not False or kwargs.get("env") != {}:
            raise AssertionError(f"unsafe telemetry invocation: {kwargs!r}")


class FakeDockerRunner:
    def __init__(self, *, tamper_profile: int | None = None) -> None:
        self.tamper_profile = tamper_profile
        self.calls: list[tuple[str, ...]] = []
        self.build_calls: list[tuple[str, ...]] = []
        self.images_by_id: dict[str, dict[str, object]] = {}

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        if kwargs.get("shell") is not False or kwargs.get("env") != {}:
            raise AssertionError(f"unsafe Docker invocation: {kwargs!r}")
        arguments = tuple(argv[3:])
        if arguments[:2] == ("image", "build"):
            self.build_calls.append(tuple(argv))
            if "--tag" in arguments:
                raise AssertionError("image builds must not create a tag")
            version = int(
                arguments[arguments.index("--build-arg") + 1].removeprefix(
                    "FILE_SYSTEM_VERSION="
                )
            )
            iidfile = Path(arguments[arguments.index("--iidfile") + 1])
            metadata = iidfile.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_size != 0
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise AssertionError("iidfile was not precreated mode 0600")
            plan_label = next(
                arguments[index + 1]
                for index, value in enumerate(arguments)
                if value == "--label"
                and arguments[index + 1].startswith(
                    "org.edgeloopbench.build.plan-sha256="
                )
            )
            image_id = tagged(f"{plan_label}:fs{version}".encode())
            iidfile.unlink()
            iidfile.write_text(image_id, encoding="ascii")
            iidfile.chmod(0o644)
            labels = {
                "org.opencontainers.image.source": (
                    "https://github.com/princeton-nlp/intercode"
                ),
                "org.opencontainers.image.revision": REVISION,
                "org.edgeloopbench.role": "agent",
                "org.edgeloopbench.runtime-network": "none-required",
                "org.edgeloopbench.filesystem-version": str(version),
                "org.edgeloopbench.state-collector.profile": f"fs{version}",
                "org.edgeloopbench.state-collector.sha256": (
                    "sha256:28cdd90502bb9b5d6ede8800bde5378a9f828ade09f97c08f60f49201626f6f5"
                ),
                "org.edgeloopbench.state-collector.policy-sha256": (
                    "sha256:1645f88e660e5c002af6a9b2a20aba06a8003cd4068008e38b417dd704b70794"
                ),
                "org.edgeloopbench.state-collector.root-baseline-sha256": (
                    "sha256:06dcf54e33c9412b1c0bb2cf7ddab33848169e640012209b9d05c81ee1da457f"
                ),
                "org.edgeloopbench.state-collector.profile-set-sha256": (
                    "sha256:19e2b86952ab1bb93d6a4648d00d200421cd328064e6caf6da4575e9a194c8d3"
                ),
                "org.edgeloopbench.state-collector.argv": (
                    "/usr/bin/python3 -I -S -B "
                    "/opt/edgeloop/state_collector.py --profile fsN"
                ),
            }
            for index, argument in enumerate(arguments):
                if argument == "--label":
                    key, value = arguments[index + 1].split("=", 1)
                    labels[key] = value
            if self.tamper_profile == version:
                labels["org.edgeloopbench.state-collector.profile"] = "fs4"
            item: dict[str, object] = {
                "Id": image_id,
                "Os": "linux",
                "Architecture": "arm64",
                "RepoTags": [],
                "Config": {"Labels": labels},
            }
            self.images_by_id[image_id] = item
            return subprocess.CompletedProcess(argv, 0, "", "")
        if arguments[:4] == ("image", "inspect", "--format", "{{json .}}"):
            reference = arguments[-1]
            item = self.images_by_id.get(reference)
            if item is None:
                return subprocess.CompletedProcess(argv, 1, "", "missing\n")
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n",
                "",
            )
        raise AssertionError(f"unexpected Docker argv: {tuple(argv)!r}")


class InterCodeIidFileTests(unittest.TestCase):
    @staticmethod
    def project(
        path: Path,
        payload: bytes = b"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        *,
        mode: int = 0o644,
    ) -> None:
        path.unlink()
        path.write_bytes(payload)
        path.chmod(mode)

    def test_accepts_docker_remove_and_recreate_inside_private_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "image.iid"
            iid = image_build_module._IidFile(path)
            try:
                reserved = path.stat(follow_symlinks=False)
                image_id = "sha256:" + "a" * 64
                self.project(path)
                projected = path.stat(follow_symlinks=False)

                self.assertNotEqual(
                    (reserved.st_dev, reserved.st_ino),
                    (projected.st_dev, projected.st_ino),
                )
                self.assertEqual(iid.read_image_id(), image_id)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                iid.remove_after_success()
                self.assertFalse(path.exists())
            finally:
                iid.close()

    def test_rejects_in_place_write_that_does_not_replace_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "image.iid"
            iid = image_build_module._IidFile(path)
            try:
                path.write_text("sha256:" + "a" * 64, encoding="ascii")
                with self.assertRaises(InterCodeImageBuildError):
                    iid.read_image_id()
            finally:
                iid.close()

    def test_rejects_unsafe_docker_projection_types_modes_and_sizes(self) -> None:
        cases = ("symlink", "fifo", "hardlink", "mode", "oversized")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory) / "private"
                parent.mkdir(mode=0o700)
                path = parent / "image.iid"
                iid = image_build_module._IidFile(path)
                try:
                    if case == "symlink":
                        target = parent / "target"
                        target.write_bytes(b"sha256:" + b"a" * 64)
                        target.chmod(0o644)
                        path.unlink()
                        path.symlink_to(target)
                    elif case == "fifo":
                        path.unlink()
                        os.mkfifo(path, 0o644)
                    elif case == "hardlink":
                        target = parent / "target"
                        target.write_bytes(b"sha256:" + b"a" * 64)
                        target.chmod(0o644)
                        path.unlink()
                        os.link(target, path)
                    elif case == "mode":
                        self.project(path, mode=0o600)
                    else:
                        self.project(path, b"x" * 73)
                    with self.assertRaises(InterCodeImageBuildError):
                        iid.read_image_id()
                finally:
                    iid.close()

    def test_rejects_torn_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "image.iid"
            iid = image_build_module._IidFile(path)
            try:
                self.project(path, b"sha256:" + b"a" * 63)
                with self.assertRaises(InterCodeImageBuildError):
                    iid.read_image_id()
            finally:
                iid.close()

    def test_rejects_parent_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "image.iid"
            iid = image_build_module._IidFile(path)
            self.project(path)
            original = parent.with_name("private-original")
            parent.rename(original)
            parent.mkdir(mode=0o700)
            try:
                with self.assertRaises(InterCodeImageBuildError):
                    iid.read_image_id()
            finally:
                iid.close()

    def test_rejects_missing_parent_without_leaking_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "image.iid"
            iid = image_build_module._IidFile(path)
            self.project(path)
            parent.rename(parent.with_name("private-original"))
            try:
                with self.assertRaises(InterCodeImageBuildError) as captured:
                    iid.read_image_id()
                self.assertNotIn(directory, str(captured.exception))
            finally:
                iid.close()

    def test_rejects_output_path_replacement_after_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "image.iid"
            iid = image_build_module._IidFile(path)
            try:
                self.project(path)
                self.assertEqual(iid.read_image_id(), "sha256:" + "a" * 64)
                path.unlink()
                path.write_bytes(b"sha256:" + b"a" * 64)
                path.chmod(0o600)
                with self.assertRaises(InterCodeImageBuildError):
                    iid.remove_after_success()
            finally:
                iid.close()

    def test_rejects_parent_mode_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "image.iid"
            iid = image_build_module._IidFile(path)
            try:
                self.project(path)
                parent.chmod(0o755)
                with self.assertRaises(InterCodeImageBuildError):
                    iid.read_image_id()
            finally:
                iid.close()

    def test_rejects_same_inode_content_change_between_bounded_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "private"
            parent.mkdir(mode=0o700)
            path = parent / "image.iid"
            iid = image_build_module._IidFile(path)
            try:
                self.project(path)
                real_read = iid._read_output
                calls = 0

                def mutating_read() -> bytes:
                    nonlocal calls
                    payload = real_read()
                    if calls == 0:
                        path.write_bytes(b"sha256:" + b"b" * 64)
                        path.chmod(0o600)
                    calls += 1
                    return payload

                with mock.patch.object(iid, "_read_output", side_effect=mutating_read):
                    with self.assertRaises(InterCodeImageBuildError):
                        iid.read_image_id()
            finally:
                iid.close()


class InterCodeImageBuildPlanTests(unittest.TestCase):
    def test_plan_is_path_free_deterministic_and_has_four_id_only_builds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            first = create_intercode_image_build_plan(fixture.request())
            second = create_intercode_image_build_plan(fixture.request())

        self.assertEqual(first.canonical_record(), second.canonical_record())
        self.assertEqual(
            first.canonical_record()["schema"],
            "edgeloopbench.intercode-image-build-plan.v3",
        )
        self.assertEqual(
            first.canonical_record()["iidfile"],
            {
                "protocol_revision": "docker-remove-recreate-private-parent-v1",
                "projected_mode": "0644",
                "normalized_mode": "0600",
            },
        )
        self.assertEqual(first.platform, "linux/arm64")
        self.assertEqual(first.dockerfile_sha256, DOCKERFILE_AGENT_SHA256)
        self.assertEqual(first.dockerignore_sha256, DOCKERIGNORE_SHA256)
        self.assertNotIn(str(fixture.repo), json.dumps(first.canonical_record()))
        self.assertEqual([entry.filesystem_version for entry in first.entries], [1, 2, 3, 4])
        self.assertEqual([entry.profile for entry in first.entries], ["fs1", "fs2", "fs3", "fs4"])
        for entry in first.entries:
            self.assertFalse(hasattr(entry, "tag"))
            argv = first.build_argv(
                entry,
                iidfile=fixture.repo / f"private-fs{entry.filesystem_version}.iid",
            )
            self.assertEqual(argv[0:3], (str(fixture.binary), "--host", fixture.pins.endpoint))
            self.assertIn(("--platform", "linux/arm64"), tuple(zip(argv, argv[1:])))
            self.assertIn(
                ("--build-arg", f"FILE_SYSTEM_VERSION={entry.filesystem_version}"),
                tuple(zip(argv, argv[1:])),
            )
            self.assertNotIn("--tag", argv)
            self.assertEqual(argv[-1], str(fixture.repo))

    def test_cli_defaults_to_plan_only_and_does_not_create_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            manifest = Path(directory) / "private/images.jsonl"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--repo-root",
                        str(fixture.repo),
                        "--docker-binary",
                        str(fixture.binary),
                        "--docker-binary-sha256",
                        fixture.binary_sha256,
                        "--docker-endpoint",
                        fixture.pins.endpoint,
                        "--docker-client-version",
                        fixture.pins.client_version,
                        "--docker-server-version",
                        fixture.pins.server_version,
                        "--manifest",
                        str(manifest),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["mode"], "plan")
        self.assertFalse(manifest.exists())

    def test_plan_rejects_symlink_binary_and_reviewed_context_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            symlink = Path(directory) / "docker-link"
            symlink.symlink_to(fixture.binary)
            with self.assertRaises(InterCodeImageBuildError):
                create_intercode_image_build_plan(
                    InterCodeImageBuildRequest(
                        repo_root=fixture.repo,
                        docker_binary=symlink,
                        docker_pins=fixture.pins,
                    )
                )

            dockerfile = fixture.repo / "docker/intercode/Dockerfile.agent"
            dockerfile.write_bytes(dockerfile.read_bytes() + b"\n# drift\n")
            with self.assertRaises(InterCodeImageBuildError):
                create_intercode_image_build_plan(fixture.request())

    def test_plan_rejects_a_symlinked_context_directory_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            setup = fixture.repo / "docker/intercode/setup"
            real_setup = fixture.repo / "docker/intercode/setup-real"
            setup.rename(real_setup)
            setup.symlink_to(real_setup, target_is_directory=True)

            with self.assertRaises(InterCodeImageBuildError):
                create_intercode_image_build_plan(fixture.request())

    def test_execute_flag_without_private_live_inputs_fails_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            manifest = Path(directory) / "private/images.jsonl"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "--repo-root",
                        str(fixture.repo),
                        "--docker-binary",
                        str(fixture.binary),
                        "--docker-binary-sha256",
                        fixture.binary_sha256,
                        "--docker-endpoint",
                        fixture.pins.endpoint,
                        "--docker-client-version",
                        fixture.pins.client_version,
                        "--docker-server-version",
                        fixture.pins.server_version,
                        "--manifest",
                        str(manifest),
                        "--execute",
                    ]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("host-safety", stderr.getvalue())
        self.assertFalse(manifest.exists())

    def test_cli_rejects_abbreviated_execute_flag_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            manifest = Path(directory) / "private/images.jsonl"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "--repo-root",
                        str(fixture.repo),
                        "--docker-binary",
                        str(fixture.binary),
                        "--docker-binary-sha256",
                        fixture.binary_sha256,
                        "--docker-endpoint",
                        fixture.pins.endpoint,
                        "--docker-client-version",
                        fixture.pins.client_version,
                        "--docker-server-version",
                        fixture.pins.server_version,
                        "--manifest",
                        str(manifest),
                        "--exec",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments: --exec", stderr.getvalue())
        self.assertFalse(manifest.exists())


class InterCodeImageBuildExecutionTests(unittest.TestCase):
    def test_repository_execution_lock_prevents_cross_manifest_build_races(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            collector, policy, telemetry = fixture.admission()
            docker = FakeDockerRunner()
            descriptor = os.open(fixture.repo, os.O_RDONLY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(InterCodeImageBuildError):
                    execute_intercode_image_build(
                        plan,
                        manifest_path=Path(directory) / "other/images.jsonl",
                        collector=collector,
                        policy=policy,
                        runner=docker,
                        environment={},
                    )
            finally:
                os.close(descriptor)

        self.assertEqual(telemetry.calls, [])
        self.assertEqual(docker.calls, [])

    def test_execute_requires_quiescence_builds_and_attests_all_four_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            collector, policy, telemetry = fixture.admission()
            docker = FakeDockerRunner()
            manifest = Path(directory) / "private/images.jsonl"

            result = execute_intercode_image_build(
                plan,
                manifest_path=manifest,
                collector=collector,
                policy=policy,
                runner=docker,
                environment={},
            )

            self.assertEqual(result.built_profiles, ("fs1", "fs2", "fs3", "fs4"))
            self.assertEqual(result.resumed_profiles, ())
            self.assertEqual(len(result.image_ids), 4)
            manifest_text = manifest.read_text(encoding="ascii")
            self.assertEqual(len(manifest_text.splitlines()), 5)
            self.assertNotIn('"tag"', manifest_text)
            self.assertTrue(
                all(
                    json.loads(line)["schema"]
                    == "edgeloopbench.intercode-image-build-manifest.v3"
                    for line in manifest_text.splitlines()
                )
            )
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(manifest.parent.stat().st_mode), 0o700)
            self.assertFalse(any(manifest.parent.glob("*.iid")))
            self.assertGreaterEqual(len(telemetry.calls), 9 * 4)

        self.assertEqual(len(docker.build_calls), 4)
        for argv in docker.build_calls:
            self.assertEqual(argv[:3], (str(fixture.binary), "--host", fixture.pins.endpoint))
            self.assertIn("--iidfile", argv)
            self.assertNotIn("--tag", argv)
            self.assertEqual(argv[-1], str(fixture.repo))
        flattened = "\n".join(" ".join(argv) for argv in docker.calls)
        self.assertNotRegex(flattened, r"\b(?:tag|rm|rmi|prune)\b")

    def test_valid_prefix_reattests_ids_and_rebuilds_every_unrecorded_stratum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            docker = FakeDockerRunner()
            first_manifest = Path(directory) / "first/images.jsonl"
            collector, policy, _telemetry = fixture.admission()
            execute_intercode_image_build(
                plan,
                manifest_path=first_manifest,
                collector=collector,
                policy=policy,
                runner=docker,
                environment={},
            )
            lines = first_manifest.read_bytes().splitlines(keepends=True)
            second_manifest = Path(directory) / "second/images.jsonl"
            second_manifest.parent.mkdir(mode=0o700)
            second_manifest.write_bytes(b"".join(lines[:3]))
            second_manifest.chmod(0o600)
            docker.build_calls.clear()

            collector, policy, _telemetry = fixture.admission()
            result = execute_intercode_image_build(
                plan,
                manifest_path=second_manifest,
                collector=collector,
                policy=policy,
                runner=docker,
                environment={},
            )

        self.assertEqual(result.resumed_profiles, ("fs1", "fs2"))
        self.assertEqual(result.built_profiles, ("fs3", "fs4"))
        self.assertEqual(len(docker.build_calls), 2)
        self.assertIn("FILE_SYSTEM_VERSION=3", docker.build_calls[0])
        self.assertIn("FILE_SYSTEM_VERSION=4", docker.build_calls[1])

    def test_torn_manifest_is_rejected_before_telemetry_or_docker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            manifest = Path(directory) / "private/images.jsonl"
            manifest.parent.mkdir(mode=0o700)
            manifest.write_bytes(b'{"schema":"torn"}')
            manifest.chmod(0o600)
            collector, policy, telemetry = fixture.admission()
            docker = FakeDockerRunner()

            with self.assertRaises(InterCodeImageBuildError):
                execute_intercode_image_build(
                    plan,
                    manifest_path=manifest,
                    collector=collector,
                    policy=policy,
                    runner=docker,
                    environment={},
                )

        self.assertEqual(telemetry.calls, [])
        self.assertEqual(docker.calls, [])

    def test_manifest_replacement_during_build_prevents_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            manifest = Path(directory) / "private/images.jsonl"

            class ReplacingDockerRunner(FakeDockerRunner):
                def __init__(self) -> None:
                    super().__init__()
                    self.replaced = False

                def __call__(
                    self, argv: list[str], **kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    if tuple(argv[3:5]) == ("image", "build") and not self.replaced:
                        replace_regular_file(manifest)
                        self.replaced = True
                    return super().__call__(argv, **kwargs)

            collector, policy, _telemetry = fixture.admission()
            docker = ReplacingDockerRunner()
            with self.assertRaises(InterCodeImageBuildError):
                execute_intercode_image_build(
                    plan,
                    manifest_path=manifest,
                    collector=collector,
                    policy=policy,
                    runner=docker,
                    environment={},
                )

            self.assertEqual(manifest.read_bytes(), b'{"replacement":true}\n')

    def test_manifest_replacement_during_append_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            manifest = Path(directory) / "private/images.jsonl"
            collector, policy, _telemetry = fixture.admission()
            docker = FakeDockerRunner()
            real_fsync = os.fsync
            fsync_count = 0

            def replacing_fsync(descriptor: int) -> None:
                nonlocal fsync_count
                real_fsync(descriptor)
                fsync_count += 1
                if fsync_count == 2:
                    replace_regular_file(manifest)

            with mock.patch.object(image_build_module.os, "fsync", replacing_fsync):
                with self.assertRaises(InterCodeImageBuildError):
                    execute_intercode_image_build(
                        plan,
                        manifest_path=manifest,
                        collector=collector,
                        policy=policy,
                        runner=docker,
                        environment={},
                    )

            self.assertEqual(manifest.read_bytes(), b'{"replacement":true}\n')

    def test_manifest_parent_replacement_after_final_parse_prevents_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            manifest = Path(directory) / "private/images.jsonl"
            collector, policy, _telemetry = fixture.admission()
            docker = FakeDockerRunner()
            real_parse = image_build_module._parse_manifest
            replaced = False

            def replacing_parse(
                payload: bytes,
                supplied_plan: image_build_module.InterCodeImageBuildPlan,
            ) -> tuple[
                dict[str, object],
                tuple[image_build_module._ManifestImageRecord, ...],
            ]:
                nonlocal replaced
                result = real_parse(payload, supplied_plan)
                if len(payload.splitlines()) == 5 and not replaced:
                    replace_private_parent(manifest)
                    replaced = True
                return result

            with mock.patch.object(
                image_build_module,
                "_parse_manifest",
                replacing_parse,
            ):
                with self.assertRaises(InterCodeImageBuildError):
                    execute_intercode_image_build(
                        plan,
                        manifest_path=manifest,
                        collector=collector,
                        policy=policy,
                        runner=docker,
                        environment={},
                    )

            self.assertTrue(replaced)
            self.assertEqual(manifest.read_bytes(), b'{"replacement":true}\n')

    def test_busy_transition_during_iid_preparation_prevents_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            manifest = Path(directory) / "private/images.jsonl"
            collector, policy, telemetry = fixture.admission()
            docker = FakeDockerRunner()
            real_iid_file = image_build_module._IidFile

            class BusyIidFile(real_iid_file):
                def __init__(self, path: Path) -> None:
                    super().__init__(path)
                    telemetry.running_containers = ("c" * 64,)

            with mock.patch.object(image_build_module, "_IidFile", BusyIidFile):
                with self.assertRaises(InterCodeImageBuildError):
                    execute_intercode_image_build(
                        plan,
                        manifest_path=manifest,
                        collector=collector,
                        policy=policy,
                        runner=docker,
                        environment={},
                    )

            self.assertEqual(docker.build_calls, [])
            iidfiles = tuple(manifest.parent.glob("*.iid"))
            self.assertEqual(len(iidfiles), 1)
            self.assertTrue(iidfiles[0].is_file())
            self.assertEqual(stat.S_IMODE(iidfiles[0].stat().st_mode), 0o600)

    def test_hash_chain_tamper_is_rejected_before_telemetry_or_docker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            manifest = Path(directory) / "private/images.jsonl"
            collector, policy, _telemetry = fixture.admission()
            docker = FakeDockerRunner()
            execute_intercode_image_build(
                plan,
                manifest_path=manifest,
                collector=collector,
                policy=policy,
                runner=docker,
                environment={},
            )
            payload = manifest.read_bytes()
            manifest.write_bytes(payload.replace(b'"kind":"image"', b'"kind":"other"', 1))
            collector, policy, telemetry = fixture.admission()
            docker.calls.clear()
            with self.assertRaises(InterCodeImageBuildError):
                execute_intercode_image_build(
                    plan,
                    manifest_path=manifest,
                    collector=collector,
                    policy=policy,
                    runner=docker,
                    environment={},
                )

        self.assertEqual(telemetry.calls, [])
        self.assertEqual(docker.calls, [])

    def test_recorded_image_missing_on_resume_refuses_to_rebuild_or_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            manifest = Path(directory) / "private/images.jsonl"
            collector, policy, _telemetry = fixture.admission()
            docker = FakeDockerRunner()
            result = execute_intercode_image_build(
                plan,
                manifest_path=manifest,
                collector=collector,
                policy=policy,
                runner=docker,
                environment={},
            )
            docker.images_by_id.pop(result.image_ids[0])
            docker.calls.clear()
            docker.build_calls.clear()
            collector, policy, _telemetry = fixture.admission()
            with self.assertRaises(InterCodeImageBuildError):
                execute_intercode_image_build(
                    plan,
                    manifest_path=manifest,
                    collector=collector,
                    policy=policy,
                    runner=docker,
                    environment={},
                )

        self.assertEqual(docker.build_calls, [])
        flattened = "\n".join(" ".join(argv) for argv in docker.calls)
        self.assertNotRegex(flattened, r"\b(?:rm|rmi|prune)\b")

    def test_unrelated_running_container_stops_before_manifest_or_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            collector, policy, _telemetry = fixture.admission(
                running_containers=("c" * 64,)
            )
            docker = FakeDockerRunner()
            manifest = Path(directory) / "private/images.jsonl"

            with self.assertRaises(InterCodeImageBuildError):
                execute_intercode_image_build(
                    plan,
                    manifest_path=manifest,
                    collector=collector,
                    policy=policy,
                    runner=docker,
                    environment={},
                )

        self.assertFalse(manifest.exists())
        self.assertEqual(docker.calls, [])

    def test_context_drift_and_inspection_drift_fail_closed_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            dockerfile = fixture.repo / "docker/intercode/Dockerfile.agent"
            dockerfile.write_bytes(dockerfile.read_bytes() + b"\n# drift\n")
            collector, policy, telemetry = fixture.admission()
            docker = FakeDockerRunner()
            manifest = Path(directory) / "context/images.jsonl"
            with self.assertRaises(InterCodeImageBuildError):
                execute_intercode_image_build(
                    plan,
                    manifest_path=manifest,
                    collector=collector,
                    policy=policy,
                    runner=docker,
                    environment={},
                )
            self.assertEqual(telemetry.calls, [])
            self.assertEqual(docker.calls, [])

        with tempfile.TemporaryDirectory() as directory:
            fixture = PlanFixture(Path(directory))
            plan = create_intercode_image_build_plan(fixture.request())
            collector, policy, _telemetry = fixture.admission()
            docker = FakeDockerRunner(tamper_profile=2)
            manifest = Path(directory) / "inspect/images.jsonl"
            with self.assertRaises(InterCodeImageBuildError):
                execute_intercode_image_build(
                    plan,
                    manifest_path=manifest,
                    collector=collector,
                    policy=policy,
                    runner=docker,
                    environment={},
                )
            records = manifest.read_text(encoding="ascii").splitlines()
            iidfiles = tuple(manifest.parent.glob("*.iid"))
            self.assertEqual(len(iidfiles), 1)
            self.assertEqual(stat.S_IMODE(iidfiles[0].stat().st_mode), 0o600)
            builds_before_retry = len(docker.build_calls)
            collector, policy, _telemetry = fixture.admission()
            with self.assertRaises(InterCodeImageBuildError):
                execute_intercode_image_build(
                    plan,
                    manifest_path=manifest,
                    collector=collector,
                    policy=policy,
                    runner=docker,
                    environment={},
                )
            self.assertEqual(len(docker.build_calls), builds_before_retry)

        self.assertEqual(len(records), 2)
        flattened = "\n".join(" ".join(argv) for argv in docker.calls)
        self.assertNotRegex(flattened, r"\b(?:rm|rmi|prune)\b")


if __name__ == "__main__":
    unittest.main()
