#!/usr/bin/env python3
"""Generate the frozen v0.2 calibration and confirmatory manifests."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
OUTPUT = ROOT / "configs" / "experiments" / "v0.2"
CALIBRATION_REVISION = "11d5ce8b9118920baccedeac0f17347f883cdfb6"
CONFIRMATORY_REVISION = "3e7dd740ca501c72c4398d209843c99cac18e07b"
CALIBRATION_TASKS = (
    "python-localized-001",
    "python-localized-002",
    "python-cross-file-001",
    "python-cross-file-002",
    "python-diagnosis-001",
    "python-adversarial-001",
)
MODELS = {
    "qwen35-4b": (
        "qwen3.5:4b",
        "81fb60c7daa80fc1123380b98970b320ae233409f0f71a72ed7b9b0d62f40490",
        "Q4_K_M",
    ),
    "qwen35-9b": (
        "qwen3.5:9b",
        "dec52a44569a2a25341c4e4d3fee25846eed4f6f0b936278e3a3c900bb99d37c",
        "Q4_K_M",
    ),
    "gemma4-12b": (
        "gemma4:12b-it-q4_K_M",
        "1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606",
        "Q4_K_M",
    ),
}


def render(label: str, phase: str, tasks: tuple[str, ...]) -> str:
    model_id, digest, quantization = MODELS[label]
    controller_revision = (
        CALIBRATION_REVISION if phase == "calibration" else CONFIRMATORY_REVISION
    )
    task_lines = "\n".join(f'  "{task}",' for task in tasks)
    return f'''schema_version = 1
id = "v02-{phase}-{label}"
track = "effectiveness"
draft = false

tasks = [
{task_lines}
]
strategies = ["direct", "bounded_retry", "maker_verifier"]
seeds = [20260715]

[generation]
thinking = false
temperature = 0.0
edit_schema_revision = "full-file-edits-v1"
controller_revision = "{controller_revision}"

[model]
id = "{model_id}"
revision = "sha256:{digest}"
artifact_sha256 = "sha256:{digest}"
weight_quantization = "{quantization}"
context_limit_tokens = 4096

[backend]
name = "ollama"
version = "0.31.1"
artifact_sha256 = "sha256:67d4b4e0e8a6742b8fec7491ea67653c4cc802651a8fa396aa569af4e12026a2"
command = ["ollama", "serve"]

[backend.environment]
OLLAMA_HOST = "127.0.0.1:11434"
OLLAMA_NO_CLOUD = "1"
OLLAMA_NUM_PARALLEL = "1"
OLLAMA_MAX_LOADED_MODELS = "1"
OLLAMA_KEEP_ALIVE = "-1"
OLLAMA_CONTEXT_LENGTH = "4096"
OLLAMA_FLASH_ATTENTION = "1"
OLLAMA_KV_CACHE_TYPE = "q8_0"

[budgets.medium]
prompt_tokens = 12000
completion_tokens = 2400
model_calls = 3
tool_calls = 8
public_test_runs = 2
per_call_context_tokens = 4096
'''


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    confirmatory = tuple(
        path.parent.name
        for path in sorted((ROOT / "tasks" / "confirmatory").glob("*/task.toml"))
    )
    if len(confirmatory) != 30:
        raise SystemExit("confirmatory suite must contain exactly 30 tasks")
    for label in MODELS:
        for phase, tasks in (
            ("calibration", CALIBRATION_TASKS),
            ("confirmatory", confirmatory),
        ):
            (OUTPUT / f"{phase}-{label}.toml").write_text(
                render(label, phase, tasks), encoding="utf-8"
            )
    print("generated 6 v0.2 manifests")


if __name__ == "__main__":
    main()
