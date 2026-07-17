from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.docker_action_executor import DockerActionLimits
from edgeloopbench.docker_cli import DockerLimits

from edgeloopbench.intercode_campaign_ledger import (
    CAMPAIGN_ARMS,
    CAMPAIGN_MODELS,
    CAMPAIGN_TASK_IDS,
)
from edgeloopbench.intercode_local_model import (
    LocalModelAttestation,
    LocalModelAttestationError,
    OLLAMA_GENERATION_ENDPOINT_SHA256,
    OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
    _ATTESTATION_SEAL,
)
from edgeloopbench.intercode_qualification import (
    QUALIFIED_SUITE_NAME,
    QualificationManifest,
    QualificationReason,
    QualificationRecord,
    _MANIFEST_CONSTRUCTION_SEAL,
    _canonical_json as qualification_canonical_json,
    _qualification_public_core_record_from_values,
)
from edgeloopbench.intercode_replay_environment import (
    V07_STRICT_REPLAY_EVALUATOR_SHA256,
)
from edgeloopbench.intercode_source import (
    EXPECTED_SOURCE_COUNTS,
    INTERCODE_REVISION,
    PUBLIC_POPULATION_SHA256,
    SOURCE_CORPUS_SHA256,
    STATIC_EXCLUSION_AUDIT_SHA256,
)
from edgeloopbench.intercode_source_inventory import (
    VerifiedSourceInventory,
    build_verified_source_inventory,
)
from edgeloopbench.intercode_v07_manifest import (
    V07_CALIBRATION_DESIGN_SHA256,
    V07_INTERVENTION_JOURNAL_REVISION,
    V07_RUN_ID_POLICY_REVISION,
    V07_SCHEDULE_SHA256,
    V07ArtifactPins,
    V07BudgetPins,
    V07DesignPins,
    V07ExecutionPins,
    V07HostSafetyPins,
    V07HostIdentityPins,
    V07ModelIdentityPins,
    V07PrecalibrationManifest,
    V07QualificationPins,
    V07TokenizerPins,
    bind_v07_model_identity,
    bind_v07_qualification_manifest,
    build_v07_artifact_pins,
    build_v07_execution_pins,
    build_v07_precalibration_manifest,
)
from edgeloopbench.intercode_v07_image_provenance import (
    V07_STATE_NORMALIZATION_REVISION,
)
from edgeloopbench.intercode_v07_protocol import V07_SAMPLE_MANIFEST_SHA256
from edgeloopbench.intercode_v07_qualification import (
    V07_QUALIFICATION_NETWORK_MODE,
    V07_QUALIFICATION_PLATFORM,
    V07_QUALIFICATION_REPLAY_COUNT,
    V07_QUALIFICATION_REPLAYS_PER_TASK,
    V07_QUALIFICATION_TASK_COUNT,
    VerifiedV07QualificationEvidence,
    _EVIDENCE_CONSTRUCTION_SEAL,
    _digest as qualification_evidence_digest,
    _public_core_from_values as qualification_evidence_core,
)
from edgeloopbench.model_adapter import (
    OllamaGenerationConfig,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
    RestrictedRawRenderingProfile,
)


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("ascii")).hexdigest()


_SOURCE_FIXTURE = tempfile.TemporaryDirectory()
_SOURCE_INVENTORY: VerifiedSourceInventory | None = None


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))


def source_inventory() -> VerifiedSourceInventory:
    global _SOURCE_INVENTORY
    if _SOURCE_INVENTORY is not None:
        return _SOURCE_INVENTORY
    root = Path(_SOURCE_FIXTURE.name) / "checkout"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "manifest@example.invalid")
    _git(root, "config", "user.name", "Manifest Test")
    sources = {
        "src/edgeloopbench/interactive_controller.py": b"CONTROLLER = 4\n",
        "src/edgeloopbench/intercode_evaluator.py": b"PROGRESS = 1\n",
        "src/edgeloopbench/intercode_replay_environment.py": b"REPLAY = 1\n",
        "src/edgeloopbench/intercode_host_safety.py": b"HOST_SAFETY = 1\n",
        "src/edgeloopbench/intercode_v07_host_policy.py": b"V07_POLICY = 1\n",
        "README.md": b"# fixture\n",
    }
    for relative, payload in sources.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "fixture")
    _SOURCE_INVENTORY = build_verified_source_inventory(root)
    return _SOURCE_INVENTORY


