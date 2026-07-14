from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from edgeloopbench.config import (
    MAX_MANIFEST_FILE_BYTES,
    ValidationError,
    load_experiment,
)


VALID_EFFECTIVENESS = """
schema_version = 1
id = "test-effectiveness"
track = "effectiveness"
draft = false
tasks = ["task-a", "task-b"]
strategies = ["direct", "bounded_retry", "maker_verifier"]
seeds = [1, 2]

[model]
id = "example/model"
revision = "abc1234"
artifact_sha256 = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
weight_quantization = "q4"
context_limit_tokens = 8192

[backend]
name = "ollama"
version = "0.31.2"
artifact_sha256 = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
command = ["ollama", "serve"]

[backend.environment]
OLLAMA_HOST = "127.0.0.1:11434"

[budgets.medium]
prompt_tokens = 12000
completion_tokens = 4000
model_calls = 4
tool_calls = 20
public_test_runs = 4
per_call_context_tokens = 8192
"""


VALID_SERVING = """
schema_version = 1
id = "test-serving"
track = "serving"
draft = false

[model]
id = "example/model"
revision = "abc1234"
artifact_sha256 = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
weight_quantization = "q4"
context_limit_tokens = 8192

[backend]
name = "vllm-metal"
version = "commit-deadbeef"
artifact_sha256 = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
command = ["vllm", "serve", "example/model", "--host", "127.0.0.1"]

[backend.environment]
VLLM_METAL_USE_PAGED_ATTENTION = "1"
VLLM_METAL_MEMORY_FRACTION = "0.7"

[[request_shapes]]
name = "short"
prompt_tokens = 512
completion_tokens = 128

[measurement]
warmups = 3
repetitions = 10
concurrency = 1
"""


