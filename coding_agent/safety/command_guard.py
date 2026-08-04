from __future__ import annotations

import shlex
import re
from dataclasses import dataclass, field
from pathlib import Path


PYTHON_EXECUTABLE_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?(?:\.exe)?$", re.IGNORECASE)


def canonical_executable(value: str) -> str:
    name = Path(value).name.lower()
    if PYTHON_EXECUTABLE_RE.fullmatch(name):
        return "python"
    if name == "pytest.exe":
        return "pytest"
    return name


@dataclass
class CommandPolicy:
    allow: set[str] = field(default_factory=lambda: {
        "python", "pytest", "sh", "bash", "grep", "find", "ls", "cat", "sed", "head", "tail", "wc", "git"
    })
    read_only_allow: set[str] = field(default_factory=lambda: {
        "grep", "ls", "cat", "head", "tail", "wc", "git"
    })
    blocked_patterns: list[str] = field(default_factory=lambda: [
        "rm -rf /", "rm -rf", ":(){ :|:& };:", "mkfs", "shutdown", "reboot", "dd if=", "curl | sh", "wget | sh", "> /", "sudo"
    ])


class CommandGuard:
    def __init__(self, policy: CommandPolicy | None = None, *, read_only: bool = False, workspace: str | Path | None = None):
        self.policy = policy or CommandPolicy()
        self.read_only = read_only
        self.workspace = Path(workspace).resolve() if workspace else None

    def _check_paths(self, parts: list[str]) -> None:
        if self.workspace is None:
            return
        for raw in parts[1:]:
            value = raw.split("=", 1)[1] if raw.startswith("--") and "=" in raw else raw
            if not value or value.startswith("-"):
                continue
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
            try:
                resolved = candidate.resolve()
            except OSError as exc:
                raise ValueError(f"Cannot resolve command path argument: {raw}") from exc
            if resolved != self.workspace and self.workspace not in resolved.parents:
                raise ValueError(f"Command path escapes workspace: {raw}")

    @staticmethod
    def _check_git(parts: list[str]) -> None:
        if canonical_executable(parts[0]) != "git":
            return
        subcommand = next((part for part in parts[1:] if not part.startswith("-")), "")
        allowed = {"status", "diff", "log", "show", "ls-files", "rev-parse"}
        if subcommand not in allowed:
            raise ValueError(f"Git subcommand is not read-only/allowed: {subcommand or '<missing>'}")
        if any(part == "--output" or part.startswith("--output=") for part in parts[1:]):
            raise ValueError("Git --output is not allowed through run_shell")

    @staticmethod
    def _check_find(parts: list[str]) -> None:
        if canonical_executable(parts[0]) != "find":
            return
        actions = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
        blocked = next((part for part in parts[1:] if part.lower() in actions), None)
        if blocked:
            raise ValueError(f"find action may execute or write and is not allowed: {blocked}")

    @staticmethod
    def _check_sed(parts: list[str]) -> None:
        if canonical_executable(parts[0]) != "sed":
            return
        if any(part == "--in-place" or part.startswith("--in-place=") or re.fullmatch(r"-i.*", part) for part in parts[1:]):
            raise ValueError("sed in-place editing is not allowed through run_shell; use edit_file")

    def check(self, command: list[str] | str) -> list[str]:
        if isinstance(command, str):
            parts = shlex.split(command)
        else:
            parts = [str(x) for x in command]
        if not parts:
            raise ValueError("Empty command")
        joined = " ".join(parts)
        for pat in self.policy.blocked_patterns:
            if pat in joined:
                raise ValueError(f"Blocked dangerous command pattern: {pat}")
        exe = canonical_executable(parts[0])
        allowed = self.policy.read_only_allow if self.read_only else self.policy.allow
        if exe not in allowed:
            raise ValueError(f"Command not allowed by policy: {parts[0]}")
        if exe == "python" and len(parts) > 1 and parts[1] in {"-c", "-"}:
            raise ValueError("Inline Python is not allowed; run a workspace script or module")
        if exe == "python" and len(parts) > 2 and parts[1:3] == ["-m", "pip"]:
            raise ValueError("Package installation is not allowed through run_shell")
        if exe in {"sh", "bash"}:
            if len(parts) < 2 or parts[1].startswith("-"):
                raise ValueError("Shell interpreters may only run a workspace script directly; inline commands and interpreter options are not allowed")
            script = Path(parts[1])
            if script.suffix.lower() not in {".sh", ".bash"}:
                raise ValueError("Shell interpreters may only run a .sh or .bash workspace script directly")
        self._check_git(parts)
        self._check_find(parts)
        self._check_sed(parts)
        self._check_paths(parts)
        return parts
