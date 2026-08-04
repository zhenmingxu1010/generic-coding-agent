from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from typing import Any

from coding_agent.safety.path_guard import PathGuard


ENTRY_TARGET_RE = re.compile(
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$"
)
_FALLBACK_SCRIPT_RE = re.compile(
    r'''^\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_.-]+))\s*=\s*(?:"([^"]+)"|'([^']+)')'''
)


def _fallback_project_scripts(text: str) -> dict[str, str]:
    """Parse the common PEP 621 scripts form when stdlib TOML is unavailable.

    Python 3.11+ uses ``tomllib`` below.  This conservative fallback keeps the
    project usable on Python 3.10 without pretending to be a general TOML
    parser; unsupported syntax simply produces no executable adapter.
    """
    scripts: dict[str, str] = {}
    in_scripts = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_scripts = line == "[project.scripts]"
            continue
        if not in_scripts or not line or line.startswith("#"):
            continue
        match = _FALLBACK_SCRIPT_RE.match(raw_line)
        if not match:
            continue
        name = next((value for value in match.groups()[:3] if value is not None), "")
        target = next((value for value in match.groups()[3:] if value is not None), "")
        if name and ENTRY_TARGET_RE.fullmatch(target):
            scripts[name] = target
    return scripts


def load_pep621_console_scripts(workspace: str | Path) -> dict[str, str]:
    path = PathGuard(workspace).resolve("pyproject.toml")
    if not path.is_file():
        return {}
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    try:
        import tomllib

        data: dict[str, Any] = tomllib.loads(raw.decode("utf-8"))
        values = (data.get("project") or {}).get("scripts") or {}
        if not isinstance(values, dict):
            return {}
        return {
            str(name): str(target)
            for name, target in values.items()
            if isinstance(name, str)
            and isinstance(target, str)
            and ENTRY_TARGET_RE.fullmatch(target)
        }
    except (ImportError, UnicodeDecodeError, ValueError):
        return _fallback_project_scripts(raw.decode("utf-8", errors="replace"))


def adapt_console_command(
    workspace: str | Path,
    command: list[str],
) -> tuple[list[str], dict[str, Any] | None]:
    """Adapt a declared PEP 621 console command to a controlled module call."""
    if not command:
        return command, None
    scripts = load_pep621_console_scripts(workspace)
    public_name = str(command[0])
    target = scripts.get(public_name)
    if target is None and public_name.lower().endswith(".exe"):
        target = scripts.get(public_name[:-4])
    if target is None:
        return command, None
    adapted = [
        "python",
        "-m",
        "coding_agent.verification.console_entry",
        public_name,
        target,
        "--",
        *[str(part) for part in command[1:]],
    ]
    return adapted, {
        "version": "pep621_console_entry_v1",
        "kind": "pep621_console_script",
        "public_command": [str(part) for part in command],
        "script_name": public_name,
        "entry_target": target,
        "adapted_command": adapted,
    }


def invoke_entry_point(script_name: str, target: str, args: list[str]) -> Any:
    if not ENTRY_TARGET_RE.fullmatch(target):
        raise ValueError(f"invalid console entry target: {target}")
    module_name, attr_path = target.split(":", 1)
    value: Any = importlib.import_module(module_name)
    for attr in attr_path.split("."):
        value = getattr(value, attr)
    if not callable(value):
        raise TypeError(f"console entry target is not callable: {target}")
    previous_argv = sys.argv
    try:
        sys.argv = [script_name, *args]
        return value()
    finally:
        sys.argv = previous_argv


def main() -> None:
    values = sys.argv[1:]
    if len(values) < 3 or "--" not in values[2:]:
        raise SystemExit("usage: console_entry SCRIPT_NAME MODULE:CALLABLE -- [ARGS...]")
    script_name, target = values[:2]
    separator = values.index("--", 2)
    result = invoke_entry_point(script_name, target, values[separator + 1 :])
    raise SystemExit(result)


if __name__ == "__main__":
    main()
