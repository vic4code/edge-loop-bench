#!/usr/bin/env python3
"""Fetch the exact, hash-pinned source assets used by the v0.6 study."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


INTERCODE_REVISION = "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8"
NL2BASH_REVISION = "d6b9f5bdff45621d190134e31ab63b7bf7002190"
MAX_ASSET_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Asset:
    repository: str
    revision: str
    source_path: str
    destination: str
    sha256: str

    @property
    def url(self) -> str:
        return (
            f"https://raw.githubusercontent.com/{self.repository}/"
            f"{self.revision}/{self.source_path}"
        )


ASSETS = (
    Asset(
        "princeton-nlp/intercode",
        INTERCODE_REVISION,
        "LICENSE.md",
        f"vendor/intercode/{INTERCODE_REVISION}/LICENSE.md",
        "837bf0fc3fe75298e6bcca9dbb66028b449bc456e16621d7a0f65292fa037274",
    ),
    Asset(
        "princeton-nlp/intercode",
        INTERCODE_REVISION,
        "data/nl2bash/README.md",
        f"vendor/intercode/{INTERCODE_REVISION}/data/nl2bash/README.md",
        "c796ac8e6c633eceaf102e1cbfecb133e293a3ed5db620d22df3641536513667",
    ),
    Asset(
        "princeton-nlp/intercode",
        INTERCODE_REVISION,
        "data/nl2bash/nl2bash_fs_1.json",
        f"vendor/intercode/{INTERCODE_REVISION}/data/nl2bash/nl2bash_fs_1.json",
        "60f88e1aacc7ebba535093f9890c5c33203f4e5f32958e0e94fbe90ec4f01c82",
    ),
    Asset(
        "princeton-nlp/intercode",
        INTERCODE_REVISION,
        "data/nl2bash/nl2bash_fs_2.json",
        f"vendor/intercode/{INTERCODE_REVISION}/data/nl2bash/nl2bash_fs_2.json",
        "8f4ce24e535fab782fda607e37db2ae1d6c5f99993c638d1ac0a7e0b542f633e",
    ),
    Asset(
        "princeton-nlp/intercode",
        INTERCODE_REVISION,
        "data/nl2bash/nl2bash_fs_3.json",
        f"vendor/intercode/{INTERCODE_REVISION}/data/nl2bash/nl2bash_fs_3.json",
        "a2d4ec8bc7ad69a4e2fb3eb84033994cf65ee9cfb355e3e63099df67a339b2e1",
    ),
    Asset(
        "princeton-nlp/intercode",
        INTERCODE_REVISION,
        "data/nl2bash/nl2bash_fs_4.json",
        f"vendor/intercode/{INTERCODE_REVISION}/data/nl2bash/nl2bash_fs_4.json",
        "ce41b89450f87765a02a51df259ca0c1762e8249185c022adb089147e2c16200",
    ),
    Asset(
        "princeton-nlp/intercode",
        INTERCODE_REVISION,
        "data/nl2bash/test_queries.json",
        f"vendor/intercode/{INTERCODE_REVISION}/data/nl2bash/test_queries.json",
        "d24a7a1eb61c2621c48a42f942d08f6aa02066630ab49c2a07de2530a226e0aa",
    ),
    Asset(
        "princeton-nlp/intercode",
        INTERCODE_REVISION,
        "docker/nl2bash.Dockerfile",
        f"vendor/intercode/{INTERCODE_REVISION}/docker/nl2bash.Dockerfile",
        "c8b52b44cc276921f1b139d49562152792872c7b013261b748305a78d4230189",
    ),
    Asset(
        "princeton-nlp/intercode",
        INTERCODE_REVISION,
        "docker/docker.gitignore",
        f"vendor/intercode/{INTERCODE_REVISION}/docker/docker.gitignore",
        "5479a1cafa260c77e836e8601ba9a345d39df777dc9cb07d6a93f0ac29b69166",
    ),
    *(
        Asset(
            "princeton-nlp/intercode",
            INTERCODE_REVISION,
            f"docker/bash_scripts/setup_nl2b_fs_{filesystem}.sh",
            (
                f"vendor/intercode/{INTERCODE_REVISION}/docker/bash_scripts/"
                f"setup_nl2b_fs_{filesystem}.sh"
            ),
            digest,
        )
        for filesystem, digest in (
            (1, "02b9a2206d809a9fca03b755e61b94618248a400fd3132ac61d32b6f3009dd3f"),
            (2, "05c3109c4e9999e661d66c6d74137f0238b88017ec9cf884abdda0499e94ff1d"),
            (3, "5e8d9f832f272c31dfb73567e75d33efb970d4e4bf9a8e691582d4fa09422d09"),
            (4, "c5fb550aa1578fe2454e8ab06221165df90311231cb71d3d9b0ce036a8235274"),
        )
    ),
    Asset(
        "TellinaTool/nl2bash",
        NL2BASH_REVISION,
        "data/bash/LICENSE",
        f"vendor/nl2bash/{NL2BASH_REVISION}/data/bash/LICENSE",
        "4ac5c8b7fb1d1fccfa52916749674d67b2024c76616fed89db7f67a976056750",
    ),
)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    for asset in ASSETS:
        destination = _safe_destination(root, asset.destination)
        if destination.exists():
            payload = destination.read_bytes()
            _verify(payload, asset)
            print(f"verified {asset.destination}")
            continue
        payload = _download(asset)
        _verify(payload, asset)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        print(f"fetched {asset.destination}")
    return 0


def _download(asset: Asset) -> bytes:
    request = urllib.request.Request(
        asset.url,
        headers={"User-Agent": "EdgeLoopBench-v0.6-vendor/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(MAX_ASSET_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"could not fetch {asset.source_path}: {error}") from error
    if len(payload) > MAX_ASSET_BYTES:
        raise RuntimeError(f"asset exceeds safety limit: {asset.source_path}")
    return payload


def _verify(payload: bytes, asset: Asset) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if actual != asset.sha256:
        raise RuntimeError(
            f"SHA-256 mismatch for {asset.source_path}: expected {asset.sha256}, got {actual}"
        )


def _safe_destination(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeError(f"unsafe vendor destination: {relative}")
    destination = root.joinpath(*candidate.parts)
    try:
        destination.relative_to(root)
    except ValueError as error:  # pragma: no cover - PurePosixPath is the primary guard
        raise RuntimeError(f"unsafe vendor destination: {relative}") from error
    return destination


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError) as error:
        print(f"vendor_intercode: {error}", file=sys.stderr)
        raise SystemExit(1) from error