class ExperimentConfigTests(unittest.TestCase):
    def load_text(self, content: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "experiment.toml"
            path.write_text(textwrap.dedent(content), encoding="utf-8")
            return load_experiment(path)

    def test_loads_effectiveness_plan_with_shared_budget(self) -> None:
        plan = self.load_text(VALID_EFFECTIVENESS)

        self.assertEqual(plan.track, "effectiveness")
        self.assertEqual(plan.strategies, ("direct", "bounded_retry", "maker_verifier"))
        self.assertEqual(plan.budgets["medium"].completion_tokens, 4000)
        self.assertEqual(plan.backend.environment["OLLAMA_HOST"], "127.0.0.1:11434")
        self.assertEqual(plan.run_count, 12)

    def test_loads_serving_plan(self) -> None:
        plan = self.load_text(VALID_SERVING)

        self.assertEqual(plan.track, "serving")
        self.assertEqual(plan.request_shapes[0].prompt_tokens, 512)
        self.assertEqual(plan.measurement.repetitions, 10)
        self.assertEqual(plan.run_count, 10)

    def test_rejects_duplicate_strategy(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            '["direct", "bounded_retry", "maker_verifier"]',
            '["direct", "direct", "maker_verifier"]',
        )

        with self.assertRaisesRegex(ValidationError, "strategies.*unique"):
            self.load_text(invalid)

    def test_rejects_strategy_specific_budgets(self) -> None:
        invalid = (
            VALID_EFFECTIVENESS
            + """

[strategy_budgets.direct]
completion_tokens = 99999
"""
        )

        with self.assertRaisesRegex(ValidationError, "shared.*budgets"):
            self.load_text(invalid)

    def test_rejects_unpinned_publishable_plan(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            'revision = "abc1234"', 'revision = "UNPINNED"'
        )

        with self.assertRaisesRegex(ValidationError, "model.revision.*pinned"):
            self.load_text(invalid)

    def test_rejects_obvious_moving_revision_names(self) -> None:
        for revision in ("master", "nightly", "refs/heads/main"):
            with self.subTest(revision=revision):
                invalid = VALID_EFFECTIVENESS.replace(
                    'revision = "abc1234"', f'revision = "{revision}"'
                )

                with self.assertRaisesRegex(ValidationError, "model.revision.*pinned"):
                    self.load_text(invalid)

    def test_allows_unpinned_draft(self) -> None:
        draft = VALID_EFFECTIVENESS.replace("draft = false", "draft = true").replace(
            'revision = "abc1234"', 'revision = "UNPINNED"'
        )

        self.assertTrue(self.load_text(draft).draft)

    def test_publishable_plan_requires_artifact_checksums(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            'artifact_sha256 = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
            'artifact_sha256 = "UNPINNED"',
        )

        with self.assertRaisesRegex(ValidationError, "model.artifact_sha256.*SHA-256"):
            self.load_text(invalid)

    def test_backend_requires_replayable_command(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace('command = ["ollama", "serve"]\n', "")

        with self.assertRaisesRegex(ValidationError, "command.*non-empty array"):
            self.load_text(invalid)

    def test_backend_manifest_rejects_secret_environment_names(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            'OLLAMA_HOST = "127.0.0.1:11434"',
            'OLLAMA_HOST = "127.0.0.1:11434"\nHF_TOKEN = "must-not-be-published"',
        )

        with self.assertRaisesRegex(ValidationError, "environment.HF_TOKEN.*secret"):
            self.load_text(invalid)

    def test_backend_manifest_rejects_aws_secret_environment_name(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            'OLLAMA_HOST = "127.0.0.1:11434"',
            'OLLAMA_HOST = "127.0.0.1:11434"\nAWS_SECRET_ACCESS_KEY = "not-public"',
        )

        with self.assertRaisesRegex(
            ValidationError, "environment.AWS_SECRET_ACCESS_KEY.*secret"
        ):
            self.load_text(invalid)

    def test_backend_manifest_rejects_personal_access_token_name(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            'OLLAMA_HOST = "127.0.0.1:11434"',
            'OLLAMA_HOST = "127.0.0.1:11434"\nGITHUB_PAT = "not-public"',
        )

        with self.assertRaisesRegex(ValidationError, "environment.GITHUB_PAT.*secret"):
            self.load_text(invalid)

    def test_backend_manifest_rejects_secret_command_flags(self) -> None:
        for argument in (
            "--api-key",
            "--github-token=not-public",
            "--bearer-token",
            "HF_TOKEN=not-public",
            "Authorization: Bearer not-public",
        ):
            with self.subTest(argument=argument):
                invalid = VALID_EFFECTIVENESS.replace(
                    'command = ["ollama", "serve"]',
                    f'command = ["ollama", "serve", "{argument}"]',
                )

                with self.assertRaisesRegex(ValidationError, "command.*secret-bearing"):
                    self.load_text(invalid)

    def test_ollama_backend_requires_loopback_host(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            'OLLAMA_HOST = "127.0.0.1:11434"', 'OLLAMA_HOST = "0.0.0.0:11434"'
        )

        with self.assertRaisesRegex(ValidationError, "OLLAMA_HOST.*loopback"):
            self.load_text(invalid)

    def test_accepts_unbracketed_ipv6_loopback_for_vllm(self) -> None:
        ipv6 = VALID_SERVING.replace('"--host", "127.0.0.1"', '"--host", "::1"')

        self.assertEqual(self.load_text(ipv6).backend.name, "vllm-metal")

    def test_mlx_server_manifest_requires_explicit_loopback_host(self) -> None:
        invalid = VALID_SERVING.replace(
            'name = "vllm-metal"', 'name = "mlx-lm"'
        ).replace(
            'command = ["vllm", "serve", "example/model", "--host", "127.0.0.1"]',
            'command = ["mlx_lm.server", "--model", "example/model"]',
        )

        with self.assertRaisesRegex(ValidationError, "command.*loopback"):
            self.load_text(invalid)

    def test_rejects_loopback_userinfo_with_remote_url_host(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            'OLLAMA_HOST = "127.0.0.1:11434"',
            'OLLAMA_HOST = "http://127.0.0.1:11434@remote.invalid"',
        )

        with self.assertRaisesRegex(ValidationError, "OLLAMA_HOST.*loopback"):
            self.load_text(invalid)

    def test_rejects_oversized_port_without_raw_integer_error(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            'OLLAMA_HOST = "127.0.0.1:11434"',
            'OLLAMA_HOST = "127.0.0.1:' + "9" * 5000 + '"',
        )

        with self.assertRaisesRegex(ValidationError, "OLLAMA_HOST.*loopback"):
            self.load_text(invalid)

    def test_rejects_context_above_model_limit(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            "per_call_context_tokens = 8192", "per_call_context_tokens = 16384"
        )

        with self.assertRaisesRegex(ValidationError, "model context limit"):
            self.load_text(invalid)

    def test_rejects_duplicate_request_shape_name(self) -> None:
        invalid = (
            VALID_SERVING
            + """

[[request_shapes]]
name = "short"
prompt_tokens = 1024
completion_tokens = 128
"""
        )

        with self.assertRaisesRegex(ValidationError, "request shape names.*unique"):
            self.load_text(invalid)

    def test_rejects_non_finite_deployment_budget(self) -> None:
        invalid = (
            VALID_EFFECTIVENESS.replace(
                'track = "effectiveness"', 'track = "deployment"'
            )
            + """

[physical_budget]
max_wall_seconds = nan
"""
        )

        with self.assertRaisesRegex(ValidationError, "finite"):
            self.load_text(invalid)

    def test_deployment_budget_field_order_is_stable(self) -> None:
        deployment = (
            VALID_EFFECTIVENESS.replace(
                'track = "effectiveness"', 'track = "deployment"'
            )
            + """

[physical_budget]
max_energy_joules = 5000
max_wall_seconds = 300
"""
        )

        plan = self.load_text(deployment)

        self.assertEqual(
            tuple(plan.physical_budget or {}),
            ("max_wall_seconds", "max_energy_joules"),
        )

    def test_missing_budget_error_order_is_stable(self) -> None:
        invalid = VALID_EFFECTIVENESS.replace(
            "completion_tokens = 4000\nmodel_calls = 4\ntool_calls = 20\n"
            "public_test_runs = 4\nper_call_context_tokens = 8192\n",
            "",
        )

        with self.assertRaisesRegex(
            ValidationError, "completion_tokens must be an integer"
        ):
            self.load_text(invalid)

    def test_rejects_excessive_toml_nesting_with_domain_error(self) -> None:
        nested = "value = " + "[" * 2000 + "0" + "]" * 2000

        with self.assertRaisesRegex(ValidationError, "nesting is too deep"):
            self.load_text(nested)

    def test_rejects_plan_larger_than_result_safety_limit(self) -> None:
        tasks = ", ".join(f'"task-{index}"' for index in range(1001))
        seeds = ", ".join(str(index) for index in range(84))
        oversized = VALID_EFFECTIVENESS.replace(
            'tasks = ["task-a", "task-b"]', f"tasks = [{tasks}]"
        ).replace("seeds = [1, 2]", f"seeds = [{seeds}]")

        with self.assertRaisesRegex(ValidationError, "planned run count.*250000"):
            self.load_text(oversized)

    def test_rejects_manifest_above_byte_safety_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.toml"
            path.write_bytes(b"#" * (MAX_MANIFEST_FILE_BYTES + 1))

            with self.assertRaisesRegex(ValidationError, "byte safety limit"):
                load_experiment(path)


if __name__ == "__main__":
    unittest.main()
