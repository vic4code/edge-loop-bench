from __future__ import annotations

import base64
import hashlib
import json
import unittest
from collections.abc import Sequence

from edgeloopbench.docker_action_executor import (
    ActionDisposition,
    ActionPolicyFailure,
    CleanupOutcome,
    DockerActionLimits,
    DockerActionResult,
    InfrastructureFailure,
)
from edgeloopbench.docker_cli import (
    DockerContainer,
    DockerContainerSpec,
    DockerLimits,
    DockerTrustedState,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    ROLE_LABEL,
    RUN_LABEL,
)
from edgeloopbench.interactive_environment import (
    ACTION_POLICY_OBSERVATIONS,
    ActionExecution,
    ActionPolicyFailureKind,
)
from edgeloopbench.intercode_docker_attempt import (
    DockerAttemptBoundary,
    DockerAttemptInfrastructureError,
    candidate_material_from_executed_action,
)
from edgeloopbench.intercode_evaluator_bridge import adapt_collector_state
from edgeloopbench.intercode_replay_environment import CandidateMaterial


RUN_ID = "v07-attempt-001"
IMAGE_ID = "sha256:" + "a" * 64
IMAGE = "local/intercode@sha256:" + "b" * 64
FIRST_ID = "c" * 64
SECOND_ID = "d" * 64
SECRET_ACTION = "printf SECRET_ACTION"
SECRET_STREAM = b"SECRET_STREAM"


def digest(value: str | bytes) -> str:
    encoded = value if isinstance(value, bytes) else value.encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


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


def container_spec() -> DockerContainerSpec:
    return DockerContainerSpec(RUN_ID, "agent", IMAGE, limits(), IMAGE_ID)


def container(identifier: str, spec: DockerContainerSpec) -> DockerContainer:
    name = f"elb-{RUN_ID}-agent-{identifier[:16]}"
    labels = {
        MANAGED_LABEL: "v0.6",
        RUN_LABEL: RUN_ID,
        ROLE_LABEL: "agent",
        INSTANCE_LABEL: name,
    }
    return DockerContainer(
        identifier=identifier,
        name=name,
        image_id=IMAGE_ID,
        labels=tuple(sorted(labels.items())),
        spec=spec,
    )


def state_entry(label: str) -> dict[str, object]:
    path = f"testbed/{label}.txt"
    encoded = path.encode("utf-8")
    return {
        "content_sha256": digest(f"content-{label}"),
        "gid": 65532,
        "hardlink_group_sha256": None,
        "mode": 0o640,
        "path": path,
        "path_bytes_b64": base64.b64encode(encoded).decode("ascii"),
        "size_bytes": len(encoded),
        "symlink_target": None,
        "symlink_target_bytes_b64": None,
        "type": "file",
        "uid": 65532,
    }


def trusted_state(label: str, *, profile: str = "fs1") -> DockerTrustedState:
    entries = [] if label == "base" else [state_entry(label)]
    profile_sha256 = digest(f"profile-{profile}")
    audit_sha256 = digest(f"audit-{label}")
    state_sha256 = digest(
        canonical(
            {
                "entries": entries,
                "profile_sha256": profile_sha256,
                "schema": "edgeloopbench.filesystem-state.v1",
                "writable_surface_audit_sha256": audit_sha256,
            }
        )
    )
    payload = {
        "common_roots": ["home/agent", "tmp"],
        "dynamic_root_policy": "non_baseline_top_level",
        "entries": entries,
        "entry_count": len(entries),
        "policy_sha256": digest("policy"),
        "profile": profile,
        "profile_sha256": profile_sha256,
        "root_baseline_sha256": digest("root"),
        "schema": "edgeloopbench.filesystem-state.v1",
        "state_sha256": state_sha256,
        "strict_surface": {"failures": [], "status": "representable"},
        "task_roots": ["testbed"],
        "total_file_bytes": sum(int(item["size_bytes"]) for item in entries),
        "writable_surface_audit_sha256": audit_sha256,
    }
    return DockerTrustedState(
        canonical_json=canonical(payload),
        state_sha256=state_sha256,
        profile=profile,
        profile_sha256=profile_sha256,
        policy_sha256=digest("policy"),
        root_baseline_sha256=digest("root"),
        writable_surface_audit_sha256=audit_sha256,
        collector_source_sha256=digest("collector"),
        strict_representable=True,
        strict_failures=(),
    )


def action_limits() -> DockerActionLimits:
    return DockerActionLimits(
        deadline_seconds=10.0,
        private_stream_limit_bytes=4096,
        observation_limit_bytes=2048,
    )


