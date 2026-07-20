from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.build_pinned_tokenizer import (
    LLAMA_CPP_COMMIT,
    LLAMA_CPP_REPOSITORY,
    LLAMA_CPP_TAG,
    OLLAMA_COMMIT,
    _assert_unused_build_directory,
    _execute_step,
    _provenance_record,
    _select_built_artifact,
    build_plan,
)


class PinnedTokenizerBuildPlanTests(unittest.TestCase):
    def test_plan_uses_full_immutable_sources_and_the_ollama_compat_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_plan(root / "work", root / "llama-tokenize")

        self.assertEqual(
            OLLAMA_COMMIT,
            "710292ff4f191d8da9f6a4230804fbc693338d4a",
        )
        self.assertEqual(LLAMA_CPP_TAG, "b9840")
        self.assertEqual(
            LLAMA_CPP_COMMIT,
            "8c146a8366304c871efc26057cc90370ccf58dad",
        )
        self.assertEqual(plan["network_phase"], "source provisioning only")
        steps = plan["steps"]
        rendered = "\n".join(" ".join(step["argv"]) for step in steps)
        self.assertIn(OLLAMA_COMMIT, rendered)
        self.assertIn("llama/server", rendered)
        self.assertIn("llama-tokenize", rendered)
        self.assertIn("--parallel 2", rendered)
        self.assertIn("-DGGML_METAL=OFF", rendered)
        self.assertIn("-DBUILD_SHARED_LIBS=OFF", rendered)

    def test_plan_prepares_exact_local_source_before_enabling_compat_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_plan(
                Path(directory) / "work",
                Path(directory) / "llama-tokenize",
            )

        steps = plan["steps"]
        rendered = "\n".join(" ".join(step["argv"]) for step in steps)
        encoded_steps = json.dumps(steps, sort_keys=True)
        patch_index = next(
            index
            for index, step in enumerate(steps)
            if step["name"] == "apply-compat-patches"
        )
        configure_index = next(
            index
            for index, step in enumerate(steps)
            if step["name"] == "configure-tokenizer"
        )
        patch_step = steps[patch_index]
        configure_step = steps[configure_index]
        self.assertLess(patch_index, configure_index)
        self.assertIn("refs/tags/b9840", rendered)
        self.assertIn("--depth 1", rendered)
        self.assertIn(LLAMA_CPP_COMMIT, encoded_steps)
        self.assertIn("-DOLLAMA_LLAMA_CPP_SKIP_COMPAT_PATCH=ON", rendered)
        self.assertNotIn("OLLAMA_LLAMA_CPP_COMPAT=0", rendered)
        self.assertEqual(plan["llama_cpp_source_mode"], "exact-shallow-tag")
        expected_source = str(Path(plan["work_dir"]) / "llama.cpp")
        self.assertEqual(patch_step["cwd"], expected_source)
        self.assertEqual(patch_step["environment"], {})
        self.assertIsNone(configure_step["cwd"])
        self.assertEqual(
            configure_step["environment"],
            {"OLLAMA_LLAMA_CPP_SOURCE": expected_source},
        )

    def test_step_executor_uses_only_the_declared_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_plan(
                Path(directory) / "work",
                Path(directory) / "llama-tokenize",
            )
        steps = {step["name"]: step for step in plan["steps"]}
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        inherited = {
            "OLLAMA_LLAMA_CPP_SOURCE": "/wrong/source",
            "OLLAMA_LLAMA_CPP_SKIP_COMPAT_PATCH": "wrong",
            "OLLAMA_LLAMA_CPP_COMPAT": "0",
        }
        with patch.dict(os.environ, inherited), patch(
            "tools.build_pinned_tokenizer.subprocess.run",
            return_value=completed,
        ) as run:
            _execute_step(steps["apply-compat-patches"])
            patch_call = run.call_args
            _execute_step(steps["configure-tokenizer"])
            configure_call = run.call_args

        self.assertEqual(
            patch_call.kwargs["cwd"],
            Path(plan["work_dir"]) / "llama.cpp",
        )
        self.assertNotIn("OLLAMA_LLAMA_CPP_SOURCE", patch_call.kwargs["env"])
        self.assertEqual(configure_call.kwargs["cwd"], None)
        self.assertEqual(
            configure_call.kwargs["env"]["OLLAMA_LLAMA_CPP_SOURCE"],
            str(Path(plan["work_dir"]) / "llama.cpp"),
        )
        self.assertNotIn(
            "OLLAMA_LLAMA_CPP_SKIP_COMPAT_PATCH",
            configure_call.kwargs["env"],
        )
        self.assertNotIn("OLLAMA_LLAMA_CPP_COMPAT", configure_call.kwargs["env"])

    def test_step_executor_fails_when_a_pinned_identity_differs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_plan(
                Path(directory) / "work",
                Path(directory) / "llama-tokenize",
            )
        verify = next(
            step
            for step in plan["steps"]
            if step["name"] == "verify-llama-cpp-commit"
        )
        completed = subprocess.CompletedProcess(
            verify["argv"],
            0,
            stdout="0" * 40 + "\n",
            stderr="",
        )
        with patch(
            "tools.build_pinned_tokenizer.subprocess.run",
            return_value=completed,
        ), self.assertRaisesRegex(RuntimeError, "pinned identity"):
            _execute_step(verify)

    def test_execution_rejects_reused_build_state_and_symlink_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_dir = root / "cmake-build"
            build_dir.mkdir()
            with self.assertRaisesRegex(RuntimeError, "fresh work directory"):
                _assert_unused_build_directory(build_dir)

            target = root / "real-tokenizer"
            target.write_bytes(b"binary")
            target.chmod(0o755)
            linked = root / "llama-tokenize"
            linked.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "regular non-symlink"):
                _select_built_artifact((linked,))

    def test_published_provenance_is_stable_and_contains_no_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_plan(root / "private-work", root / "private-output")
            provenance = _provenance_record(plan, "sha256:" + "a" * 64)

        encoded = json.dumps(provenance, sort_keys=True)
        self.assertNotIn(str(root), encoded)
        self.assertEqual(provenance["llama_cpp_repository"], LLAMA_CPP_REPOSITORY)
        self.assertEqual(provenance["build_recipe"]["target"], "llama-tokenize")
        self.assertEqual(provenance["build_recipe"]["parallel_jobs"], 2)
        self.assertEqual(
            provenance["build_recipe"]["source_provisioning"],
            {
                "compatibility_patch": "preapplied-from-pinned-ollama",
                "llama_cpp_fetch": "exact-shallow-tag",
            },
        )
        self.assertIn(
            "-DGGML_METAL=OFF",
            provenance["build_recipe"]["cmake_definitions"],
        )


if __name__ == "__main__":
    unittest.main()
