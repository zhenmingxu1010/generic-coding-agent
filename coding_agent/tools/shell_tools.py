from __future__ import annotations

import os
import subprocess
import sys
import re
from pathlib import Path

from coding_agent.core.schemas import ToolResult
from coding_agent.safety.command_guard import CommandGuard
from coding_agent.core.utils import coerce_text, truncate


def target_python_executable() -> str:
    """Return the configured target-project interpreter, or the agent's own."""
    configured = os.getenv("AGENT_TARGET_PYTHON", "").strip()
    if not configured:
        return sys.executable
    # Keep a virtual environment's launcher path intact. Resolving its symlink
    # can execute the base interpreter and silently lose the venv site-packages.
    path = Path(os.path.abspath(os.path.expanduser(configured)))
    if not path.is_file() or not os.access(path, os.X_OK):
        return sys.executable
    return str(path)


def _resolve_python_executable(parts: list[str]) -> list[str]:
    if not parts:
        return parts
    python = target_python_executable()
    exe = Path(parts[0]).name.lower()
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", exe):
        out = list(parts)
        out[0] = python
        return out
    if exe in {"pytest", "pytest.exe"}:
        return [python, "-m", "pytest", *parts[1:]]
    return parts


def run_shell(
    workspace: str,
    command: list[str] | str,
    timeout_sec: int = 60,
    max_output_chars: int = 12000,
    read_only: bool = False,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> ToolResult:
    guard = CommandGuard(read_only=read_only, workspace=workspace)
    try:
        parts = guard.check(command)
        parts = _resolve_python_executable(parts)
    except Exception as e:
        return ToolResult(tool="run_shell", ok=False, message=str(e), data={
            "command": command,
            "returncode": 1,
            "stdout": "",
            "stderr": str(e),
            "timed_out": False,
            "executed": False,
            "failure_kind": "command_policy",
        })
    try:
        env = os.environ.copy()
        env.update({str(key): str(value) for key, value in (extra_env or {}).items()})
        proc = subprocess.run(
            parts,
            cwd=str(Path(workspace).resolve()),
            text=True,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            env=env,
        )
        return ToolResult(tool="run_shell", ok=(proc.returncode == 0), message="command finished", data={
            "command": parts,
            "returncode": proc.returncode,
            "stdout": truncate(proc.stdout, max_output_chars),
            "stderr": truncate(proc.stderr, max_output_chars),
            "timed_out": False,
            "executed": True,
            "failure_kind": "" if proc.returncode == 0 else "process_exit",
        })
    except subprocess.TimeoutExpired as e:
        return ToolResult(tool="run_shell", ok=False, message="command timed out", data={
            "command": parts,
            "returncode": -1,
            "stdout": truncate(coerce_text(e.stdout), max_output_chars),
            "stderr": truncate(coerce_text(e.stderr), max_output_chars),
            "timed_out": True,
            "executed": True,
            "failure_kind": "timeout",
        })
    except OSError as e:
        return ToolResult(tool="run_shell", ok=False, message=str(e), data={
            "command": parts,
            "returncode": 1,
            "stdout": "",
            "stderr": str(e),
            "timed_out": False,
            "executed": False,
            "failure_kind": "launch_error",
        })


def git_diff(workspace: str, max_output_chars: int = 20000) -> ToolResult:
    return run_shell(workspace, ["git", "diff", "--"], timeout_sec=30, max_output_chars=max_output_chars)
