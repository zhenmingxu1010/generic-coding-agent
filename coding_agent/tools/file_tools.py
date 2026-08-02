from __future__ import annotations

import ast
import difflib
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from coding_agent.core.schemas import ToolResult
from coding_agent.safety.path_guard import PathGuard
from coding_agent.core.utils import sha16, truncate


SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "node_modules",
    ".coding_agent",
    ".coding_agent_test",
}
BINARY_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".zip", ".pt", ".pth", ".npy", ".npz", ".parquet", ".wav", ".mp3", ".so"}


def _walk_files(root: Path, max_files: int = 10000) -> list[str]:
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root)).replace("\\", "/")
            files.append(rel)
            if len(files) >= max_files:
                return sorted(files)
    return sorted(files)


def list_files(workspace: str, max_files: int = 500) -> ToolResult:
    root = Path(workspace).resolve()
    files = _walk_files(root, max_files=max_files)
    return ToolResult(
        tool="list_files",
        ok=True,
        message="file list truncated" if len(files) >= max_files else "ok",
        data={"files": files, "truncated": len(files) >= max_files},
    )


def filter_files(
    workspace: str,
    glob: str | None = None,
    regex: str | None = None,
    suffixes: list[str] | None = None,
    contains: str | None = None,
    max_matches: int = 200,
) -> ToolResult:
    """Search file *paths*, not file contents."""
    root = Path(workspace).resolve()
    all_files = _walk_files(root, max_files=20000)
    pattern = re.compile(regex) if regex else None
    suffixes = suffixes or []
    matches: list[str] = []
    for rel in all_files:
        ok = True
        if glob:
            ok = fnmatch.fnmatch(rel, glob)
        if ok and pattern:
            ok = bool(pattern.search(rel))
        if ok and suffixes:
            ok = any(rel.endswith(suf) for suf in suffixes)
        if ok and contains:
            ok = contains in rel
        if ok:
            matches.append(rel)
            if len(matches) >= max_matches:
                break
    return ToolResult(tool="filter_files", ok=True, message="ok", data={"matches": matches, "truncated": len(matches) >= max_matches})


def read_file(workspace: str, path: str, start_line: int = 1, limit: int = 220, max_chars: int = 20000) -> ToolResult:
    guard = PathGuard(workspace)
    p = guard.resolve(path)
    if not p.exists():
        return ToolResult(tool="read_file", ok=False, message=f"file not found: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(start_line))
    end = min(len(lines), start + int(limit) - 1)
    snippet = "\n".join(f"{i+1}: {lines[i]}" for i in range(start - 1, end))
    snippet = truncate(snippet, max_chars)
    return ToolResult(tool="read_file", ok=True, message="ok", data={
        "path": path,
        "start_line": start,
        "end_line": end,
        "total_lines": len(lines),
        "sha16": sha16(text),
        "content": snippet,
    })


def read_many_files(workspace: str, paths: list[str], per_file_chars: int = 8000, max_total_chars: int = 40000) -> ToolResult:
    guard = PathGuard(workspace)
    items: list[dict[str, Any]] = []
    total = 0
    for path in paths:
        p = guard.resolve(path)
        if not p.exists() or not p.is_file():
            items.append({"path": path, "ok": False, "message": "not found"})
            continue
        if p.suffix in BINARY_SUFFIXES:
            items.append({"path": path, "ok": False, "message": "binary or skipped suffix"})
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        snippet = truncate(text, per_file_chars)
        if total + len(snippet) > max_total_chars:
            remaining = max(0, max_total_chars - total)
            snippet = truncate(snippet, remaining)
        total += len(snippet)
        items.append({"path": path, "ok": True, "sha16": sha16(text), "chars": len(text), "content": snippet})
        if total >= max_total_chars:
            break
    return ToolResult(tool="read_many_files", ok=True, message="ok", data={"files": items, "total_chars": total})


def _make_diff(path: str, before: str, after: str, max_chars: int = 20000) -> str:
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))
    return truncate(diff, max_chars)


def _nearest_edit_context(content: str, missing_text: str, *, radius: int = 12) -> str:
    """Return nearby current source when an exact edit anchor is stale."""
    lines = content.splitlines()
    needles = [line.strip() for line in missing_text.splitlines() if line.strip()]
    if not lines or not needles:
        return ""
    best_index = 0
    best_score = -1.0
    for index, line in enumerate(lines):
        candidate = line.strip()
        if not candidate:
            continue
        score = max(
            difflib.SequenceMatcher(None, needle, candidate).ratio()
            for needle in needles[:8]
        )
        if score > best_score:
            best_index = index
            best_score = score
    start = max(0, best_index - radius)
    end = min(len(lines), best_index + radius + 1)
    return truncate("\n".join(lines[start:end]), 4000)



def _python_syntax_check(path: str, content: str) -> dict[str, Any]:
    if not str(path).endswith(".py"):
        return {"checked": False, "ok": True}
    try:
        ast.parse(content, filename=path)
        return {"checked": True, "ok": True}
    except SyntaxError as e:
        return {
            "checked": True,
            "ok": False,
            "error_type": e.__class__.__name__,
            "message": str(e),
            "lineno": e.lineno,
            "offset": e.offset,
            "text": (e.text or "").rstrip(),
        }


