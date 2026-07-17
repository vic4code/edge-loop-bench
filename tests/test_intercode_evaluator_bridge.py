from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import json
import pickle
import unittest

from edgeloopbench.docker_cli import DockerTrustedState
from edgeloopbench.interactive_environment import EnvironmentCheckpoint
from edgeloopbench.intercode_evaluator import (
    MAX_NORMALIZED_OUTPUT_BYTES,
    hardlink_group_sha256,
)
from edgeloopbench.intercode_evaluator_bridge import (
    COLLECTOR_STATE_ADAPTER_REVISION,
    CollectorStateBridgeError,
    PrivateCheckpointMaterialRegistry,
    adapt_collector_state,
)


def digest(value: str | bytes) -> str:
    encoded = value if isinstance(value, bytes) else value.encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def collector_hardlink_digest(paths: tuple[str, ...]) -> str:
    accumulator = hashlib.sha256(b"edgeloopbench-hardlink-group-v1\0")
    for path in sorted(item.encode("utf-8") for item in paths):
        accumulator.update(len(path).to_bytes(4, "big"))
        accumulator.update(path)
    return "sha256:" + accumulator.hexdigest()


def entry(
    path: str,
    *,
    kind: str = "file",
    content: str | None = None,
    hardlink: str | None = None,
) -> dict[str, object]:
    encoded = path.encode("utf-8")
    return {
        "content_sha256": digest(path) if content is None and kind == "file" else content,
        "gid": None if kind == "missing" else 65532,
        "hardlink_group_sha256": hardlink,
        "mode": None if kind == "missing" else 0o640,
        "path": path,
        "path_bytes_b64": base64.b64encode(encoded).decode("ascii"),
        "size_bytes": len(path.encode("utf-8")) if kind == "file" else None,
        "symlink_target": "target" if kind == "symlink" else None,
        "symlink_target_bytes_b64": (
            base64.b64encode(b"target").decode("ascii")
            if kind == "symlink"
            else None
        ),
        "type": kind,
        "uid": None if kind == "missing" else 65532,
    }


def trusted_state(entries: list[dict[str, object]]) -> DockerTrustedState:
    profile_sha256 = digest("profile")
    audit_sha256 = digest("audit")
    state_sha256 = digest(
        canonical(
            {
                "entries": entries,
                "profile_sha256": profile_sha256,
                "schema": "edgeloopbench.filesystem-state.v1",
                "writable_surface_audit_sha256": audit_sha256,
            }
        )
    )
    payload = {
        "common_roots": ["home/agent", "tmp"],
        "dynamic_root_policy": "non_baseline_top_level",
        "entries": entries,
        "entry_count": len(entries),
        "policy_sha256": digest("policy"),
        "profile": "fs1",
        "profile_sha256": profile_sha256,
        "root_baseline_sha256": digest("root"),
        "schema": "edgeloopbench.filesystem-state.v1",
        "state_sha256": state_sha256,
        "strict_surface": {"failures": [], "status": "representable"},
        "task_roots": ["testbed"],
        "total_file_bytes": sum(
            int(item["size_bytes"] or 0) for item in entries
        ),
        "writable_surface_audit_sha256": audit_sha256,
    }
    return DockerTrustedState(
        canonical_json=canonical(payload),
        state_sha256=state_sha256,
        profile="fs1",
        profile_sha256=profile_sha256,
        policy_sha256=digest("policy"),
        root_baseline_sha256=digest("root"),
        writable_surface_audit_sha256=audit_sha256,
        collector_source_sha256=digest("collector-source"),
        strict_representable=True,
        strict_failures=(),
    )


