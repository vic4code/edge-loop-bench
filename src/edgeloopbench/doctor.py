"""Safe, read-only host discovery for experiment planning."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable


CommandRunner = Callable[[list[str]], str | None]


def collect_host_info(run_command: CommandRunner | None = None) -> dict[str, object]:
    """Return non-privileged host facts without requiring a model server."""

    runner = run_command or _run_command
    system = platform.system()
    result: dict[str, object] = {
        "platform": system,
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_executable": _normalize_home(sys.executable),
        "runtimes": {
            "ollama": _runtime("ollama"),
            "vllm": _runtime("vllm"),
            "mlx_lm": _runtime("mlx_lm.server"),
        },
    }
    if system == "Darwin":
        result["chip"] = runner(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"])
        memory = runner(["/usr/sbin/sysctl", "-n", "hw.memsize"])
        result["memory_bytes"] = int(memory) if memory and memory.isdigit() else None
        cpu_count = runner(["/usr/sbin/sysctl", "-n", "hw.ncpu"])
        result["logical_cpu_count"] = (
            int(cpu_count) if cpu_count and cpu_count.isdigit() else None
        )
    else:
        result["chip"] = platform.processor() or None
        result["memory_bytes"] = None
        result["logical_cpu_count"] = None
    return result


def _runtime(executable: str) -> dict[str, object]:
    path = shutil.which(executable)
    return {
        "available": path is not None,
        "path": _normalize_home(path) if path else None,
    }


def _normalize_home(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def _run_command(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None
