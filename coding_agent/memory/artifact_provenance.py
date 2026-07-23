from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from coding_agent.workspace.run_paths import project_memory_dir_for


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def provenance_path(workspace: str | Path) -> Path:
    return project_memory_dir_for(workspace) / "artifact_provenance.json"


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _base_store(workspace: str | Path) -> dict[str, Any]:
    return {
        "version": "v1.16",
        "workspace": str(Path(workspace).resolve()),
        "updated_at": _now(),
        "artifacts": {},
    }


def load_artifact_provenance(workspace: str | Path) -> dict[str, Any]:
    path = provenance_path(workspace)
    data = _load_json(path, _base_store(workspace))
    data.setdefault("version", "v1.16")
    data.setdefault("workspace", str(Path(workspace).resolve()))
    data.setdefault("artifacts", {})
    return data


def save_artifact_provenance(workspace: str | Path, store: dict[str, Any]) -> None:
    store.setdefault("version", "v1.16")
    store["workspace"] = str(Path(workspace).resolve())
    store["updated_at"] = _now()
    store.setdefault("artifacts", {})
    _write_json(provenance_path(workspace), store)


def record_artifact_event(
    workspace: str | Path,
    *,
    path: str,
    thread_id: str | None,
    task: str | None,
    action: str,
    origin: str = "agent_generated",
    safe_to_modify_by_future_agent: bool = True,
    kind: str | None = None,
    before_sha16: str | None = None,
    after_sha16: str | None = None,
) -> dict[str, Any]:
    rel = _norm(path)
    store = load_artifact_provenance(workspace)
    artifacts = store.setdefault("artifacts", {})
    rec = artifacts.get(rel) or {
        "path": rel,
        "created_by_agent": origin.startswith("agent"),
        "origin": origin,
        "first_seen_thread_id": thread_id,
        "first_seen_at": _now(),
        "safe_to_modify_by_future_agent": bool(safe_to_modify_by_future_agent),
        "events": [],
    }
    rec.update({
        "path": rel,
        "origin": origin if rec.get("origin") in {None, "external"} else rec.get("origin", origin),
        "created_by_agent": bool(rec.get("created_by_agent", origin.startswith("agent")) or origin.startswith("agent")),
        "safe_to_modify_by_future_agent": bool(rec.get("safe_to_modify_by_future_agent", safe_to_modify_by_future_agent)),
        "kind": kind or rec.get("kind"),
        "last_thread_id": thread_id,
        "last_updated_at": _now(),
        "before_sha16": before_sha16 or rec.get("before_sha16"),
        "after_sha16": after_sha16 or rec.get("after_sha16"),
    })
    rec.setdefault("events", []).append({
        "ts": _now(),
        "thread_id": thread_id,
        "action": action,
        "task": (task or "")[:500],
        "before_sha16": before_sha16,
        "after_sha16": after_sha16,
    })
    rec["events"] = rec["events"][-30:]
    artifacts[rel] = rec
    save_artifact_provenance(workspace, store)
    return rec


def artifact_record_for_path(workspace: str | Path, path: str) -> dict[str, Any] | None:
    rel = _norm(path)
    return (load_artifact_provenance(workspace).get("artifacts") or {}).get(rel)