def executed(
    *,
    stdout: bytes = b"ok\n",
    stderr: bytes = b"",
    observation: str = "ok\n",
    exit_code: int = 0,
) -> DockerActionResult:
    return DockerActionResult(
        disposition=ActionDisposition.EXECUTED,
        infrastructure_failure=None,
        policy_failure=None,
        failure_stage=None,
        cleanup_outcome=CleanupOutcome.NOT_REQUIRED,
        exit_code=exit_code,
        action_started=True,
        observation=observation,
        private_stdout=stdout,
        private_stderr=stderr,
        stdout_bytes_observed=len(stdout),
        stderr_bytes_observed=len(stderr),
        observation_truncated=False,
        elapsed_seconds=0.1,
    )


def policy_failure() -> DockerActionResult:
    failure = ActionPolicyFailure.TIMEOUT
    return DockerActionResult(
        disposition=ActionDisposition.POLICY_FAILURE,
        infrastructure_failure=None,
        policy_failure=failure,
        failure_stage="action",
        cleanup_outcome=CleanupOutcome.REMOVED,
        exit_code=None,
        action_started=True,
        observation="Command timed out.",
        private_stdout=b"",
        private_stderr=b"",
        stdout_bytes_observed=0,
        stderr_bytes_observed=0,
        observation_truncated=False,
        elapsed_seconds=10.0,
    )


def infrastructure_invalid() -> DockerActionResult:
    return DockerActionResult(
        disposition=ActionDisposition.INFRASTRUCTURE_INVALID,
        infrastructure_failure=InfrastructureFailure.PROCESS_AUDIT,
        policy_failure=None,
        failure_stage="post_audit",
        cleanup_outcome=CleanupOutcome.REMOVED,
        exit_code=1,
        action_started=True,
        observation="",
        private_stdout=SECRET_STREAM,
        private_stderr=b"",
        stdout_bytes_observed=len(SECRET_STREAM),
        stderr_bytes_observed=0,
        observation_truncated=False,
        elapsed_seconds=0.2,
    )


class FakeDockerCli:
    def __init__(
        self,
        *,
        containers: Sequence[DockerContainer],
        states: Sequence[DockerTrustedState],
        list_results: Sequence[tuple[str, ...]] = ((),),
        remove_results: Sequence[tuple[str, ...] | BaseException] = (),
    ) -> None:
        self.containers = list(containers)
        self.states = list(states)
        self.list_results = list(list_results)
        self.remove_results = list(remove_results)
        self.calls: list[tuple[object, ...]] = []

    def list_run_containers(self, run_id: str) -> tuple[str, ...]:
        self.calls.append(("list", run_id))
        if not self.list_results:
            raise AssertionError("unplanned list_run_containers")
        return self.list_results.pop(0)

    def create_container(self, spec: DockerContainerSpec) -> DockerContainer:
        self.calls.append(("create", spec))
        if not self.containers:
            raise AssertionError("unplanned create_container")
        return self.containers.pop(0)

    def start_container(self, value: DockerContainer) -> DockerContainer:
        self.calls.append(("start", value))
        return value

    def collect_trusted_state(
        self, value: DockerContainer, *, profile: str
    ) -> DockerTrustedState:
        self.calls.append(("collect", value, profile))
        if not self.states:
            raise AssertionError("unplanned collect_trusted_state")
        return self.states.pop(0)

    def remove_run_containers(
        self, run_id: str, identifiers: Sequence[str]
    ) -> tuple[str, ...]:
        frozen = tuple(identifiers)
        self.calls.append(("remove", run_id, frozen))
        if self.remove_results:
            result = self.remove_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return frozen


