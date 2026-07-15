from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTERCODE_REVISION = "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8"
BASE_DIGEST = "sha256:2e05d3b43282818e548d97f7a7c4dd7cab14760603972353e5cecdac0839146b"
RAW_ROOT = ROOT / "vendor" / "intercode" / INTERCODE_REVISION / "docker"
DERIVED_ROOT = ROOT / "docker" / "intercode"

RAW_SHA256 = {
    "nl2bash.Dockerfile": "c8b52b44cc276921f1b139d49562152792872c7b013261b748305a78d4230189",
    "bash_scripts/setup_nl2b_fs_1.sh": "02b9a2206d809a9fca03b755e61b94618248a400fd3132ac61d32b6f3009dd3f",
    "bash_scripts/setup_nl2b_fs_2.sh": "05c3109c4e9999e661d66c6d74137f0238b88017ec9cf884abdda0499e94ff1d",
    "bash_scripts/setup_nl2b_fs_3.sh": "5e8d9f832f272c31dfb73567e75d33efb970d4e4bf9a8e691582d4fa09422d09",
    "bash_scripts/setup_nl2b_fs_4.sh": "c5fb550aa1578fe2454e8ab06221165df90311231cb71d3d9b0ce036a8235274",
    "docker.gitignore": "5479a1cafa260c77e836e8601ba9a345d39df777dc9cb07d6a93f0ac29b69166",
}

