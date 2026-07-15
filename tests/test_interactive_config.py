from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from edgeloopbench.config import ValidationError, load_experiment


VALID_LEGACY = """
schema_version = 1
id = "legacy-effectiveness"
track = "effectiveness"
draft = false
tasks = ["task-a"]
strategies = ["direct", "bounded_retry"]
seeds = [1]

[generation]
thinking = false
temperature = 0.0
edit_schema_revision = "full-file-edits-v1"
controller_revision = "35f7f97000000000000000000000000000000000"

[model]
id = "example/model"
revision = "abc1234"
artifact_sha256 = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
weight_quantization = "q4_k_m"
context_limit_tokens = 8192

[backend]
name = "ollama"
version = "0.31.2"
artifact_sha256 = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
command = ["ollama", "serve"]

[backend.environment]
OLLAMA_HOST = "127.0.0.1:11434"

[budgets.fixed]
prompt_tokens = 12000
completion_tokens = 4000
model_calls = 4
tool_calls = 20
public_test_runs = 4
per_call_context_tokens = 8192
"""


VALID_INTERACTIVE = """
schema_version = 1
id = "v06-calibration-qwen"
track = "effectiveness"
draft = false
tasks = ["bash-calibration-000"]
strategies = [
  "direct",
  "independent_verified_sampling",
  "raw_feedback_loop",
  "engineered_loop",
]
seeds = [11, 29, 47]

[generation]
thinking = false
temperature = 0.2
action_schema_revision = "bash-command-v1"
controller_revision = "35f7f97000000000000000000000000000000000"

[model]
id = "example/qwen-4b"
revision = "abc1234"
artifact_sha256 = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
weight_quantization = "q4_k_m"
kv_cache_quantization = "f16"
context_limit_tokens = 8192

[backend]
name = "ollama"
version = "0.31.2"
artifact_sha256 = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
command = ["ollama", "serve"]

[backend.environment]
OLLAMA_HOST = "127.0.0.1:11434"

[budgets.fixed]
prompt_tokens = 60000
completion_tokens = 8000
model_calls = 10
tool_calls = 10
public_test_runs = 10
environment_actions = 10
evaluator_calls = 10
checkpoint_creates = 10
checkpoint_restores = 10
per_call_context_tokens = 8192

[environment]
adapter = "intercode-bash-v1"
phase = "calibration"
adapter_revision = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
source_revision = "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8"
source_sha256 = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
suite_sha256 = "sha256:3333333333333333333333333333333333333333333333333333333333333333"
evaluator_revision = "sha256:4444444444444444444444444444444444444444444444444444444444444444"
prompt_revision = "sha256:5555555555555555555555555555555555555555555555555555555555555555"
observation_policy_revision = "agent-observation-v1"
stop_signal_policy_revision = "controller-stop-signal-v1"
checkpoint_policy_revision = "checkpoint-policy-v1"
max_attempts = 10
"""


