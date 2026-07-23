from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.core.utils import truncate
from coding_agent.memory.context_pack import render_context_pack_markdown
from coding_agent.memory.project_memory import compact_project_memory_for_prompt


def build_context_summary(state: dict[str, Any], max_chars: int = 24000) -> str:
    if state.get("context_pack"):
        return render_context_pack_markdown(state.get("context_pack") or {}, max_chars=max_chars)

    parts: list[str] = []
    parts.append("# Coding Agent Context Summary")
    parts.append(f"\n## Task\n{state.get('task', '')}")
    parts.append(f"\n## Mode\nread_only={state.get('read_only')} mode={state.get('mode')} round={state.get('round_idx')}")
    invariants = state.get("invariants") or []
    if invariants:
        parts.append("\n## Invariants")
        parts.extend(f"- {x}" for x in invariants)
    if state.get("task_spec"):
        parts.append("\n## Task Spec")
        parts.append(str(state.get("task_spec")))
    if state.get("project_memory"):
        parts.append("\n## Long-Term Project Memory")
        parts.append(compact_project_memory_for_prompt(state.get("project_memory") or {}, max_chars=8000))
    if state.get("memory_context"):
        parts.append("\n## Memory Context")
        parts.append(str(state.get("memory_context"))[:5000])
    if state.get("repo_map"):
        files = state.get("repo_map", {}).get("files", [])
        py_files = state.get("repo_map", {}).get("py_files", [])
        parts.append("\n## Repo Map")
        parts.append(f"file_count={len(files)} py_count={len(py_files)}")
        parts.append("\n".join(f"- {x}" for x in files[:260]))
    if state.get("evidence_index"):
        parts.append("\n## Evidence Index")
        parts.append(str(state.get("evidence_index"))[:8000])
    if state.get("repo_analysis_context"):
        ctx = state.get("repo_analysis_context", {})
        parts.append("\n## Repo Analysis Context")
        parts.append("Selected files:")
        parts.append("\n".join(f"- {x}" for x in ctx.get("selected_files", [])[:80]))
        read_files = ((ctx.get("read_result") or {}).get("data") or {}).get("files", [])
        for item in read_files[:12]:
            if item.get("ok"):
                parts.append(f"\n### {item.get('path')}\n" + truncate(item.get("content", ""), 3000))
    if state.get("plan"):
        parts.append("\n## Current Plan")
        parts.append(str(state.get("plan")))
        parts.append(f"plan_step_idx={state.get('plan_step_idx')} completed={state.get('completed_steps')} blocked={state.get('blocked_steps')}")
    if state.get("artifact_registry"):
        parts.append("\n## Artifact Registry")
        reg = state.get("artifact_registry") or {}
        parts.append(str({
            "agent_generated_tests": reg.get("agent_generated_tests"),
            "external_tests": reg.get("external_tests"),
            "code_files": reg.get("code_files"),
        })[:4000])
    if state.get("test_oracle_review"):
        parts.append("\n## Test Oracle Review")
        parts.append(str(state.get("test_oracle_review"))[:5000])
    if state.get("strategy_decision"):
        parts.append("\n## Strategy Decision")
        parts.append(str(state.get("strategy_decision"))[:5000])
    if state.get("failure_owner"):
        parts.append("\n## Failure Owner")
        parts.append(str(state.get("failure_owner")))
    if state.get("verification"):
        parts.append("\n## Verification")
        parts.append(str(state.get("verification"))[:6000])
    if state.get("failure"):
        parts.append("\n## Active Failure")
        parts.append(str(state.get("failure"))[:6000])
    if state.get("failure_history"):
        parts.append("\n## Failure History")
        parts.append(str(state.get("failure_history")[-4:])[:6000])
    if state.get("repair_history"):
        parts.append("\n## Repair History")
        parts.append(str(state.get("repair_history"))[:6000])
    if state.get("action_history"):
        parts.append("\n## Recent Actions")
        parts.append(str(state.get("action_history", [])[-8:])[:5000])
    if state.get("observations"):
        parts.append("\n## Recent Observations")
        for obs in state.get("observations", [])[-8:]:
            parts.append(str(obs)[:3000])
    if state.get("analysis_report"):
        parts.append("\n## Analysis Report Preview")
        parts.append(truncate(state.get("analysis_report", ""), 8000))
    return truncate("\n".join(parts), max_chars)


def write_context_summary(path: str | Path, state: dict[str, Any], max_chars: int = 24000) -> str:
    summary = build_context_summary(state, max_chars=max_chars)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(summary, encoding="utf-8")
    return summary
