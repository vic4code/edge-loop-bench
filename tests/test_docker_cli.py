from __future__ import annotations

import copy
import json
import subprocess
import unittest

from edgeloopbench.docker_cli import (
    DockerAdmissionError,
    DockerCleanupRefused,
    DockerCli,
    DockerCommandError,
    DockerContainer,
    DockerContainerSpec,
    DockerLimits,
    DockerOrphanedResourceError,
    DockerSecurityError,
    DockerTrustedState,
    MANAGED_LABEL,
    RUN_LABEL,
)


IMAGE = "local/intercode-bash@sha256:" + "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
CONTAINER_ID = "c" * 64
RUN_ID = "v06-calibration-001"
NONCE = "1234567890abcdef"
NAME = f"elb-{RUN_ID}-agent-{NONCE}"


class FakeRunner:
    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), dict(kwargs)))
        if not self.responses:
            raise AssertionError(f"unexpected Docker CLI call: {argv!r}")
        returncode, stdout, stderr = self.responses.pop(0)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def limits() -> DockerLimits:
    return DockerLimits(
        memory_bytes=536_870_912,
        memory_swap_bytes=536_870_912,
        storage_bytes=268_435_456,
        nano_cpus=1_000_000_000,
        pids_limit=64,
        nofile_soft=1024,
        nofile_hard=1024,
        nproc_soft=64,
        nproc_hard=64,
    )


def spec() -> DockerContainerSpec:
    return DockerContainerSpec(
        run_id=RUN_ID,
        role="agent",
        image=IMAGE,
        limits=limits(),
        image_id=IMAGE_ID,
    )