class InteractiveExperimentConfigTests(unittest.TestCase):
    def load_text(self, content: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.toml"
            path.write_text(textwrap.dedent(content), encoding="utf-8")
            return load_experiment(path)

    def test_environment_is_additive_and_legacy_manifest_still_loads(self) -> None:
        plan = self.load_text(VALID_LEGACY)

        self.assertIsNone(getattr(plan, "environment", None))
        self.assertEqual(plan.strategies, ("direct", "bounded_retry"))
        self.assertEqual(
            plan.generation.edit_schema_revision,
            "full-file-edits-v1",
        )
        self.assertIsNone(getattr(plan.generation, "action_schema_revision", None))

    def test_loads_fully_pinned_interactive_manifest(self) -> None:
        plan = self.load_text(VALID_INTERACTIVE)

        self.assertEqual(
            plan.strategies,
            (
                "direct",
                "independent_verified_sampling",
                "raw_feedback_loop",
                "engineered_loop",
            ),
        )
        self.assertEqual(plan.environment.adapter, "intercode-bash-v1")
        self.assertEqual(plan.environment.phase, "calibration")
        self.assertEqual(plan.environment.max_attempts, 10)
        self.assertEqual(plan.generation.action_schema_revision, "bash-command-v1")
        self.assertIsNone(plan.generation.edit_schema_revision)
        self.assertEqual(plan.model.weight_quantization, "q4_k_m")
        self.assertEqual(plan.model.kv_cache_quantization, "f16")
        self.assertEqual(plan.budgets["fixed"].environment_actions, 10)
        self.assertEqual(plan.budgets["fixed"].evaluator_calls, 10)
        self.assertEqual(plan.budgets["fixed"].checkpoint_creates, 10)
        self.assertEqual(plan.budgets["fixed"].checkpoint_restores, 10)

    def test_interactive_manifest_requires_each_reproducibility_pin(self) -> None:
        fields = (
            "adapter_revision",
            "source_revision",
            "source_sha256",
            "suite_sha256",
            "evaluator_revision",
            "prompt_revision",
        )

        for field in fields:
            with self.subTest(field=field):
                invalid = self.without_line(VALID_INTERACTIVE, field)

                with self.assertRaisesRegex(ValidationError, rf"environment.*{field}"):
                    self.load_text(invalid)

    def test_interactive_manifest_requires_named_adapter(self) -> None:
        invalid = self.without_line(VALID_INTERACTIVE, "adapter")

        with self.assertRaisesRegex(ValidationError, "environment.*adapter"):
            self.load_text(invalid)

    def test_interactive_manifest_rejects_moving_revisions(self) -> None:
        fields = (
            "adapter_revision",
            "source_revision",
            "evaluator_revision",
            "prompt_revision",
        )

        for field in fields:
            with self.subTest(field=field):
                invalid = VALID_INTERACTIVE.replace(
                    next(
                        line
                        for line in VALID_INTERACTIVE.splitlines()
                        if line.startswith(f"{field} =")
                    ),
                    f'{field} = "latest"',
                )

                with self.assertRaisesRegex(
                    ValidationError, rf"environment.*{field}.*immutable|pinned"
                ):
                    self.load_text(invalid)

    def test_interactive_source_revision_must_be_an_immutable_commit(self) -> None:
        invalid = VALID_INTERACTIVE.replace(
            'source_revision = "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8"',
            'source_revision = "v1.0.0"',
        )

        with self.assertRaisesRegex(
            ValidationError, "environment.*source_revision.*commit"
        ):
            self.load_text(invalid)

    def test_interactive_manifest_requires_sha256_source_and_suite_digests(self) -> None:
        for field in ("source_sha256", "suite_sha256"):
            with self.subTest(field=field):
                original = next(
                    line
                    for line in VALID_INTERACTIVE.splitlines()
                    if line.startswith(f"{field} =")
                )
                invalid = VALID_INTERACTIVE.replace(original, f'{field} = "abc1234"')

                with self.assertRaisesRegex(
                    ValidationError, rf"environment.*{field}.*SHA-256"
                ):
                    self.load_text(invalid)

    def test_interactive_manifest_requires_three_information_policy_revisions(self) -> None:
        fields = (
            "observation_policy_revision",
            "stop_signal_policy_revision",
            "checkpoint_policy_revision",
        )

        for field in fields:
            with self.subTest(field=field):
                invalid = self.without_line(VALID_INTERACTIVE, field)

                with self.assertRaisesRegex(ValidationError, rf"environment.*{field}"):
                    self.load_text(invalid)

    def test_interactive_information_policy_revisions_must_be_immutable(self) -> None:
        invalid = VALID_INTERACTIVE.replace(
            'stop_signal_policy_revision = "controller-stop-signal-v1"',
            'stop_signal_policy_revision = "latest"',
        )

        with self.assertRaisesRegex(
            ValidationError,
            "environment.*stop_signal_policy_revision.*immutable",
        ):
            self.load_text(invalid)

    def test_interactive_manifest_requires_exact_strategy_family(self) -> None:
        variants = (
            VALID_INTERACTIVE.replace('  "engineered_loop",\n', ""),
            VALID_INTERACTIVE.replace(
                '  "engineered_loop",\n', '  "engineered_loop",\n  "bounded_retry",\n'
            ),
        )

        for invalid in variants:
            strategies = invalid.split("strategies =", 1)[1].split("]", 1)[0]
            with self.subTest(strategies=strategies):
                with self.assertRaisesRegex(
                    ValidationError, "exact interactive strategy family"
                ):
                    self.load_text(invalid)

    def test_interactive_manifest_rejects_mixed_legacy_arms(self) -> None:
        for legacy_arm in (
            "bounded_retry",
            "maker_verifier",
            "evidence_gated_loop",
            "goal_skill_loop",
        ):
            with self.subTest(legacy_arm=legacy_arm):
                invalid = VALID_INTERACTIVE.replace(
                    '  "engineered_loop",\n',
                    f'  "engineered_loop",\n  "{legacy_arm}",\n',
                )

                with self.assertRaisesRegex(
                    ValidationError, "legacy.*interactive|interactive.*legacy"
                ):
                    self.load_text(invalid)

    def test_interactive_budgets_require_action_evaluation_and_checkpoint_ceilings(
        self,
    ) -> None:
        fields = (
            "environment_actions",
            "evaluator_calls",
            "checkpoint_creates",
            "checkpoint_restores",
        )

        for field in fields:
            with self.subTest(field=field):
                invalid = self.without_line(VALID_INTERACTIVE, field)

                with self.assertRaisesRegex(ValidationError, rf"budgets\.fixed.*{field}"):
                    self.load_text(invalid)

    def test_interactive_model_requires_separate_kv_cache_quantization(self) -> None:
        invalid = VALID_INTERACTIVE.replace('kv_cache_quantization = "f16"\n', "")

        with self.assertRaisesRegex(
            ValidationError, "model.*kv_cache_quantization.*separate|required"
        ):
            self.load_text(invalid)

    def test_interactive_generation_requires_action_schema_not_edit_schema(self) -> None:
        missing = self.without_line(VALID_INTERACTIVE, "action_schema_revision")
        legacy_schema = VALID_INTERACTIVE.replace(
            'action_schema_revision = "bash-command-v1"',
            'edit_schema_revision = "full-file-edits-v1"',
        )

        with self.assertRaisesRegex(
            ValidationError, "generation.*action_schema_revision.*required"
        ):
            self.load_text(missing)
        with self.assertRaisesRegex(
            ValidationError,
            "generation.*edit_schema_revision.*interactive|"
            "interactive.*edit_schema_revision",
        ):
            self.load_text(legacy_schema)

    def test_interactive_manifest_does_not_accept_collapsed_quantization(self) -> None:
        invalid = VALID_INTERACTIVE.replace(
            'weight_quantization = "q4_k_m"\nkv_cache_quantization = "f16"',
            'weight_quantization = "q4_k_m; kv=f16"',
        )

        with self.assertRaisesRegex(
            ValidationError, "model.*kv_cache_quantization.*separate|required"
        ):
            self.load_text(invalid)

    def test_verified_sampling_requires_k_ten(self) -> None:
        for maximum in (1, 4, 9, 11):
            with self.subTest(maximum=maximum):
                invalid = VALID_INTERACTIVE.replace(
                    "max_attempts = 10", f"max_attempts = {maximum}"
                )

                with self.assertRaisesRegex(
                    ValidationError,
                    "max_attempts.*10|K.*10",
                ):
                    self.load_text(invalid)

    def test_verified_sampling_requires_nonzero_temperature(self) -> None:
        invalid = VALID_INTERACTIVE.replace("temperature = 0.2", "temperature = 0.0")

        with self.assertRaisesRegex(
            ValidationError,
            "temperature.*positive|nonzero.*temperature|temperature.*nonzero",
        ):
            self.load_text(invalid)

    def test_confirmatory_manifest_requires_calibration_manifest_identity(self) -> None:
        invalid = VALID_INTERACTIVE.replace(
            'phase = "calibration"', 'phase = "confirmatory"'
        )

        with self.assertRaisesRegex(
            ValidationError, "environment.*calibration_manifest_sha256.*confirmatory"
        ):
            self.load_text(invalid)

    def test_confirmatory_manifest_loads_with_calibration_manifest_identity(self) -> None:
        confirmatory = VALID_INTERACTIVE.replace(
            'phase = "calibration"',
            'phase = "confirmatory"\n'
            'calibration_manifest_sha256 = '
            '"sha256:6666666666666666666666666666666666666666666666666666666666666666"',
        )

        plan = self.load_text(confirmatory)

        self.assertEqual(plan.environment.phase, "confirmatory")
        self.assertEqual(
            plan.environment.calibration_manifest_sha256,
            "sha256:6666666666666666666666666666666666666666666666666666666666666666",
        )

    def test_confirmatory_calibration_manifest_identity_must_be_sha256(self) -> None:
        invalid = VALID_INTERACTIVE.replace(
            'phase = "calibration"',
            'phase = "confirmatory"\ncalibration_manifest_sha256 = "abc1234"',
        )

        with self.assertRaisesRegex(
            ValidationError,
            "environment.*calibration_manifest_sha256.*SHA-256",
        ):
            self.load_text(invalid)

    def test_interactive_phase_is_explicit(self) -> None:
        invalid = VALID_INTERACTIVE.replace('phase = "calibration"', 'phase = "pilot"')

        with self.assertRaisesRegex(
            ValidationError, "environment.*phase.*calibration.*confirmatory"
        ):
            self.load_text(invalid)

    @staticmethod
    def without_line(content: str, field: str) -> str:
        line = next(
            line for line in content.splitlines() if line.startswith(f"{field} =")
        )
        return content.replace(line + "\n", "")


if __name__ == "__main__":
    unittest.main()
