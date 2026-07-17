"""Verifier-sealed image and state-normalizer provenance for v0.7."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from pathlib import Path
from types import MappingProxyType

from .intercode_image_build import VerifiedInterCodeImageBuild
from .intercode_source_inventory import (
    SourceInventoryError,
    VerifiedSourceInventory,
    derive_source_subset_sha256,
    revalidate_source_inventory,
)


V07_IMAGE_SET_SCHEMA_REVISION = "intercode-v0.7-verified-image-set-v1"
V07_STATE_NORMALIZATION_REVISION = "intercode-v0.7-state-normalization-v1"
V07_STATE_NORMALIZATION_SOURCES = (
    "docker/intercode/state_collector.py",
    "src/edgeloopbench/intercode_evaluator.py",
    "src/edgeloopbench/intercode_evaluator_bridge.py",
    "src/edgeloopbench/intercode_docker_attempt.py",
    "src/edgeloopbench/intercode_replay_environment.py",
)

_IMAGE_SET_SEAL = object()
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_STRATA = ("fs1", "fs2", "fs3", "fs4")


class V07ImageProvenanceError(ValueError):
    """The image build or clean-source normalizer proof is not admissible."""


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedV07ImageSet:
    """Path-free authority for the only four images admitted to v0.7."""

    source_inventory_sha256: str
    build_plan_sha256: str
    build_manifest_sha256: str
    build_verification_sha256: str
    image_id_by_stratum: Mapping[str, str]
    state_normalization_revision: str
    state_normalization_source_sha256: str
    state_normalization_sha256: str
    image_set_sha256: str
    _construction_seal: InitVar[object | None] = None

    def __post_init__(self, _construction_seal: object | None) -> None:
        if _construction_seal is not _IMAGE_SET_SEAL:
            raise V07ImageProvenanceError(
                "v0.7 image sets are verifier-sealed"
            )
        if not isinstance(self.image_id_by_stratum, Mapping):
            raise V07ImageProvenanceError("v0.7 image set is not a mapping")
        images = dict(self.image_id_by_stratum)
        if tuple(sorted(images)) != _STRATA:
            raise V07ImageProvenanceError(
                "v0.7 image set requires exact four stratum image IDs"
            )
        object.__setattr__(
            self,
            "image_id_by_stratum",
            MappingProxyType({stratum: images[stratum] for stratum in _STRATA}),
        )
        _validate_image_set(self)

    def canonical_record(self) -> dict[str, object]:
        _validate_image_set(self)
        return {
            **_image_set_core(
                source_inventory_sha256=self.source_inventory_sha256,
                build_plan_sha256=self.build_plan_sha256,
                build_manifest_sha256=self.build_manifest_sha256,
                build_verification_sha256=self.build_verification_sha256,
                image_id_by_stratum=self.image_id_by_stratum,
                state_normalization_revision=self.state_normalization_revision,
                state_normalization_source_sha256=(
                    self.state_normalization_source_sha256
                ),
                state_normalization_sha256=self.state_normalization_sha256,
            ),
            "image_set_sha256": self.image_set_sha256,
        }

    def require_admitted(self) -> None:
        _validate_image_set(self)

    def __repr__(self) -> str:
        return (
            "<VerifiedV07ImageSet "
            f"root={self.image_set_sha256} images={len(self.image_id_by_stratum)}>"
        )


def verify_v07_image_set(
    *,
    source_inventory: VerifiedSourceInventory,
    repository_root: Path,
    verified_build: VerifiedInterCodeImageBuild,
) -> VerifiedV07ImageSet:
    """Revalidate clean source and bind its normalizer to a reopened build."""

    if type(source_inventory) is not VerifiedSourceInventory:
        raise V07ImageProvenanceError(
            "v0.7 image provenance requires verified source inventory"
        )
    if type(verified_build) is not VerifiedInterCodeImageBuild:
        raise V07ImageProvenanceError(
            "v0.7 image provenance requires a verified image build"
        )
    try:
        verified_build.require_admitted()
        revalidate_source_inventory(source_inventory, repository_root)
        normalizer_source = derive_source_subset_sha256(
            source_inventory,
            V07_STATE_NORMALIZATION_SOURCES,
        )
    except (SourceInventoryError, ValueError):
        raise V07ImageProvenanceError(
            "v0.7 image source inventory revalidation failed"
        ) from None
    images = verified_build.image_id_by_profile
    normalizer = _digest(
        {
            "revision": V07_STATE_NORMALIZATION_REVISION,
            "schema": "intercode-v0.7-state-normalization-identity-v1",
            "source_inventory_sha256": source_inventory.inventory_sha256,
            "source_subset_sha256": normalizer_source,
        }
    )
    values: dict[str, object] = {
        "source_inventory_sha256": source_inventory.inventory_sha256,
        "build_plan_sha256": verified_build.plan_sha256,
        "build_manifest_sha256": verified_build.manifest_sha256,
        "build_verification_sha256": verified_build.verification_sha256,
        "image_id_by_stratum": images,
        "state_normalization_revision": V07_STATE_NORMALIZATION_REVISION,
        "state_normalization_source_sha256": normalizer_source,
        "state_normalization_sha256": normalizer,
    }
    core = _image_set_core(**values)  # type: ignore[arg-type]
    return VerifiedV07ImageSet(
        **values,  # type: ignore[arg-type]
        image_set_sha256=_digest(core),
        _construction_seal=_IMAGE_SET_SEAL,
    )


def _image_set_core(
    *,
    source_inventory_sha256: str,
    build_plan_sha256: str,
    build_manifest_sha256: str,
    build_verification_sha256: str,
    image_id_by_stratum: Mapping[str, str],
    state_normalization_revision: str,
    state_normalization_source_sha256: str,
    state_normalization_sha256: str,
) -> dict[str, object]:
    return {
        "schema": V07_IMAGE_SET_SCHEMA_REVISION,
        "source_inventory_sha256": source_inventory_sha256,
        "image_build": {
            "plan_sha256": build_plan_sha256,
            "manifest_sha256": build_manifest_sha256,
            "verification_sha256": build_verification_sha256,
        },
        "image_id_by_stratum": {
            stratum: image_id_by_stratum[stratum] for stratum in _STRATA
        },
        "state_normalization": {
            "revision": state_normalization_revision,
            "source_sha256": state_normalization_source_sha256,
            "identity_sha256": state_normalization_sha256,
        },
    }


def _validate_image_set(value: VerifiedV07ImageSet) -> None:
    for field in (
        "source_inventory_sha256",
        "build_plan_sha256",
        "build_manifest_sha256",
        "build_verification_sha256",
        "state_normalization_source_sha256",
        "state_normalization_sha256",
        "image_set_sha256",
    ):
        item = getattr(value, field)
        if type(item) is not str or _SHA256.fullmatch(item) is None:
            raise V07ImageProvenanceError(f"v0.7 image {field} is invalid")
    if value.state_normalization_revision != V07_STATE_NORMALIZATION_REVISION:
        raise V07ImageProvenanceError("v0.7 state normalizer revision drifted")
    images = dict(value.image_id_by_stratum)
    if tuple(images) != _STRATA or any(
        type(images[stratum]) is not str
        or _SHA256.fullmatch(images[stratum]) is None
        for stratum in _STRATA
    ):
        raise V07ImageProvenanceError(
            "v0.7 image set requires exact four stratum image IDs"
        )
    if len(set(images.values())) != len(_STRATA):
        raise V07ImageProvenanceError(
            "v0.7 image set requires four distinct image IDs"
        )
    core = _image_set_core(
        source_inventory_sha256=value.source_inventory_sha256,
        build_plan_sha256=value.build_plan_sha256,
        build_manifest_sha256=value.build_manifest_sha256,
        build_verification_sha256=value.build_verification_sha256,
        image_id_by_stratum=value.image_id_by_stratum,
        state_normalization_revision=value.state_normalization_revision,
        state_normalization_source_sha256=(
            value.state_normalization_source_sha256
        ),
        state_normalization_sha256=value.state_normalization_sha256,
    )
    if value.image_set_sha256 != _digest(core):
        raise V07ImageProvenanceError("v0.7 image-set root is invalid")


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = (
    "V07_IMAGE_SET_SCHEMA_REVISION",
    "V07_STATE_NORMALIZATION_REVISION",
    "V07_STATE_NORMALIZATION_SOURCES",
    "V07ImageProvenanceError",
    "VerifiedV07ImageSet",
    "verify_v07_image_set",
)
