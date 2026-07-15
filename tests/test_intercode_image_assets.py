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
    "Dockerfile.agent": "6c2b440dc7ebe277355fb21664de2a94eb0644f86698c92bcb836b88667a214f",
    "Dockerfile.evaluator": "318fc5e51345036ada580f2552ae8fed61d37d31c9853eddcd3a893fd9c22ffa",
    "evaluator_placeholder.py": "de4642dd71f18a3b5f1bfcb7a73f99292129aa9e73a25034a49d76269cd32cad",
    "state_collector.py": "513a0261fad1e52ce77479afd1c3196921ce558cc80e83632b68795e5639bba0",
}
DOCKERIGNORE_SHA256 = "41f598c8c3bb3868c615a3e59c23b215a4ce3754c2127538427f43b5a3653983"


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
            "git",
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

    def test_agent_includes_source_pinned_root_owned_state_collector(self) -> None:
        content = (DERIVED_ROOT / "Dockerfile.agent").read_text(encoding="utf-8")
        helper_sha256 = DERIVED_AUX_SHA256["state_collector.py"]
        policy_sha256 = (
            "sha256:70eeeda4091cb2da38aa8024af7c52dbacb464cf5b20a9f6bfdac5d66ecb67a9"
        )
        root_baseline_sha256 = (
            "sha256:06dcf54e33c9412b1c0bb2cf7ddab33848169e640012209b9d05c81ee1da457f"
        )
        profile_set_sha256 = (
            "sha256:1c515db46e794a58c457ac5d906ad80cae2ecb696ce2f07932733087368b1990"
        )
        fixed_argv = (
            "/usr/bin/python3 -I -S -B "
            "/opt/edgeloop/state_collector.py --profile fsN"
        )

        self.assertIn(
            "COPY docker/intercode/state_collector.py "
            "/opt/edgeloop/state_collector.py",
            content,
        )
        self.assertIn("chown 0:0 /opt/edgeloop/state_collector.py", content)
        self.assertIn("chmod 0555 /opt/edgeloop/state_collector.py", content)
        self.assertIn(
            f'org.edgeloopbench.state-collector.sha256="sha256:{helper_sha256}"',
            content,
        )
        self.assertIn(
            f'org.edgeloopbench.state-collector.argv="{fixed_argv}"', content
        )
        self.assertIn(
            f'org.edgeloopbench.state-collector.policy-sha256="{policy_sha256}"',
            content,
        )
        self.assertIn(
            "org.edgeloopbench.state-collector.root-baseline-sha256="
            f'"{root_baseline_sha256}"',
            content,
        )
        self.assertIn(
            "org.edgeloopbench.state-collector.profile-set-sha256="
            f'"{profile_set_sha256}"',
            content,
        )
        self.assertIn(
            'org.edgeloopbench.state-collector.profile="fs${FILE_SYSTEM_VERSION}"',
            content,
        )
        self.assertIn(
            'org.edgeloopbench.filesystem-version="${FILE_SYSTEM_VERSION}"',
            content,
        )
        self.assertNotRegex(
            content,
            r"(?m)^\s*(?:ENTRYPOINT|CMD)\s+.*state_collector",
        )

    def test_runtime_setup_scripts_have_no_network_or_package_install(self) -> None:
        forbidden = ("curl ", "wget ", "git clone", "apt-get", "apt ", "pip ")
        for script in sorted((DERIVED_ROOT / "setup").glob("*.sh")):
            lowered = script.read_text(encoding="utf-8").lower()
            with self.subTest(script=script.name):
                for token in forbidden:
                    self.assertNotIn(token, lowered)

    def test_build_context_and_agent_preserve_read_only_upstream_git_baseline(self) -> None:
        dockerignore_path = ROOT / ".dockerignore"
        dockerignore = dockerignore_path.read_text(encoding="utf-8")
        self.assertEqual(
            dockerignore.splitlines(),
            [
                "**",
                "!docker/",
                "!docker/intercode/",
                "!docker/intercode/**",
                "!vendor/",
                "!vendor/intercode/",
                f"!vendor/intercode/{INTERCODE_REVISION}/",
                f"!vendor/intercode/{INTERCODE_REVISION}/docker/",
                (
                    f"!vendor/intercode/{INTERCODE_REVISION}/docker/"
                    "docker.gitignore"
                ),
            ],
        )
        self.assertEqual(sha256(dockerignore_path), DOCKERIGNORE_SHA256)

        content = (DERIVED_ROOT / "Dockerfile.agent").read_text(encoding="utf-8")
        source = (
            f"vendor/intercode/{INTERCODE_REVISION}/docker/docker.gitignore"
        )
        self.assertIn(f"COPY --chown=0:0 --chmod=0444 {source} /.gitignore", content)
        self.assertIn("git init --quiet /", content)
        self.assertIn("git -C / add -A", content)
        self.assertIn("GIT_AUTHOR_DATE='@1685577598 +0000'", content)
        self.assertIn("GIT_COMMITTER_DATE='@1685577598 +0000'", content)
        self.assertIn("rm -f /.git/index", content)
        self.assertIn("git -C / read-tree HEAD", content)
        self.assertIn(
            'find /.git -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +',
            content,
        )
        self.assertIn('touch -d "@${SOURCE_DATE_EPOCH}" /.gitignore', content)
        self.assertIn("chmod -R a-w /.git", content)
        self.assertIn("ENV GIT_OPTIONAL_LOCKS=0", content)
        self.assertLess(content.index("commit --quiet"), content.index("rm -f /.git/index"))
        self.assertLess(content.index("git -C / read-tree HEAD"), content.index("chmod -R a-w /.git"))
        self.assertLess(
            content.index("git init --quiet /"),
            content.index("--build-audit"),
        )
        for forbidden in (
            "gold_command",
            "gold.json",
            "/opt/edgeloop/evaluator",
            "evaluator_placeholder",
            "intercode/data/",
        ):
            self.assertNotIn(forbidden, content.lower())

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
