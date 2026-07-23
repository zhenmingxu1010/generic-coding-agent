from __future__ import annotations

import json
from typing import Any

from coding_agent.memory.artifact_provenance import load_artifact_provenance
from coding_agent.workspace.run_paths import agent_test_root_rel, is_agent_test_path
from coding_agent.workspace.run_paths import normalize_rel
from coding_agent.memory.workspace_baseline import baseline_path


def _repair_changed_files(state: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in state.get("repair_history") or []:
        if not isinstance(item, dict) or not item.get("changed"):
            continue
        for rel in item.get("files_changed") or []:
            norm = normalize_rel(str(rel))
            if norm:
                out.append(norm)
    return out


def _generated_paths(state: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in state.get("generated_files") or []:
        if not isinstance(item, dict) or item.get("ok") is False:
            continue
        norm = normalize_rel(str(item.get("path") or ""))
        if norm:
            out.append(norm)
    return out


def _unique(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        norm = normalize_rel(path)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _agent_created_artifact(provenance: dict[str, Any] | None) -> bool:
    """Return whether provenance shows the file was created by an agent.

    This is intentionally narrower than "agent touched this path": a file that
    existed in the project and was later modified by an agent remains an
    existing project modification. Only agent-created deliverables are excluded
    from the "user project original was modified" audit bucket.
    """
    if not provenance or not provenance.get("created_by_agent"):
        return False
    origin = str(provenance.get("origin") or "")
    if origin.startswith("agent_generated"):
        return True
    events = provenance.get("events") or []
    first = events[0] if events and isinstance(events[0], dict) else {}
    before = str(first.get("before_sha16") or "")
    return before in {"", "e3b0c44298fc1c14"}


def build_write_scope_audit(state: dict[str, Any]) -> dict[str, Any]:
    """Summarize whether this run changed user-visible project files."""
    changed_files = _unique([normalize_rel(str(x)) for x in state.get("changed_files") or []])
    generated_files = _unique(_generated_paths(state))
    repair_changed_files = _unique(_repair_changed_files(state))
    all_recorded = _unique(changed_files + generated_files + repair_changed_files)
    agent_test_root = normalize_rel(agent_test_root_rel(state=state))
    agent_test_files = [path for path in all_recorded if is_agent_test_path(path, state=state)]
    source_changed_files = [
        path
        for path in all_recorded
        if path and not is_agent_test_path(path, state=state)
    ]
    workspace = state.get("workspace")
    baseline_known = False
    existing_project_modified_files: list[str] = []
    new_project_files: list[str] = []
    baseline_files: dict[str, Any] = {}
    provenance_files: dict[str, Any] = {}
    if workspace:
        try:
            path = baseline_path(workspace)
            if path.exists():
                baseline = json.loads(path.read_text(encoding="utf-8"))
                baseline_files = baseline.get("files") or {}
                baseline_known = True
        except Exception:
            baseline_files = {}
        try:
            provenance_files = (load_artifact_provenance(workspace).get("artifacts") or {})
        except Exception:
            provenance_files = {}
        for path in source_changed_files:
            if path in baseline_files and not _agent_created_artifact(provenance_files.get(path)):
                existing_project_modified_files.append(path)
            else:
                new_project_files.append(path)
    else:
        existing_project_modified_files = list(source_changed_files)
    return {
        "version": "write_scope_audit_v1",
        "agent_test_root": agent_test_root,
        "workspace_baseline_known": baseline_known,
        "changed_files": changed_files,
        "generated_files": generated_files,
        "repair_changed_files": repair_changed_files,
        "all_recorded_changes": all_recorded,
        "agent_test_changed_files": agent_test_files,
        "source_changed_files": source_changed_files,
        "existing_project_modified_files": existing_project_modified_files,
        "new_project_files": new_project_files,
        "source_changed_count": len(source_changed_files),
        "agent_test_changed_count": len(agent_test_files),
        "no_existing_project_modification": len(existing_project_modified_files) == 0,
    }
