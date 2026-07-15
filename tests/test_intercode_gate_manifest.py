from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest

from edgeloopbench.intercode_gate_manifest import (
    ConfirmatoryBudgetPins,
    HostSafetyPins,
    InterCodeArtifactPins,
    QuantizationPins,
    TokenizerPins,
    build_precalibration_gate_manifest,
)
from edgeloopbench.model_adapter import (
    OllamaGenerationConfig,
    PHI4_MINI_RAW_PROFILE,
    QWEN35_RAW_PROFILE,
)


def digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("ascii")).hexdigest()


def generation(profile=QWEN35_RAW_PROFILE) -> OllamaGenerationConfig:
    return OllamaGenerationConfig(
        profile=profile,
        runtime_version="0.31.1",
        runtime_binary_sha256=digest("ollama-binary"),
        context_tokens=8192,
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
        stop=("<|im_end|>",),
        keep_alive_seconds=-1,
        request_timeout_seconds=120.0,
    )


def artifacts() -> InterCodeArtifactPins:
    return InterCodeArtifactPins(
        qualification_suite_sha256=digest("qualification-suite"),
        confirmatory_sample_sha256=digest("sample"),
        block_schedule_sha256=digest("schedule"),
        qualification_evidence_root_sha256=digest("qualification-evidence"),
        image_sha256_by_stratum={
            "fs4": digest("fs4"),
            "fs2": digest("fs2"),
            "fs1": digest("fs1"),
            "fs3": digest("fs3"),
        },
        evaluator_sha256=digest("evaluator"),
        state_normalization_sha256=digest("normalizer"),
        prompt_policy_sha256=digest("prompt-policy"),
        controller_sha256=digest("controller"),
    )


def budgets() -> ConfirmatoryBudgetPins:
    return ConfirmatoryBudgetPins(
        logical_prompt_tokens=60_000,
        completion_tokens=8_192,
        per_call_context_tokens=8_192,
        max_output_tokens=1_024,
        model_calls=6,
        environment_actions=6,
        evaluator_calls=7,
        diagnostic_evaluator_calls=13,
        checkpoint_creates=6,
        checkpoint_restores=6,
        safety_recoveries=6,
    )


def tokenizer(profile=QWEN35_RAW_PROFILE) -> TokenizerPins:
    return TokenizerPins(
        helper_sha256=digest("llama-tokenize"),
        model_artifact_sha256=profile.model_artifact_sha256,
        llama_cpp_commit="8c146a8366304c871efc26057cc90370ccf58dad",
        policy_revision="ollama-llama-tokenize-v1",
    )


