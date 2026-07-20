"""Trusted real-Docker qualification authority for the v0.7 selected sample.

Gold is resolved only inside this module from a source-owned capability.  The
public result contains sealed path-free evidence and opaque trusted-gold
objects; it never exposes a command, raw stream, collector document, Docker
identifier, or host path.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .docker_action_executor import (
    ActionDisposition,
    DockerActionExecutor,
    DockerActionResult,
)
from .docker_cli import (
    DockerCli,
    DockerContainer,
    DockerContainerSpec,
    DockerTrustedState,
)
from .intercode_campaign_ledger import CAMPAIGN_TASK_IDS
from .intercode_v07_calibration import V07_CALIBRATION_TASK_IDS
from .intercode_docker_attempt import candidate_material_from_executed_action
from .intercode_evaluator_bridge import AdaptedCollectorState, adapt_collector_state
from .intercode_replay_environment import (
    CandidateMaterial,
    V07_STRICT_REPLAY_EVALUATOR_SHA256,
)
from .intercode_source import (
    CALIBRATION_POPULATION_SHA256,
    PUBLIC_POPULATION_SHA256,
    SOURCE_CORPUS_SHA256,
    STATIC_EXCLUSION_AUDIT_SHA256,
    InterCodeSource,
    PrivateTaskReference,
    PublicBashTask,
)
from .intercode_v07_manifest import (
    V07_RUN_ID_POLICY_REVISION,
    V07ExecutionPins,
)
from .intercode_v07_image_provenance import VerifiedV07ImageSet
from .intercode_v07_protocol import V07_TASK_IDS, build_v07_sample
from .intercode_v07_qualification import (
    V07_QUALIFICATION_NETWORK_MODE,
    V07_QUALIFICATION_PLATFORM,
    V07QualificationReplay,
    VerifiedV07QualificationEvidence,
    _issue_trusted_v07_qualification_replay,
    build_v07_qualification_evidence,
    verify_v07_qualification_evidence,
)


V07_DOCKER_QUALIFICATION_AUTHORITY_REVISION = (
    "intercode-v0.7-docker-qualification-authority-v2"
)

_STRATA = ("fs1", "fs2", "fs3", "fs4")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TASK_ID = re.compile(r"bash-(fs[1-4])-[0-9]{3}\Z")
_TRUSTED_GOLD_TASK_ID = re.compile(
    r"(?:bash-fs[1-4]-[0-9]{3}|bash-calibration-00[0-3])\Z"
)
_RUN_ID_DOMAIN = b"edgeloopbench.v0.7.run-id-policy.v1\0"
_CAMPAIGN_DOMAIN = b"edgeloopbench.v0.7.qualification-campaign.v1\0"
_LIFECYCLE_DOMAIN = b"edgeloopbench.v0.7.qualification-lifecycle.v1\0"
_CONTAINER_DOMAIN = b"edgeloopbench.v0.7.qualification-container.v1\0"
_EXIT_DOMAIN = b"edgeloopbench.v0.7.qualification-exit-policy.v1\0"
_REPLAY_DOMAIN = b"edgeloopbench.v0.7.qualification-replay-receipt.v1\0"
_GOLD_RECEIPT_DOMAIN = b"edgeloopbench.v0.7.trusted-gold-receipt.v1\0"
_GOLD_SEAL = object()
_RESULT_SEAL = object()


class V07DockerQualificationError(RuntimeError):
    """One redacted failure for every private or infrastructure fault."""

    def __init__(self) -> None:
        super().__init__("v0.7 Docker qualification failed")

    def __repr__(self) -> str:
        return "<V07DockerQualificationError redacted>"


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
        limits: object,
    ) -> DockerActionResult: ...


class V07TrustedGoldMaterial:
    """Authority-sealed evaluator material that cannot be serialized or copied."""

    __slots__ = (
        "task_id",
        "source_capability_sha256",
        "image_id",
        "evaluator_sha256",
        "state_normalization_sha256",
        "replay_receipt_sha256",
        "_material",
        "_locked",
    )

    def __init__(
        self,
        *,
        task_id: str,
        source_capability_sha256: str,
        image_id: str,
        evaluator_sha256: str,
        state_normalization_sha256: str,
        replay_receipt_sha256: str,
        material: CandidateMaterial,
        _construction_seal: object | None = None,
    ) -> None:
        if _construction_seal is not _GOLD_SEAL:
            raise V07DockerQualificationError()
        if _TRUSTED_GOLD_TASK_ID.fullmatch(task_id) is None:
            raise V07DockerQualificationError()
        for value in (
            source_capability_sha256,
            image_id,
            evaluator_sha256,
            state_normalization_sha256,
            replay_receipt_sha256,
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise V07DockerQualificationError()
        if type(material) is not CandidateMaterial:
            raise V07DockerQualificationError()
        object.__setattr__(self, "task_id", task_id)
        object.__setattr__(
            self,
            "source_capability_sha256",
            source_capability_sha256,
        )
        object.__setattr__(self, "image_id", image_id)
        object.__setattr__(self, "evaluator_sha256", evaluator_sha256)
        object.__setattr__(
            self,
            "state_normalization_sha256",
            state_normalization_sha256,
        )
        object.__setattr__(self, "replay_receipt_sha256", replay_receipt_sha256)
        object.__setattr__(self, "_material", material)
        object.__setattr__(self, "_locked", True)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("trusted gold material is immutable")

    def __repr__(self) -> str:
        return "<V07TrustedGoldMaterial redacted>"

    def __copy__(self) -> object:
        raise TypeError("trusted gold material cannot be copied")

    def __deepcopy__(self, _memo: object) -> object:
        raise TypeError("trusted gold material cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("trusted gold material cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("trusted gold material cannot be serialized")


@dataclass(frozen=True, slots=True)
class V07DockerQualificationResult:
    """Verified evidence plus one opaque agreed gold material per task."""

    evidence: VerifiedV07QualificationEvidence
    qualification_campaign_sha256: str
    trusted_gold_by_task_id: Mapping[str, V07TrustedGoldMaterial]
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _RESULT_SEAL:
            raise V07DockerQualificationError()
        if type(self.evidence) is not VerifiedV07QualificationEvidence:
            raise V07DockerQualificationError()
        self.evidence.require_admitted()
        if _SHA256.fullmatch(self.qualification_campaign_sha256) is None:
            raise V07DockerQualificationError()
        values = dict(self.trusted_gold_by_task_id)
        if tuple(values) != CAMPAIGN_TASK_IDS or any(
            type(value) is not V07TrustedGoldMaterial
            or value.task_id != task_id
            for task_id, value in values.items()
        ):
            raise V07DockerQualificationError()
        object.__setattr__(
            self,
            "trusted_gold_by_task_id",
            MappingProxyType(values),
        )


@dataclass(frozen=True, slots=True)
class V07CalibrationGoldResult:
    """Opaque calibration gold only; this type carries no qualification claim."""

    calibration_campaign_sha256: str
    trusted_gold_by_task_id: Mapping[str, V07TrustedGoldMaterial]
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _RESULT_SEAL:
            raise V07DockerQualificationError()
        if _SHA256.fullmatch(self.calibration_campaign_sha256) is None:
            raise V07DockerQualificationError()
        values = dict(self.trusted_gold_by_task_id)
        if tuple(values) != V07_CALIBRATION_TASK_IDS or any(
            type(value) is not V07TrustedGoldMaterial
            or value.task_id != task_id
            for task_id, value in values.items()
        ):
            raise V07DockerQualificationError()
        object.__setattr__(
            self,
            "trusted_gold_by_task_id",
            MappingProxyType(values),
        )


@dataclass(frozen=True, slots=True, repr=False)
class _ReplayProduct:
    fact: V07QualificationReplay | None
    material: CandidateMaterial
    replay_receipt_sha256: str


def v07_qualification_run_id(
    *,
    qualification_campaign_sha256: str,
    episode_index: int,
    run_id_policy_revision: str,
) -> str:
    """Derive the manifest-pinned `v07-<20 hex>` run ID."""

    if (
        type(qualification_campaign_sha256) is not str
        or _SHA256.fullmatch(qualification_campaign_sha256) is None
        or isinstance(episode_index, bool)
        or not isinstance(episode_index, int)
        or not 1 <= episode_index <= 60
        or run_id_policy_revision != V07_RUN_ID_POLICY_REVISION
    ):
        raise V07DockerQualificationError()
    record = {
        "campaign_id": qualification_campaign_sha256,
        "episode_index": episode_index,
        "revision": run_id_policy_revision,
        "role": "qualification",
    }
    hasher = hashlib.sha256(_RUN_ID_DOMAIN)
    hasher.update(_canonical_json(record))
    return "v07-" + hasher.hexdigest()[:20]


def run_v07_docker_qualification(
    *,
    source: InterCodeSource,
    journal_path: str | Path,
    image_set: VerifiedV07ImageSet,
    evaluator_sha256: str,
    execution_pins: V07ExecutionPins,
    docker_cli: DockerCli,
    action_executor: DockerActionExecutor,
) -> V07DockerQualificationResult:
    """Execute, journal, independently verify, and seal the 30-by-2 proof."""

    try:
        return _run_v07_docker_qualification(
            source=source,
            journal_path=journal_path,
            image_set=image_set,
            evaluator_sha256=evaluator_sha256,
            execution_pins=execution_pins,
            docker_cli=docker_cli,
            action_executor=action_executor,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise V07DockerQualificationError() from None


def run_v07_docker_calibration_gold(
    *,
    source: InterCodeSource,
    image_set: VerifiedV07ImageSet,
    evaluator_sha256: str,
    execution_pins: V07ExecutionPins,
    docker_cli: DockerCli,
    action_executor: DockerActionExecutor,
) -> V07CalibrationGoldResult:
    """Issue gold for exactly four upstream quickstart tasks under fs1."""

    try:
        images, evaluator, normalizer, provenance = _validate_inputs(
            source,
            image_set,
            evaluator_sha256,
            execution_pins,
            docker_cli,
            action_executor,
        )
        if (
            source.calibration_population_sha256
            != CALIBRATION_POPULATION_SHA256
            or tuple(
                task.task_id for task in source.calibration_tasks[:4]
            )
            != V07_CALIBRATION_TASK_IDS
        ):
            raise ValueError("calibration source identity differs")
        docker: _DockerBoundary = docker_cli  # type: ignore[assignment]
        executor: _ActionExecutor = action_executor  # type: ignore[assignment]
        campaign_sha256 = _domain_digest(
            _CAMPAIGN_DOMAIN,
            [
                V07_DOCKER_QUALIFICATION_AUTHORITY_REVISION,
                "calibration-gold-fs1-v1",
                source.source_sha256,
                _canonical_json(list(V07_CALIBRATION_TASK_IDS)).decode("ascii"),
                images["fs1"],
                evaluator,
                normalizer,
                provenance.image_set_sha256,
                execution_pins.execution_pins_sha256,
            ],
        )
        products: list[_ReplayProduct] = []
        task_by_id = {
            task.task_id: task for task in source.calibration_tasks[:4]
        }
        episode_index = 1
        for task_id in V07_CALIBRATION_TASK_IDS:
            task = task_by_id[task_id]
            reference = source.private_reference(task_id)
            observed_task, capability = source.qualification_identity(reference)
            if observed_task != task:
                raise ValueError("calibration capability identity differs")
            for replay_index in (1, 2):
                run_id = v07_qualification_run_id(
                    qualification_campaign_sha256=campaign_sha256,
                    episode_index=episode_index,
                    run_id_policy_revision=execution_pins.run_id_policy_revision,
                )
                products.append(
                    _run_one_replay(
                        source=source,
                        task=task,
                        reference=reference,
                        source_capability_sha256=capability,
                        image_id=images["fs1"],
                        replay_index=replay_index,
                        run_id=run_id,
                        docker=docker,
                        executor=executor,
                        execution_pins=execution_pins,
                        evaluator_sha256=evaluator,
                        state_normalization_sha256=normalizer,
                        profile="fs1",
                        issue_qualification_fact=False,
                    )
                )
                episode_index += 1
        _require_duplicate_material_agreement(products, expected_count=8)
        trusted: dict[str, V07TrustedGoldMaterial] = {}
        for index, task_id in enumerate(V07_CALIBRATION_TASK_IDS):
            first, second = products[index * 2 : index * 2 + 2]
            reference = source.private_reference(task_id)
            _task, capability = source.qualification_identity(reference)
            trusted[task_id] = _issue_v07_trusted_gold_material(
                task_id=task_id,
                source_capability_sha256=capability,
                image_id=images["fs1"],
                evaluator_sha256=evaluator,
                state_normalization_sha256=normalizer,
                replay_receipt_sha256=_domain_digest(
                    _GOLD_RECEIPT_DOMAIN,
                    [
                        first.replay_receipt_sha256,
                        second.replay_receipt_sha256,
                    ],
                ),
                material=first.material,
            )
        return V07CalibrationGoldResult(
            calibration_campaign_sha256=campaign_sha256,
            trusted_gold_by_task_id=trusted,
            _construction_seal=_RESULT_SEAL,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise V07DockerQualificationError() from None


def _run_v07_docker_qualification(
    *,
    source: InterCodeSource,
    journal_path: str | Path,
    image_set: VerifiedV07ImageSet,
    evaluator_sha256: str,
    execution_pins: V07ExecutionPins,
    docker_cli: object,
    action_executor: object,
) -> V07DockerQualificationResult:
    images, evaluator, normalizer, provenance = _validate_inputs(
        source,
        image_set,
        evaluator_sha256,
        execution_pins,
        docker_cli,
        action_executor,
    )
    docker: _DockerBoundary = docker_cli  # type: ignore[assignment]
    executor: _ActionExecutor = action_executor  # type: ignore[assignment]
    matrix: list[
        tuple[PublicBashTask, PrivateTaskReference, str, str, int, int]
    ] = []
    episode_index = 1
    for task_id in CAMPAIGN_TASK_IDS:
        reference = source.private_reference(task_id)
        task, capability = source.qualification_identity(reference)
        image_id = images[task.stratum]
        for replay_index in (1, 2):
            matrix.append(
                (
                    task,
                    reference,
                    capability,
                    image_id,
                    replay_index,
                    episode_index,
                )
            )
            episode_index += 1
    campaign_sha256 = _qualification_campaign_sha256(
        source,
        images,
        evaluator,
        normalizer,
        provenance.image_set_sha256,
        execution_pins,
    )
    products: list[_ReplayProduct] = []
    run_ids: set[str] = set()
    for task, reference, capability, image_id, replay_index, position in matrix:
        run_id = v07_qualification_run_id(
            qualification_campaign_sha256=campaign_sha256,
            episode_index=position,
            run_id_policy_revision=execution_pins.run_id_policy_revision,
        )
        if run_id in run_ids:
            raise RuntimeError("qualification run ID collision")
        run_ids.add(run_id)
        products.append(
            _run_one_replay(
                source=source,
                task=task,
                reference=reference,
                source_capability_sha256=capability,
                image_id=image_id,
                replay_index=replay_index,
                run_id=run_id,
                docker=docker,
                executor=executor,
                execution_pins=execution_pins,
                evaluator_sha256=evaluator,
                state_normalization_sha256=normalizer,
            )
        )
    _require_duplicate_material_agreement(products)
    replays = tuple(product.fact for product in products)
    evidence = build_v07_qualification_evidence(
        source=source,
        journal_path=journal_path,
        source_inventory_sha256=provenance.source_inventory_sha256,
        build_plan_sha256=provenance.build_plan_sha256,
        build_manifest_sha256=provenance.build_manifest_sha256,
        build_verification_sha256=provenance.build_verification_sha256,
        image_set_sha256=provenance.image_set_sha256,
        image_id_by_stratum=images,
        evaluator_sha256=evaluator,
        state_normalization_revision=provenance.state_normalization_revision,
        state_normalization_source_sha256=(
            provenance.state_normalization_source_sha256
        ),
        state_normalization_sha256=normalizer,
        replays=replays,
    )
    verified = verify_v07_qualification_evidence(
        source=source,
        journal_path=journal_path,
        source_inventory_sha256=provenance.source_inventory_sha256,
        build_plan_sha256=provenance.build_plan_sha256,
        build_manifest_sha256=provenance.build_manifest_sha256,
        build_verification_sha256=provenance.build_verification_sha256,
        image_set_sha256=provenance.image_set_sha256,
        image_id_by_stratum=images,
        evaluator_sha256=evaluator,
        state_normalization_revision=provenance.state_normalization_revision,
        state_normalization_source_sha256=(
            provenance.state_normalization_source_sha256
        ),
        state_normalization_sha256=normalizer,
    )
    if verified != evidence:
        raise RuntimeError("qualification evidence changed after verification")
    trusted: dict[str, V07TrustedGoldMaterial] = {}
    for index, task_id in enumerate(CAMPAIGN_TASK_IDS):
        first, second = products[index * 2 : index * 2 + 2]
        fact = first.fact
        trusted[task_id] = _issue_v07_trusted_gold_material(
            task_id=task_id,
            source_capability_sha256=fact.source_capability_sha256,
            image_id=fact.image_id,
            evaluator_sha256=fact.evaluator_sha256,
            state_normalization_sha256=fact.state_normalization_sha256,
            replay_receipt_sha256=_domain_digest(
                _GOLD_RECEIPT_DOMAIN,
                [
                    first.replay_receipt_sha256,
                    second.replay_receipt_sha256,
                ],
            ),
            material=first.material,
        )
    return V07DockerQualificationResult(
        evidence=verified,
        qualification_campaign_sha256=campaign_sha256,
        trusted_gold_by_task_id=trusted,
        _construction_seal=_RESULT_SEAL,
    )


def _validate_inputs(
    source: object,
    image_set: object,
    evaluator_sha256: object,
    execution_pins: object,
    docker_cli: object,
    action_executor: object,
) -> tuple[dict[str, str], str, str, VerifiedV07ImageSet]:
    if type(source) is not InterCodeSource:
        raise ValueError("source type differs")
    if (
        source.population_sha256 != PUBLIC_POPULATION_SHA256
        or source.source_sha256 != SOURCE_CORPUS_SHA256
        or source.static_exclusion_audit_sha256
        != STATIC_EXCLUSION_AUDIT_SHA256
        or build_v07_sample(source) != CAMPAIGN_TASK_IDS
        or CAMPAIGN_TASK_IDS != tuple(V07_TASK_IDS)
    ):
        raise ValueError("source identity differs")
    if type(execution_pins) is not V07ExecutionPins:
        raise ValueError("execution pins type differs")
    execution_pins.canonical_record()
    if (
        execution_pins.qualification_replay_actions != 60
        or execution_pins.run_id_policy_revision != V07_RUN_ID_POLICY_REVISION
    ):
        raise ValueError("execution pins differ")
    if type(image_set) is not VerifiedV07ImageSet:
        raise ValueError("image provenance type differs")
    image_set.require_admitted()
    if image_set.source_inventory_sha256 != execution_pins.source_inventory_sha256:
        raise ValueError("image and execution source inventories differ")
    images = dict(image_set.image_id_by_stratum)
    if evaluator_sha256 != V07_STRICT_REPLAY_EVALUATOR_SHA256:
        raise ValueError("evaluator identity differs")
    for dependency, names in (
        (
            docker_cli,
            (
                "list_run_containers",
                "create_container",
                "start_container",
                "collect_trusted_state",
                "remove_run_containers",
            ),
        ),
        (action_executor, ("execute",)),
    ):
        if any(not callable(getattr(dependency, name, None)) for name in names):
            raise ValueError("qualification dependency is incomplete")
    return images, evaluator_sha256, image_set.state_normalization_sha256, image_set


def _run_one_replay(
    *,
    source: InterCodeSource,
    task: PublicBashTask,
    reference: PrivateTaskReference,
    source_capability_sha256: str,
    image_id: str,
    replay_index: int,
    run_id: str,
    docker: _DockerBoundary,
    executor: _ActionExecutor,
    execution_pins: V07ExecutionPins,
    evaluator_sha256: str,
    state_normalization_sha256: str,
    profile: str | None = None,
    issue_qualification_fact: bool = True,
) -> _ReplayProduct:
    selected_profile = task.stratum if profile is None else profile
    if selected_profile not in _STRATA:
        raise RuntimeError("qualification collector profile differs")
    resources = docker.list_run_containers(run_id)
    if type(resources) is not tuple or resources:
        raise RuntimeError("qualification run is not initially empty")
    spec = DockerContainerSpec(
        run_id=run_id,
        role="agent",
        image=image_id,
        limits=execution_pins.docker_limits,
        image_id=image_id,
    )
    operation: tuple[
        DockerContainer,
        AdaptedCollectorState,
        CandidateMaterial,
    ] | None = None
    try:
        container = docker.create_container(spec)
        if (
            type(container) is not DockerContainer
            or container.spec is not spec
            or container.image_id != image_id
        ):
            raise RuntimeError("container attestation differs")
        if docker.start_container(container) is not container:
            raise RuntimeError("container start attestation differs")
        initial_raw = docker.collect_trusted_state(
            container,
            profile=selected_profile,
        )
        if (
            type(initial_raw) is not DockerTrustedState
            or initial_raw.profile != selected_profile
        ):
            raise RuntimeError("initial collector attestation differs")
        initial = adapt_collector_state(initial_raw)
        gold_action = source.gold_for_evaluator(reference)
        result = executor.execute(
            container=container,
            action=gold_action,
            cwd="/",
            limits=execution_pins.docker_action_limits,
        )
        if (
            type(result) is not DockerActionResult
            or result.disposition is not ActionDisposition.EXECUTED
        ):
            raise RuntimeError("gold action did not execute")
        final_raw = docker.collect_trusted_state(
            container,
            profile=selected_profile,
        )
        if (
            type(final_raw) is not DockerTrustedState
            or final_raw.profile != selected_profile
        ):
            raise RuntimeError("final collector attestation differs")
        final = adapt_collector_state(final_raw)
        material = candidate_material_from_executed_action(
            result=result,
            previous_state=initial,
            final_state=final,
            observation_limit_bytes=(
                execution_pins.docker_action_limits.observation_limit_bytes
            ),
        )
        operation = (container, initial, material)
    except Exception:
        operation = None
    cleanup_verified = _cleanup_exact_run(docker, run_id)
    if operation is None or not cleanup_verified:
        raise RuntimeError("qualification replay failed")
    container, initial, material = operation
    lifecycle_identity = _domain_digest(
        _LIFECYCLE_DOMAIN,
        [
            V07_DOCKER_QUALIFICATION_AUTHORITY_REVISION,
            run_id,
            task.task_id,
            str(replay_index),
            source_capability_sha256,
            image_id,
        ],
    )
    container_identity = _domain_digest(
        _CONTAINER_DOMAIN,
        [
            lifecycle_identity,
            container.identifier,
            container.name,
            container.image_id,
            _canonical_json(list(container.labels)).decode("ascii"),
        ],
    )
    exit_policy = _domain_digest(_EXIT_DOMAIN, [str(material.exit_code)])
    stdout_sha256 = _text_digest(material.normalized_stdout)
    stderr_sha256 = _text_digest(material.normalized_stderr)
    replay_receipt = _domain_digest(
        _REPLAY_DOMAIN,
        [
            lifecycle_identity,
            container_identity,
            initial.collector_state_sha256,
            material.collector_state_sha256,
            exit_policy,
            stdout_sha256,
            stderr_sha256,
        ],
    )
    fact = (
        _issue_trusted_v07_qualification_replay(
            task_id=task.task_id,
            stratum=selected_profile,
            replay_index=replay_index,
            source_capability_sha256=source_capability_sha256,
            image_id=image_id,
            platform=V07_QUALIFICATION_PLATFORM,
            network_mode=V07_QUALIFICATION_NETWORK_MODE,
            evaluator_sha256=evaluator_sha256,
            state_normalization_sha256=state_normalization_sha256,
            lifecycle_identity_sha256=lifecycle_identity,
            container_identity_sha256=container_identity,
            container_absent_before=True,
            clean_initial_state=True,
            container_profile_match=True,
            infrastructure_valid=True,
            setup_valid=True,
            evaluator_valid=True,
            gold_replay_passed=True,
            exit_policy_sha256=exit_policy,
            initial_state_sha256=initial.collector_state_sha256,
            normalized_stdout_sha256=stdout_sha256,
            normalized_stderr_sha256=stderr_sha256,
            observable_state_sha256=material.collector_state_sha256,
            container_destroyed=True,
            container_absent_after=True,
            cleanup_verified=True,
        )
        if issue_qualification_fact
        else None
    )
    return _ReplayProduct(fact, material, replay_receipt)


def _cleanup_exact_run(docker: _DockerBoundary, run_id: str) -> bool:
    try:
        resources = docker.list_run_containers(run_id)
        if type(resources) is not tuple:
            return False
        if resources:
            removed = docker.remove_run_containers(run_id, resources)
            if removed != resources:
                return False
        after = docker.list_run_containers(run_id)
        return type(after) is tuple and not after
    except Exception:
        return False


def _require_duplicate_material_agreement(
    products: list[_ReplayProduct],
    *,
    expected_count: int = 60,
) -> None:
    if len(products) != expected_count or expected_count % 2:
        raise RuntimeError("qualification replay matrix is incomplete")
    for index in range(0, expected_count, 2):
        first, second = products[index : index + 2]
        if first.material != second.material:
            raise RuntimeError("qualification duplicate materials disagree")


def _qualification_campaign_sha256(
    source: InterCodeSource,
    images: Mapping[str, str],
    evaluator_sha256: str,
    normalizer_sha256: str,
    image_set_sha256: str,
    execution_pins: V07ExecutionPins,
) -> str:
    return _domain_digest(
        _CAMPAIGN_DOMAIN,
        [
            V07_DOCKER_QUALIFICATION_AUTHORITY_REVISION,
            source.source_sha256,
            _canonical_json(list(CAMPAIGN_TASK_IDS)).decode("ascii"),
            _canonical_json(dict(images)).decode("ascii"),
            evaluator_sha256,
            normalizer_sha256,
            image_set_sha256,
            execution_pins.execution_pins_sha256,
        ],
    )


def _issue_v07_trusted_gold_material(
    *,
    task_id: str,
    source_capability_sha256: str,
    image_id: str,
    evaluator_sha256: str,
    state_normalization_sha256: str,
    replay_receipt_sha256: str,
    material: CandidateMaterial,
) -> V07TrustedGoldMaterial:
    return V07TrustedGoldMaterial(
        task_id=task_id,
        source_capability_sha256=source_capability_sha256,
        image_id=image_id,
        evaluator_sha256=evaluator_sha256,
        state_normalization_sha256=state_normalization_sha256,
        replay_receipt_sha256=replay_receipt_sha256,
        material=material,
        _construction_seal=_GOLD_SEAL,
    )


def _open_v07_trusted_gold_material(
    value: V07TrustedGoldMaterial,
    *,
    task_id: str,
) -> CandidateMaterial:
    if (
        type(value) is not V07TrustedGoldMaterial
        or value.task_id != task_id
        or type(value._material) is not CandidateMaterial
    ):
        raise V07DockerQualificationError()
    return value._material


def _domain_digest(domain: bytes, values: list[str]) -> str:
    hasher = hashlib.sha256(domain)
    for value in values:
        payload = value.encode("utf-8")
        hasher.update(len(payload).to_bytes(8, "big"))
        hasher.update(payload)
    return "sha256:" + hasher.hexdigest()


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


__all__ = (
    "V07_DOCKER_QUALIFICATION_AUTHORITY_REVISION",
    "V07CalibrationGoldResult",
    "V07DockerQualificationError",
    "V07DockerQualificationResult",
    "V07TrustedGoldMaterial",
    "run_v07_docker_calibration_gold",
    "run_v07_docker_qualification",
    "v07_qualification_run_id",
)