def source_repository_root() -> Path:
    source_inventory()
    return Path(_SOURCE_FIXTURE.name) / "checkout"


def generation(
    profile: RestrictedRawRenderingProfile,
    *,
    runtime_binary_sha256: str | None = None,
    stop: tuple[str, ...] = ("<stop>",),
) -> OllamaGenerationConfig:
    return OllamaGenerationConfig(
        profile=profile,
        runtime_version="0.31.1",
        runtime_binary_sha256=runtime_binary_sha256 or digest("ollama-binary"),
        context_tokens=4_096,
        num_batch=128,
        num_gpu=99,
        main_gpu=0,
        use_mmap=True,
        num_thread=8,
        draft_num_predict=0,
        temperature=0.2,
        top_k=40,
        top_p=0.9,
        min_p=0.0,
        typical_p=1.0,
        repeat_last_n=64,
        repeat_penalty=1.1,
        presence_penalty=0.0,
        frequency_penalty=0.0,
        stop=stop,
        keep_alive_seconds=-1,
        request_timeout_seconds=120.0,
    )


def tokenizer(profile: RestrictedRawRenderingProfile) -> V07TokenizerPins:
    return V07TokenizerPins(
        helper_sha256=digest(f"tokenizer-{profile.model}"),
        model_artifact_sha256=profile.model_artifact_sha256,
        llama_cpp_commit="8c146a8366304c871efc26057cc90370ccf58dad",
        policy_revision="ollama-llama-tokenize-v1",
    )


