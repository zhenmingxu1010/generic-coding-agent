from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.memory.trace_store import TraceStore
from coding_agent.workspace.run_paths import project_memory_dir_for, run_dir_for


def get_trace(state: dict[str, Any]) -> TraceStore:
    return TraceStore(state["trace_path"], state.get("state_snapshot_path"))


def ensure_run_artifacts(state: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(state["workspace"]).resolve()
    run_dir = run_dir_for(workspace, state.get("thread_id", "default"))
    patches_dir = run_dir / "patches"
    run_dir.mkdir(parents=True, exist_ok=True)
    patches_dir.mkdir(parents=True, exist_ok=True)
    state.setdefault("run_dir", str(run_dir))
    state.setdefault("trace_path", str(run_dir / "trace.jsonl"))
    state.setdefault("messages_path", str(run_dir / "messages.jsonl"))
    state.setdefault("context_pack_path", str(run_dir / "context_pack.json"))
    state.setdefault("context_summary_path", str(run_dir / "context_summary.md"))
    state.setdefault("state_snapshot_path", str(run_dir / "state_snapshot.json"))
    state.setdefault("patches_dir", str(patches_dir))
    state.setdefault("final_path", str(run_dir / "final.json"))
    # Shared long-term project memory is agent-owned metadata. Keep it outside
    # the user's project tree so read-only tasks and generated-code tasks do not
    # pollute the workspace.
    project_memory_dir = project_memory_dir_for(workspace)
    project_memory_dir.mkdir(parents=True, exist_ok=True)
    state.setdefault("project_memory_dir", str(project_memory_dir))
    state.setdefault("project_profile_path", str(project_memory_dir / "project_profile.json"))
    state.setdefault("long_term_memory_path", str(project_memory_dir / "project_profile.md"))
    state.setdefault("short_term_memory_path", str(run_dir / "short_term_memory.md"))
    state.setdefault("repository_map_path", str(run_dir / "repository_map.json"))
    state.setdefault("artifact_provenance_path", str(project_memory_dir / "artifact_provenance.json"))
    return state
