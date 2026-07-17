from __future__ import annotations

import dataclasses
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.intercode_image_build import (
    InterCodeImageBuildError,
    create_intercode_image_build_plan,
    execute_intercode_image_build,
    verify_intercode_image_build_result,
)
from edgeloopbench.intercode_source_inventory import (
    build_verified_source_inventory,
)
from edgeloopbench.intercode_v07_image_provenance import (
    V07_STATE_NORMALIZATION_SOURCES,
    VerifiedV07ImageSet,
    verify_v07_image_set,
)
from tests.test_intercode_image_build import (
    FakeDockerRunner,
    PlanFixture,
)


ROOT = Path(__file__).resolve().parents[1]


def git(root: Path, *arguments: str) -> None:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", "replace"))


def commit_normalizer_sources(repo: Path) -> None:
    for relative in V07_STATE_NORMALIZATION_SOURCES:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    git(repo, "init", "--quiet")
    git(repo, "config", "user.email", "image-provenance@example.invalid")
    git(repo, "config", "user.name", "Image Provenance Test")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "fixture")


class InterCodeV07ImageProvenanceTests(unittest.TestCase):
    def build_fixture(
        self,
        root: Path,
    ) -> tuple[
        PlanFixture,
        object,
        Path,
        object,
        FakeDockerRunner,
    ]:
        fixture = PlanFixture(root)
        commit_normalizer_sources(fixture.repo)
        inventory = build_verified_source_inventory(fixture.repo)
        plan = create_intercode_image_build_plan(fixture.request())
        collector, policy, _telemetry = fixture.admission()
        docker = FakeDockerRunner()
        manifest = root / "private/images.jsonl"
        result = execute_intercode_image_build(
            plan,
            manifest_path=manifest,
            collector=collector,
            policy=policy,
            runner=docker,
            environment={},
        )
        return fixture, inventory, manifest, result, docker

    def test_reopens_complete_build_and_seals_exact_v07_image_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, inventory, manifest, result, docker = self.build_fixture(root)
            plan = create_intercode_image_build_plan(fixture.request())

            verified_build = verify_intercode_image_build_result(
                plan,
                manifest_path=manifest,
                result=result,
                runner=docker,
                environment={},
            )
            image_set = verify_v07_image_set(
                source_inventory=inventory,
                repository_root=fixture.repo,
                verified_build=verified_build,
            )

        self.assertIs(type(image_set), VerifiedV07ImageSet)
        self.assertEqual(image_set.build_plan_sha256, result.plan_sha256)
        self.assertEqual(image_set.build_manifest_sha256, result.manifest_sha256)
        self.assertEqual(
            tuple(image_set.image_id_by_stratum),
            ("fs1", "fs2", "fs3", "fs4"),
        )
        self.assertEqual(
            tuple(image_set.image_id_by_stratum.values()),
            result.image_ids,
        )
        self.assertEqual(
            image_set.source_inventory_sha256,
            inventory.inventory_sha256,
        )
        self.assertRegex(
            image_set.state_normalization_source_sha256,
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertRegex(
            image_set.state_normalization_sha256,
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertRegex(image_set.image_set_sha256, r"^sha256:[0-9a-f]{64}$")
        image_set.require_admitted()

        with self.assertRaisesRegex(ValueError, "verifier-sealed"):
            dataclasses.replace(
                image_set,
                state_normalization_sha256="sha256:" + "0" * 64,
            )

    def test_cross_build_result_and_manifest_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, _inventory, manifest, result, docker = self.build_fixture(root)
            plan = create_intercode_image_build_plan(fixture.request())

            wrong = dataclasses.replace(
                result,
                plan_sha256="sha256:" + "0" * 64,
            )
            with self.assertRaisesRegex(InterCodeImageBuildError, "result"):
                verify_intercode_image_build_result(
                    plan,
                    manifest_path=manifest,
                    result=wrong,
                    runner=docker,
                    environment={},
                )

            payload = manifest.read_bytes()
            manifest.write_bytes(payload.replace(b'"profile":"fs1"', b'"profile":"fs4"', 1))
            with self.assertRaises(InterCodeImageBuildError):
                verify_intercode_image_build_result(
                    plan,
                    manifest_path=manifest,
                    result=result,
                    runner=docker,
                    environment={},
                )

    def test_normalizer_is_derived_from_revalidated_clean_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, inventory, manifest, result, docker = self.build_fixture(root)
            plan = create_intercode_image_build_plan(fixture.request())
            verified_build = verify_intercode_image_build_result(
                plan,
                manifest_path=manifest,
                result=result,
                runner=docker,
                environment={},
            )
            normalizer = fixture.repo / V07_STATE_NORMALIZATION_SOURCES[0]
            normalizer.write_bytes(normalizer.read_bytes() + b"\n# drift\n")

            with self.assertRaisesRegex(ValueError, "source inventory"):
                verify_v07_image_set(
                    source_inventory=inventory,
                    repository_root=fixture.repo,
                    verified_build=verified_build,
                )


if __name__ == "__main__":
    unittest.main()
