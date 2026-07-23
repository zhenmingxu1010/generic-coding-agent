from __future__ import annotations

import json
import os
from typing import Any

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.workspace.repo_map import (
    ROLES,
    add_targeted_retrieval,
    compact_repo_map_for_llm,
    coverage_check,
    heuristic_select_evidence,
    role_summaries_from_evidence,
)
from coding_agent.tools.file_tools import read_many_files
from coding_agent.core.utils import extract_json_object, truncate, write_json
from coding_agent.contracts.analysis_contract import build_analysis_contract, build_task_focused_file_hints, extract_structured_memory
from .common import get_trace


SELECT_EVIDENCE_SYSTEM = """You are a repository evidence selector for a coding agent.
You do NOT write the final report. You only select files to read.
Input is a repository map with paths, symbols, roles and signals.
Return JSON only.
Schema:
{
  "selected_files": ["relative/path.py"],
  "role_assignments": {
    "project_overview": ["..."],
    "entrypoint": ["..."],
    "data_pipeline": ["..."],
    "model_definition": ["..."],
    "loss_definition": ["..."],
    "metric_evaluation": ["..."],
    "run_workflow": ["..."],
    "config_or_arguments": ["..."],
    "results_or_outputs": ["..."],
    "tests": ["..."]
  },
  "missing_roles": ["..."],
  "reason": "short explanation"
}
Rules:
- Select by semantic role, not by fixed names. Do not assume every repo uses scripts/, src/, train.py, or experiments/.
- Prefer files with strong signals for entrypoints, data, model, loss, metrics, config, workflow, results, and tests.
- Include concise evidence for each role when available.
- Keep selected_files under the provided max_files.
"""


def _sanitize_selection(obj: dict[str, Any], repo_map: dict[str, Any], max_files: int) -> dict[str, Any]:
    valid = set(repo_map.get("files") or [])
    selected: list[str] = []
    for p in obj.get("selected_files") or []:
        if isinstance(p, str) and p in valid and p not in selected:
            selected.append(p)
        if len(selected) >= max_files:
            break
    role_assignments: dict[str, list[str]] = {}
    raw_roles = obj.get("role_assignments") or {}
    if isinstance(raw_roles, dict):
        for role in ROLES:
            vals = raw_roles.get(role) or []
            paths = []
            for p in vals:
                if isinstance(p, str) and p in valid:
                    paths.append(p)
                    if p not in selected and len(selected) < max_files:
                        selected.append(p)
            if paths:
                role_assignments[role] = list(dict.fromkeys(paths))
    return {
        "selected_files": selected[:max_files],
        "role_assignments": role_assignments,
        "missing_roles": [r for r in ROLES if not role_assignments.get(r)],
        "reason": str(obj.get("reason", ""))[:1000],
        "selector": obj.get("selector", "llm"),
    }


def _llm_select_evidence(state: dict, compact_map: dict[str, Any], max_files: int) -> dict[str, Any]:
    client = OpenAICompatClient("configs/model.yaml", messages_path=state["messages_path"])
    memory_context = state.get("memory_context") or {}
    relevant_context = state.get("relevant_context") or {}
    user = (
        f"Task:\n{state.get('task')}\n\n"
        f"Max files to select: {max_files}\n"
        f"Required semantic roles: {ROLES}\n\n"
        f"Long-term project memory hints from prior scans/runs:\n{json.dumps(memory_context, ensure_ascii=False, indent=2)}\n\n"
        f"Task-term and memory retrieval hints:\n{json.dumps({k: relevant_context.get(k) for k in ['matched_files','memory_matched_files']}, ensure_ascii=False, indent=2)}\n\n"
        f"Repository map JSON:\n{json.dumps(compact_map, ensure_ascii=False, indent=2)}"
    )
    text = client.chat([
        {"role": "system", "content": SELECT_EVIDENCE_SYSTEM},
        {"role": "user", "content": user},
    ], purpose="select_evidence", max_tokens=int(os.getenv("AGENT_SELECT_EVIDENCE_MAX_TOKENS", "1536")))
    obj = extract_json_object(text)
    obj["selector"] = "llm"
    return obj


def _dedupe_preserve(paths: list[str], valid: set[str], max_files: int) -> list[str]:
    out: list[str] = []
    for p in paths:
        if isinstance(p, str) and p in valid and p not in out:
            out.append(p)
        if len(out) >= max_files:
            break
    return out


def _build_evidence_index(read_result: dict[str, Any], repo_map: dict[str, Any]) -> dict[str, Any]:
    rec_by_path = {r.get("path"): r for r in repo_map.get("records", [])}
    files = ((read_result.get("data") or {}).get("files") or [])
    out_files = []
    metric_terms: dict[str, list[str]] = {}
    for item in files:
        path = item.get("path")
        if not item.get("ok") or not path:
            continue
        content = str(item.get("content", ""))
        rec = rec_by_path.get(path) or {}
        symbols = rec.get("symbols") or {}
        # Generic evidence terms: function/class names plus domain-ish metrics observed in snippets.
        terms = []
        for name in (symbols.get("functions") or []) + (symbols.get("classes") or []):
            low = str(name).lower()
            if any(k in low for k in ["metric", "accuracy", "acc", "iou", "mae", "mse", "loss", "score", "eval", "precision", "recall", "auc"]):
                terms.append(str(name))
        import re
        for m in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*(?:accuracy|acc|iou|mae|mse|loss|score|metric|precision|recall|f1|auc)[a-zA-Z0-9_]*\b", content, flags=re.I):
            if m not in terms:
                terms.append(m)
        metric_terms[path] = terms[:40]
        out_files.append({
            "path": path,
            "roles": rec.get("roles", {}),
            "functions": (symbols.get("functions") or [])[:25],
            "classes": (symbols.get("classes") or [])[:20],
            "evidence_terms": terms[:40],
            "chars": len(content),
        })
    return {"files": out_files, "metric_or_domain_terms_by_file": metric_terms}


