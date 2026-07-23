from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from coding_agent.workspace.run_paths import is_test_like_path

def _is_test_path(rel: str) -> bool:
    return is_test_like_path(rel)


def _py_files(workspace: str, state: dict[str, Any] | None = None) -> list[str]:
    root = Path(workspace).resolve()
    out: list[str] = []
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(root)).replace("\\", "/")
        if "__pycache__" in rel:
            continue
        if rel.startswith(".coding_agent/") or "/.coding_agent/" in rel:
            continue
        out.append(rel)
    return sorted(out)


def _read_ast(workspace: str, rel: str) -> ast.AST | None:
    try:
        text = (Path(workspace) / rel).read_text(encoding="utf-8", errors="replace")
        return ast.parse(text, filename=rel)
    except Exception:
        return None


def _defined_symbols(tree: ast.AST | None) -> set[str]:
    if tree is None:
        return set()
    out: set[str] = set()

    def target_names(target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, (ast.Tuple, ast.List)):
            return {name for item in target.elts for name in target_names(item)}
        return set()

    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                out.update(target_names(target))
        elif isinstance(node, ast.AnnAssign):
            out.update(target_names(node.target))
        elif isinstance(node, ast.ImportFrom):
            # Explicit imports bind names on the module and may intentionally
            # re-export them. ``__all__`` does not restrict explicit imports.
            out.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Import):
            out.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
    return out


def _module_candidates(module: str, py_files: list[str], state: dict[str, Any] | None = None, test_rel: str | None = None) -> list[str]:
    mod_path = module.replace(".", "/") + ".py"
    base = module.split(".")[-1] + ".py"
    out: list[str] = []
    for rel in py_files:
        if rel == mod_path or rel.endswith("/" + mod_path) or Path(rel).name == base:
            out.append(rel)
    return out


def _imported_from_modules(tree: ast.AST | None) -> list[dict[str, Any]]:
    if tree is None:
        return []
    imports: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [alias.name for alias in node.names if alias.name != "*"]
            if names:
                imports.append({"module": node.module, "names": names, "lineno": getattr(node, "lineno", None)})
    return imports


def run_interface_consistency_check(workspace: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check whether generated tests import symbols that target modules define.

    This is deliberately generic: it does not know project semantics. It only
    checks Python import contracts between tests and implementation files.
    """
    state = state or {}
    py_files = _py_files(workspace, state)
    tests = [p for p in py_files if _is_test_path(p)]
    issues: list[dict[str, Any]] = []
    module_defs: dict[str, dict[str, Any]] = {}
    for rel in py_files:
        if _is_test_path(rel):
            continue
        tree = _read_ast(workspace, rel)
        module_defs[rel] = {"symbols": sorted(_defined_symbols(tree)), "tree_ok": tree is not None}

    for test_rel in tests:
        tree = _read_ast(workspace, test_rel)
        for imp in _imported_from_modules(tree):
            cands = _module_candidates(imp["module"], list(module_defs.keys()), state, test_rel)
            if not cands:
                # Do not flag imports that are clearly stdlib/third-party; only
                # local-looking modules with matching files are actionable.
                continue
            # Prefer shortest path / exact basename.
            target = sorted(cands, key=lambda x: (len(Path(x).parts), x))[0]
            symbols = set(module_defs.get(target, {}).get("symbols") or [])
            missing = [name for name in imp["names"] if name not in symbols]
            if missing:
                issues.append({
                    "type": "missing_imported_symbol",
                    "owner": "generated_test_or_interface",
                    "test_file": test_rel,
                    "target_file": target,
                    "module": imp["module"],
                    "missing_symbols": missing,
                    "available_symbols": sorted(symbols)[:200],
                    "lineno": imp.get("lineno"),
                    "message": f"{test_rel} imports {missing} from {imp['module']} but {target} does not define them",
                })

    return {"ok": not issues, "issues": issues, "checked_tests": tests, "checked_modules": sorted(module_defs)}