def write_file(workspace: str, path: str, content: str, create_dirs: bool = True) -> ToolResult:
    guard = PathGuard(workspace)
    p = guard.resolve(path)
    before = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    syntax = _python_syntax_check(path, content)
    if syntax.get("checked") and not syntax.get("ok"):
        return ToolResult(tool="write_file", ok=False, message="write rejected: Python syntax check failed", data={
            "path": path,
            "changed": False,
            "before_sha16": sha16(before),
            "after_sha16": sha16(content),
            "diff": _make_diff(path, before, content),
            "syntax_check": syntax,
            "rejected_write": True,
        })
    if create_dirs:
        p.parent.mkdir(parents=True, exist_ok=True)
    if before == content:
        return ToolResult(tool="write_file", ok=True, message="no-op: content unchanged", data={
            "path": path,
            "changed": False,
            "before_sha16": sha16(before),
            "after_sha16": sha16(content),
            "diff": "",
            "syntax_check": syntax,
        })
    p.write_text(content, encoding="utf-8")
    ok = bool(syntax.get("ok", True))
    return ToolResult(tool="write_file", ok=ok, message="written" if ok else "written but Python syntax check failed", data={
        "path": path,
        "changed": True,
        "before_sha16": sha16(before),
        "after_sha16": sha16(content),
        "diff": _make_diff(path, before, content),
        "syntax_check": syntax,
    })


def edit_file(
    workspace: str,
    path: str,
    old_text: str | None = None,
    new_text: str | None = None,
    expected_replacements: int = 1,
    replacements: list[dict[str, Any]] | None = None,
) -> ToolResult:
    """Apply one or more exact replacements atomically to a single file."""
    guard = PathGuard(workspace)
    p = guard.resolve(path)
    if not p.exists():
        return ToolResult(tool="edit_file", ok=False, message=f"file not found: {path}", data={"path": path})
    before = p.read_text(encoding="utf-8", errors="replace")
    batch_mode = replacements is not None
    if replacements is None:
        replacements = [{
            "old_text": old_text,
            "new_text": new_text,
            "expected_replacements": expected_replacements,
        }]
    if not isinstance(replacements, list) or not replacements:
        return ToolResult(
            tool="edit_file",
            ok=False,
            message="replacements must be a non-empty list",
            data={"path": path, "changed": False, "tool_schema_error": True},
        )

    after = before
    applied: list[dict[str, Any]] = []
    for index, replacement in enumerate(replacements):
        if not isinstance(replacement, dict):
            return ToolResult(
                tool="edit_file",
                ok=False,
                message=f"replacement {index} must be an object",
                data={"path": path, "changed": False, "tool_schema_error": True, "failed_replacement": index},
            )
        old = replacement.get("old_text")
        new = replacement.get("new_text")
        if not isinstance(old, str) or not isinstance(new, str):
            return ToolResult(
                tool="edit_file",
                ok=False,
                message=f"replacement {index} requires string old_text and new_text",
                data={"path": path, "changed": False, "tool_schema_error": True, "failed_replacement": index},
            )
        expected = replacement.get("expected_replacements", 1)
        try:
            expected = int(expected)
        except (TypeError, ValueError):
            return ToolResult(
                tool="edit_file",
                ok=False,
                message=f"replacement {index} expected_replacements must be an integer",
                data={"path": path, "changed": False, "tool_schema_error": True, "failed_replacement": index},
            )
        count = after.count(old)
        if count == 0:
            return ToolResult(
                tool="edit_file",
                ok=False,
                message=f"old_text not found for replacement {index}" if batch_mode else "old_text not found",
                data={
                    "path": path,
                    "changed": False,
                    "failed_replacement": index,
                    "failed_old_text": truncate(old, 3000),
                    "nearest_current_context": _nearest_edit_context(before, old),
                    "current_sha16": sha16(before),
                    "applied_before_failure": applied,
                },
            )
        if expected and count != expected:
            return ToolResult(
                tool="edit_file",
                ok=False,
                message=(
                    f"replacement {index} expected {expected} matches but found {count}"
                    if batch_mode
                    else f"expected {expected} replacements but found {count}"
                ),
                data={
                    "path": path,
                    "changed": False,
                    "found": count,
                    "failed_replacement": index,
                    "applied_before_failure": applied,
                },
            )
        replace_count = expected if expected else count
        after = after.replace(old, new, replace_count)
        applied.append({"index": index, "replacements": replace_count})
    syntax = _python_syntax_check(path, after)
    if syntax.get("checked") and not syntax.get("ok"):
        return ToolResult(tool="edit_file", ok=False, message="edit rejected: Python syntax check failed", data={
            "path": path,
            "changed": False,
            "before_sha16": sha16(before),
            "after_sha16": sha16(after),
            "diff": _make_diff(path, before, after),
            "syntax_check": syntax,
            "rejected_write": True,
            "replacement_results": applied,
        })
    if before == after:
        return ToolResult(tool="edit_file", ok=True, message="no-op: content unchanged", data={"path": path, "changed": False, "before_sha16": sha16(before), "after_sha16": sha16(after), "diff": "", "syntax_check": syntax, "replacement_results": applied})
    p.write_text(after, encoding="utf-8")
    ok = bool(syntax.get("ok", True))
    return ToolResult(tool="edit_file", ok=ok, message="edited" if ok else "edited but Python syntax check failed", data={"path": path, "changed": True, "before_sha16": sha16(before), "after_sha16": sha16(after), "diff": _make_diff(path, before, after), "syntax_check": syntax, "replacement_results": applied})


def search_text(workspace: str, pattern: str, max_matches: int = 80, regex: bool = False) -> ToolResult:
    root = Path(workspace).resolve()
    matches: list[dict[str, Any]] = []
    rx = re.compile(pattern) if regex else None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix in BINARY_SUFFIXES:
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines, 1):
                found = bool(rx.search(line)) if rx else pattern in line
                if found:
                    matches.append({"path": str(p.relative_to(root)), "line": i, "text": line[:500]})
                    if len(matches) >= max_matches:
                        return ToolResult(tool="search_text", ok=True, message="matches truncated", data={"matches": matches, "truncated": True})
    return ToolResult(tool="search_text", ok=True, message="ok", data={"matches": matches, "truncated": False})