def analyze_repo_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("analyze_repo_start")
    repo_map = state.get("repo_map") or {}
    files = repo_map.get("files", [])

    max_files = int(os.getenv("AGENT_ANALYZE_MAX_FILES", "36"))
    per_file_chars = int(os.getenv("AGENT_ANALYZE_PER_FILE_CHARS", "3200"))
    max_total_chars = int(os.getenv("AGENT_ANALYZE_READ_CHARS", "52000"))
    compact_max_records = int(os.getenv("AGENT_REPO_MAP_COMPACT_RECORDS", "120"))

    analysis_contract = build_analysis_contract(state.get("task", ""), state.get("task_spec") or {})
    state["analysis_contract"] = analysis_contract
    if state.get("run_dir"):
        write_json(os.path.join(state["run_dir"], "analysis_contract.json"), analysis_contract)
    compact_map = compact_repo_map_for_llm(repo_map, max_records=compact_max_records)
    trace.event(
        "select_evidence_start",
        file_count=len(files),
        project_types=compact_map.get("project_types"),
        max_files=max_files,
    )
    try:
        raw_selection = _llm_select_evidence(state, compact_map, max_files=max_files)
        selection = _sanitize_selection(raw_selection, repo_map, max_files=max_files)
    except Exception as e:
        trace.event("select_evidence_llm_failed", error=str(e)[:2000])
        selection = heuristic_select_evidence(repo_map, max_files=max_files)
        selection["selector"] = "heuristic_after_llm_error"

    if not selection.get("selected_files"):
        selection = heuristic_select_evidence(repo_map, max_files=max_files)
        selection["selector"] = "heuristic_empty_selection"

    # Merge task-focused hints derived from the analysis contract and structured long-term memory.
    task_hints = build_task_focused_file_hints(analysis_contract, repo_map, state.get("project_memory") or {}, max_files=max_files)
    if task_hints:
        valid_paths = set(repo_map.get("files") or [])
        merged = _dedupe_preserve(list(task_hints) + list(selection.get("selected_files", [])), valid_paths, max_files=max_files)
        selection["selected_files"] = merged
        selection["task_focused_hints_merged"] = [p for p in task_hints if p in merged]

    # Merge long-term memory retrieval hints into evidence selection before targeted coverage fill.
    valid_paths = set(repo_map.get("files") or [])
    memory_hints = (state.get("relevant_context") or {}).get("memory_matched_files") or []
    if memory_hints:
        merged = _dedupe_preserve(list(selection.get("selected_files", [])) + list(memory_hints), valid_paths, max_files=max_files)
        selection["selected_files"] = merged
        selection["memory_hints_merged"] = [p for p in memory_hints if p in merged]

    coverage_before = coverage_check(selection, repo_map)
    selection = add_targeted_retrieval(selection, repo_map, max_files=max_files)
    coverage_after = coverage_check(selection, repo_map)

    selected = selection.get("selected_files", [])[:max_files]
    res = read_many_files(state["workspace"], selected, per_file_chars=per_file_chars, max_total_chars=max_total_chars)
    role_summaries = role_summaries_from_evidence(selection, res.model_dump(), repo_map)
    evidence_index = _build_evidence_index(res.model_dump(), repo_map)
    state["evidence_index"] = evidence_index

    structured_memory = extract_structured_memory({**state, "repo_analysis_context": {"read_result": res.model_dump(), "evidence_index": evidence_index}})
    state["structured_memory"] = structured_memory

    context = {
        "selection": selection,
        "selected_files": selected,
        "read_result": res.model_dump(),
        "compact_repo_map": compact_map,
        "role_coverage_before": coverage_before,
        "role_coverage_after": coverage_after,
        "role_summaries": role_summaries,
        "evidence_index": evidence_index,
        "analysis_contract": analysis_contract,
        "structured_memory": structured_memory,
        "project_memory_compact": (state.get("relevant_context") or {}).get("project_memory_compact", ""),
        "file_count": len(files),
        "py_files": repo_map.get("py_files", [])[:200],
        "project_types": repo_map.get("project_types", []),
        "context_budget": {
            "max_files": max_files,
            "per_file_chars": per_file_chars,
            "max_total_chars": max_total_chars,
            "actual_total_chars": res.data.get("total_chars") if res.data else None,
            "compact_map_records": compact_max_records,
        },
    }
    state["repo_analysis_context"] = context
    trace.event(
        "analyze_repo_done",
        selector=selection.get("selector"),
        selected_files=selected,
        covered_roles=coverage_after.get("covered_roles"),
        missing_roles=coverage_after.get("missing_roles"),
        coverage_ratio=coverage_after.get("coverage_ratio"),
        read_ok=res.ok,
        total_chars=res.data.get("total_chars") if res.data else None,
        evidence_terms=sum(len(v) for v in evidence_index.get("metric_or_domain_terms_by_file", {}).values()),
        context_preview=truncate(json.dumps({"selected_files": selected, "coverage": coverage_after, "memory_hints": selection.get("memory_hints_merged")}, ensure_ascii=False), 4000),
    )
    trace.snapshot(state)
    return state
