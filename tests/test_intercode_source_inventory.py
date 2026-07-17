from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from edgeloopbench.intercode_source_inventory import (
    SOURCE_INVENTORY_SCHEMA_REVISION,
    SourceInventoryError,
    VerifiedSourceInventory,
    build_verified_source_inventory,
    derive_source_subset_sha256,
    revalidate_source_inventory,
)


def _git(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), *arguments],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))
    return completed.stdout


def _repository(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "inventory@example.invalid")
    _git(root, "config", "user.name", "Inventory Test")
    (root / "src").mkdir()
    (root / "src" / "controller.py").write_bytes(b"CONTROLLER = 1\n")
    (root / "README.md").write_bytes(b"# inventory\n")
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", "fixture")
    return root


class InterCodeSourceInventoryTests(unittest.TestCase):
    def test_clean_head_builds_builder_sealed_path_free_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _repository(Path(directory) / "checkout")

            inventory = build_verified_source_inventory(root)
            record = inventory.canonical_record()

            self.assertIs(type(inventory), VerifiedSourceInventory)
            self.assertEqual(record["schema"], SOURCE_INVENTORY_SCHEMA_REVISION)
            self.assertEqual(record["git_object_format"], "sha1")
            self.assertEqual(record["tracked_file_count"], 2)
            self.assertEqual(
                record["tracked_byte_count"],
                len(b"CONTROLLER = 1\n") + len(b"# inventory\n"),
            )
            self.assertRegex(record["head_commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(record["head_tree"], r"^[0-9a-f]{40}$")
            self.assertRegex(record["inventory_sha256"], r"^sha256:[0-9a-f]{64}$")
            rendered = json.dumps(record, sort_keys=True)
            self.assertNotIn(os.fspath(root), rendered)
            self.assertNotIn("controller.py", rendered)
            self.assertNotIn("README.md", rendered)
            self.assertNotIn(os.fspath(root), repr(inventory))
            self.assertNotIn("controller.py", repr(inventory))

            with self.assertRaisesRegex(SourceInventoryError, "builder-sealed"):
                VerifiedSourceInventory(  # type: ignore[call-arg]
                    git_object_format="sha1",
                    head_commit="0" * 40,
                    head_tree="0" * 40,
                    tracked_file_count=0,
                    tracked_byte_count=0,
                    inventory_sha256="sha256:" + "0" * 64,
                )

    def test_subset_hash_is_stable_ordered_and_requires_tracked_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _repository(Path(directory) / "checkout")
            inventory = build_verified_source_inventory(root)

            controller = derive_source_subset_sha256(
                inventory, ("src/controller.py",)
            )
            both = derive_source_subset_sha256(
                inventory, ("README.md", "src/controller.py")
            )

            self.assertRegex(controller, r"^sha256:[0-9a-f]{64}$")
            self.assertNotEqual(controller, both)
            self.assertEqual(
                both,
                derive_source_subset_sha256(
                    inventory, ("README.md", "src/controller.py")
                ),
            )
            with self.assertRaisesRegex(SourceInventoryError, "tracked file"):
                derive_source_subset_sha256(inventory, ("missing.py",))
            with self.assertRaisesRegex(SourceInventoryError, "duplicate"):
                derive_source_subset_sha256(
                    inventory, ("src/controller.py", "src/controller.py")
                )
            for invalid in ("/absolute.py", "../escape.py", "src/../README.md", ""):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(SourceInventoryError):
                        derive_source_subset_sha256(inventory, (invalid,))

    def test_dirty_staged_or_untracked_checkout_is_rejected(self) -> None:
        mutations = {
            "modified": lambda root: (root / "README.md").write_bytes(b"changed\n"),
            "staged": lambda root: (
                (root / "README.md").write_bytes(b"staged\n"),
                _git(root, "add", "README.md"),
            ),
            "untracked": lambda root: (root / "scratch.txt").write_bytes(b"scratch\n"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = _repository(Path(directory) / "checkout")
                mutate(root)
                with self.assertRaisesRegex(SourceInventoryError, "clean committed HEAD"):
                    build_verified_source_inventory(root)

    def test_tracked_symlink_and_non_repository_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            with self.assertRaisesRegex(SourceInventoryError, "Git repository"):
                build_verified_source_inventory(root)

        if not hasattr(os, "symlink"):
            self.skipTest("platform has no symlink support")
        with tempfile.TemporaryDirectory() as directory:
            root = _repository(Path(directory) / "checkout")
            link = root / "tracked-link"
            link.symlink_to("README.md")
            _git(root, "add", "tracked-link")
            _git(root, "commit", "--quiet", "-m", "track symlink")

            with self.assertRaisesRegex(SourceInventoryError, "regular non-symlink"):
                build_verified_source_inventory(root)

    def test_revalidation_requires_the_same_clean_head_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _repository(Path(directory) / "checkout")
            inventory = build_verified_source_inventory(root)

            self.assertIs(revalidate_source_inventory(inventory, root), inventory)

            (root / "src" / "controller.py").write_bytes(b"CONTROLLER = 2\n")
            with self.assertRaisesRegex(SourceInventoryError, "revalidation"):
                revalidate_source_inventory(inventory, root)
            _git(root, "restore", "src/controller.py")
            (root / "new.py").write_bytes(b"NEW = True\n")
            _git(root, "add", "new.py")
            _git(root, "commit", "--quiet", "-m", "new clean head")
            with self.assertRaisesRegex(SourceInventoryError, "revalidation"):
                revalidate_source_inventory(inventory, root)


if __name__ == "__main__":
    unittest.main()