class FakeExecutor:
    def __init__(self, results: Sequence[DockerActionResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[DockerContainer, str, str, DockerActionLimits]] = []

    def execute(
        self,
        *,
        container: DockerContainer,
        action: str,
        cwd: str,
        limits: DockerActionLimits,
    ) -> DockerActionResult:
        self.calls.append((container, action, cwd, limits))
        if not self.results:
            raise AssertionError("unplanned action execution")
        return self.results.pop(0)


class DockerAttemptBoundaryTests(unittest.TestCase):
    def test_shared_executed_action_conversion_is_exact_and_private(self) -> None:
        previous = adapt_collector_state(trusted_state("base"))
        final = adapt_collector_state(trusted_state("final"))

        material = candidate_material_from_executed_action(
            result=executed(
                stdout=b"ok\r\n",
                stderr=b"warn\r",
                observation="ok\n\n[stderr]\nwarn\n",
                exit_code=7,
            ),
            previous_state=previous,
            final_state=final,
            observation_limit_bytes=2048,
        )

        self.assertEqual(material.state, final.snapshot)
        self.assertEqual(
            material.collector_state_sha256,
            final.collector_state_sha256,
        )
        self.assertEqual(material.exit_code, 7)
        self.assertEqual(material.normalized_stdout, "ok\n")
        self.assertEqual(material.normalized_stderr, "warn\n")
        self.assertEqual(material.agent_observation, "ok\n\n[stderr]\nwarn\n")
        self.assertTrue(material.state_changed)

    def test_fixed_init_execute_calls_normalize_streams_and_track_state_change(self) -> None:
        spec = container_spec()
        first = container(FIRST_ID, spec)
        cli = FakeDockerCli(
            containers=[first],
            states=[trusted_state("base"), trusted_state("one"), trusted_state("one")],
        )
        action_cap = action_limits()
        executor = FakeExecutor(
            [
                executed(
                    stdout=b"ok\r\n",
                    stderr=b"warn\r",
                    observation="ok\n\n[stderr]\nwarn\n",
                ),
                executed(stdout=b"same\n", observation="same\n"),
            ]
        )

        boundary = DockerAttemptBoundary(
            docker_cli=cli,  # type: ignore[arg-type]
            action_executor=executor,  # type: ignore[arg-type]
            container_spec=spec,
            profile="fs1",
            action_limits=action_cap,
        )
        first_material = boundary.execute("first")
        second_material = boundary.execute("second")

        self.assertIsInstance(first_material, CandidateMaterial)
        self.assertEqual(first_material.normalized_stdout, "ok\n")
        self.assertEqual(first_material.normalized_stderr, "warn\n")
        self.assertTrue(first_material.state_changed)
        self.assertIsInstance(second_material, CandidateMaterial)
        self.assertFalse(second_material.state_changed)
        self.assertEqual(
            cli.calls[:4],
            [
                ("list", RUN_ID),
                ("create", spec),
                ("start", first),
                ("collect", first, "fs1"),
            ],
        )
        self.assertEqual(
            executor.calls,
            [
                (first, "first", "/", action_cap),
                (first, "second", "/", action_cap),
            ],
        )
        rendered = repr(boundary)
        self.assertNotIn(FIRST_ID, rendered)
        self.assertNotIn("first", rendered)
        boundary.close()

    def test_policy_failure_recreates_and_exactly_replays_prior_actions(self) -> None:
        spec = container_spec()
        first = container(FIRST_ID, spec)
        second = container(SECOND_ID, spec)
        first_result = executed(stdout=b"one\n", observation="one\n")
        cli = FakeDockerCli(
            containers=[first, second],
            states=[
                trusted_state("base"),
                trusted_state("one"),
                trusted_state("base"),
                trusted_state("one"),
            ],
            list_results=[(), ()],
        )
        executor = FakeExecutor([first_result, policy_failure(), first_result])
        boundary = DockerAttemptBoundary(
            docker_cli=cli,  # type: ignore[arg-type]
            action_executor=executor,  # type: ignore[arg-type]
            container_spec=spec,
            profile="fs1",
            action_limits=action_limits(),
        )
        first_material = boundary.execute("one")

        outcome = boundary.execute(SECRET_ACTION)

        self.assertIsInstance(first_material, CandidateMaterial)
        self.assertIsInstance(outcome, ActionExecution)
        self.assertFalse(outcome.admissible)
        self.assertEqual(outcome.policy_failure, ActionPolicyFailureKind.TIMEOUT)
        self.assertEqual(
            outcome.observation,
            ACTION_POLICY_OBSERVATIONS[ActionPolicyFailureKind.TIMEOUT],
        )
        self.assertEqual(outcome.state_sha256, first_material.collector_state_sha256)
        self.assertTrue(outcome.safety_recovery_performed)
        self.assertRegex(
            outcome.safety_recovery_evidence_sha256 or "",
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(
            [(item[0], item[1]) for item in executor.calls],
            [(first, "one"), (first, SECRET_ACTION), (second, "one")],
        )
        self.assertIn(("list", RUN_ID), cli.calls[5:])
        boundary.close()

    def test_replay_mismatch_cleans_new_container_and_raises_redacted_error(self) -> None:
        spec = container_spec()
        first = container(FIRST_ID, spec)
        second = container(SECOND_ID, spec)
        cli = FakeDockerCli(
            containers=[first, second],
            states=[
                trusted_state("base"),
                trusted_state("one"),
                trusted_state("base"),
                trusted_state("one"),
            ],
            list_results=[(), ()],
        )
        executor = FakeExecutor(
            [
                executed(stdout=b"one\n", observation="one\n"),
                policy_failure(),
                executed(stdout=b"DIFFERENT\n", observation="DIFFERENT\n"),
            ]
        )
        boundary = DockerAttemptBoundary(
            docker_cli=cli,  # type: ignore[arg-type]
            action_executor=executor,  # type: ignore[arg-type]
            container_spec=spec,
            profile="fs1",
            action_limits=action_limits(),
        )
        boundary.execute("one")

        with self.assertRaises(DockerAttemptInfrastructureError) as caught:
            boundary.execute(SECRET_ACTION)

        rendered = f"{caught.exception!s} {caught.exception!r} {boundary!r}"
        for secret in (SECRET_ACTION, "DIFFERENT", FIRST_ID, SECOND_ID):
            self.assertNotIn(secret, rendered)
        self.assertIn(("remove", RUN_ID, (SECOND_ID,)), cli.calls)
        boundary.close()

    def test_policy_cleanup_must_be_independently_absent_before_recreation(self) -> None:
        spec = container_spec()
        first = container(FIRST_ID, spec)
        cli = FakeDockerCli(
            containers=[first],
            states=[trusted_state("base")],
            list_results=[(), (FIRST_ID,)],
        )
        boundary = DockerAttemptBoundary(
            docker_cli=cli,  # type: ignore[arg-type]
            action_executor=FakeExecutor([policy_failure()]),  # type: ignore[arg-type]
            container_spec=spec,
            profile="fs1",
            action_limits=action_limits(),
        )

        with self.assertRaises(DockerAttemptInfrastructureError):
            boundary.execute(SECRET_ACTION)

        self.assertEqual([call[0] for call in cli.calls].count("create"), 1)
        boundary.close()
        self.assertEqual(cli.calls[-1], ("remove", RUN_ID, (FIRST_ID,)))

    def test_infrastructure_invalid_is_redacted_and_never_becomes_material(self) -> None:
        spec = container_spec()
        first = container(FIRST_ID, spec)
        cli = FakeDockerCli(
            containers=[first],
            states=[trusted_state("base")],
            list_results=[(), ()],
        )
        boundary = DockerAttemptBoundary(
            docker_cli=cli,  # type: ignore[arg-type]
            action_executor=FakeExecutor([infrastructure_invalid()]),  # type: ignore[arg-type]
            container_spec=spec,
            profile="fs1",
            action_limits=action_limits(),
        )

        with self.assertRaises(DockerAttemptInfrastructureError) as caught:
            boundary.execute(SECRET_ACTION)

        rendered = f"{caught.exception!s} {caught.exception!r} {boundary!r}"
        for secret in (SECRET_ACTION, SECRET_STREAM.decode(), FIRST_ID, "/Users/"):
            self.assertNotIn(secret, rendered)
        boundary.close()

    def test_close_removes_only_current_exact_container_once_and_ambiguity_raises(self) -> None:
        spec = container_spec()
        first = container(FIRST_ID, spec)
        cli = FakeDockerCli(containers=[first], states=[trusted_state("base")])
        boundary = DockerAttemptBoundary(
            docker_cli=cli,  # type: ignore[arg-type]
            action_executor=FakeExecutor([]),  # type: ignore[arg-type]
            container_spec=spec,
            profile="fs1",
            action_limits=action_limits(),
        )

        boundary.close()
        boundary.close()

        self.assertEqual(
            [call for call in cli.calls if call[0] == "remove"],
            [("remove", RUN_ID, (FIRST_ID,))],
        )

        ambiguous_cli = FakeDockerCli(
            containers=[first],
            states=[trusted_state("base")],
            remove_results=[()],
        )
        ambiguous = DockerAttemptBoundary(
            docker_cli=ambiguous_cli,  # type: ignore[arg-type]
            action_executor=FakeExecutor([]),  # type: ignore[arg-type]
            container_spec=spec,
            profile="fs1",
            action_limits=action_limits(),
        )
        with self.assertRaises(DockerAttemptInfrastructureError) as caught:
            ambiguous.close()
        self.assertNotIn(FIRST_ID, f"{caught.exception!s} {caught.exception!r}")

    def test_preexisting_exact_run_resource_is_refused_without_mutation(self) -> None:
        spec = container_spec()
        first = container(FIRST_ID, spec)
        cli = FakeDockerCli(
            containers=[first],
            states=[trusted_state("base")],
            list_results=[(FIRST_ID,)],
        )

        with self.assertRaises(DockerAttemptInfrastructureError) as caught:
            DockerAttemptBoundary(
                docker_cli=cli,  # type: ignore[arg-type]
                action_executor=FakeExecutor([]),  # type: ignore[arg-type]
                container_spec=spec,
                profile="fs1",
                action_limits=action_limits(),
            )

        self.assertEqual(cli.calls, [("list", RUN_ID)])
        self.assertNotIn(FIRST_ID, f"{caught.exception!s} {caught.exception!r}")


if __name__ == "__main__":
    unittest.main()
