"""One private Docker attempt boundary for the v0.7 replay environment.

The boundary owns exactly one run-labelled container at a time.  It exposes no
generic Docker command, source resolver, gold material, model, network, journal,
or host-path surface.  Policy failures are retained in the effectiveness
denominator only after exact cleanup, clean recreation, and deterministic replay
of every previously admissible action.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Never, Protocol

from .docker_action_executor import (
    ActionDisposition,
    CleanupOutcome,
    DockerActionExecutor,
    DockerActionLimits,
    DockerActionResult,
)
from .docker_cli import (
    DockerCli,
    DockerContainer,
    DockerContainerSpec,
    DockerTrustedState,
)
from .interactive_environment import (
    ACTION_POLICY_OBSERVATIONS,
    ActionExecution,
    ActionPolicyFailureKind,
)
from .intercode_evaluator_bridge import (
    AdaptedCollectorState,
    adapt_collector_state,
)
from .intercode_replay_environment import CandidateMaterial


_PROFILE_PATTERN = re.compile(r"^fs[1-4]$")
_RECOVERY_DOMAIN = b"edgeloopbench.v0.7.docker-policy-recovery.v1\0"
_MATERIAL_DOMAIN = b"edgeloopbench.v0.7.docker-replay-material.v1\0"


class DockerAttemptInfrastructureError(RuntimeError):
    """A redacted Docker attempt failure that cannot be charged to the model."""

    def __init__(self) -> None:
        super().__init__("Docker attempt infrastructure failure")

    def __repr__(self) -> str:
        return "<DockerAttemptInfrastructureError redacted>"


class _DockerBoundary(Protocol):
    def list_run_containers(self, run_id: str) -> tuple[str, ...]: ...

    def create_container(self, spec: DockerContainerSpec) -> DockerContainer: ...

    def start_container(self, container: DockerContainer) -> DockerContainer: ...

    def collect_trusted_state(
        self,
        container: DockerContainer,
        *,
        profile: str,
    ) -> DockerTrustedState: ...

    def remove_run_containers(
        self,
        run_id: str,
        identifiers: Sequence[str],
    ) -> tuple[str, ...]: ...


class _ActionExecutor(Protocol):
    def execute(
        self,
        *,
        container: DockerContainer,
        action: str,
        cwd: str,
        limits: DockerActionLimits,
    ) -> DockerActionResult: ...


class _HashUpdater(Protocol):
    def update(self, payload: bytes, /) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class _HistoryStep:
    action: str
    material: CandidateMaterial

    def __repr__(self) -> str:
        return "<_HistoryStep redacted>"


class DockerAttemptBoundary:
    """Own one bounded Docker attempt and provide deterministic replay recovery."""

    __slots__ = (
        "_docker",
        "_executor",
        "_spec",
        "_profile",
        "_limits",
        "_container",
        "_baseline",
        "_current",
        "_history",
        "_recovery_count",
        "_closed",
        "_failed",
    )

    def __init__(
        self,
        *,
        docker_cli: DockerCli,
        action_executor: DockerActionExecutor,
        container_spec: DockerContainerSpec,
        profile: str,
        action_limits: DockerActionLimits,
    ) -> None:
        if type(container_spec) is not DockerContainerSpec:
            raise ValueError("Docker attempt requires a frozen container spec")
        if type(action_limits) is not DockerActionLimits:
            raise ValueError("Docker attempt requires frozen action limits")
        if not isinstance(profile, str) or _PROFILE_PATTERN.fullmatch(profile) is None:
            raise ValueError("Docker attempt profile must be fs1 through fs4")
        for dependency, methods, field in (
            (
                docker_cli,
                (
                    "list_run_containers",
                    "create_container",
                    "start_container",
                    "collect_trusted_state",
                    "remove_run_containers",
                ),
                "Docker boundary",
            ),
            (action_executor, ("execute",), "Docker action executor"),
        ):
            if any(not callable(getattr(dependency, name, None)) for name in methods):
                raise ValueError(f"{field} is incomplete")

        self._docker: _DockerBoundary = docker_cli
        self._executor: _ActionExecutor = action_executor
        self._spec = container_spec
        self._profile = profile
        self._limits = action_limits
        self._container: DockerContainer | None = None
        self._baseline: AdaptedCollectorState | None = None
        self._current: AdaptedCollectorState | None = None
        self._history: tuple[_HistoryStep, ...] = ()
        self._recovery_count = 0
        self._closed = False
        self._failed = False

        try:
            self._require_no_run_resources()
            initial = self._create_started_and_collect()
            self._baseline = initial
            self._current = initial
        except Exception:
            self._failed = True
            self._cleanup_current_best_effort()
            raise DockerAttemptInfrastructureError() from None

    def __repr__(self) -> str:
        state = "closed" if self._closed else "failed" if self._failed else "open"
        return f"<DockerAttemptBoundary state={state} steps={len(self._history)}>"

    def __reduce__(self) -> object:
        raise TypeError("Docker attempt boundaries cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("Docker attempt boundaries cannot be serialized")

    def execute(self, action: str) -> CandidateMaterial | ActionExecution:
        """Execute once, or recover a model-caused policy failure by replay."""

        if self._closed or self._failed or self._container is None:
            raise DockerAttemptInfrastructureError()
        if not isinstance(action, str) or not action:
            raise ValueError("Docker attempt action must be non-empty text")
        container = self._container
        try:
            result = self._executor.execute(
                container=container,
                action=action,
                cwd="/",
                limits=self._limits,
            )
        except Exception:
            self._fail_and_cleanup_current()
        if type(result) is not DockerActionResult:
            self._fail_and_cleanup_current()

        if result.disposition is ActionDisposition.EXECUTED:
            try:
                material, adapted = self._material_after_executed(result)
            except Exception:
                self._fail_and_cleanup_current()
            self._history = (*self._history, _HistoryStep(action, material))
            self._current = adapted
            return material

        if result.disposition is ActionDisposition.POLICY_FAILURE:
            return self._recover_policy_failure(result)

        self._handle_infrastructure_invalid(result)

    def close(self) -> None:
        """Remove only the exact currently owned container, at most once."""

        if self._closed:
            return
        self._closed = True
        container = self._container
        if container is None:
            return
        if not self._remove_exact(container):
            self._failed = True
            raise DockerAttemptInfrastructureError() from None
        self._container = None

    def _require_no_run_resources(self) -> None:
        resources = self._docker.list_run_containers(self._spec.run_id)
        if type(resources) is not tuple or resources:
            raise RuntimeError("run resource precondition failed")

    def _create_started_and_collect(self) -> AdaptedCollectorState:
        created = self._docker.create_container(self._spec)
        if type(created) is not DockerContainer:
            raise RuntimeError("container creation attestation failed")
        self._container = created
        if created.spec is not self._spec or created.image_id != self._spec.image_id:
            raise RuntimeError("container spec attestation failed")
        started = self._docker.start_container(created)
        if started is not created:
            raise RuntimeError("container start attestation failed")
        return self._collect_adapted(created)

    def _collect_adapted(
        self,
        container: DockerContainer,
    ) -> AdaptedCollectorState:
        trusted = self._docker.collect_trusted_state(
            container,
            profile=self._profile,
        )
        return adapt_collector_state(trusted)

    def _material_after_executed(
        self,
        result: DockerActionResult,
    ) -> tuple[CandidateMaterial, AdaptedCollectorState]:
        if self._container is None or self._current is None:
            raise RuntimeError("attempt state is unavailable")
        adapted = self._collect_adapted(self._container)
        material = candidate_material_from_executed_action(
            result=result,
            previous_state=self._current,
            final_state=adapted,
            observation_limit_bytes=self._limits.observation_limit_bytes,
        )
        return material, adapted

    def _recover_policy_failure(
        self,
        result: DockerActionResult,
    ) -> ActionExecution:
        if (
            result.cleanup_outcome is not CleanupOutcome.REMOVED
            or result.policy_failure is None
        ):
            self._failed = True
            raise DockerAttemptInfrastructureError() from None

        # The executor says it removed the contaminated container. Independently
        # prove the exact run is empty before forgetting the old handle.
        try:
            self._require_no_run_resources()
        except Exception:
            self._failed = True
            raise DockerAttemptInfrastructureError() from None
        self._container = None

        try:
            recreated = self._create_started_and_collect()
            baseline = self._baseline
            if baseline is None or not _same_adapted_state(recreated, baseline):
                raise RuntimeError("clean baseline replay differs")
            current = recreated
            for step in self._history:
                container = self._container
                if container is None:
                    raise RuntimeError("replay container is unavailable")
                replay_result = self._executor.execute(
                    container=container,
                    action=step.action,
                    cwd="/",
                    limits=self._limits,
                )
                if (
                    type(replay_result) is not DockerActionResult
                    or replay_result.disposition is not ActionDisposition.EXECUTED
                ):
                    raise RuntimeError("replay action did not execute")
                self._current = current
                replayed, current = self._material_after_executed(replay_result)
                if not _same_candidate_material(replayed, step.material):
                    raise RuntimeError("replay material differs")
            self._current = current
        except Exception:
            self._fail_and_cleanup_current()

        try:
            policy_kind = ActionPolicyFailureKind(result.policy_failure.value)
        except ValueError:
            self._fail_and_cleanup_current()
        if result.observation != ACTION_POLICY_OBSERVATIONS[policy_kind]:
            self._fail_and_cleanup_current()
        self._recovery_count += 1
        current = self._current
        if current is None:  # pragma: no cover - recovery invariant
            self._fail_and_cleanup_current()
        evidence = _recovery_evidence_sha256(
            recovery_count=self._recovery_count,
            policy_kind=policy_kind,
            state_sha256=current.collector_state_sha256,
            history=self._history,
        )
        return ActionExecution(
            observation=result.observation,
            exit_code=result.exit_code,
            state_sha256=current.collector_state_sha256,
            output_sha256=_text_sha256(result.observation),
            admissible=False,
            state_changed=False,
            policy_failure=policy_kind,
            safety_recovery_performed=True,
            safety_recovery_evidence_sha256=evidence,
            safety_recovery_replayed_environment_actions=len(self._history),
        )

    def _handle_infrastructure_invalid(self, result: DockerActionResult) -> None:
        # A typed REMOVED result is independently checked before releasing the
        # handle. Ambiguous cleanup deliberately remains available to close().
        if result.cleanup_outcome is CleanupOutcome.REMOVED:
            try:
                self._require_no_run_resources()
            except Exception:
                self._failed = True
                raise DockerAttemptInfrastructureError() from None
            self._container = None
        self._failed = True
        raise DockerAttemptInfrastructureError() from None

    def _remove_exact(self, container: DockerContainer) -> bool:
        try:
            removed = self._docker.remove_run_containers(
                self._spec.run_id,
                (container.identifier,),
            )
        except Exception:
            return False
        return removed == (container.identifier,)

    def _cleanup_current_best_effort(self) -> None:
        container = self._container
        if container is None:
            return
        if self._remove_exact(container):
            self._container = None

    def _fail_and_cleanup_current(self) -> Never:
        self._failed = True
        self._cleanup_current_best_effort()
        raise DockerAttemptInfrastructureError() from None


def _normalize_private_stream(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise ValueError("private stream must be bytes")
    text = value.decode("utf-8", errors="strict").replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    for character in text:
        if character in {"\n", "\t"}:
            continue
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"Zl", "Zp"}:
            raise UnicodeError("private stream violates text policy")
    return text


def candidate_material_from_executed_action(
    *,
    result: DockerActionResult,
    previous_state: AdaptedCollectorState,
    final_state: AdaptedCollectorState,
    observation_limit_bytes: int,
) -> CandidateMaterial:
    """Convert one attested executed action through the frozen private policy."""

    if (
        type(result) is not DockerActionResult
        or result.disposition is not ActionDisposition.EXECUTED
    ):
        raise ValueError("executed action material requires an executed result")
    if type(previous_state) is not AdaptedCollectorState or type(
        final_state
    ) is not AdaptedCollectorState:
        raise ValueError("executed action material requires exact adapted states")
    if (
        isinstance(observation_limit_bytes, bool)
        or not isinstance(observation_limit_bytes, int)
        or observation_limit_bytes <= 0
    ):
        raise ValueError("executed action observation limit must be positive")
    stdout = _normalize_private_stream(result.private_stdout)
    stderr = _normalize_private_stream(result.private_stderr)
    observation, truncated = _bounded_observation(
        stdout,
        stderr,
        limit=observation_limit_bytes,
    )
    if (
        result.observation != observation
        or result.observation_truncated is not truncated
    ):
        raise ValueError("action observation attestation failed")
    if isinstance(result.exit_code, bool) or not isinstance(result.exit_code, int):
        raise ValueError("action exit attestation failed")
    return CandidateMaterial(
        state=final_state.snapshot,
        collector_state_sha256=final_state.collector_state_sha256,
        exit_code=result.exit_code,
        normalized_stdout=stdout,
        normalized_stderr=stderr,
        agent_observation=result.observation,
        state_changed=(
            final_state.collector_state_sha256
            != previous_state.collector_state_sha256
        ),
    )


def _bounded_observation(stdout: str, stderr: str, *, limit: int) -> tuple[str, bool]:
    combined = f"{stdout}\n[stderr]\n{stderr}" if stdout and stderr else stdout or stderr
    encoded = combined.encode("utf-8")
    if len(encoded) <= limit:
        return combined, False
    prefix = encoded[:limit]
    while prefix:
        try:
            return prefix.decode("utf-8"), True
        except UnicodeDecodeError as error:
            prefix = prefix[: error.start]
    return "", True


def _same_adapted_state(
    left: AdaptedCollectorState,
    right: AdaptedCollectorState,
) -> bool:
    return bool(
        left.snapshot == right.snapshot
        and left.collector_state_sha256 == right.collector_state_sha256
        and left.adapter_revision == right.adapter_revision
        and left.binding_sha256 == right.binding_sha256
    )


def _same_candidate_material(
    left: CandidateMaterial,
    right: CandidateMaterial,
) -> bool:
    return bool(
        left.state == right.state
        and left.collector_state_sha256 == right.collector_state_sha256
        and left.exit_code == right.exit_code
        and left.normalized_stdout == right.normalized_stdout
        and left.normalized_stderr == right.normalized_stderr
        and left.agent_observation == right.agent_observation
        and left.state_changed == right.state_changed
    )


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _material_sha256(material: CandidateMaterial) -> str:
    hasher = hashlib.sha256(_MATERIAL_DOMAIN)
    for value in (
        material.collector_state_sha256,
        str(material.exit_code),
        _text_sha256(material.normalized_stdout),
        _text_sha256(material.normalized_stderr),
        _text_sha256(material.agent_observation),
        "1" if material.state_changed else "0",
    ):
        _update_length_prefixed(hasher, value.encode("ascii"))
    return "sha256:" + hasher.hexdigest()


def _recovery_evidence_sha256(
    *,
    recovery_count: int,
    policy_kind: ActionPolicyFailureKind,
    state_sha256: str,
    history: tuple[_HistoryStep, ...],
) -> str:
    hasher = hashlib.sha256(_RECOVERY_DOMAIN)
    for value in (
        str(recovery_count),
        policy_kind.value,
        state_sha256,
        str(len(history)),
    ):
        _update_length_prefixed(hasher, value.encode("ascii"))
    for step in history:
        _update_length_prefixed(
            hasher,
            _material_sha256(step.material).encode("ascii"),
        )
    return "sha256:" + hasher.hexdigest()


def _update_length_prefixed(hasher: _HashUpdater, payload: bytes) -> None:
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)
