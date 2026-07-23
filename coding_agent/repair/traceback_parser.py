from __future__ import annotations

import re
from typing import Any

from coding_agent.workspace.run_paths import is_test_like_path


EXCEPTION_RE = re.compile(
    r"(?m)^(?:E\s+)?"
    r"((?:ImportError|ModuleNotFoundError|ValueError|TypeError|KeyError|NameError|AttributeError|FileNotFoundError|RuntimeError|AssertionError|SyntaxError|IndentationError|TabError):[^\n]+)"
)
FRAME_RE = re.compile(r'File "(?:\./)?([^"\n]+\.py)", line (\d+)(?:, in ([^\n]+))?')
PYTEST_FRAME_RE = re.compile(r"(?m)^([^\s:\n]+\.py):(\d+):\s+in\s+([^\n]+)")
PYTEST_ASSERT_FRAME_RE = re.compile(r"(?m)^([^\s:\n]+\.py):(\d+):\s+(AssertionError(?::[^\n]+)?)")
MISSING_IMPORT_RE = re.compile(
    r"ImportError:\s+cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]"
)
MISSING_MODULE_RE = re.compile(
    r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]"
)
UNRECOGNIZED_ARGS_RE = re.compile(r"(?:error:\s+)?unrecognized arguments:\s+([^\\\n'\"]+)")


def _owner_for_path(path: str | None) -> str:
    if not path:
        return "unknown"
    if is_test_like_path(path):
        return "generated_test_or_external_test"
    return "implementation"


def _issue_type(exc_line: str) -> str:
    return exc_line.split(":", 1)[0].strip().lower()


def parse_traceback_issues(text: str) -> list[dict[str, Any]]:
    """Extract deterministic issues from pytest/contract traceback output."""
    text = text or ""
    issues: list[dict[str, Any]] = []
    frames = [
        {
            "path": m.group(1).replace("\\", "/"),
            "line": int(m.group(2)),
            "function": (m.group(3) or "").strip() or None,
            "pos": m.start(),
        }
        for m in FRAME_RE.finditer(text)
    ]
    frames.extend(
        {
            "path": m.group(1).replace("\\", "/"),
            "line": int(m.group(2)),
            "function": (m.group(3) or "").strip() or None,
            "pos": m.start(),
        }
        for m in PYTEST_FRAME_RE.finditer(text)
    )
    frames.sort(key=lambda item: item["pos"])

    for m in MISSING_IMPORT_RE.finditer(text):
        frame = next((f for f in reversed(frames) if f["pos"] < m.start()), None)
        issues.append({
            "owner": "generated_test_or_interface",
            "type": "import_error_missing_symbol",
            "exception_type": "ImportError",
            "symbol": m.group(1),
            "module": m.group(2),
            "file": frame.get("path") if frame else None,
            "line": frame.get("line") if frame else None,
            "message": m.group(0),
            "repair_hint": "align the failing test with the task's public API, or correct the implementation when that API is required",
            "source": "traceback_parser",
        })

    for m in UNRECOGNIZED_ARGS_RE.finditer(text):
        frame = next((f for f in reversed(frames) if f["pos"] < m.start()), None)
        issues.append({
            "owner": "implementation",
            "type": "cli_unrecognized_arguments",
            "exception_type": "SystemExit",
            "file": frame.get("path") if frame else None,
            "line": frame.get("line") if frame else None,
            "function": frame.get("function") if frame else None,
            "arguments": m.group(1).strip(),
            "message": m.group(0).strip(),
            "repair_hint": "update the CLI parser and behavior to accept the argument form exercised by the tests or task contract",
            "source": "traceback_parser",
        })

    for m in EXCEPTION_RE.finditer(text):
        exc_line = m.group(1).strip()
        if "cannot import name" in exc_line:
            continue
        frame = next((f for f in reversed(frames) if f["pos"] < m.start()), None)
        path = frame.get("path") if frame else None
        issue = {
            "owner": _owner_for_path(path),
            "type": _issue_type(exc_line),
            "exception_type": exc_line.split(":", 1)[0].strip(),
            "file": path,
            "line": frame.get("line") if frame else None,
            "function": frame.get("function") if frame else None,
            "message": exc_line,
            "repair_hint": "fix implementation runtime behavior" if _owner_for_path(path) == "implementation" else "inspect test oracle and implementation API",
            "source": "traceback_parser",
        }
        missing_module = MISSING_MODULE_RE.search(exc_line)
        if missing_module:
            issue["missing_module"] = missing_module.group(1)
            issue["module"] = missing_module.group(1)
            issue["repair_hint"] = (
                "inspect package layout, working directory, imports, project configuration, "
                "and verification command before choosing a repair"
            )
        issues.append(issue)

    for m in PYTEST_ASSERT_FRAME_RE.finditer(text):
        path = m.group(1).replace("\\", "/")
        issues.append({
            "owner": _owner_for_path(path),
            "type": "assertionerror",
            "exception_type": "AssertionError",
            "file": path,
            "line": int(m.group(2)),
            "function": None,
            "message": m.group(3).strip(),
            "repair_hint": "inspect test oracle and implementation API",
            "source": "pytest_assert_frame",
        })

    deduped: list[dict[str, Any]] = []
    seen = set()
    for item in issues:
        key = (
            item.get("type"),
            item.get("symbol"),
            item.get("module"),
            item.get("file"),
            item.get("line"),
            item.get("message"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
