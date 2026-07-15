from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.build_pinned_tokenizer import (
    LLAMA_CPP_COMMIT,
    LLAMA_CPP_TAG,
    OLLAMA_COMMIT,
    _assert_unused_build_directory,
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
        commands = plan["commands"]
        rendered = "\n".join(" ".join(command) for command in commands)
        self.assertIn(OLLAMA_COMMIT, rendered)
        self.assertIn("llama/server", rendered)
        self.assertIn("llama-tokenize", rendered)
        self.assertIn("--parallel 2", rendered)
        self.assertIn("-DGGML_METAL=OFF", rendered)
        self.assertIn("-DBUILD_SHARED_LIBS=OFF", rendered)

    def test_plan_never_disables_ollama_compatibility_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_plan(
                Path(directory) / "work",
                Path(directory) / "llama-tokenize",
            )

        rendered = "\n".join(" ".join(command) for command in plan["commands"])
        self.assertNotIn("OLLAMA_LLAMA_CPP_SOURCE", rendered)
        self.assertNotIn("OLLAMA_LLAMA_CPP_SKIP_COMPAT_PATCH=ON", rendered)
        self.assertNotIn("OLLAMA_LLAMA_CPP_COMPAT=0", rendered)

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
        self.assertEqual(provenance["build_recipe"]["target"], "llama-tokenize")
        self.assertEqual(provenance["build_recipe"]["parallel_jobs"], 2)
        self.assertIn(
            "-DGGML_METAL=OFF",
            provenance["build_recipe"]["cmake_definitions"],
        )


if __name__ == "__main__":
    unittest.main()
