from __future__ import annotations

import os

from coding_agent.workspace.repo_map import build_repository_map
from coding_agent.workspace.artifacts import build_artifact_registry
from coding_agent.scope.scope_grounding import ground_scope_contract_to_repo
from coding_agent.memory.project_memory import update_project_memory_from_repo
from coding_agent.memory.artifact_provenance import load_artifact_provenance
from coding_agent.memory.workspace_baseline import ensure_workspace_baseline
from coding_agent.core.utils import write_json
from .common import get_trace


def repo_scan_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("repo_scan_start")
    max_files = int(os.getenv("AGENT_REPO_MAP_MAX_FILES", "20000"))
    baseline = ensure_workspace_baseline(state["workspace"])
    state["workspace_baseline"] = baseline
    repo_map = build_repository_map(state["workspace"], max_files=max_files)
    state["repo_map"] = repo_map
    scope_grounding = ground_scope_contract_to_repo(state)
    if state.get("repository_map_path"):
        write_json(state["repository_map_path"], repo_map)
    state["artifact_provenance"] = load_artifact_provenance(state["workspace"])
    state["artifact_registry"] = build_artifact_registry(state)
    project_memory = update_project_memory_from_repo(state)
    state["project_memory"] = project_memory
    trace.event(
        "repo_scan_done",
        file_count=len(repo_map.get("files", [])),
        py_count=len(repo_map.get("py_files", [])),
        project_types=repo_map.get("project_types", []),
        roles=list((repo_map.get("candidates_by_role") or {}).keys()),
        repository_map_path=state.get("repository_map_path"),
        project_memory_path=state.get("project_profile_path"),
        memory_facts=len(project_memory.get("stable_facts", [])),
        artifact_counts={"entries": len((state.get("artifact_registry") or {}).get("entries", [])), "agent_generated_tests": len((state.get("artifact_registry") or {}).get("agent_generated_tests", []))},
        baseline_file_count=len((baseline.get("files") or {})),
        scope_grounding=scope_grounding,
    )
    trace.snapshot(state)
    return state