def inspect_payload(
    *,
    identifier: str = CONTAINER_ID,
    name: str = NAME,
    run_id: str = RUN_ID,
    running: bool = False,
    exited: bool = False,
) -> dict[str, object]:
    if running and exited:
        raise ValueError("inspection fixture cannot be running and exited")
    return {
        "Id": identifier,
        "Name": f"/{name}",
        "Image": IMAGE_ID,
        "Platform": "linux",
        "State": {
            "Status": "running" if running else "exited" if exited else "created",
            "Running": running,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
        },
        "Config": {
            "Image": IMAGE,
            "Hostname": "edgeloop-agent",
            "User": "65532:65532",
            "WorkingDir": "/",
            "Entrypoint": ["/bin/bash"],
            "Cmd": [
                "--noprofile",
                "--norc",
                "-c",
                "exec /usr/bin/tail -f /dev/null",
            ],
            "Labels": {
                MANAGED_LABEL: "v0.6",
                RUN_LABEL: run_id,
                "org.edgeloopbench.role": "agent",
                "org.edgeloopbench.instance": name,
                "org.edgeloopbench.runtime-network": "none-required",
                "org.edgeloopbench.filesystem-version": "1",
                "org.edgeloopbench.state-collector.argv": (
                    "/usr/bin/python3 -I -S -B /opt/edgeloop/state_collector.py "
                    "--profile fsN"
                ),
                "org.edgeloopbench.state-collector.policy-sha256": (
                    "sha256:" + "1" * 64
                ),
                "org.edgeloopbench.state-collector.profile": "fs1",
                "org.edgeloopbench.state-collector.profile-set-sha256": (
                    "sha256:" + "2" * 64
                ),
                "org.edgeloopbench.state-collector.root-baseline-sha256": (
                    "sha256:" + "3" * 64
                ),
                "org.edgeloopbench.state-collector.sha256": "sha256:" + "4" * 64,
                "org.opencontainers.image.revision": "c3e46d8",
            },
            "Volumes": None,
        },
        "HostConfig": {
            "NetworkMode": "none",
            "Binds": None,
            "Mounts": None,
            "Tmpfs": None,
            "VolumesFrom": None,
            "Devices": [],
            "DeviceRequests": None,
            "DeviceCgroupRules": None,
            "Privileged": False,
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges=true", "seccomp=builtin"],
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
            "Memory": 536_870_912,
            "MemorySwap": 536_870_912,
            "StorageOpt": {"size": "268435456"},
            "NanoCpus": 1_000_000_000,
            "PidsLimit": 64,
            "Ulimits": [
                {"Name": "nofile", "Soft": 1024, "Hard": 1024},
                {"Name": "nproc", "Soft": 64, "Hard": 64},
            ],
            "PublishAllPorts": False,
            "PortBindings": {},
            "Links": None,
            "ExtraHosts": None,
            "Sysctls": None,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "AutoRemove": False,
            "OomKillDisable": False,
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": {"none": {}}},
    }


def inspect_stdout(payload: dict[str, object] | None = None) -> str:
    return json.dumps([payload or inspect_payload()]) + "\n"


def local_daemon_responses() -> list[tuple[int, str, str]]:
    return [
        (0, "desktop-linux\n", ""),
        (0, '"unix:///Users/test/.docker/run/docker.sock"\n', ""),
    ]


def image_inspect_stdout() -> str:
    return json.dumps(
        [
            {
                "Id": IMAGE_ID,
                "Os": "linux",
                "Architecture": "arm64",
                "RepoDigests": [IMAGE],
                "Config": {
                    "Labels": {
                        "org.edgeloopbench.role": "agent",
                        "org.edgeloopbench.runtime-network": "none-required",
                        "org.edgeloopbench.filesystem-version": "1",
                        "org.edgeloopbench.state-collector.argv": (
                            "/usr/bin/python3 -I -S -B "
                            "/opt/edgeloop/state_collector.py --profile fsN"
                        ),
                        "org.edgeloopbench.state-collector.policy-sha256": (
                            "sha256:" + "1" * 64
                        ),
                        "org.edgeloopbench.state-collector.profile": "fs1",
                        "org.edgeloopbench.state-collector.profile-set-sha256": (
                            "sha256:" + "2" * 64
                        ),
                        "org.edgeloopbench.state-collector.root-baseline-sha256": (
                            "sha256:" + "3" * 64
                        ),
                        "org.edgeloopbench.state-collector.sha256": (
                            "sha256:" + "4" * 64
                        ),
                    }
                },
            }
        ]
    ) + "\n"


def create_prerequisite_responses() -> list[tuple[int, str, str]]:
    return local_daemon_responses() + [
        (0, "", ""),
        (0, image_inspect_stdout(), ""),
    ]


def trusted_container() -> DockerContainer:
    labels = {
        MANAGED_LABEL: "v0.6",
        RUN_LABEL: RUN_ID,
        "org.edgeloopbench.role": "agent",
        "org.edgeloopbench.instance": NAME,
    }
    return DockerContainer(
        identifier=CONTAINER_ID,
        name=NAME,
        image_id=IMAGE_ID,
        labels=tuple(sorted(labels.items())),
        spec=spec(),
    )


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def trusted_state_stdout(profile: str = "fs1") -> str:
    payload = {
        "common_roots": ["home/agent", "usr/workspace", "tmp", "var/tmp", "run/lock"],
        "dynamic_root_policy": "non_baseline_top_level",
        "entries": [],
        "entry_count": 0,
        "policy_sha256": "sha256:" + "1" * 64,
        "profile": profile,
        "profile_sha256": "sha256:" + "5" * 64,
        "root_baseline_sha256": "sha256:" + "3" * 64,
        "schema": "edgeloopbench.filesystem-state.v1",
        "state_sha256": "sha256:" + "6" * 64,
        "strict_surface": {"failures": [], "status": "representable"},
        "task_roots": ["testbed"],
        "total_file_bytes": 0,
        "writable_surface_audit_sha256": "sha256:" + "7" * 64,
    }
    return canonical_json(payload) + "\n"


class DockerCliTests(unittest.TestCase):
    def client(
        self,
        runner: FakeRunner,
        *,
        env: dict[str, str] | None = None,
    ) -> DockerCli:
        return DockerCli(
            expected_context="desktop-linux",
            expected_endpoint="unix:///Users/test/.docker/run/docker.sock",
            docker_binary="/usr/local/bin/docker",
            env={} if env is None else env,
            runner=runner,
            nonce_factory=lambda: NONCE,
        )

    def assert_no_host_shell(self, runner: FakeRunner) -> None:
        for _argv, kwargs in runner.calls:
            self.assertIs(kwargs["shell"], False)
            self.assertIs(kwargs["check"], False)
            self.assertIs(kwargs["capture_output"], True)
            self.assertIs(kwargs["text"], True)

    def test_admission_rejects_any_docker_host_override_without_calling_docker(self) -> None:
        for docker_host in (
            "tcp://127.0.0.1:2375",
            "ssh://remote.example",
            "unix:///tmp/other.sock",
        ):
            with self.subTest(docker_host=docker_host):
                runner = FakeRunner([])
                with self.assertRaisesRegex(DockerAdmissionError, "DOCKER_HOST"):
                    self.client(runner, env={"DOCKER_HOST": docker_host}).admit()
                self.assertEqual(runner.calls, [])

    def test_admission_rejects_unexpected_context_and_remote_endpoint(self) -> None:
        cases = (
            (
                [(0, "default\n", "")],
                "unexpected Docker context",
            ),
            (
                [
                    (0, "desktop-linux\n", ""),
                    (0, '"tcp://remote.example:2376"\n', ""),
                ],
                "remote or unexpected Docker endpoint",
            ),
        )
        for responses, message in cases:
            with self.subTest(message=message):
                runner = FakeRunner(responses)
                with self.assertRaisesRegex(DockerAdmissionError, message):
                    self.client(runner).admit()
                self.assertFalse(
                    any("container" in argv and "ls" in argv for argv, _ in runner.calls)
                )
                self.assert_no_host_shell(runner)

    def test_admission_lists_running_containers_read_only_and_can_block(self) -> None:
        running_id = "d" * 64
        responses = [
            (0, "desktop-linux\n", ""),
            (0, '"unix:///Users/test/.docker/run/docker.sock"\n', ""),
            (0, f"{running_id}\n", ""),
        ]
        runner = FakeRunner(responses)
        result = self.client(runner).admit(require_no_running=False)
        self.assertEqual(result.running_container_ids, (running_id,))
        self.assertEqual(result.context, "desktop-linux")
        self.assertEqual(result.endpoint, "unix:///Users/test/.docker/run/docker.sock")
        self.assertFalse(any("rm" in argv for argv, _ in runner.calls))
        self.assert_no_host_shell(runner)

        blocking_runner = FakeRunner(responses)
        with self.assertRaisesRegex(DockerAdmissionError, "running container") as caught:
            self.client(blocking_runner).admit()
        self.assertEqual(caught.exception.running_container_ids, (running_id,))
        self.assertFalse(any("rm" in argv for argv, _ in blocking_runner.calls))

    def test_create_uses_frozen_security_flags_and_validates_inspection(self) -> None:
        runner = FakeRunner(
            create_prerequisite_responses()
            + [
                (0, CONTAINER_ID + "\n", ""),
                (0, inspect_stdout(), ""),
            ]
        )
        container = self.client(runner).create_container(spec())
        self.assertEqual(container.identifier, CONTAINER_ID)
        self.assertEqual(container.name, NAME)
        self.assertEqual(container.image_id, IMAGE_ID)

        argv = runner.calls[4][0]
        self.assertEqual(
            argv[:4],
            (
                "/usr/local/bin/docker",
                "--host",
                "unix:///Users/test/.docker/run/docker.sock",
                "container",
            ),
        )
        self.assertEqual(argv[4], "create")
        required_pairs = (
            ("--name", NAME),
            ("--network", "none"),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges=true"),
            ("--security-opt", "seccomp=builtin"),
            ("--memory", "536870912"),
            ("--memory-swap", "536870912"),
            ("--storage-opt", "size=268435456"),
            ("--cpus", "1"),
            ("--pids-limit", "64"),
            ("--ulimit", "nofile=1024:1024"),
            ("--ulimit", "nproc=64:64"),
            ("--ipc", "private"),
            ("--hostname", "edgeloop-agent"),
            ("--user", "65532:65532"),
            ("--workdir", "/"),
            ("--pull", "never"),
            ("--entrypoint", "/bin/bash"),
        )
        adjacent = set(zip(argv, argv[1:]))
        for pair in required_pairs:
            self.assertIn(pair, adjacent)
        self.assertIn(("--label", f"{RUN_LABEL}={RUN_ID}"), adjacent)
        self.assertIn(("--label", f"{MANAGED_LABEL}=v0.6"), adjacent)
        self.assertEqual(
            argv[-5:],
            (
                IMAGE,
                "--noprofile",
                "--norc",
                "-c",
                "exec /usr/bin/tail -f /dev/null",
            ),
        )
        self.assertIn(IMAGE, argv)
        forbidden = {
            "--mount",
            "--volume",
            "-v",
            "--device",
            "--privileged",
            "--network=host",
            "--pid",
            "--uts",
        }
        self.assertTrue(forbidden.isdisjoint(argv))
        self.assert_no_host_shell(runner)

    def test_create_rejects_mutable_image_and_invalid_identity_before_subprocess(self) -> None:
        runner = FakeRunner([])
        self.assertEqual(
            DockerContainerSpec(RUN_ID, "agent", IMAGE_ID, limits(), IMAGE_ID).image,
            IMAGE_ID,
        )
        invalid_values = (
            (RUN_ID, "agent", "ubuntu:latest"),
            ("../escape", "agent", IMAGE),
            (RUN_ID, "agent;rm", IMAGE),
        )
        for run_id, role, image in invalid_values:
            with self.subTest(run_id=run_id, role=role, image=image):
                with self.assertRaises(ValueError):
                    DockerContainerSpec(run_id, role, image, limits(), IMAGE_ID)
        self.assertEqual(runner.calls, [])

    def test_prepare_exec_keeps_adversarial_action_in_one_unexecuted_argv_value(self) -> None:
        action = "printf '%s' \"$(touch /host-marker)\"; echo `id` && true"
        runner = FakeRunner(
            local_daemon_responses()
            + [(0, inspect_stdout(inspect_payload(running=True)), "")]
        )
        prepared = self.client(runner).prepare_exec_action(
            container=trusted_container(),
            action=action,
            cwd="/testbed/dir1",
        )
        argv = prepared.argv
        wrapper = (
            "printf '\\036ELB_ACTION_STARTED_V1\\037\\n'\n"
            "printf '\\036ELB_ACTION_STARTED_V1\\037\\n' >&2\n"
            "exec /bin/bash --noprofile --norc -c \"$1\""
        )
        self.assertEqual(
            argv[-12:],
            (
                "--workdir",
                "/testbed/dir1",
                "--user",
                "65532:65532",
                CONTAINER_ID,
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                wrapper,
                "edgeloop-action-v1",
                action,
            ),
        )
        self.assertEqual(argv.count(action), 1)
        self.assertFalse(any(action in call for call, _ in runner.calls))
        self.assert_no_host_shell(runner)

    def test_start_attests_exact_created_and_running_container(self) -> None:
        runner = FakeRunner(
            local_daemon_responses()
            + [
                (0, inspect_stdout(inspect_payload()), ""),
                (0, CONTAINER_ID + "\n", ""),
                (0, inspect_stdout(inspect_payload(running=True)), ""),
            ]
        )
        container = trusted_container()

        result = self.client(runner).start_container(container)

        self.assertIs(result, container)
        self.assertEqual(
            runner.calls[3][0][-3:],
            ("start", "--", CONTAINER_ID),
        )
        self.assertNotIn(NAME, runner.calls[3][0])
        self.assert_no_host_shell(runner)

    def test_start_rejects_wrong_post_state_after_exact_mutation(self) -> None:
        runner = FakeRunner(
            local_daemon_responses()
            + [
                (0, inspect_stdout(inspect_payload()), ""),
                (0, CONTAINER_ID + "\n", ""),
                (0, inspect_stdout(inspect_payload()), ""),
            ]
        )

        with self.assertRaisesRegex(DockerSecurityError, "running"):
            self.client(runner).start_container(trusted_container())

    def test_trusted_state_uses_fixed_root_argv_and_validates_pins(self) -> None:
        runner = FakeRunner(
            local_daemon_responses()
            + [
                (0, inspect_stdout(inspect_payload(running=True)), ""),
                (0, trusted_state_stdout(), ""),
                (0, inspect_stdout(inspect_payload(running=True)), ""),
            ]
        )

        result = self.client(runner).collect_trusted_state(
            trusted_container(), profile="fs1"
        )

        self.assertIsInstance(result, DockerTrustedState)
        self.assertEqual(result.state_sha256, "sha256:" + "6" * 64)
        self.assertEqual(result.policy_sha256, "sha256:" + "1" * 64)
        self.assertEqual(result.collector_source_sha256, "sha256:" + "4" * 64)
        self.assertEqual(result.canonical_json + "\n", trusted_state_stdout())
        self.assertEqual(
            runner.calls[3][0][-11:],
            (
                "exec",
                "--user",
                "0:0",
                CONTAINER_ID,
                "/usr/bin/python3",
                "-I",
                "-S",
                "-B",
                "/opt/edgeloop/state_collector.py",
                "--profile",
                "fs1",
            ),
        )
        self.assertNotIn("/bin/sh", runner.calls[3][0])
        self.assertNotIn("/bin/bash", runner.calls[3][0])
        self.assert_no_host_shell(runner)

    def test_trusted_state_rejects_label_digest_and_payload_drift(self) -> None:
        malformed_label = inspect_payload(running=True)
        malformed_label["Config"]["Labels"][  # type: ignore[index]
            "org.edgeloopbench.state-collector.sha256"
        ] = "sha256:not-a-digest"
        cases = (
            (
                malformed_label,
                trusted_state_stdout(),
                "digest labels",
            ),
            (
                inspect_payload(running=True),
                trusted_state_stdout().replace('"profile":"fs1"', '"profile":"fs2"'),
                "profile",
            ),
            (
                inspect_payload(running=True),
                trusted_state_stdout().replace(
                    '"policy_sha256":"sha256:' + "1" * 64 + '"',
                    '"policy_sha256":"sha256:' + "9" * 64 + '"',
                ),
                "policy pin",
            ),
            (
                inspect_payload(running=True),
                trusted_state_stdout()[:-1] + " \n",
                "canonical",
            ),
        )
        for inspected, stdout, message in cases:
            with self.subTest(message=message):
                runner = FakeRunner(
                    local_daemon_responses()
                    + [
                        (0, inspect_stdout(inspected), ""),
                        (0, stdout, ""),
                        (0, inspect_stdout(inspect_payload(running=True)), ""),
                    ]
                )
                with self.assertRaisesRegex(DockerSecurityError, message):
                    self.client(runner).collect_trusted_state(
                        trusted_container(), profile="fs1"
                    )

    def test_trusted_state_rejects_profile_command_injection_before_docker(self) -> None:
        runner = FakeRunner([])

        with self.assertRaisesRegex(ValueError, "fs1 through fs4"):
            self.client(runner).collect_trusted_state(
                trusted_container(), profile="fs1; touch /host-marker"
            )

        self.assertEqual(runner.calls, [])

    def test_inspect_container_running_accepts_only_attested_running_or_exited_state(self) -> None:
        for expected, payload in (
            (True, inspect_payload(running=True)),
            (False, inspect_payload(exited=True)),
        ):
            with self.subTest(expected=expected):
                runner = FakeRunner(
                    local_daemon_responses()
                    + [(0, inspect_stdout(payload), "")]
                )
                self.assertIs(
                    self.client(runner).inspect_container_running(
                        container=trusted_container()
                    ),
                    expected,
                )
                self.assert_no_host_shell(runner)

    def test_inspect_container_running_rejects_ambiguous_or_drifted_state(self) -> None:
        ambiguous = inspect_payload(exited=True)
        ambiguous["State"]["Dead"] = True  # type: ignore[index]
        runner = FakeRunner(
            local_daemon_responses()
            + [(0, inspect_stdout(ambiguous), "")]
        )
        with self.assertRaisesRegex(DockerSecurityError, "lifecycle"):
            self.client(runner).inspect_container_running(
                container=trusted_container()
            )

    def test_create_rechecks_context_before_mutating_the_daemon(self) -> None:
        runner = FakeRunner([(0, "default\n", "")])
        with self.assertRaisesRegex(DockerAdmissionError, "unexpected Docker context"):
            self.client(runner).create_container(spec())
        self.assertFalse(any("create" in argv for argv, _ in runner.calls))

    def test_create_blocks_unrelated_running_container_without_mutation(self) -> None:
        unrelated_id = "d" * 64
        unrelated = inspect_payload(
            identifier=unrelated_id,
            name="some-user-container",
            run_id="another-run",
            running=True,
        )
        runner = FakeRunner(
            local_daemon_responses()
            + [
                (0, unrelated_id + "\n", ""),
                (0, inspect_stdout(unrelated), ""),
            ]
        )
        with self.assertRaisesRegex(DockerAdmissionError, "unrelated container"):
            self.client(runner).create_container(spec())
        self.assertFalse(any("create" in argv for argv, _ in runner.calls))
        self.assertFalse(any("rm" in argv for argv, _ in runner.calls))

    def test_create_rejects_wrong_resolved_image_before_container_creation(self) -> None:
        wrong_image = json.dumps(
            [
                {
                    "Id": IMAGE_ID,
                    "Os": "linux",
                    "Architecture": "amd64",
                    "RepoDigests": [IMAGE],
                    "Config": {
                        "Labels": {
                            "org.edgeloopbench.role": "agent",
                            "org.edgeloopbench.runtime-network": "none-required",
                        }
                    },
                }
            ]
        )
        runner = FakeRunner(
            local_daemon_responses() + [(0, "", ""), (0, wrong_image, "")]
        )
        with self.assertRaisesRegex(DockerSecurityError, "architecture"):
            self.client(runner).create_container(spec())
        self.assertFalse(any("create" in argv for argv, _ in runner.calls))

    def test_create_rejects_evaluator_image_for_agent_role(self) -> None:
        payload = json.loads(image_inspect_stdout())
        payload[0]["Config"]["Labels"]["org.edgeloopbench.role"] = "evaluator"
        runner = FakeRunner(
            local_daemon_responses()
            + [(0, "", ""), (0, json.dumps(payload), "")]
        )
        with self.assertRaisesRegex(DockerSecurityError, "image role"):
            self.client(runner).create_container(spec())
        self.assertFalse(any("create" in argv for argv, _ in runner.calls))

    def test_constructor_rejects_ambiguous_endpoint_and_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            DockerCli(
                expected_context="desktop-linux",
                expected_endpoint="unix:///var/run/docker.sock",
                docker_binary="docker",
                env={},
                runner=FakeRunner([]),
            )
        for endpoint in (
            "unix:///var/run/docker.sock?override=1",
            "unix:///var/run/docker.sock#fragment",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    DockerCli(
                        expected_context="desktop-linux",
                        expected_endpoint=endpoint,
                        docker_binary="/usr/local/bin/docker",
                        env={},
                        runner=FakeRunner([]),
                    )
        for timeout in (True, 0, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    DockerCli(
                        expected_context="desktop-linux",
                        expected_endpoint="unix:///var/run/docker.sock",
                        docker_binary="/usr/local/bin/docker",
                        env={},
                        runner=FakeRunner([]),
                        command_timeout_seconds=timeout,
                    )

    def test_failed_profile_cleanup_refuses_container_without_exact_run_label(self) -> None:
        payload = inspect_payload(run_id="another-run")
        runner = FakeRunner(
            create_prerequisite_responses()
            + [
                (0, CONTAINER_ID + "\n", ""),
                (0, inspect_stdout(payload), ""),
            ]
        )
        with self.assertRaisesRegex(DockerSecurityError, "automatic cleanup refused"):
            self.client(runner).create_container(spec())
        self.assertFalse(any("rm" in argv for argv, _ in runner.calls))

    def test_ambiguous_create_response_is_reinspected_by_name_and_removed(self) -> None:
        runner = FakeRunner(
            create_prerequisite_responses()
            + [
                (0, "not-a-container-id\n", ""),
                (0, inspect_stdout(), ""),
                (0, CONTAINER_ID + "\n", ""),
            ]
        )
        with self.assertRaisesRegex(DockerSecurityError, "ambiguous"):
            self.client(runner).create_container(spec())
        self.assertEqual(
            runner.calls[-1][0][-4:],
            ("--force", "--volumes", "--", CONTAINER_ID),
        )

    def test_ambiguous_create_reports_typed_orphan_when_ownership_is_unproven(self) -> None:
        unrelated = inspect_payload(run_id="another-run")
        runner = FakeRunner(
            create_prerequisite_responses()
            + [
                (0, "not-a-container-id\n", ""),
                (0, inspect_stdout(unrelated), ""),
            ]
        )
        with self.assertRaises(DockerOrphanedResourceError) as caught:
            self.client(runner).create_container(spec())
        self.assertEqual(caught.exception.run_id, RUN_ID)
        self.assertEqual(caught.exception.name, NAME)
        self.assertFalse(any("rm" in argv for argv, _ in runner.calls))

    def test_inspection_rejects_each_critical_security_drift(self) -> None:
        mutations = {
            "mutable image": lambda item: item["Config"].__setitem__("Image", "ubuntu:latest"),  # type: ignore[union-attr]
            "image config": lambda item: item.__setitem__("Image", "sha256:" + "e" * 64),
            "platform": lambda item: item.__setitem__("Platform", "windows"),
            "lifecycle": lambda item: item["State"].__setitem__("Status", "running"),  # type: ignore[union-attr]
            "entrypoint": lambda item: item["Config"].__setitem__("Entrypoint", ["python3"]),  # type: ignore[union-attr]
            "network": lambda item: item["HostConfig"].__setitem__("NetworkMode", "bridge"),  # type: ignore[union-attr]
            "mount": lambda item: item.__setitem__("Mounts", [{"Source": "/tmp"}]),
            "device": lambda item: item["HostConfig"].__setitem__("Devices", [{"PathOnHost": "/dev/null"}]),  # type: ignore[union-attr]
            "privileged": lambda item: item["HostConfig"].__setitem__("Privileged", True),  # type: ignore[union-attr]
            "capabilities": lambda item: item["HostConfig"].__setitem__("CapDrop", []),  # type: ignore[union-attr]
            "no-new-privileges": lambda item: item["HostConfig"].__setitem__("SecurityOpt", ["seccomp=builtin"]),  # type: ignore[union-attr]
            "seccomp": lambda item: item["HostConfig"].__setitem__("SecurityOpt", ["no-new-privileges=true"]),  # type: ignore[union-attr]
            "pid namespace": lambda item: item["HostConfig"].__setitem__("PidMode", "host"),  # type: ignore[union-attr]
            "memory": lambda item: item["HostConfig"].__setitem__("Memory", 0),  # type: ignore[union-attr]
            "storage": lambda item: item["HostConfig"].__setitem__("StorageOpt", {}),  # type: ignore[union-attr]
            "pids": lambda item: item["HostConfig"].__setitem__("PidsLimit", 0),  # type: ignore[union-attr]
            "user": lambda item: item["Config"].__setitem__("User", "0:0"),  # type: ignore[union-attr]
        }
        for expected, mutate in mutations.items():
            with self.subTest(expected=expected):
                payload = copy.deepcopy(inspect_payload())
                mutate(payload)
                runner = FakeRunner(
                    create_prerequisite_responses()
                    + [
                        (0, CONTAINER_ID + "\n", ""),
                        (0, inspect_stdout(payload), ""),
                        (0, CONTAINER_ID + "\n", ""),
                    ]
                )
                with self.assertRaisesRegex(DockerSecurityError, expected):
                    self.client(runner).create_container(spec())
                self.assertEqual(
                    runner.calls[-1][0][-4:],
                    ("--force", "--volumes", "--", CONTAINER_ID),
                )

    def test_cleanup_refuses_unlabeled_or_wrong_run_before_any_remove(self) -> None:
        unrelated_id = "d" * 64
        cases = (
            {},
            {
                MANAGED_LABEL: "v0.6",
                RUN_LABEL: "another-run",
                "org.edgeloopbench.instance": NAME,
            },
            {
                MANAGED_LABEL: "v0.6",
                RUN_LABEL: RUN_ID,
                "org.edgeloopbench.role": "evaluator",
                "org.edgeloopbench.instance": NAME,
            },
        )
        for labels in cases:
            with self.subTest(labels=labels):
                first = inspect_payload()
                second = inspect_payload(identifier=unrelated_id)
                second["Config"]["Labels"] = labels  # type: ignore[index]
                runner = FakeRunner(
                    local_daemon_responses()
                    + [
                        (0, inspect_stdout(first), ""),
                        (0, inspect_stdout(second), ""),
                    ]
                )
                with self.assertRaises(DockerCleanupRefused):
                    self.client(runner).remove_run_containers(
                        RUN_ID, [CONTAINER_ID, unrelated_id]
                    )
                self.assertEqual(len(runner.calls), 4)
                self.assertFalse(any("rm" in argv for argv, _ in runner.calls))

    def test_cleanup_inspects_all_exact_labels_before_run_scoped_removal(self) -> None:
        second_id = "d" * 64
        second_name = f"elb-{RUN_ID}-agent-fedcba0987654321"
        first = inspect_payload()
        second = inspect_payload(identifier=second_id, name=second_name)
        second["Config"]["Labels"]["org.edgeloopbench.instance"] = second_name  # type: ignore[index]
        runner = FakeRunner(
            local_daemon_responses()
            + [
                (0, inspect_stdout(first), ""),
                (0, inspect_stdout(second), ""),
                (0, CONTAINER_ID + "\n", ""),
                (0, second_id + "\n", ""),
            ]
        )
        removed = self.client(runner).remove_run_containers(
            RUN_ID, [CONTAINER_ID, second_id]
        )
        self.assertEqual(removed, (CONTAINER_ID, second_id))
        commands = [argv for argv, _ in runner.calls]
        self.assertIn("inspect", commands[2])
        self.assertIn("inspect", commands[3])
        self.assertEqual(commands[4][-4:], ("--force", "--volumes", "--", CONTAINER_ID))
        self.assertEqual(commands[5][-4:], ("--force", "--volumes", "--", second_id))
        self.assert_no_host_shell(runner)

    def test_command_failure_is_typed_and_does_not_retry_or_fall_back_to_shell(self) -> None:
        runner = FakeRunner([(125, "", "daemon unavailable")])
        with self.assertRaisesRegex(DockerCommandError, "daemon unavailable") as caught:
            self.client(runner).list_running_containers()
        self.assertEqual(caught.exception.result.returncode, 125)
        self.assertEqual(len(runner.calls), 1)
        self.assert_no_host_shell(runner)


if __name__ == "__main__":
    unittest.main()