class PrecalibrationGateManifestTests(unittest.TestCase):
    def build(self, *, profile=QWEN35_RAW_PROFILE):
        return build_precalibration_gate_manifest(
            generation=generation(profile),
            tokenizer=tokenizer(profile),
            quantization=QuantizationPins(
                weight_quantization="Q4_K_M",
                kv_cache_quantization="f16",
            ),
            artifacts=artifacts(),
            budgets=budgets(),
            host_safety=HostSafetyPins(
                policy_sha256=digest("host-policy"),
                telemetry_collector_sha256=digest("telemetry"),
            ),
        )

    def test_gate_seals_every_frozen_identity_before_calibration(self) -> None:
        manifest = self.build()
        record = manifest.canonical_record()

        self.assertEqual(record["phase"], "pre_calibration_gate")
        self.assertEqual(record["model"]["model"], "qwen3.5:4b")
        self.assertEqual(
            record["model"]["model_artifact_sha256"],
            QWEN35_RAW_PROFILE.model_artifact_sha256,
        )
        self.assertEqual(
            record["runtime"]["generation_config_sha256"], generation().sha256
        )
        self.assertEqual(
            record["tokenizer"]["model_artifact_sha256"],
            record["model"]["model_artifact_sha256"],
        )
        self.assertEqual(record["quantization"]["weight_quantization"], "Q4_K_M")
        self.assertEqual(record["quantization"]["kv_cache_quantization"], "f16")
        self.assertEqual(record["design"]["confirmatory_seeds"], [11, 29])
        self.assertEqual(record["design"]["max_attempts"], 6)
        self.assertEqual(record["design"]["attempt_curves"], [1, 2, 4, 6])
        self.assertEqual(record["calibration_gates"]["seed_probe_requests"], 96)
        self.assertEqual(record["calibration_gates"]["host_load_requests_per_model"], 64)
        self.assertEqual(record["host_safety"]["hard_request_cap_two_models"], 4_460)
        self.assertEqual(record["host_safety"]["max_global_block_requeues"], 12)
        self.assertTrue(record["host_safety"]["require_ac_power"])
        self.assertTrue(record["host_safety"]["require_low_power_mode_off"])
        self.assertTrue(record["host_safety"]["require_no_thermal_warnings"])
        self.assertEqual(record["host_safety"]["required_vm_pressure_level"], 1)
        self.assertEqual(record["host_safety"]["admission_free_percent_minimum"], 25)
        self.assertEqual(record["host_safety"]["abort_free_percent_below"], 12)
        self.assertEqual(record["host_safety"]["cooldown_free_percent_minimum"], 20)
        self.assertEqual(record["host_safety"]["admission_disk_free_bytes_minimum"], 32 << 30)
        self.assertEqual(record["host_safety"]["abort_disk_free_bytes_below"], 24 << 30)
        self.assertEqual(record["host_safety"]["max_phase_swap_growth_bytes"], 1 << 30)
        self.assertEqual(record["host_safety"]["max_block_swap_growth_bytes"], 512 << 20)
        self.assertEqual(record["host_safety"]["cooldown_max_swap_growth_bytes"], 64 << 20)
        self.assertEqual(record["host_safety"]["sample_interval_seconds"], 30)
        self.assertEqual(record["host_safety"]["cooldown_consecutive_samples"], 2)
        self.assertEqual(record["host_safety"]["cooldown_timeout_seconds"], 600)
        self.assertEqual(record["budgets"]["evaluator_calls"], 7)
        self.assertEqual(record["budgets"]["diagnostic_evaluator_calls"], 13)
        self.assertNotIn("calibration_outcome", record)

        core = dict(record)
        observed = core.pop("gate_sha256")
        expected = digest(
            json.dumps(
                core,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        self.assertEqual(observed, expected)

    def test_canonical_hash_is_mapping_order_independent_and_model_specific(self) -> None:
        first = self.build()
        second_artifacts = dataclasses.replace(
            artifacts(),
            image_sha256_by_stratum={
                "fs1": digest("fs1"),
                "fs2": digest("fs2"),
                "fs3": digest("fs3"),
                "fs4": digest("fs4"),
            },
        )
        second = build_precalibration_gate_manifest(
            generation=generation(),
            tokenizer=tokenizer(),
            quantization=QuantizationPins("Q4_K_M", "f16"),
            artifacts=second_artifacts,
            budgets=budgets(),
            host_safety=HostSafetyPins(
                policy_sha256=digest("host-policy"),
                telemetry_collector_sha256=digest("telemetry"),
            ),
        )
        phi = self.build(profile=PHI4_MINI_RAW_PROFILE)

        self.assertEqual(first.gate_sha256, second.gate_sha256)
        self.assertNotEqual(first.gate_sha256, phi.gate_sha256)

    def test_gate_is_builder_sealed_and_cannot_be_replaced_after_results(self) -> None:
        manifest = self.build()

        with self.assertRaisesRegex(ValueError, "builder-sealed"):
            dataclasses.replace(manifest, gate_sha256=digest("forged"))

    def test_tokenizer_must_be_bound_to_the_same_model_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "tokenizer.*model artifact"):
            build_precalibration_gate_manifest(
                generation=generation(),
                tokenizer=tokenizer(PHI4_MINI_RAW_PROFILE),
                quantization=QuantizationPins("Q4_K_M", "f16"),
                artifacts=artifacts(),
                budgets=budgets(),
                host_safety=HostSafetyPins(
                    policy_sha256=digest("host-policy"),
                    telemetry_collector_sha256=digest("telemetry"),
                ),
            )

    def test_budget_and_safety_constants_cannot_silently_expand(self) -> None:
        invalid_budgets = (
            {"model_calls": 7},
            {"evaluator_calls": 6},
            {"diagnostic_evaluator_calls": 12},
            {"safety_recoveries": 7},
        )
        for replacement in invalid_budgets:
            with self.subTest(replacement=replacement):
                with self.assertRaises(ValueError):
                    dataclasses.replace(budgets(), **replacement)

        with self.assertRaises(ValueError):
            HostSafetyPins(
                policy_sha256=digest("host-policy"),
                telemetry_collector_sha256=digest("telemetry"),
                max_global_block_requeues=13,
            )
        invalid_safety = (
            {"require_ac_power": False},
            {"required_vm_pressure_level": 2},
            {"admission_free_percent_minimum": 24},
            {"abort_free_percent_below": 13},
            {"max_phase_swap_growth_bytes": (1 << 30) + 1},
            {"sample_interval_seconds": 60},
            {"cooldown_timeout_seconds": 601},
        )
        for replacement in invalid_safety:
            with self.subTest(replacement=replacement):
                with self.assertRaises(ValueError):
                    dataclasses.replace(
                        HostSafetyPins(
                            policy_sha256=digest("host-policy"),
                            telemetry_collector_sha256=digest("telemetry"),
                        ),
                        **replacement,
                    )

    def test_artifact_surface_rejects_missing_strata_and_unpinned_digests(self) -> None:
        with self.assertRaises(ValueError):
            dataclasses.replace(
                artifacts(),
                image_sha256_by_stratum={"fs1": digest("fs1")},
            )
        with self.assertRaises(ValueError):
            dataclasses.replace(artifacts(), evaluator_sha256="latest")


if __name__ == "__main__":
    unittest.main()
