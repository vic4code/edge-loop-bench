#!/usr/bin/env python3
"""Provision and build the exact vocab-only tokenizer used by v0.7.

This is an explicit, networked provisioning tool. Benchmark tasks and measured
episodes remain offline. Running without ``--execute`` only prints the frozen
plan and performs no filesystem or network mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


OLLAMA_REPOSITORY = "https://github.com/ollama/ollama.git"
OLLAMA_COMMIT = "710292ff4f191d8da9f6a4230804fbc693338d4a"
LLAMA_CPP_REPOSITORY = "https://github.com/ggml-org/llama.cpp.git"
LLAMA_CPP_TAG = "b9840"
LLAMA_CPP_COMMIT = "8c146a8366304c871efc26057cc90370ccf58dad"


def _step(
    name: str,
    argv: list[str],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    expected_stdout: str | None = None,
) -> dict[str, Any]:
    return {
        "argv": argv,
        "cwd": None if cwd is None else str(cwd),
        "environment": {} if environment is None else environment,
        "expected_stdout": expected_stdout,
        "name": name,
    }


def build_plan(work_dir: Path, output: Path) -> dict[str, Any]:
    """Return the complete immutable command plan without executing it."""

    work_dir = work_dir.expanduser().absolute()
    output = output.expanduser().absolute()
    source_dir = work_dir / "ollama"
    llama_source = work_dir / "llama.cpp"
    build_dir = work_dir / "cmake-build"
    configure = [
        "cmake",
        "-S",
        str(source_dir / "llama" / "server"),
        "-B",
        str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DGGML_BACKEND_DL=OFF",
        "-DGGML_CPU_ALL_VARIANTS=OFF",
        "-DGGML_METAL=OFF",
        "-DGGML_NATIVE=OFF",
        "-DGGML_OPENMP=OFF",
        "-DLLAMA_CURL=OFF",
        "-DOLLAMA_LLAMA_CPP_SKIP_COMPAT_PATCH=ON",
        "-DOLLAMA_RUNNER_DIR=",
    ]
    if sys.platform == "darwin":
        configure.append("-DCMAKE_OSX_ARCHITECTURES=arm64")
    steps = [
        _step("init-ollama", ["git", "init", str(source_dir)]),
        _step(
            "set-ollama-origin",
            [
                "git",
                "-C",
                str(source_dir),
                "remote",
                "add",
                "origin",
                OLLAMA_REPOSITORY,
            ],
        ),
        _step(
            "fetch-ollama",
            [
                "git",
                "-C",
                str(source_dir),
                "fetch",
                "--depth",
                "1",
                "origin",
                OLLAMA_COMMIT,
            ],
        ),
        _step(
            "checkout-ollama",
            ["git", "-C", str(source_dir), "checkout", "--detach", "FETCH_HEAD"],
        ),
        _step(
            "verify-ollama-commit",
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            expected_stdout=OLLAMA_COMMIT,
        ),
        _step("init-llama-cpp", ["git", "init", str(llama_source)]),
        _step(
            "set-llama-cpp-origin",
            [
                "git",
                "-C",
                str(llama_source),
                "remote",
                "add",
                "origin",
                LLAMA_CPP_REPOSITORY,
            ],
        ),
        _step(
            "fetch-llama-cpp",
            [
                "git",
                "-C",
                str(llama_source),
                "fetch",
                "--depth",
                "1",
                "origin",
                f"refs/tags/{LLAMA_CPP_TAG}",
            ],
        ),
        _step(
            "checkout-llama-cpp",
            ["git", "-C", str(llama_source), "checkout", "--detach", "FETCH_HEAD"],
        ),
        _step(
            "verify-llama-cpp-commit",
            ["git", "-C", str(llama_source), "rev-parse", "HEAD"],
            expected_stdout=LLAMA_CPP_COMMIT,
        ),
        _step(
            "apply-compat-patches",
            [
                "cmake",
                f"-DPATCH_DIR={source_dir / 'llama' / 'compat'}",
                "-P",
                str(source_dir / "llama" / "compat" / "apply-patch.cmake"),
            ],
            cwd=llama_source,
        ),
        _step(
            "configure-tokenizer",
            configure,
            environment={"OLLAMA_LLAMA_CPP_SOURCE": str(llama_source)},
        ),
        _step(
            "build-tokenizer",
            [
                "cmake",
                "--build",
                str(build_dir),
                "--target",
                "llama-tokenize",
                "--parallel",
                "2",
            ],
        ),
    ]
    return {
        "artifact": str(output),
        "llama_cpp_commit": LLAMA_CPP_COMMIT,
        "llama_cpp_repository": LLAMA_CPP_REPOSITORY,
        "llama_cpp_source_mode": "exact-shallow-tag",
        "llama_cpp_tag": LLAMA_CPP_TAG,
        "network_phase": "source provisioning only",
        "ollama_commit": OLLAMA_COMMIT,
        "ollama_repository": OLLAMA_REPOSITORY,
        "steps": steps,
        "work_dir": str(work_dir),
    }


def _run(
    command: list[str],
    *,
    capture: bool = False,
    cwd: Path | None = None,
    llama_cpp_source: Path | None = None,
) -> str:
    environment = os.environ.copy()
    for name in (
        "OLLAMA_LLAMA_CPP_SOURCE",
        "OLLAMA_LLAMA_CPP_SKIP_COMPAT_PATCH",
        "OLLAMA_LLAMA_CPP_COMPAT",
    ):
        environment.pop(name, None)
    if llama_cpp_source is not None:
        if not llama_cpp_source.is_absolute():
            raise RuntimeError("llama.cpp source override must be absolute")
        environment["OLLAMA_LLAMA_CPP_SOURCE"] = str(llama_cpp_source)
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        cwd=cwd,
        env=environment,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed with status {completed.returncode}: {command[0]}")
    return completed.stdout.strip() if capture else ""


def _execute_step(step: dict[str, Any]) -> str:
    if set(step) != {"argv", "cwd", "environment", "expected_stdout", "name"}:
        raise RuntimeError("build step shape is invalid")
    argv = step["argv"]
    cwd_value = step["cwd"]
    environment = step["environment"]
    expected_stdout = step["expected_stdout"]
    name = step["name"]
    if (
        type(name) is not str
        or not name
        or type(argv) is not list
        or not argv
        or any(type(argument) is not str or not argument for argument in argv)
        or (cwd_value is not None and type(cwd_value) is not str)
        or type(environment) is not dict
        or any(
            type(key) is not str or type(value) is not str
            for key, value in environment.items()
        )
        or (expected_stdout is not None and type(expected_stdout) is not str)
    ):
        raise RuntimeError("build step value is invalid")
    if set(environment) - {"OLLAMA_LLAMA_CPP_SOURCE"}:
        raise RuntimeError("build step environment is not permitted")
    cwd = None if cwd_value is None else Path(cwd_value)
    llama_source_value = environment.get("OLLAMA_LLAMA_CPP_SOURCE")
    llama_source = None if llama_source_value is None else Path(llama_source_value)
    observed = _run(
        argv,
        capture=expected_stdout is not None,
        cwd=cwd,
        llama_cpp_source=llama_source,
    )
    if expected_stdout is not None and observed != expected_stdout:
        raise RuntimeError(f"{name} did not resolve to the pinned identity")
    return observed


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return "sha256:" + hasher.hexdigest()


def _assert_dedicated_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("work directory must not be a symlink")
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise RuntimeError("work directory must be a fresh empty directory")
        return
    path.mkdir(parents=True)


def _assert_unused_build_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(
            "cmake build state already exists; choose a fresh work directory"
        )


def _select_built_artifact(candidates: tuple[Path, ...]) -> Path:
    for candidate in candidates:
        if candidate.is_symlink():
            raise RuntimeError("built tokenizer must be a regular non-symlink file")
        if candidate.is_file():
            return candidate
    raise RuntimeError("llama-tokenize build completed without the expected artifact")


def _assert_regular_or_absent(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError("artifact destination must be a regular non-symlink file")


def _provenance_record(plan: dict[str, Any], artifact_sha256: str) -> dict[str, Any]:
    configure = next(
        step["argv"]
        for step in plan["steps"]
        if step["name"] == "configure-tokenizer"
    )
    return {
        "artifact_sha256": artifact_sha256,
        "build_recipe": {
            "cmake_definitions": [
                argument for argument in configure if argument.startswith("-D")
            ],
            "parallel_jobs": 2,
            "source_provisioning": {
                "compatibility_patch": "preapplied-from-pinned-ollama",
                "llama_cpp_fetch": "exact-shallow-tag",
            },
            "target": "llama-tokenize",
            "target_platform": "macos-arm64",
        },
        "llama_cpp_commit": LLAMA_CPP_COMMIT,
        "llama_cpp_repository": LLAMA_CPP_REPOSITORY,
        "llama_cpp_tag": LLAMA_CPP_TAG,
        "ollama_commit": OLLAMA_COMMIT,
        "ollama_repository": OLLAMA_REPOSITORY,
    }


def execute_build(work_dir: Path, output: Path) -> dict[str, Any]:
    """Build after explicit authorization and return stable provenance."""

    if sys.platform != "darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        raise RuntimeError("the published tokenizer recipe is pinned to macOS arm64")
    work_dir = work_dir.expanduser().absolute()
    output = output.expanduser().absolute()
    _assert_dedicated_directory(work_dir)
    source_dir = work_dir / "ollama"
    llama_source = work_dir / "llama.cpp"
    build_dir = work_dir / "cmake-build"
    _assert_unused_build_directory(build_dir)
    plan = build_plan(work_dir, output)
    for step in plan["steps"]:
        _execute_step(step)
        if step["name"] == "verify-ollama-commit":
            version = (source_dir / "LLAMA_CPP_VERSION").read_text(
                encoding="utf-8"
            ).strip()
            if version != LLAMA_CPP_TAG:
                raise RuntimeError("Ollama no longer contains the pinned llama.cpp tag")
        if step["name"] == "apply-compat-patches":
            patched_loader = llama_source / "src" / "llama-model-loader.cpp"
            if "ollama_compat" not in patched_loader.read_text(encoding="utf-8"):
                raise RuntimeError("Ollama compatibility hooks were not applied")
    candidates = (
        build_dir / "bin" / "llama-tokenize",
        build_dir / "tools" / "tokenize" / "llama-tokenize",
    )
    built = _select_built_artifact(candidates)
    if not built.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise RuntimeError("built llama-tokenize artifact is not executable")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    _assert_regular_or_absent(output)
    _assert_regular_or_absent(temporary)
    shutil.copy2(built, temporary)
    os.replace(temporary, output)
    provenance = _provenance_record(plan, _sha256_file(output))
    provenance_path = output.with_name(output.name + ".provenance.json")
    provenance_tmp = provenance_path.with_name(provenance_path.name + ".tmp")
    _assert_regular_or_absent(provenance_path)
    _assert_regular_or_absent(provenance_tmp)
    provenance_tmp.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(provenance_tmp, provenance_path)
    return provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("build/pinned-tokenizer"),
        help="dedicated source/build cache",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/artifacts/llama-tokenize"),
        help="final helper artifact",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform network provisioning and compilation",
    )
    args = parser.parse_args(argv)
    if args.execute:
        result = execute_build(args.work_dir, args.output)
    else:
        result = build_plan(args.work_dir, args.output)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