def local_attestation(
    profile: RestrictedRawRenderingProfile,
    *,
    model_id: str | None = None,
    weight_quantization: str = "Q4_K_M",
    kv_cache_quantization: str = "q8_0",
    model_artifact_size_bytes: int | None = None,
    runtime_binary_sha256: str | None = None,
) -> LocalModelAttestation:
    values: dict[str, object] = {
        "model": model_id or profile.model,
        "renderer_profile_sha256": profile.sha256,
        "model_manifest_sha256": profile.model_manifest_sha256,
        "model_config_sha256": digest(f"config-{profile.model}"),
        "model_artifact_sha256": profile.model_artifact_sha256,
        "model_artifact_size_bytes": model_artifact_size_bytes
        or ((3 << 30) if profile is QWEN35_RAW_PROFILE else (2 << 30)),
        "model_family": "qwen35" if profile is QWEN35_RAW_PROFILE else "phi4mini",
        "model_parameter_label": "4B" if profile is QWEN35_RAW_PROFILE else "3.8B",
        "weight_quantization": weight_quantization,
        "kv_cache_quantization": kv_cache_quantization,
        "runtime_version": "0.31.1",
        "runtime_binary_sha256": runtime_binary_sha256 or digest("ollama-binary"),
    }
    core = {
        "schema": "edgeloopbench.local-ollama-model-attestation.v1",
        **values,
    }
    attestation_sha256 = "sha256:" + hashlib.sha256(
        json.dumps(
            core,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return LocalModelAttestation(
        **values,  # type: ignore[arg-type]
        attestation_sha256=attestation_sha256,
        _seal=_ATTESTATION_SEAL,
    )


def model(profile: RestrictedRawRenderingProfile) -> V07ModelIdentityPins:
    return bind_v07_model_identity(
        attestation=local_attestation(profile),
        generation=generation(profile),
        tokenizer=tokenizer(profile),
    )


def qualification_manifest(
    *,
    excluded_task_id: str | None = None,
) -> QualificationManifest:
    records: list[QualificationRecord] = []
    counts: dict[str, int] = {stratum: 0 for stratum in EXPECTED_SOURCE_COUNTS}
    for stratum, count in EXPECTED_SOURCE_COUNTS.items():
        for index in range(count):
            task_id = f"bash-{stratum}-{index:03d}"
            included = task_id != excluded_task_id
            records.append(
                QualificationRecord(
                    task_id=task_id,
                    stratum=stratum,
                    included=included,
                    exclusion_reasons=(
                        ()
                        if included
                        else (QualificationReason.INFRASTRUCTURE_INVALID,)
                    ),
                )
            )
            counts[stratum] += int(included)
    frozen_records = tuple(records)
    images = {stratum: digest(f"image-{stratum}") for stratum in EXPECTED_SOURCE_COUNTS}
    values = {
        "suite_name": QUALIFIED_SUITE_NAME,
        "source_revision": INTERCODE_REVISION,
        "source_population_sha256": PUBLIC_POPULATION_SHA256,
        "source_corpus_sha256": SOURCE_CORPUS_SHA256,
        "static_exclusion_audit_sha256": STATIC_EXCLUSION_AUDIT_SHA256,
        "evidence_root_sha256": digest("qualification-root"),
        "aggregate_recovery_count": 0,
        "image_sha256_by_stratum": images,
        "evaluator_sha256": digest("qualification-evaluator"),
        "state_normalization_sha256": digest("qualification-normalizer"),
        "records": frozen_records,
        "qualified_count": sum(counts.values()),
        "qualified_by_stratum": counts,
        "scoring_admitted": True,
    }
    suite_sha256 = "sha256:" + hashlib.sha256(
        qualification_canonical_json(
            _qualification_public_core_record_from_values(**values)
        )
    ).hexdigest()
    return QualificationManifest(
        **values,  # type: ignore[arg-type]
        suite_sha256=suite_sha256,
        _construction_seal=_MANIFEST_CONSTRUCTION_SEAL,
    )


def qualification() -> V07QualificationPins:
    return bind_v07_qualification_manifest(qualification_evidence())


def qualification_evidence() -> VerifiedV07QualificationEvidence:
    images = {
        stratum: digest(f"image-{stratum}")
        for stratum in ("fs1", "fs2", "fs3", "fs4")
    }
    evidence_root = digest("selected-qualification-root")
    values = {
        "source_inventory_sha256": source_inventory().inventory_sha256,
        "build_plan_sha256": digest("build-plan"),
        "build_manifest_sha256": digest("build-manifest"),
        "build_verification_sha256": digest("build-verification"),
        "image_set_sha256": digest("image-set"),
        "source_revision": INTERCODE_REVISION,
        "source_population_sha256": PUBLIC_POPULATION_SHA256,
        "source_corpus_sha256": SOURCE_CORPUS_SHA256,
        "static_exclusion_audit_sha256": STATIC_EXCLUSION_AUDIT_SHA256,
        "sample_manifest_sha256": "sha256:" + V07_SAMPLE_MANIFEST_SHA256,
        "task_count": V07_QUALIFICATION_TASK_COUNT,
        "replay_count": V07_QUALIFICATION_REPLAY_COUNT,
        "replays_per_task": V07_QUALIFICATION_REPLAYS_PER_TASK,
        "qualified_task_ids": CAMPAIGN_TASK_IDS,
        "source_capability_set_sha256": digest("selected-capability-set"),
        "platform": V07_QUALIFICATION_PLATFORM,
        "network_mode": V07_QUALIFICATION_NETWORK_MODE,
        "image_id_by_stratum": images,
        "evaluator_sha256": V07_STRICT_REPLAY_EVALUATOR_SHA256,
        "state_normalization_revision": V07_STATE_NORMALIZATION_REVISION,
        "state_normalization_source_sha256": digest("normalizer-source"),
        "state_normalization_sha256": digest("selected-normalizer"),
        "lifecycle_identity_set_sha256": digest("selected-lifecycle-set"),
        "container_identity_set_sha256": digest("selected-container-set"),
        "journal_sha256": digest("selected-qualification-journal"),
        "journal_root_sha256": evidence_root,
        "evidence_root_sha256": evidence_root,
    }
    return VerifiedV07QualificationEvidence(
        **values,  # type: ignore[arg-type]
        suite_sha256=qualification_evidence_digest(
            qualification_evidence_core(**values)  # type: ignore[arg-type]
        ),
        _construction_seal=_EVIDENCE_CONSTRUCTION_SEAL,
    )


def host() -> V07HostIdentityPins:
    return V07HostIdentityPins(
        docker_binary_sha256=digest("docker-binary"),
        docker_endpoint_sha256=digest("docker-endpoint"),
        docker_client_version="27.3.1",
        docker_server_version="27.3.1",
        ollama_runtime_binary_sha256=digest("ollama-binary"),
        ollama_server_version="0.31.1",
        ollama_launch_environment_sha256=OLLAMA_LAUNCH_ENVIRONMENT_SHA256,
        ollama_generation_endpoint_sha256=OLLAMA_GENERATION_ENDPOINT_SHA256,
    )


def artifacts() -> V07ArtifactPins:
    return build_v07_artifact_pins(
        source_inventory=source_inventory(),
        qualification_evidence=qualification_evidence(),
    )


def execution(
    *, host_pins: V07HostIdentityPins | None = None
) -> V07ExecutionPins:
    return build_v07_execution_pins(
        source_inventory=source_inventory(),
        host_identity=host_pins or host(),
    )


def build(
    *,
    models: tuple[V07ModelIdentityPins, ...] | None = None,
    artifact_pins: V07ArtifactPins | None = None,
    host_pins: V07HostIdentityPins | None = None,
    execution_pins: V07ExecutionPins | None = None,
    budgets: V07BudgetPins | None = None,
    design: V07DesignPins | None = None,
) -> V07PrecalibrationManifest:
    selected_host = host_pins or host()
    return build_v07_precalibration_manifest(
        artifacts=artifact_pins or artifacts(),
        models=(
            models
            if models is not None
            else (model(QWEN35_RAW_PROFILE), model(PHI4_MINI_RAW_PROFILE))
        ),
        host_identity=selected_host,
        execution=execution_pins or execution(host_pins=selected_host),
        budgets=budgets or V07BudgetPins(),
        design=design or V07DesignPins(),
    )


class InterCodeV07ManifestTests(unittest.TestCase):
    def test_manifest_seals_the_complete_outcome_free_preregistration(self) -> None:
        manifest = build(
            models=(model(PHI4_MINI_RAW_PROFILE), model(QWEN35_RAW_PROFILE))
        )
        record = manifest.canonical_record()

        self.assertEqual(record["phase"], "pre_calibration")
        self.assertEqual(record["source"]["revision"], INTERCODE_REVISION)
        self.assertEqual(record["source"]["corpus_sha256"], SOURCE_CORPUS_SHA256)
        self.assertEqual(
            record["source"]["static_exclusion_audit_sha256"],
            STATIC_EXCLUSION_AUDIT_SHA256,
        )
        self.assertEqual(record["sample"]["task_ids"], list(CAMPAIGN_TASK_IDS))
        self.assertEqual(record["sample"]["task_count"], 30)
        self.assertEqual(record["sample"]["schedule_sha256"], V07_SCHEDULE_SHA256)
        self.assertEqual(
            tuple(record["qualification"]["image_id_by_stratum"]),
            ("fs1", "fs2", "fs3", "fs4"),
        )
        self.assertEqual(record["qualification"]["container_platform"], "linux/arm64")
        self.assertEqual(record["qualification"]["network_mode"], "none")
        self.assertEqual(
            record["qualification"]["suite_sha256"],
            qualification().suite_sha256,
        )
        self.assertEqual(
            [item["model_id"] for item in record["models"]], list(CAMPAIGN_MODELS)
        )
        self.assertEqual(
            [item["model_artifact_sha256"] for item in record["models"]],
            [
                QWEN35_RAW_PROFILE.model_artifact_sha256,
                PHI4_MINI_RAW_PROFILE.model_artifact_sha256,
            ],
        )
        for item in record["models"]:
            self.assertIn("renderer_profile_sha256", item["profile"])
            self.assertIn("tokenizer_identity_sha256", item["tokenizer"])
            self.assertIn("generation_config_sha256", item["generation"])
            self.assertIn("runtime_binary_sha256", item["runtime"])
            self.assertEqual(item["quantization"]["weight"], "Q4_K_M")
            self.assertEqual(item["quantization"]["kv_cache"], "q8_0")
        self.assertEqual(record["design"]["arms"], list(CAMPAIGN_ARMS))
        self.assertEqual(record["design"]["seed"], 11)
        self.assertEqual(record["design"]["attempt_cap"], 4)
        self.assertEqual(record["analysis"]["bootstrap_seed"], 20_260_716)
        self.assertEqual(record["analysis"]["bootstrap_replicates"], 10_000)
        self.assertEqual(record["hard_prompt_caps"], {"calibration": 26, "confirmatory": 780})
        self.assertEqual(
            record["episode_budgets"],
            {
                "attempts": 4,
                "checkpoint_creates": 4,
                "checkpoint_restores": 4,
                "completion_tokens": 2_048,
                "environment_actions": 4,
                "evaluator_calls": 5,
                "logical_prompt_tokens": 16_380,
                "max_output_tokens": 512,
                "model_calls": 4,
                "per_call_context_tokens": 4_096,
                "safety_recoveries": 4,
            },
        )
        self.assertEqual(
            record["calibration"]["calibration_design_sha256"],
            V07_CALIBRATION_DESIGN_SHA256,
        )
        self.assertTrue(record["calibration"]["host_safety_must_hold"])
        self.assertEqual(record["calibration"]["confirmatory_task_multiplier"], 30)
        self.assertEqual(
            record["code"]["source_code_sha256"],
            source_inventory().inventory_sha256,
        )
        self.assertEqual(
            record["code"]["source_head_commit"], source_inventory().head_commit
        )
        self.assertEqual(
            record["code"]["source_head_tree"], source_inventory().head_tree
        )
        for field in (
            "prompt_policy_sha256",
            "controller_source_sha256",
            "progress_evaluator_source_sha256",
        ):
            self.assertRegex(record["code"][field], r"^sha256:[0-9a-f]{64}$")
        for field in ("policy_source_sha256", "telemetry_collector_source_sha256"):
            self.assertRegex(
                record["execution"]["host_safety"][field],
                r"^sha256:[0-9a-f]{64}$",
            )
        self.assertEqual(
            record["execution"]["docker_limits"],
            {
                "memory_bytes": 512 << 20,
                "memory_swap_bytes": 512 << 20,
                "nano_cpus": 1_000_000_000,
                "nofile_hard": 1024,
                "nofile_soft": 1024,
                "nproc_hard": 64,
                "nproc_soft": 64,
                "pids_limit": 64,
                "storage_bytes": 256 << 20,
            },
        )
        self.assertEqual(
            record["execution"]["docker_action_limits"],
            {
                "deadline_seconds": 10.0,
                "io_queue_chunks": 8,
                "observation_limit_bytes": 2048,
                "private_stream_limit_bytes": 4096,
                "read_chunk_bytes": 4096,
            },
        )
        self.assertEqual(
            record["execution"]["phase_caps"],
            {
                "calibration_model_prompts": 26,
                "confirmatory_model_prompts": 780,
                "qualification_replay_actions": 60,
            },
        )
        self.assertEqual(
            record["execution"]["run_id_policy"]["revision"],
            V07_RUN_ID_POLICY_REVISION,
        )
        self.assertEqual(
            record["execution"]["intervention_journal_revision"],
            V07_INTERVENTION_JOURNAL_REVISION,
        )

        serialized = manifest.canonical_bytes()
        serialized.decode("ascii")
        self.assertTrue(serialized.endswith(b"\n"))
        core = dict(record)
        observed_sha256 = core.pop("manifest_sha256")
        expected = "sha256:" + hashlib.sha256(
            json.dumps(
                core,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        self.assertEqual(observed_sha256, expected)
        lowered = serialized.lower()
        for forbidden in (b'"gold"', b'"outcome"', b'"strict_success"', b"/users/", b"/tmp/"):
            self.assertNotIn(forbidden, lowered)

    def test_manifest_is_builder_sealed_and_returns_detached_projections(self) -> None:
        manifest = build()

        with self.assertRaisesRegex(ValueError, "builder-sealed"):
            V07PrecalibrationManifest(
                artifacts=artifacts(),
                models=(model(QWEN35_RAW_PROFILE), model(PHI4_MINI_RAW_PROFILE)),
                host_identity=host(),
                execution=execution(),
                budgets=V07BudgetPins(),
                design=V07DesignPins(),
                manifest_sha256=digest("forged"),
            )
        with self.assertRaisesRegex(ValueError, "builder-sealed"):
            dataclasses.replace(manifest, manifest_sha256=digest("forged"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.manifest_sha256 = digest("forged")  # type: ignore[misc]
        with self.assertRaises(TypeError):
            manifest.artifacts.qualification.image_id_by_stratum["fs1"] = digest("forged")  # type: ignore[index]

        projection = manifest.canonical_record()
        projection["sample"]["task_ids"].clear()
        generation_record = projection["models"][0]["generation"]["generation_config"]
        generation_record["action_schema"]["properties"].clear()
        fresh = manifest.canonical_record()
        self.assertEqual(len(fresh["sample"]["task_ids"]), 30)
        self.assertIn(
            "command",
            fresh["models"][0]["generation"]["generation_config"]
            ["action_schema"]["properties"],
        )
        self.assertEqual(fresh["manifest_sha256"], manifest.manifest_sha256)

    def test_model_set_is_all_or_none_canonical_and_small_only(self) -> None:
        qwen = model(QWEN35_RAW_PROFILE)
        phi = model(PHI4_MINI_RAW_PROFILE)

        for invalid in ((), (qwen,), (qwen, qwen), (qwen, phi, phi)):
            with self.subTest(size=len(invalid)):
                with self.assertRaisesRegex(ValueError, "exact two-model set"):
                    build(models=invalid)
        for forbidden in ("qwen3.5:9b", "gemma4:12b"):
            with self.subTest(model=forbidden):
                with self.assertRaisesRegex(LocalModelAttestationError, "model tag"):
                    local_attestation(QWEN35_RAW_PROFILE, model_id=forbidden)

    def test_artifact_revision_sample_schedule_and_code_drift_are_rejected(self) -> None:
        base = artifacts()
        for field in (
            "source_revision",
            "source_corpus_sha256",
            "static_exclusion_audit_sha256",
            "task_ids",
            "sample_manifest_sha256",
            "schedule_sha256",
            "strict_evaluator_sha256",
            "calibration_design_sha256",
            "source_code_sha256",
            "controller_source_sha256",
        ):
            change = {field: digest(f"forged-{field}")}
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, "builder-sealed"):
                    dataclasses.replace(base, **change)

    def test_exact_execution_and_v07_host_safety_pins_cannot_be_relaxed(self) -> None:
        pins = execution()
        self.assertIs(type(pins.host_safety), V07HostSafetyPins)
        self.assertEqual(pins.host_safety.max_phase_swap_growth_bytes, 1 << 30)
        self.assertEqual(pins.host_safety.max_episode_swap_growth_bytes, 512 << 20)
        self.assertEqual(pins.host_safety.cooldown_max_swap_growth_bytes, 64 << 20)
        self.assertFalse(hasattr(pins.host_safety, "max_global_block_requeues"))
        self.assertFalse(hasattr(pins.host_safety, "confirmatory_max_requests"))

        for replacement in (
            DockerLimits(
                memory_bytes=1 << 30,
                memory_swap_bytes=1 << 30,
                storage_bytes=256 << 20,
                nano_cpus=1_000_000_000,
                pids_limit=64,
                nofile_soft=1024,
                nofile_hard=1024,
                nproc_soft=64,
                nproc_hard=64,
            ),
            DockerLimits(
                memory_bytes=512 << 20,
                memory_swap_bytes=512 << 20,
                storage_bytes=256 << 20,
                nano_cpus=2_000_000_000,
                pids_limit=64,
                nofile_soft=1024,
                nofile_hard=1024,
                nproc_soft=64,
                nproc_hard=64,
            ),
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(ValueError, "builder-sealed"):
                    dataclasses.replace(pins, docker_limits=replacement)
        relaxed_action = DockerActionLimits(
            deadline_seconds=11.0,
            private_stream_limit_bytes=4096,
            observation_limit_bytes=2048,
            read_chunk_bytes=4096,
            io_queue_chunks=8,
        )
        with self.assertRaisesRegex(ValueError, "builder-sealed"):
            dataclasses.replace(pins, docker_action_limits=relaxed_action)

    def test_images_are_exactly_four_path_free_immutable_ids(self) -> None:
        bound = qualification()
        with self.assertRaisesRegex(ValueError, "builder-sealed"):
            dataclasses.replace(
                bound,
                image_id_by_stratum={
                    **dict(bound.image_id_by_stratum),
                    "fs1": digest("forged"),
                },
            )
        with self.assertRaisesRegex(ValueError, "selected-sample evidence"):
            bind_v07_qualification_manifest(qualification_manifest())

    def test_model_profile_tokenizer_runtime_and_quantization_drift_are_rejected(self) -> None:
        qwen = model(QWEN35_RAW_PROFILE)

        with self.assertRaisesRegex(ValueError, "tokenizer policy"):
            dataclasses.replace(qwen.tokenizer, policy_revision="../../tokenizer-v1")
        with self.assertRaisesRegex(ValueError, "tokenizer and model artifacts"):
            bind_v07_model_identity(
                attestation=local_attestation(QWEN35_RAW_PROFILE),
                generation=generation(QWEN35_RAW_PROFILE),
                tokenizer=V07TokenizerPins(
                    helper_sha256=qwen.tokenizer.helper_sha256,
                    model_artifact_sha256=digest("wrong-model"),
                    llama_cpp_commit=qwen.tokenizer.llama_cpp_commit,
                    policy_revision=qwen.tokenizer.policy_revision,
                ),
            )
        for attestation, pattern in (
            (
                local_attestation(
                    QWEN35_RAW_PROFILE,
                    weight_quantization="F16",
                ),
                "weight quantization",
            ),
            (
                local_attestation(
                    QWEN35_RAW_PROFILE,
                    kv_cache_quantization="f16",
                ),
                "KV-cache quantization",
            ),
            (
                local_attestation(
                    QWEN35_RAW_PROFILE,
                    model_artifact_size_bytes=1,
                ),
                "small-model artifact",
            ),
        ):
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValueError, pattern):
                    bind_v07_model_identity(
                        attestation=attestation,
                        generation=generation(QWEN35_RAW_PROFILE),
                        tokenizer=tokenizer(QWEN35_RAW_PROFILE),
                    )

        different_runtime = bind_v07_model_identity(
            attestation=local_attestation(
                QWEN35_RAW_PROFILE,
                runtime_binary_sha256=digest("other-runtime"),
            ),
            generation=generation(
                QWEN35_RAW_PROFILE,
                runtime_binary_sha256=digest("other-runtime"),
            ),
            tokenizer=tokenizer(QWEN35_RAW_PROFILE),
        )
        with self.assertRaisesRegex(ValueError, "runtime binary"):
            build(models=(different_runtime, model(PHI4_MINI_RAW_PROFILE)))

    def test_host_identity_is_complete_and_matches_frozen_ollama_contract(self) -> None:
        base = host()
        changes = (
            ({"docker_binary_sha256": None}, "Docker identity"),
            ({"ollama_server_version": "0.32.0"}, "Ollama version"),
            ({"ollama_launch_environment_sha256": digest("other-env")}, "launch environment"),
            ({"ollama_generation_endpoint_sha256": digest("remote-endpoint")}, "generation endpoint"),
        )
        for change, pattern in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, pattern):
                    dataclasses.replace(base, **change)

    def test_exact_budget_design_and_hard_caps_cannot_be_relaxed(self) -> None:
        budget_changes = (
            ("logical_prompt_tokens", 16_381),
            ("completion_tokens", 2_049),
            ("per_call_context_tokens", 8_192),
            ("max_output_tokens", 1_024),
            ("model_calls", 5),
            ("environment_actions", 5),
            ("evaluator_calls", 6),
            ("checkpoint_creates", 5),
            ("checkpoint_restores", 5),
            ("safety_recoveries", 5),
        )
        for field, value in budget_changes:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    dataclasses.replace(V07BudgetPins(), **{field: value})

        design_changes = (
            ("seed", 12),
            ("attempt_cap", 5),
            ("arms", tuple(reversed(CAMPAIGN_ARMS))),
            ("bootstrap_seed", 20_260_715),
            ("bootstrap_replicates", 999),
            ("calibration_prompt_cap", 27),
            ("confirmatory_prompt_cap", 781),
        )
        for field, value in design_changes:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    dataclasses.replace(V07DesignPins(), **{field: value})

    def test_local_paths_in_generation_payload_are_rejected(self) -> None:
        for stop in (
            ("/Users/victor/private-stop",),
            ("secret command text",),
            ("foo/../bar",),
            ["<stop>"],
        ):
            with self.subTest(stop=stop):
                with self.assertRaisesRegex(ValueError, "immutable path-free"):
                    bind_v07_model_identity(
                        attestation=local_attestation(QWEN35_RAW_PROFILE),
                        generation=generation(QWEN35_RAW_PROFILE, stop=stop),  # type: ignore[arg-type]
                        tokenizer=tokenizer(QWEN35_RAW_PROFILE),
                    )


if __name__ == "__main__":
    unittest.main()
