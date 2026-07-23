from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coding_agent.workspace.repo_map import ROLES
from coding_agent.core.utils import now_iso, truncate, write_json, read_json
from coding_agent.contracts.analysis_contract import extract_structured_memory
from coding_agent.memory.artifact_provenance import load_artifact_provenance
from coding_agent.workspace.run_paths import project_memory_dir_for


def memory_paths(workspace: str | Path) -> dict[str, Path]:
    mem_dir = project_memory_dir_for(workspace)
    return {
        "dir": mem_dir,
        "profile_json": mem_dir / "project_profile.json",
        "profile_md": mem_dir / "project_profile.md",
        "architecture_memory_md": mem_dir / "architecture_memory.md",
        "known_commands_json": mem_dir / "known_commands.json",
        "known_failures_jsonl": mem_dir / "known_failures.jsonl",
        "artifact_provenance_json": mem_dir / "artifact_provenance.json",
    }


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return read_json(path)
    except Exception:
        return default
    return default


def load_project_memory(workspace: str | Path) -> dict[str, Any]:
    paths = memory_paths(workspace)
    profile = _load_json(paths["profile_json"], {})
    profile.setdefault("version", "v1.13")
    profile.setdefault("created_at", now_iso())
    profile.setdefault("updated_at", now_iso())
    profile.setdefault("project_types", [])
    profile.setdefault("file_count", 0)
    profile.setdefault("py_file_count", 0)
    profile.setdefault("role_files", {})
    profile.setdefault("known_entrypoints", [])
    profile.setdefault("known_tests", [])
    profile.setdefault("known_results", [])
    profile.setdefault("known_configs", [])
    profile.setdefault("known_commands", [])
    profile.setdefault("analysis_runs", [])
    profile.setdefault("task_summaries", [])
    profile.setdefault("stable_facts", [])
    profile.setdefault("structured_memory", {})
    profile["artifact_provenance"] = load_artifact_provenance(workspace)
    return profile


def _top_paths_for_role(repo_map: dict[str, Any], role: str, n: int = 8) -> list[str]:
    return [x.get("path") for x in (repo_map.get("candidates_by_role") or {}).get(role, [])[:n] if x.get("path")]


def update_project_memory_from_repo(state: dict[str, Any]) -> dict[str, Any]:
    workspace = state["workspace"]
    paths = memory_paths(workspace)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    repo_map = state.get("repo_map") or {}
    profile = load_project_memory(workspace)
    profile["updated_at"] = now_iso()
    profile["workspace"] = str(Path(workspace).resolve())
    profile["project_types"] = repo_map.get("project_types", [])
    profile["file_count"] = len(repo_map.get("files", []) or [])
    profile["py_file_count"] = len(repo_map.get("py_files", []) or [])
    profile["role_files"] = {role: _top_paths_for_role(repo_map, role) for role in ROLES}
    profile["known_entrypoints"] = sorted(set(_top_paths_for_role(repo_map, "entrypoint", 12) + _top_paths_for_role(repo_map, "run_workflow", 12)))[:24]
    profile["known_tests"] = sorted(set(_top_paths_for_role(repo_map, "tests", 20)))[:24]
    profile["known_results"] = sorted(set(_top_paths_for_role(repo_map, "results_or_outputs", 20)))[:24]
    profile["known_configs"] = sorted(set(_top_paths_for_role(repo_map, "config_or_arguments", 20)))[:24]
    # Known command candidates are shell or Python entrypoint files; actual commands remain evidence-derived.
    profile["known_command_sources"] = profile["known_entrypoints"][:20]
    # Keep a lightweight structured memory from repository-level signals even before any analysis report.
    profile.setdefault("structured_memory", {})
    profile["artifact_provenance"] = load_artifact_provenance(workspace)
    repo_structured = extract_structured_memory({"repo_map": repo_map})
    profile["structured_memory"] = _merge_structured_memory(profile.get("structured_memory") or {}, repo_structured)
    write_project_memory(workspace, profile)
    return profile


def _append_unique_run(profile: dict[str, Any], run: dict[str, Any], key: str, limit: int = 20) -> None:
    items = list(profile.get(key) or [])
    sig = (run.get("thread_id"), run.get("task_sha") or run.get("task"))
    if not any((x.get("thread_id"), x.get("task_sha") or x.get("task")) == sig for x in items):
        items.append(run)
    profile[key] = items[-limit:]


