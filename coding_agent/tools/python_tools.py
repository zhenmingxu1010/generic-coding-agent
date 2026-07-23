from __future__ import annotations

import ast
from pathlib import Path

from coding_agent.core.schemas import ToolResult
from coding_agent.safety.path_guard import PathGuard


def inspect_python(workspace: str, path: str) -> ToolResult:
    guard = PathGuard(workspace)
    p = guard.resolve(path)
    if not p.exists():
        return ToolResult(tool="inspect_python", ok=False, message=f"file not found: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return ToolResult(tool="inspect_python", ok=False, message="syntax_error", data={
            "path": path,
            "lineno": e.lineno,
            "offset": e.offset,
            "msg": e.msg,
            "text": e.text,
        })
    imports, functions, classes = [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.FunctionDef):
            functions.append({"name": node.name, "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            classes.append({"name": node.name, "line": node.lineno})
    return ToolResult(tool="inspect_python", ok=True, message="ok", data={
        "path": path,
        "imports": imports,
        "functions": functions,
        "classes": classes,
    })
