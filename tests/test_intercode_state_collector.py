from __future__ import annotations

import base64
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "docker" / "intercode" / "state_collector.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "edgeloopbench_test_state_collector", HELPER
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError("state collector could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_helper()


def _payload(root: Path, profile: str = "fs4", *, limits=None) -> dict[str, object]:
    encoded = collector.collect_canonical_bytes(
        profile,
        _root_prefix=os.fsencode(root),
        _limits=limits,
        _acl_probe=lambda _descriptor: (),
    )
    return json.loads(encoded)


def _entry_by_path(payload: dict[str, object], path: str) -> dict[str, object]:
    entries = payload["entries"]
    assert isinstance(entries, list)
    return next(entry for entry in entries if entry["path"] == path)


def _build_audit_bytes(
    root: Path,
    profile: str = "fs4",
    *,
    acl_probe=lambda _descriptor: (),
) -> bytes:
    # TemporaryDirectory roots are owned by the host test user. Root identity
    # is covered independently by the pure validator below; this wrapper keeps
    # the remaining descriptor-relative audit integration real.
    with mock.patch.object(collector, "_validate_audit_root", return_value=None):
        return collector.build_writable_surface_audit_bytes(
            profile,
            _root_prefix=os.fsencode(root),
            _acl_probe=acl_probe,
        )


def _touch_common_roots(
    root: Path,
    profile: str = "fs4",
    *,
    write_audit: bool = True,
) -> None:
    root.chmod(0o1777)
    for name in collector.IMMUTABLE_ROOT_NAMES:
        path = root / name
        if name in {".dockerenv", ".gitignore"}:
            path.touch()
        else:
            path.mkdir()
    for relative in collector.COMMON_WRITABLE_ROOTS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in collector.EPHEMERAL_EMPTY_ROOTS:
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in ("tmp", "var/tmp", "run/lock"):
        (root / relative).chmod(0o1777)
    var_lock = root / "var/lock"
    if not var_lock.exists():
        os.symlink("../run/lock", var_lock)
    for relative, header in collector.SYSVIPC_HEADERS.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(" ".join(header) + "\n", encoding="ascii")
    (root / "opt/edgeloop").mkdir(parents=True, exist_ok=True)
    if write_audit:
        with mock.patch.object(
            collector, "_validate_audit_root", return_value=None
        ):
            collector.write_build_writable_surface_audit(
                profile,
                _root_prefix=os.fsencode(root),
                _acl_probe=lambda _path: (),
            )


class InterCodeStateCollectorTests(unittest.TestCase):
    def test_policy_and_profile_roots_are_explicit_and_digest_bound(self) -> None:
        self.assertEqual(
            {
                "fs1": ("testbed",),
                "fs2": ("system",),
                "fs3": ("workspace", "backup"),
                "fs4": (),
            },
            dict(collector.PROFILE_TASK_ROOTS),
        )
        self.assertEqual(
            ("home/agent", "usr/workspace", "tmp", "var/tmp", "run/lock"),
            collector.COMMON_WRITABLE_ROOTS,
        )
        self.assertEqual(
            ("dev/shm", "dev/mqueue"), collector.EPHEMERAL_EMPTY_ROOTS
        )
        self.assertEqual(4096, collector.DEFAULT_LIMITS.max_entries)
        self.assertEqual(32, collector.DEFAULT_LIMITS.max_depth)
        self.assertEqual(
            "sha256:06dcf54e33c9412b1c0bb2cf7ddab33848169e640012209b9d05c81ee1da457f",
            collector.ROOT_BASELINE_SHA256,
        )
        self.assertEqual(
            "sha256:70eeeda4091cb2da38aa8024af7c52dbacb464cf5b20a9f6bfdac5d66ecb67a9",
            collector.POLICY_SHA256,
        )
        self.assertEqual(
            {
                "fs1": "sha256:55084cb572f3275a57a8932f11a18f1f606c092c7c9cf1a8baf75742c2232750",
                "fs2": "sha256:d1c607dea4dd49fb639ab47c2e459a01471a6a002d6bc69fef4913cf9b014dbe",
                "fs3": "sha256:9b0e2bac7388fa417f801b01bb21b69eda21b05a198b50bef4afd01432858b6c",
                "fs4": "sha256:0adbd497e28b279f6657405942253c9274e5cd68c8c4102709630dfb1b429e6e",
            },
            dict(collector.PROFILE_SHA256),
        )
        self.assertEqual(
            "sha256:1c515db46e794a58c457ac5d906ad80cae2ecb696ce2f07932733087368b1990",
            collector.PROFILE_SET_SHA256,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root, "fs3")
            canonical = collector.collect_canonical_bytes(
                "fs3",
                _root_prefix=os.fsencode(root),
                _acl_probe=lambda _descriptor: (),
            )
            payload = json.loads(canonical)

        self.assertEqual("edgeloopbench.filesystem-state.v1", payload["schema"])
        self.assertEqual("fs3", payload["profile"])
        self.assertEqual(collector.POLICY_SHA256, payload["policy_sha256"])
        self.assertEqual(
            collector.ROOT_BASELINE_SHA256, payload["root_baseline_sha256"]
        )
        self.assertEqual(
            collector.PROFILE_SHA256["fs3"], payload["profile_sha256"]
        )
        self.assertEqual(["workspace", "backup"], payload["task_roots"])
        self.assertEqual(list(collector.COMMON_WRITABLE_ROOTS), payload["common_roots"])
        self.assertEqual(
            "non_baseline_top_level", payload["dynamic_root_policy"]
        )
        self.assertEqual(collector._canonical_json(payload), canonical)

        serialized = json.dumps(payload, sort_keys=True).lower()
        for forbidden in (
            "hostname",
            "host_path",
            "container_id",
            "container_name",
            "evaluator",
            "gold",
            "inode",
            "device_id",
            "mtime",
            "ctime",
            "atime",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_creation_order_inode_and_timestamps_do_not_change_state(self) -> None:
        payloads = []
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            for index, directory in enumerate((Path(first), Path(second))):
                _touch_common_roots(directory)
                work = directory / "work"
                work.mkdir()
                names = ("b.txt", "a.txt") if index == 0 else ("a.txt", "b.txt")
                for name in names:
                    (work / name).write_bytes(name.encode("ascii"))
                os.link(work / "a.txt", work / "a-link.txt")
                timestamp = 1_000_000_000 + index * 500_000_000
                for path in (work, *work.iterdir()):
                    os.utime(path, ns=(timestamp, timestamp), follow_symlinks=False)
                payloads.append(_payload(directory))

        self.assertEqual(payloads[0]["state_sha256"], payloads[1]["state_sha256"])
        self.assertEqual(payloads[0]["entries"], payloads[1]["entries"])
        paths = [entry["path_bytes_b64"] for entry in payloads[0]["entries"]]
        raw_paths = [base64.b64decode(path) for path in paths]
        self.assertEqual(sorted(raw_paths), raw_paths)

    def test_every_relevant_filesystem_mutation_changes_the_digest(self) -> None:
        def measure(mutator) -> tuple[str, dict[str, object]]:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _touch_common_roots(root)
                work = root / "work"
                work.mkdir()
                (work / "keep.txt").write_text("base", encoding="utf-8")
                (work / "delete.txt").write_text("delete", encoding="utf-8")
                os.symlink("keep.txt", work / "base-link")
                mutator(root, work)
                payload = _payload(root)
                return payload["state_sha256"], payload

        baseline, _ = measure(lambda _root, _work: None)
        mutations = {
            "add": lambda _root, work: (work / "added.txt").write_text(
                "added", encoding="utf-8"
            ),
            "modify": lambda _root, work: (work / "keep.txt").write_text(
                "case", encoding="utf-8"
            ),
            "delete": lambda _root, work: (work / "delete.txt").unlink(),
            "chmod": lambda _root, work: os.chmod(work / "keep.txt", 0o755),
            "symlink": lambda _root, work: os.symlink("keep.txt", work / "link"),
            "symlink_target": lambda _root, work: (
                (work / "base-link").unlink(),
                os.symlink("gone.txt", work / "base-link"),
            ),
            "hardlink": lambda _root, work: os.link(
                work / "keep.txt", work / "hardlink"
            ),
            "type": lambda _root, work: (
                (work / "delete.txt").unlink(),
                (work / "delete.txt").mkdir(),
            ),
            "rename": lambda _root, work: (work / "keep.txt").rename(
                work / "renamed.txt"
            ),
        }
        for name, mutator in mutations.items():
            with self.subTest(name=name):
                digest, _ = measure(mutator)
                self.assertNotEqual(baseline, digest)

        _, hardlinked = measure(mutations["hardlink"])
        first = _entry_by_path(hardlinked, "work/keep.txt")
        second = _entry_by_path(hardlinked, "work/hardlink")
        self.assertRegex(first["hardlink_group_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            first["hardlink_group_sha256"], second["hardlink_group_sha256"]
        )

    def test_symlinks_are_hashed_as_links_and_never_followed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as outside,
        ):
            root = Path(temporary)
            _touch_common_roots(root)
            secret = Path(outside) / "secret"
            secret.write_bytes(b"outside-content")
            os.symlink(os.fspath(secret), root / "outside-link")
            os.symlink("loop", root / "loop")

            first = _payload(root)
            secret.write_bytes(b"changed-outside-content")
            second = _payload(root)

        self.assertEqual(first["state_sha256"], second["state_sha256"])
        link = _entry_by_path(first, "outside-link")
        self.assertEqual("symlink", link["type"])
        self.assertIsNone(link["content_sha256"])
        self.assertEqual(os.fspath(secret), link["symlink_target"])

    def test_symlink_hardlink_groups_are_complete_and_path_derived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            os.symlink("target", root / "link-a")
            os.link(
                root / "link-a",
                root / "link-b",
                follow_symlinks=False,
            )
            payload = _payload(root)

        first = _entry_by_path(payload, "link-a")
        second = _entry_by_path(payload, "link-b")
        self.assertEqual("symlink", first["type"])
        self.assertEqual("symlink", second["type"])
        self.assertRegex(
            first["hardlink_group_sha256"], r"^sha256:[0-9a-f]{64}$"
        )
        self.assertEqual(
            first["hardlink_group_sha256"], second["hardlink_group_sha256"]
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            os.symlink("target", root / "visible-link")
            os.link(
                root / "visible-link",
                root / "etc" / "omitted-link",
                follow_symlinks=False,
            )
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
        self.assertEqual(
            collector.CollectionFailure.INCOMPLETE_HARDLINK,
            caught.exception.kind,
        )

    def test_file_metadata_race_between_lstat_and_open_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            victim = root / "victim"
            victim.write_bytes(b"same-size")
            victim.chmod(0o600)
            real_open = collector.os.open
            raced = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal raced
                if path == "victim" and not raced:
                    raced = True
                    victim.chmod(0o755)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(collector.os, "open", side_effect=racing_open):
                with self.assertRaises(collector.StateCollectionError) as caught:
                    _payload(root)

        self.assertTrue(raced)
        self.assertEqual(
            collector.CollectionFailure.RACE_DETECTED, caught.exception.kind
        )

    def test_invalid_utf8_record_has_a_runtime_digest_without_raw_json_bytes(self) -> None:
        instance = collector._StateCollector("fs4", b"/", collector.DEFAULT_LIMITS)
        record = instance._base_record(
            b"bad-\xff", kind="missing", metadata=None
        )
        profile_sha256 = collector.PROFILE_SHA256["fs4"]
        state_sha256 = collector._sha256_record(
            {
                "entries": [record],
                "profile_sha256": profile_sha256,
                "schema": collector.SCHEMA,
            }
        )
        canonical = collector._canonical_json(record)

        self.assertRegex(state_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertIsNone(record["path"])
        self.assertEqual(
            {collector.StrictSurfaceFailure.INVALID_UTF8_PATH},
            instance.strict_failures,
        )
        self.assertNotIn(b"\xff", canonical)

    def test_uid_and_gid_are_digest_inputs(self) -> None:
        instance = collector._StateCollector("fs4", b"/", collector.DEFAULT_LIMITS)

        def digest(uid: int, gid: int) -> str:
            metadata = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o640,
                st_uid=uid,
                st_gid=gid,
            )
            record = instance._base_record(
                b"file",
                kind="file",
                metadata=metadata,
                content_sha256="sha256:" + "0" * 64,
                size_bytes=1,
            )
            return collector._sha256_record(record)

        baseline = digest(65532, 65532)
        self.assertNotEqual(baseline, digest(65531, 65532))
        self.assertNotEqual(baseline, digest(65532, 65531))

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "APFS rejects raw invalid UTF-8 names"
    )
    def test_invalid_utf8_still_has_runtime_digest_and_typed_surface_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            raw_root = os.fsencode(root)
            invalid_path = os.path.join(raw_root, b"bad-\xff")
            descriptor = os.open(invalid_path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(descriptor, b"content")
            os.close(descriptor)
            os.symlink(b"target-\xfe", os.path.join(raw_root, b"bad-link"))
            canonical = collector.collect_canonical_bytes(
                "fs4",
                _root_prefix=raw_root,
                _acl_probe=lambda _descriptor: (),
            )
            payload = json.loads(canonical)

        self.assertRegex(payload["state_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual("unrepresentable", payload["strict_surface"]["status"])
        self.assertEqual(
            ["invalid_utf8_path", "invalid_utf8_symlink_target"],
            payload["strict_surface"]["failures"],
        )
        invalid = next(
            entry
            for entry in payload["entries"]
            if entry["path_bytes_b64"]
            == base64.b64encode(b"bad-\xff").decode("ascii")
        )
        self.assertIsNone(invalid["path"])
        self.assertNotIn(b"\xff", canonical)

    def test_fifo_socket_and_device_types_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            os.mkfifo(root / "fifo")
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
            self.assertEqual(
                collector.CollectionFailure.SPECIAL_FILE, caught.exception.kind
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.bind(os.fspath(root / "socket"))
                with self.assertRaises(collector.StateCollectionError) as caught:
                    _payload(root)
                self.assertEqual(
                    collector.CollectionFailure.SPECIAL_FILE, caught.exception.kind
                )
            finally:
                sock.close()

        for special_mode in (stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(special_mode=special_mode):
                with self.assertRaises(collector.StateCollectionError) as caught:
                    collector._classify_mode(special_mode | 0o600)
                self.assertEqual(
                    collector.CollectionFailure.SPECIAL_FILE, caught.exception.kind
                )

    def test_sparse_per_file_and_total_byte_bombs_are_rejected_before_reading(
        self,
    ) -> None:
        limits = collector.CollectionLimits(
            max_entries=64,
            max_depth=8,
            max_file_bytes=1024,
            max_total_file_bytes=1500,
            max_output_bytes=64 * 1024,
            max_path_bytes=1024,
            max_symlink_target_bytes=1024,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            with (root / "sparse").open("wb") as stream:
                stream.truncate(1025)
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root, limits=limits)
            self.assertEqual(
                collector.CollectionFailure.FILE_BYTES_LIMIT, caught.exception.kind
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            (root / "first").write_bytes(b"a" * 800)
            (root / "second").write_bytes(b"b" * 800)
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root, limits=limits)
            self.assertEqual(
                collector.CollectionFailure.TOTAL_BYTES_LIMIT, caught.exception.kind
            )

    def test_4097th_entry_and_excessive_depth_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            flood = root / "flood"
            flood.mkdir()
            # Five common roots, flood, and 4090 files produce exactly 4096
            # canonical entries.  The next file is fatal.
            for index in range(4090):
                (flood / f"f{index:04d}").touch()
            _payload(root)
            (flood / "f4090").touch()
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
            self.assertEqual(
                collector.CollectionFailure.ENTRY_LIMIT, caught.exception.kind
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            current = root / "deep"
            current.mkdir()
            for _ in range(collector.DEFAULT_LIMITS.max_depth):
                current /= "d"
                current.mkdir()
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
            self.assertEqual(
                collector.CollectionFailure.DEPTH_LIMIT, caught.exception.kind
            )

    def test_incomplete_hardlink_group_and_ephemeral_mount_state_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            visible = root / "visible"
            visible.write_bytes(b"linked")
            os.link(visible, root / "etc" / "omitted-link")
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
            self.assertEqual(
                collector.CollectionFailure.INCOMPLETE_HARDLINK, caught.exception.kind
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            (root / "dev/shm/leaked").write_bytes(b"not commit-preserved")
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
            self.assertEqual(
                collector.CollectionFailure.EPHEMERAL_STATE, caught.exception.kind
            )

        for relative in collector.SYSVIPC_HEADERS:
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    _touch_common_roots(root)
                    with (root / relative).open("a", encoding="ascii") as stream:
                        stream.write("persisted ipc object\n")
                    with self.assertRaises(
                        collector.StateCollectionError
                    ) as caught:
                        _payload(root)
                self.assertEqual(
                    collector.CollectionFailure.EPHEMERAL_STATE,
                    caught.exception.kind,
                )

    def test_ephemeral_inspection_paths_are_required_regular_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            (root / "dev/mqueue").rmdir()
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
            self.assertEqual(
                collector.CollectionFailure.ROOT_BOUNDARY,
                caught.exception.kind,
            )

        relative = next(iter(collector.SYSVIPC_HEADERS))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            target = root / relative
            header_copy = root / "header-copy"
            header_copy.write_bytes(target.read_bytes())
            target.unlink()
            os.symlink(os.fspath(header_copy), target)
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
            self.assertEqual(
                collector.CollectionFailure.ROOT_BOUNDARY,
                caught.exception.kind,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            target = root / relative
            with target.open("ab") as stream:
                stream.write(b"x" * (collector._MAX_EPHEMERAL_TABLE_BYTES + 1))
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
            self.assertEqual(
                collector.CollectionFailure.EPHEMERAL_STATE,
                caught.exception.kind,
            )

    def test_missing_pinned_root_baseline_name_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            (root / "etc").rmdir()
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
        self.assertEqual(
            collector.CollectionFailure.ROOT_BOUNDARY, caught.exception.kind
        )

    def test_build_audit_binds_exact_writable_surface_without_file_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root, write_audit=False)
            secret = root / ".git/objects/private-object"
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text("must-not-enter-audit", encoding="utf-8")
            encoded = _build_audit_bytes(root)
            payload = json.loads(encoded)

        self.assertEqual(collector.AUDIT_SCHEMA, payload["schema"])
        self.assertEqual("reject_all", payload["captured_xattr_policy"])
        self.assertEqual(collector.POLICY_SHA256, payload["policy_sha256"])
        self.assertEqual(collector.PROFILE_SHA256["fs4"], payload["profile_sha256"])
        self.assertEqual(
            collector.PROFILE_SET_SHA256, payload["profile_set_sha256"]
        )
        self.assertEqual("run/lock", payload["var_lock_resolved_target"])
        self.assertEqual(list(collector.PSEUDO_MOUNT_ROOTS), payload["pseudo_mount_roots"])
        self.assertGreater(payload["scanned_entry_count"], 0)
        self.assertGreater(payload["acl_probed_entry_count"], 0)
        self.assertRegex(payload["audit_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(collector._canonical_json(payload), encoded)
        self.assertNotIn(b"must-not-enter-audit", encoded)
        self.assertNotIn(b"private-object", encoded)

    def test_build_audit_rejects_uncovered_writes_acl_and_var_lock_drift(self) -> None:
        for metadata in (
            SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o777),
            SimpleNamespace(st_uid=1234, st_mode=stat.S_IFDIR | 0o1777),
        ):
            with self.subTest(uid=metadata.st_uid, mode=metadata.st_mode):
                with self.assertRaises(collector.StateCollectionError) as caught:
                    collector._validate_audit_root(metadata)
                self.assertEqual(
                    collector.CollectionFailure.WRITABLE_SURFACE,
                    caught.exception.kind,
                )

        collector._validate_audit_root(
            SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o1777)
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root, write_audit=False)
            (root / ".git").chmod(0o777)
            with self.assertRaises(collector.StateCollectionError) as caught:
                _build_audit_bytes(root)
            self.assertEqual(
                collector.CollectionFailure.WRITABLE_SURFACE,
                caught.exception.kind,
            )

        metadata = SimpleNamespace(
            st_uid=0,
            st_gid=collector.AGENT_GID,
            st_mode=stat.S_IFDIR | stat.S_ISGID | 0o770,
        )
        self.assertTrue(collector._agent_controls(metadata, "directory"))
        owned_symlink = SimpleNamespace(
            st_uid=collector.AGENT_UID,
            st_gid=0,
            st_mode=stat.S_IFLNK | 0o777,
        )
        root_owned_symlink = SimpleNamespace(
            st_uid=0,
            st_gid=0,
            st_mode=stat.S_IFLNK | 0o777,
        )
        self.assertTrue(collector._agent_controls(owned_symlink, "symlink"))
        self.assertFalse(
            collector._agent_controls(root_owned_symlink, "symlink")
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root, write_audit=False)
            with self.assertRaises(collector.StateCollectionError) as caught:
                _build_audit_bytes(
                    root,
                    acl_probe=lambda _descriptor: ("system.posix_acl_access",),
                )
            self.assertEqual(
                collector.CollectionFailure.ACL_PRESENT, caught.exception.kind
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root, write_audit=False)
            with self.assertRaises(collector.StateCollectionError) as caught:
                _build_audit_bytes(
                    root,
                    acl_probe=lambda _descriptor: ("user.initial-state",),
                )
            self.assertEqual(
                collector.CollectionFailure.XATTR_PRESENT,
                caught.exception.kind,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root, write_audit=False)
            captured = root / "model-output"
            captured.write_text("state", encoding="utf-8")
            captured_metadata = captured.stat()

            def captured_xattr(descriptor: int):
                metadata = os.fstat(descriptor)
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                ) == (
                    captured_metadata.st_dev,
                    captured_metadata.st_ino,
                ):
                    return ("user.initial-state",)
                return ()

            with self.assertRaises(collector.StateCollectionError) as caught:
                _build_audit_bytes(root, acl_probe=captured_xattr)
            self.assertEqual(
                collector.CollectionFailure.XATTR_PRESENT,
                caught.exception.kind,
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root, write_audit=False)
            immutable = root / ".git/immutable-metadata"
            immutable.write_text("state", encoding="utf-8")
            immutable_metadata = immutable.stat()

            def immutable_xattr(descriptor: int):
                metadata = os.fstat(descriptor)
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                ) == (
                    immutable_metadata.st_dev,
                    immutable_metadata.st_ino,
                ):
                    return ("user.immutable-metadata",)
                return ()

            _build_audit_bytes(root, acl_probe=immutable_xattr)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root, write_audit=False)
            (root / "var/lock").unlink()
            os.symlink("../tmp", root / "var/lock")
            with self.assertRaises(collector.StateCollectionError) as caught:
                _build_audit_bytes(root)
            self.assertEqual(
                collector.CollectionFailure.WRITABLE_SURFACE,
                caught.exception.kind,
            )

    def test_runtime_binds_audit_and_rejects_xattrs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            payload = _payload(root)
            self.assertRegex(
                payload["writable_surface_audit_sha256"],
                r"^sha256:[0-9a-f]{64}$",
            )

            with self.assertRaises(collector.StateCollectionError) as caught:
                collector.collect_canonical_bytes(
                    "fs4",
                    _root_prefix=os.fsencode(root),
                    _acl_probe=lambda _descriptor: ("user.state",),
                )
            self.assertEqual(
                collector.CollectionFailure.XATTR_PRESENT,
                caught.exception.kind,
            )

            audit_path = root / collector.AUDIT_RELATIVE_PATH
            tampered = json.loads(audit_path.read_bytes())
            tampered["profile"] = "fs1"
            audit_path.chmod(0o644)
            audit_path.write_bytes(collector._canonical_json(tampered))
            audit_path.chmod(0o444)
            with self.assertRaises(collector.StateCollectionError) as caught:
                _payload(root)
            self.assertEqual(
                collector.CollectionFailure.AUDIT_INVALID,
                caught.exception.kind,
            )

    def test_fake_git_and_path_executables_are_only_filesystem_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _touch_common_roots(root)
            hook = root / ".fake-git" / "hooks" / "post-checkout"
            hook.parent.mkdir(parents=True)
            marker = root / "executed"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            hook.chmod(0o755)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            fake_python.chmod(0o755)

            old_path = os.environ.get("PATH")
            os.environ["PATH"] = os.fspath(fake_bin)
            try:
                payload = _payload(root)
            finally:
                if old_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = old_path

        self.assertFalse(marker.exists())
        self.assertEqual(
            "directory", _entry_by_path(payload, ".fake-git")["type"]
        )
        source = HELPER.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "os.popen", "shutil.which"):
            self.assertNotIn(forbidden, source)

    def test_cli_errors_are_canonical_sanitized_and_do_not_consult_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "fake-python-ran"
            fake = root / "python3"
            fake.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            fake.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = os.fspath(root)
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    os.fspath(HELPER),
                    "--profile",
                    "invalid",
                ],
                capture_output=True,
                check=False,
                env=environment,
            )

        self.assertEqual(64, result.returncode)
        self.assertFalse(marker.exists())
        self.assertEqual(b"", result.stderr)
        self.assertEqual(
            {"error": {"kind": "invalid_invocation"}, "schema": "edgeloopbench.filesystem-state-error.v1"},
            json.loads(result.stdout),
        )
        self.assertEqual(
            json.dumps(
                json.loads(result.stdout),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