# These values bind the reviewed correction layer independently of upstream.
DERIVED_SHA256 = {
    "setup/setup_nl2b_fs_1.sh": "3fe38c065ceb7d82a0105c413128d47788f4fd731f30ccc8a4a4d58200663c58",
    "setup/setup_nl2b_fs_2.sh": "29381bf8d1fade3ca86561f3e6bd129a9bbdddcf00f5e5236cc6358dd91d839f",
    "setup/setup_nl2b_fs_3.sh": "7d55db5d64d14ea8b4b72d86fa0fa68e7ed9fdeaa461fcfe8b80ff1f011d7026",
    "setup/setup_nl2b_fs_4.sh": "e155eece189f409162571aa0f300a1a7f57ea216adbe8dec36e6b73affd94858",
}
DERIVED_AUX_SHA256 = {
    "Dockerfile.agent": "1b517c32b59548974d4cdc9005326e34088094d9ebe645493d0cae3e80dc5912",
    "Dockerfile.evaluator": "103107c2d9bdc906380f6862ca0775adab6bf4de354aff1c9a6a4b3773a434fc",
    "evaluator_placeholder.py": "de4642dd71f18a3b5f1bfcb7a73f99292129aa9e73a25034a49d76269cd32cad",
}
DOCKERIGNORE_SHA256 = "effea9dab4a4907f298a1af85886ab8539a79a4b86c80f97c250aebd58952ca5"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InterCodeImageAssetTests(unittest.TestCase):
    def test_raw_upstream_assets_remain_byte_exact(self) -> None:
        for relative, expected in RAW_SHA256.items():
            with self.subTest(relative=relative):
                self.assertEqual(sha256(RAW_ROOT / relative), expected)

    def test_derived_scripts_are_hash_pinned_and_bash_syntax_valid(self) -> None:
        for relative, expected in DERIVED_SHA256.items():
            with self.subTest(relative=relative):
                script = DERIVED_ROOT / relative
                self.assertEqual(sha256(script), expected)
                result = subprocess.run(
                    ["bash", "-n", str(script)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("set -euo pipefail", script.read_text(encoding="utf-8"))

    def test_other_derived_sources_are_hash_pinned(self) -> None:
        for relative, expected in DERIVED_AUX_SHA256.items():
            with self.subTest(relative=relative):
                self.assertEqual(sha256(DERIVED_ROOT / relative), expected)

    def test_agent_and_evaluator_use_exact_arm64_child_digest(self) -> None:
        expected_from = f"FROM --platform=linux/arm64 ubuntu@{BASE_DIGEST}"
        for name in ("Dockerfile.agent", "Dockerfile.evaluator"):
            with self.subTest(name=name):
                content = (DERIVED_ROOT / name).read_text(encoding="utf-8")
                self.assertIn(expected_from, content)
                self.assertNotIn("ubuntu:latest", content)
                self.assertNotRegex(content, r"(?m)^FROM\s+ubuntu(?::[^@\s]+)?\s*$")
                self.assertIn("USER 65532:65532", content)

    def test_common_tool_layer_is_identical_and_complete(self) -> None:
        agent = (DERIVED_ROOT / "Dockerfile.agent").read_text(encoding="utf-8")
        evaluator = (DERIVED_ROOT / "Dockerfile.evaluator").read_text(
            encoding="utf-8"
        )
        marker_start = "# BEGIN SHARED INTERCODE TOOL LAYER"
        marker_end = "# END SHARED INTERCODE TOOL LAYER"

        def shared(content: str) -> str:
            return content.split(marker_start, 1)[1].split(marker_end, 1)[0]

        self.assertEqual(shared(agent), shared(evaluator))
        self.assertIn("--no-install-recommends", shared(agent))
        for package in (
            "md5deep",
            "ncompress",
            "rename",
            "g++",
            "debianutils",
            "dnsutils",
            "iputils-ping",
            "psmisc",
            "tree",
            "cpio",
            "jq",
            "ncal",
        ):
            with self.subTest(package=package):
                self.assertRegex(
                    shared(agent), rf"(?m)^\s+{re.escape(package)}\s+\\$"
                )

    def test_agent_preserves_cwd_with_narrow_unprivileged_write_access(self) -> None:
        content = (DERIVED_ROOT / "Dockerfile.agent").read_text(encoding="utf-8")
        self.assertIn("chmod 1777 /", content)
        self.assertIn("mkdir -p /home/agent /usr/workspace", content)
        self.assertIn("chown 65532:65532 /home/agent /usr/workspace", content)
        self.assertIn("/testbed /system /workspace /backup", content)
        self.assertIn('chown -R 65532:65532 "${path}"', content)
        self.assertIn("USER 65532:65532", content)
        self.assertRegex(content, r"(?m)^WORKDIR /$")
        self.assertNotIn("WORKDIR /work", content)
        self.assertNotIn("HOME=/work", content)
        self.assertNotIn("mkdir -p /work ", content)
        self.assertNotRegex(content, r"(?m)chown(?:\s+-R)?\s+65532:65532\s+/usr(?:\s|$)")

    def test_runtime_setup_scripts_have_no_network_or_package_install(self) -> None:
        forbidden = ("curl ", "wget ", "git clone", "apt-get", "apt ", "pip ")
        for script in sorted((DERIVED_ROOT / "setup").glob("*.sh")):
            lowered = script.read_text(encoding="utf-8").lower()
            with self.subTest(script=script.name):
                for token in forbidden:
                    self.assertNotIn(token, lowered)

    def test_build_context_and_image_omit_root_git_baseline(self) -> None:
        dockerignore_path = ROOT / ".dockerignore"
        dockerignore = dockerignore_path.read_text(encoding="utf-8")
        self.assertEqual(sha256(dockerignore_path), DOCKERIGNORE_SHA256)
        self.assertEqual(
            dockerignore.splitlines(),
            ["**", "!docker/", "!docker/intercode/", "!docker/intercode/**"],
        )
        for name in ("Dockerfile.agent", "Dockerfile.evaluator"):
            content = (DERIVED_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("git init", content)
            self.assertNotIn("git -C /", content)
            self.assertNotIn("COPY vendor/", content)
            self.assertNotRegex(content, r"(?m)^\s+git\s+\\$")

    def test_fs2_repairs_are_exact_and_deterministic(self) -> None:
        content = (DERIVED_ROOT / "setup/setup_nl2b_fs_2.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("echo - e", content)
        self.assertIn("backup_dbg/backup/.placeholder", content)
        self.assertNotIn("touch .placeholder", content)
        self.assertIn("202305022359.59", content)
        self.assertNotIn("20230522359.59", content)

    def test_fs3_repairs_are_exact_and_deterministic(self) -> None:
        content = (DERIVED_ROOT / "setup/setup_nl2b_fs_3.sh").read_text(
            encoding="utf-8"
        )
        commands = "\n".join(
            line for line in content.splitlines() if not line.lstrip().startswith("#")
        )
        for invalid in (
            'mkdir -p -m 755 -d "1 year ago"',
            "date -v-1d",
            "202302312359.59",
            "202304312359.59",
            "/workspace/dir1/new.sh",
        ):
            self.assertNotIn(invalid, commands)
        for correction in (
            "mkdir -p -m 755 /workspace/test/1dir",
            "202205312359.59",
            "202305302359.59",
            "202302282359.59",
            "202304302359.59",
            "/workspace/new.sh",
        ):
            self.assertIn(correction, content)

    def test_fs4_intentionally_creates_no_fixture_tree(self) -> None:
        lines = [
            line.strip()
            for line in (DERIVED_ROOT / "setup/setup_nl2b_fs_4.sh")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(lines, ["set -euo pipefail", "export file_system_version=4"])

    def test_placeholder_refuses_to_claim_an_evaluation(self) -> None:
        placeholder = DERIVED_ROOT / "evaluator_placeholder.py"
        result = subprocess.run(
            [sys.executable, str(placeholder)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 78)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "not_implemented")
        self.assertNotIn("success", payload)

    def test_provenance_document_binds_raw_and_corrected_assets(self) -> None:
        content = (
            ROOT / "docs/benchmarks/intercode-setup-corrections.md"
        ).read_text(encoding="utf-8")
        self.assertIn(INTERCODE_REVISION, content)
        self.assertIn(BASE_DIGEST, content)
        self.assertIn("derived base", content.lower())
        self.assertIn("not the original InterCode image", content)
        for digest in (
            *RAW_SHA256.values(),
            *DERIVED_SHA256.values(),
            *DERIVED_AUX_SHA256.values(),
            DOCKERIGNORE_SHA256,
        ):
            with self.subTest(digest=digest):
                self.assertIn(digest, content)


if __name__ == "__main__":
    unittest.main()
