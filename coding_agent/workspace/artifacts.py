from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.workspace.run_paths import is_agent_test_path, is_test_like_path
from coding_agent.memory.artifact_provenance import load_artifact_provenance
from coding_agent.memory.workspace_baseline import load_workspace_baseline
from coding_agent.scope.write_guard import is_protected_existing_file


def _norm(rel: str | None) -> str:
    out = str(rel or "").replace("\\", "/")
    while out.startswith("./"):
        out = out[2:]
    return out


def _is_test_path(rel: str) -> bool:
    return is_test_like_path(rel)


def _kind_from_path(rel: str, planned_kind: str | None = None) -> str:
    rel = _norm(rel)
    if planned_kind and not (planned_kind == "test" and not _is_test_path(rel)):
        return planned_kind
    name = Path(rel).name.lower()
    suffix = Path(rel).suffix.lower()
    if _is_test_path(rel):
        return "test"
    if name.startswith("readme") or suffix in {".md", ".rst"}:
        return "readme"
    if suffix == ".py":
        return "code"
    if suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}:
        return "config"
    return "other"


def _modifiable_by_policy(rel: str, kind: str, origin: str, state: dict[str, Any], provenance_rec: dict[str, Any] | None = None) -> bool:
    if state.get("read_only"):
        return False
    if provenance_rec and provenance_rec.get("safe_to_modify_by_future_agent") and not is_protected_existing_file(rel):
        return True
    if origin == "agent_generated_or_modified" and not is_protected_existing_file(rel):
        return True
    # Project-existing tests and protected artifacts are not modifiable by default.
    if kind == "test" or _is_test_path(rel):
        return False
    if is_protected_existing_file(rel):
        return False
    # In modify/debug modes, source code files may be edited; write intent still
    # makes the final tool decision. This field is a registry hint, not the gate.
    if kind == "code" and state.get("mode") in {"modify", "debug", "repair_existing"}:
        return True
    return False


def build_artifact_registry(state: dict[str, Any]) -> dict[str, Any]:
    repo_files = [_norm(x) for x in list((state.get("repo_map") or {}).get("files") or [])]
    if state.get("workspace"):
        provenance = load_artifact_provenance(state["workspace"])
        baseline = load_workspace_baseline(state["workspace"])
    else:
        provenance = {"version": "v1.17", "artifacts": {}}
        baseline = {"files": {}}
    prov_artifacts = provenance.get("artifacts") or {}
    baseline_files = baseline.get("files") or {}

    planned: dict[str, dict[str, Any]] = {}
    for item in (state.get("file_plan") or {}).get("files") or []:
        path = _norm(item.get("path"))
        if path:
            planned[path] = item
    generated_paths = {_norm(x.get("path")) for x in state.get("generated_files", []) if x.get("path")}
    changed_paths = {_norm(x) for x in state.get("changed_files", [])}
    for rec in state.get("repair_history", []) or []:
        for p in rec.get("files_changed", []) or []:
            changed_paths.add(_norm(p))

    entries: list[dict[str, Any]] = []
    all_paths = sorted(set(repo_files) | set(planned) | generated_paths | changed_paths | set(prov_artifacts.keys()))
    for rel in all_paths:
        if not rel:
            continue
        if rel.startswith(".coding_agent/") or "/.coding_agent/" in rel:
            continue
        if ".coding_agent_test/" in rel and not is_agent_test_path(rel, state=state):
            continue
        planned_item = planned.get(rel) or {}
        prov = prov_artifacts.get(rel)
        origin = "project_existing" if rel in baseline_files else "untracked_external_or_new"
        if prov and prov.get("created_by_agent"):
            origin = "agent_generated_or_modified"
        elif rel in generated_paths or rel in changed_paths or rel in planned:
            origin = "agent_generated_or_modified"
        kind = _kind_from_path(rel, planned_item.get("kind") or (prov or {}).get("kind"))
        entries.append({
            "path": rel,
            "kind": kind,
            "origin": origin,
            "agent_planned": rel in planned,
            "agent_generated": rel in generated_paths or bool(prov and prov.get("origin", "").startswith("agent_generated")),
            "agent_changed": rel in changed_paths or bool(prov and prov.get("origin", "").startswith("agent_modified")),
            "is_test": kind == "test" or _is_test_path(rel),
            "provenance": prov,
            "historical_agent_artifact": bool(prov and prov.get("created_by_agent")),
            "modifiable_by_agent": _modifiable_by_policy(rel, kind, origin, state, prov),
        })

    by_path = {e["path"]: e for e in entries}
    return {
        "version": "v1.17",
        "entries": entries,
        "by_path": by_path,
        "agent_generated_tests": [e["path"] for e in entries if e["is_test"] and e["origin"] == "agent_generated_or_modified"],
        "external_tests": [e["path"] for e in entries if e["is_test"] and e["origin"] != "agent_generated_or_modified"],
        "code_files": [e["path"] for e in entries if e["kind"] == "code"],
        "test_files": [e["path"] for e in entries if e["is_test"]],
        "artifact_provenance_path": state.get("artifact_provenance_path"),
    }


def artifact_for_path(registry: dict[str, Any], path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    norm = _norm(path)
    return (registry.get("by_path") or {}).get(norm)
