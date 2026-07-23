from __future__ import annotations

import re
from pathlib import Path
from typing import Any


MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]")
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".coding_agent",
    ".coding_agent_test",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "env",
}


def _norm(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _verification_results(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in (state.get("verification") or {}).get("results") or [] if isinstance(r, dict)]


def _verification_text(state: dict[str, Any], limit: int = 20000) -> str:
    chunks: list[str] = []
    for result in _verification_results(state):
        chunks.append("COMMAND: " + " ".join(str(x) for x in (result.get("command") or [])))
        chunks.append(str(result.get("stdout") or ""))
        chunks.append(str(result.get("stderr") or ""))
    return "\n".join(chunks)[:limit]


def _extract_missing_modules(state: dict[str, Any]) -> list[str]:
    modules: list[str] = []
    for source in (state.get("traceback_issues") or [], state.get("failure_issues") or []):
        for issue in source or []:
            if not isinstance(issue, dict):
                continue
            module = issue.get("missing_module")
            if not module and str(issue.get("exception_type") or "") == "ModuleNotFoundError":
                module = issue.get("module")
            if module and str(module) not in modules:
                modules.append(str(module))
            for match in MISSING_MODULE_RE.finditer(str(issue.get("message") or "")):
                if match.group(1) not in modules:
                    modules.append(match.group(1))
    for match in MISSING_MODULE_RE.finditer(_verification_text(state)):
        if match.group(1) not in modules:
            modules.append(match.group(1))
    return modules


def _failed_commands(state: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for result in _verification_results(state):
        if int(result.get("returncode", 0) or 0) == 0:
            continue
        out.append({
            "name": result.get("name"),
            "command": [str(x) for x in (result.get("command") or [])],
            "returncode": result.get("returncode"),
            "output_excerpt": (str(result.get("stdout") or "") + "\n" + str(result.get("stderr") or ""))[:3000],
        })
    return out


def _is_skipped(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    return any(part in SKIP_DIRS for part in rel.parts)


def _package_dirs(root: Path, limit: int = 120) -> list[str]:
    if not root.exists():
        return []
    out: list[str] = []
    for init_file in root.rglob("__init__.py"):
        if _is_skipped(init_file, root):
            continue
        try:
            rel = init_file.parent.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel and rel != "." and rel not in out:
            out.append(rel)
        if len(out) >= limit:
            break
    return sorted(out)


def _module_path_candidates(root: Path, module: str) -> list[dict[str, Any]]:
    parts = [p for p in module.split(".") if p]
    if not parts:
        return []
    rel = Path(*parts)
    candidates: list[dict[str, Any]] = []
    checked: set[str] = set()
    for base in [Path("."), Path("src")]:
        for suffix, kind in [
            (rel.with_suffix(".py"), "module_file"),
            (rel / "__init__.py", "package_init"),
            (rel / "__main__.py", "package_main"),
        ]:
            candidate = (base / suffix).as_posix()
            if candidate in checked:
                continue
            checked.add(candidate)
            if (root / candidate).exists():
                candidates.append({"path": candidate, "kind": kind, "exists": True})
        package_dir = root / base / rel
        if package_dir.is_dir():
            for child in sorted(package_dir.glob("*.py"))[:20]:
                if child.name in {"__init__.py", "__main__.py"}:
                    continue
                candidate = (base / rel / child.name).as_posix()
                if candidate in checked:
                    continue
                checked.add(candidate)
                text = child.read_text(encoding="utf-8", errors="replace")[:4000]
                candidates.append({
                    "path": candidate,
                    "kind": "package_python_file",
                    "exists": True,
                    "signals": {
                        "defines_main": "def main" in text,
                        "has_argument_parser": "argparse" in text or "click" in text or "typer" in text,
                    },
                })
    return candidates


def _project_config(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"files": [], "pyproject": {}}
    for name in ["pyproject.toml", "setup.cfg", "setup.py", "requirements.txt"]:
        if (root / name).exists():
            out["files"].append(name)
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        out["pyproject"] = {
            "has_project_scripts": "[project.scripts]" in low,
            "has_build_system": "[build-system]" in low,
            "mentions_package_dir": "package-dir" in low or "package_dir" in low,
            "mentions_find_where": "where" in low and "find" in low,
            "mentions_non_root_package_layout": "src" in low,
        }
    return out


def _matching_package_dirs(package_dirs: list[str], module: str) -> list[str]:
    top = module.split(".", 1)[0]
    matches: list[str] = []
    for rel in package_dirs:
        parts = rel.split("/")
        if parts and parts[-1] == top:
            matches.append(rel)
    return matches[:30]


def _commands_related_to_module(commands: list[dict[str, Any]], module: str) -> list[dict[str, Any]]:
    related: list[dict[str, Any]] = []
    top = module.split(".", 1)[0]
    for command in commands:
        haystack = " ".join(command.get("command") or []) + "\n" + str(command.get("output_excerpt") or "")
        if module in haystack or top in haystack:
            related.append(command)
    return related[:8]


def _module_facts(root: Path, module: str, package_dirs: list[str], path_candidates: list[dict[str, Any]]) -> list[str]:
    facts: list[str] = []
    matches = _matching_package_dirs(package_dirs, module)
    if matches:
        facts.append("A package directory with the missing top-level name exists in the workspace.")
    if any(str(item.get("path") or "").startswith("src/") for item in path_candidates + [{"path": m} for m in matches]):
        facts.append("The matching package appears below a non-root source directory.")
    if not matches and not path_candidates:
        facts.append("No package directory or module file with the missing top-level name was found in the scanned workspace.")
    if root.exists():
        facts.append("The failing command should be checked against the workspace root, package layout, and public entrypoint expected by the task.")
    return facts


def build_import_error_context(state: dict[str, Any]) -> dict[str, Any]:
    """Build generic evidence for ModuleNotFoundError repair.

    This intentionally does not choose a fixed repair. It packages facts that
    help the LLM decide whether to change implementation imports, project
    metadata, entrypoints, tests, or verification commands.
    """
    modules = _extract_missing_modules(state)
    if not modules:
        return {"present": False, "version": "generic_import_error_context_v1", "missing_modules": []}

    root = Path(str(state.get("workspace") or ".")).resolve()
    packages = _package_dirs(root)
    commands = _failed_commands(state)
    module_contexts: list[dict[str, Any]] = []
    for module in modules:
        path_candidates = _module_path_candidates(root, module)
        matches = _matching_package_dirs(packages, module)
        module_contexts.append({
            "module": module,
            "top_level_name": module.split(".", 1)[0],
            "candidate_package_dirs": matches,
            "module_path_candidates": path_candidates,
            "failed_commands": _commands_related_to_module(commands, module),
            "facts": _module_facts(root, module, packages, path_candidates),
            "questions_for_llm": [
                "Is the missing module part of this generated/project code, an external dependency, or a test-only import?",
                "Does the failing command run from the intended workspace and expose the package on Python's import path?",
                "Should the public entrypoint, imports, project metadata, test oracle, or verification command be changed for this project?",
            ],
        })

    return {
        "present": True,
        "version": "generic_import_error_context_v1",
        "workspace": str(root),
        "project_config": _project_config(root),
        "package_dirs_sample": packages[:50],
        "missing_modules": module_contexts,
        "guidance": (
            "Use these facts as evidence only. Do not apply a fixed import-path recipe; "
            "choose the smallest project-appropriate repair and verify it."
        ),
    }