def update_project_memory_from_analysis(state: dict[str, Any]) -> dict[str, Any]:
    workspace = state["workspace"]
    profile = load_project_memory(workspace)
    ctx = state.get("repo_analysis_context") or {}
    quality = state.get("analysis_quality") or {}
    report = state.get("analysis_report") or ""
    run = {
        "ts": now_iso(),
        "thread_id": state.get("thread_id"),
        "mode": state.get("mode"),
        "task": truncate(state.get("task", ""), 500),
        "selected_files": ctx.get("selected_files", [])[:60],
        "coverage": ctx.get("role_coverage_after"),
        "analysis_quality": quality,
        "analysis_report_path": str(Path(state.get("run_dir", "")) / "analysis_report.md") if state.get("run_dir") else None,
    }
    _append_unique_run(profile, run, "analysis_runs")
    if report:
        facts = _extract_stable_facts_from_report(report, ctx)
        profile["stable_facts"] = _merge_facts(profile.get("stable_facts") or [], facts, limit=120)
    structured_from_ctx = ctx.get("structured_memory") or state.get("structured_memory") or extract_structured_memory(state)
    profile["structured_memory"] = _merge_structured_memory(profile.get("structured_memory") or {}, structured_from_ctx)
    write_project_memory(workspace, profile)
    return profile


def update_project_memory_from_final(state: dict[str, Any]) -> dict[str, Any]:
    workspace = state["workspace"]
    profile = load_project_memory(workspace)
    verification = state.get("verification") or {}
    run = {
        "ts": now_iso(),
        "thread_id": state.get("thread_id"),
        "mode": state.get("mode"),
        "ok": state.get("final_ok"),
        "stopped_reason": state.get("stopped_reason"),
        "task": truncate(state.get("task", ""), 500),
        "changed_files": state.get("changed_files", [])[:80],
        "verification_ok": verification.get("ok"),
        "pytest_ok": verification.get("pytest_ok"),
        "compile_ok": verification.get("compile_ok"),
        "failure_owner": state.get("failure_owner"),
    }
    _append_unique_run(profile, run, "task_summaries")
    # Preserve repeated failure signatures as long-term lessons.
    failures = []
    for f in state.get("failure_history", [])[-6:]:
        if isinstance(f, dict):
            failures.append({
                "ts": now_iso(),
                "thread_id": state.get("thread_id"),
                "mode": state.get("mode"),
                "failure_type": f.get("failure_type"),
                "target_file": f.get("target_file"),
                "signature": f.get("signature"),
                "message": truncate(str(f.get("message", "")), 500),
            })
    if failures:
        paths = memory_paths(workspace)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        with paths["known_failures_jsonl"].open("a", encoding="utf-8") as fh:
            for item in failures:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    write_project_memory(workspace, profile)
    return profile