class CollectorStateAdapterTests(unittest.TestCase):
    def test_adapts_exact_canonical_collector_json_and_binds_revision(self) -> None:
        source = trusted_state(
            [entry("testbed/a.txt"), entry("testbed/deleted", kind="missing")]
        )

        adapted = adapt_collector_state(source)

        self.assertEqual(adapted.collector_state_sha256, source.state_sha256)
        self.assertEqual(adapted.adapter_revision, COLLECTOR_STATE_ADAPTER_REVISION)
        self.assertEqual(
            [(item.path, item.kind) for item in adapted.snapshot.entries],
            [("testbed/a.txt", "file"), ("testbed/deleted", "absent")],
        )
        self.assertRegex(adapted.binding_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual("<AdaptedCollectorState redacted>", repr(adapted))

    def test_rejects_duplicate_keys_noncanonical_bytes_and_false_state_digest(self) -> None:
        source = trusted_state([entry("testbed/a.txt")])
        duplicate = source.canonical_json.replace(
            '"entry_count":1',
            '"entry_count":1,"entry_count":1',
        )
        noncanonical = source.canonical_json.replace(",", ", ", 1)
        wrong_digest = dataclasses.replace(source, state_sha256=digest("wrong"))

        for bad in (
            dataclasses.replace(source, canonical_json=duplicate),
            dataclasses.replace(source, canonical_json=noncanonical),
            wrong_digest,
        ):
            with self.subTest(case=bad.canonical_json[:24]):
                with self.assertRaises(CollectorStateBridgeError):
                    adapt_collector_state(bad)

    def test_validates_collector_hardlinks_but_rebinds_evaluator_digest(self) -> None:
        paths = ("testbed/a", "testbed/b")
        collector_digest = collector_hardlink_digest(paths)
        first = entry(paths[0], hardlink=collector_digest, content=digest("same"))
        second = entry(paths[1], hardlink=collector_digest, content=digest("same"))
        first["size_bytes"] = second["size_bytes"] = 4

        adapted = adapt_collector_state(trusted_state([first, second]))

        evaluator_digest = hardlink_group_sha256(paths)
        self.assertNotEqual(collector_digest, evaluator_digest)
        self.assertEqual(
            {item.hardlink_group_sha256 for item in adapted.snapshot.entries},
            {evaluator_digest},
        )

    def test_rejects_invalid_collector_hardlink_membership_or_metadata(self) -> None:
        paths = ("testbed/a", "testbed/b")
        group = collector_hardlink_digest(paths)
        cases: list[list[dict[str, object]]] = []

        singleton = entry(paths[0], hardlink=group)
        cases.append([singleton])

        first = entry(paths[0], hardlink=group, content=digest("same"))
        second = entry(paths[1], hardlink=group, content=digest("different"))
        cases.append([first, second])

        symlink_first = entry(paths[0], kind="symlink", hardlink=group)
        symlink_second = entry(paths[1], kind="symlink", hardlink=group)
        cases.append([symlink_first, symlink_second])

        bad_group = entry(paths[0], hardlink=digest("invented"))
        bad_group_peer = entry(paths[1], hardlink=digest("invented"))
        cases.append([bad_group, bad_group_peer])

        for records in cases:
            with self.subTest(records=records):
                with self.assertRaises(CollectorStateBridgeError):
                    adapt_collector_state(trusted_state(records))


class PrivateCheckpointMaterialRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = trusted_state([entry("testbed/a.txt")])
        self.checkpoint = EnvironmentCheckpoint(
            reference_sha256=digest("checkpoint"),
            state_sha256=self.source.state_sha256,
        )
        self.registry = PrivateCheckpointMaterialRegistry(
            scope_sha256=digest("scope")
        )

    def register(self) -> None:
        result = self.registry.register_checkpoint(
            checkpoint=self.checkpoint,
            snapshot_image_id=digest("snapshot"),
            trusted_state=self.source,
            raw_stdout=b"ok\r\n",
            raw_stderr=b"warning\n",
            normalized_output="ok\n\n[stderr]\nwarning\n",
            cwd="/testbed",
            runtime_sha256=digest("runtime"),
        )
        self.assertIsNone(result)

    def test_registry_is_private_redacted_and_binds_all_material(self) -> None:
        self.register()

        material = self.registry.material_for_evaluation(self.checkpoint)

        self.assertEqual("<PrivateCheckpointMaterial redacted>", repr(material))
        self.assertEqual("<PrivateCheckpointMaterialRegistry redacted>", repr(self.registry))
        self.assertEqual(material.snapshot_image_id, digest("snapshot"))
        self.assertEqual(material.collector_state_sha256, self.source.state_sha256)
        self.assertEqual(material.raw_stdout, b"ok\r\n")
        self.assertEqual(material.raw_stderr, b"warning\n")
        self.assertEqual(material.normalized_output, "ok\n\n[stderr]\nwarning\n")
        self.assertEqual(material.cwd, "/testbed")
        self.assertEqual(material.runtime_sha256, digest("runtime"))
        self.assertEqual(material.profile, "fs1")
        self.assertRegex(material.binding_sha256, r"^sha256:[0-9a-f]{64}$")

        for private in (material, self.registry):
            with self.subTest(private=type(private).__name__):
                self.assertFalse(dataclasses.is_dataclass(private))
                with self.assertRaises(TypeError):
                    copy.copy(private)
                with self.assertRaises(TypeError):
                    copy.deepcopy(private)
                with self.assertRaises(TypeError):
                    pickle.dumps(private)
                with self.assertRaises(TypeError):
                    vars(private)
                with self.assertRaises(TypeError):
                    json.dumps(private)

    def test_rejects_contradictory_output_state_and_duplicate_registration(self) -> None:
        with self.assertRaises(CollectorStateBridgeError):
            self.registry.register_checkpoint(
                checkpoint=dataclasses.replace(
                    self.checkpoint,
                    state_sha256=digest("wrong"),
                ),
                snapshot_image_id=digest("snapshot"),
                trusted_state=self.source,
                raw_stdout=b"ok\n",
                raw_stderr=b"",
                normalized_output="ok\n",
                cwd="/",
                runtime_sha256=digest("runtime"),
            )

        with self.assertRaises(CollectorStateBridgeError):
            self.registry.register_checkpoint(
                checkpoint=self.checkpoint,
                snapshot_image_id=digest("snapshot"),
                trusted_state=self.source,
                raw_stdout=b"ok\r\n",
                raw_stderr=b"warning\n",
                normalized_output="not-the-normalized-streams",
                cwd="/",
                runtime_sha256=digest("runtime"),
            )

        self.register()
        with self.assertRaises(CollectorStateBridgeError):
            self.register()

    def test_rejects_output_that_the_frozen_evaluator_cannot_represent(self) -> None:
        oversized = b"x" * (MAX_NORMALIZED_OUTPUT_BYTES + 1)

        with self.assertRaisesRegex(CollectorStateBridgeError, "safety limit"):
            self.registry.register_checkpoint(
                checkpoint=self.checkpoint,
                snapshot_image_id=digest("snapshot"),
                trusted_state=self.source,
                raw_stdout=oversized,
                raw_stderr=b"",
                normalized_output=oversized.decode("ascii"),
                cwd="/",
                runtime_sha256=digest("runtime"),
            )

    def test_close_zeroizes_registry_membership_and_prevents_reuse(self) -> None:
        self.register()
        self.registry.close()

        with self.assertRaises(CollectorStateBridgeError):
            self.registry.material_for_evaluation(self.checkpoint)
        with self.assertRaises(CollectorStateBridgeError):
            self.register()


if __name__ == "__main__":
    unittest.main()
