from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from coding_agent.core.utils import sha16
from coding_agent.workspace.run_paths import project_memory_dir_for

SKIP_DIRS = {
    ".coding_agent",
    ".coding_agent_test",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "node_modules",
}
TEXT_LIMIT = 2_000_000


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def baseline_path(workspace: str | Path) -> Path:
    return project_memory_dir_for(workspace) / "workspace_baseline.json"


def _file_sha(path: Path) -> str | None:
    try:
        if path.stat().st_size <= TEXT_LIMIT:
            return sha16(path.read_bytes())
    except Exception:
        return None
    return None


def _scan_workspace(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    files: dict[str, Any] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                rel = _norm(str(p.relative_to(root)))
                st = p.stat()
            except Exception:
                continue
            files[rel] = {
                "path": rel,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "suffix": p.suffix.lower(),
                "sha16": _file_sha(p),
            }
    return files


def load_workspace_baseline(workspace: str | Path) -> dict[str, Any]:
    path = baseline_path(workspace)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("files", {})
            return data
        except Exception:
            pass
    return ensure_workspace_baseline(workspace)


def ensure_workspace_baseline(workspace: str | Path) -> dict[str, Any]:
    """Create an immutable-ish snapshot of project files before the agent writes.

    This is the main way to distinguish project-existing files from files later
    created by the agent. It intentionally excludes .coding_agent and other
    transient directories. Existing baseline is preserved instead of refreshed,
    so later agent files do not become project originals.
    """
    path = baseline_path(workspace)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("files", {})
            return data
        except Exception:
            pass
    data = {
        "version": "v1.17",
        "workspace": str(Path(workspace).resolve()),
        "created_at": _now(),
        "files": _scan_workspace(workspace),
        "policy": "baseline files are project_existing until proven otherwise by artifact_provenance",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def baseline_record_for_path(workspace: str | Path, path: str) -> dict[str, Any] | None:
    rel = _norm(path)
    return (load_workspace_baseline(workspace).get("files") or {}).get(rel)