def _extract_stable_facts_from_report(report: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
    selected = set(ctx.get("selected_files") or [])
    facts: list[dict[str, Any]] = []
    for line in report.splitlines():
        clean = line.strip().lstrip("-* ")
        if len(clean) < 30 or len(clean) > 500:
            continue
        evidence = [p for p in selected if p in clean]
        if evidence:
            facts.append({"fact": clean, "evidence": evidence[:5], "source": "analysis_report"})
    return facts[:80]


def _merge_facts(old: list[dict[str, Any]], new: list[dict[str, Any]], limit: int = 120) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen = set()
    for item in list(old) + list(new):
        fact = str(item.get("fact", "")).strip()
        if not fact or fact in seen:
            continue
        seen.add(fact)
        out.append(item)
    return out[-limit:]



def _merge_structured_memory(old: dict[str, Any], new: dict[str, Any], limit: int = 160) -> dict[str, Any]:
    out = dict(old or {})
    for key, vals in (new or {}).items():
        if isinstance(vals, list):
            merged = []
            for x in list(out.get(key) or []) + vals:
                if isinstance(x, str) and x.strip() and x not in merged:
                    merged.append(x)
            out[key] = merged[-limit:]
        elif vals and key not in out:
            out[key] = vals
    out["version"] = "v1.13"
    return out

def project_memory_markdown(profile: dict[str, Any]) -> str:
    parts = ["# Long-Term Project Memory", ""]
    parts.append(f"- workspace: {profile.get('workspace', '')}")
    parts.append(f"- updated_at: {profile.get('updated_at', '')}")
    parts.append(f"- project_types: {profile.get('project_types', [])}")
    parts.append(f"- file_count: {profile.get('file_count')} | py_file_count: {profile.get('py_file_count')}")
    parts.append("\n## Role Files")
    for role, files in (profile.get("role_files") or {}).items():
        if files:
            parts.append(f"\n### {role}")
            parts.extend(f"- {p}" for p in files[:10])
    parts.append("\n## Known Entry Points / Commands Sources")
    parts.extend(f"- {p}" for p in profile.get("known_entrypoints", [])[:20])
    parts.append("\n## Known Tests")
    parts.extend(f"- {p}" for p in profile.get("known_tests", [])[:20])
    parts.append("\n## Known Results / Summaries")
    parts.extend(f"- {p}" for p in profile.get("known_results", [])[:20])
    parts.append("\n## Structured Memory")
    structured = profile.get("structured_memory") or {}
    for key in ["metric_files", "summary_files", "result_files", "collector_files", "script_files", "config_files", "metric_names", "result_json_keys", "experiment_like_groups", "analysis_script_inputs"]:
        vals = structured.get(key) or []
        if vals:
            parts.append(f"\n### {key}")
            parts.extend(f"- {v}" for v in vals[:30])

    prov = profile.get("artifact_provenance") or {}
    artifacts = prov.get("artifacts") or {}
    if artifacts:
        parts.append("\n## Agent Artifact Provenance")
        for rel, rec in list(artifacts.items())[-50:]:
            parts.append(f"- {rel}: origin={rec.get('origin')} safe_to_modify={rec.get('safe_to_modify_by_future_agent')} last_thread={rec.get('last_thread_id')}")

    parts.append("\n## Stable Facts")
    for item in profile.get("stable_facts", [])[-50:]:
        parts.append(f"- {item.get('fact')}  Evidence: {item.get('evidence', [])}")
    parts.append("\n## Recent Task Summaries")
    for item in profile.get("task_summaries", [])[-12:]:
        parts.append(f"- [{item.get('ts')}] thread={item.get('thread_id')} mode={item.get('mode')} ok={item.get('ok')} task={item.get('task')}")
    parts.append("\n## Recent Analysis Runs")
    for item in profile.get("analysis_runs", [])[-8:]:
        parts.append(f"- [{item.get('ts')}] thread={item.get('thread_id')} coverage={item.get('coverage')} selected={item.get('selected_files', [])[:8]}")
    return "\n".join(parts) + "\n"


def write_project_memory(workspace: str | Path, profile: dict[str, Any]) -> None:
    paths = memory_paths(workspace)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    profile["updated_at"] = now_iso()
    write_json(paths["profile_json"], profile)
    md = project_memory_markdown(profile)
    paths["profile_md"].write_text(md, encoding="utf-8")
    paths["architecture_memory_md"].write_text(md, encoding="utf-8")
    write_json(paths["known_commands_json"], {
        "updated_at": profile.get("updated_at"),
        "known_entrypoints": profile.get("known_entrypoints", []),
        "known_command_sources": profile.get("known_command_sources", []),
        "known_tests": profile.get("known_tests", []),
    })


def compact_project_memory_for_prompt(profile: dict[str, Any], max_chars: int = 10000) -> str:
    if not profile:
        return ""
    compact = {
        "project_types": profile.get("project_types", []),
        "file_count": profile.get("file_count"),
        "py_file_count": profile.get("py_file_count"),
        "role_files": profile.get("role_files", {}),
        "known_entrypoints": profile.get("known_entrypoints", [])[:20],
        "known_tests": profile.get("known_tests", [])[:20],
        "known_results": profile.get("known_results", [])[:20],
        "known_configs": profile.get("known_configs", [])[:20],
        "stable_facts": profile.get("stable_facts", [])[-40:],
        "structured_memory": {k: (v[:40] if isinstance(v, list) else v) for k, v in (profile.get("structured_memory") or {}).items()},
        "recent_task_summaries": profile.get("task_summaries", [])[-8:],
        "recent_analysis_runs": profile.get("analysis_runs", [])[-5:],
    }
    return truncate(json.dumps(compact, ensure_ascii=False, indent=2), max_chars)
