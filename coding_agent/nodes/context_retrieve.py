from __future__ import annotations

from coding_agent.memory.retrieval_store import retrieve_by_task_terms
from coding_agent.memory.project_memory import load_project_memory, compact_project_memory_for_prompt
from coding_agent.contracts.analysis_contract import build_analysis_contract
from .common import get_trace


def _memory_hits(profile: dict, task: str, max_files: int = 12) -> list[str]:
    text = task.lower()
    buckets: list[str] = []
    # Generic role routing by task terms; no project-specific filenames.
    if any(k in text for k in ["metric", "metrics", "指标", "iou", "mae", "accuracy", "结果", "summary", "分析结果"]):
        buckets += (profile.get("role_files") or {}).get("metric_evaluation", [])
        buckets += profile.get("known_results", [])
    if any(k in text for k in ["train", "训练", "入口", "运行", "script", "脚本", "workflow"]):
        buckets += profile.get("known_entrypoints", [])
        buckets += (profile.get("role_files") or {}).get("run_workflow", [])
    if any(k in text for k in ["data", "dataset", "输入", "字段", "key", "数据"]):
        buckets += (profile.get("role_files") or {}).get("data_pipeline", [])
    if any(k in text for k in ["loss", "损失"]):
        buckets += (profile.get("role_files") or {}).get("loss_definition", [])
    if any(k in text for k in ["model", "模型", "网络", "结构"]):
        buckets += (profile.get("role_files") or {}).get("model_definition", [])
    out: list[str] = []
    for p in buckets:
        if p and p not in out:
            out.append(p)
        if len(out) >= max_files:
            break
    return out


def context_retrieve_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("context_retrieve_start")
    profile = load_project_memory(state["workspace"])
    state["project_memory"] = profile
    ctx = retrieve_by_task_terms(state["workspace"], state.get("task", ""), max_files=8)
    analysis_contract = build_analysis_contract(state.get("task", ""), state.get("task_spec") or {})
    state["analysis_contract"] = state.get("analysis_contract") or analysis_contract
    memory_files = _memory_hits(profile, state.get("task", ""), max_files=12)
    structured = profile.get("structured_memory") or {}
    if analysis_contract.get("report_type") in {"metric_result_summary", "script_design_analysis"}:
        for bucket in ["metric_files", "summary_files", "result_files", "collector_files", "analysis_script_inputs"]:
            for p in structured.get(bucket, []) or []:
                if p not in memory_files:
                    memory_files.append(p)
                if len(memory_files) >= 18:
                    break
            if len(memory_files) >= 18:
                break
    ctx["memory_matched_files"] = memory_files
    ctx["project_memory_compact"] = compact_project_memory_for_prompt(profile, max_chars=9000)
    ctx["project_profile_path"] = state.get("project_profile_path")
    state["relevant_context"] = ctx
    state["memory_context"] = {
        "project_memory_available": bool(profile),
        "project_memory_path": state.get("project_profile_path"),
        "long_term_memory_path": state.get("long_term_memory_path"),
        "memory_matched_files": memory_files,
        "stable_facts_count": len(profile.get("stable_facts", [])),
        "recent_task_summaries": profile.get("task_summaries", [])[-5:],
        "recent_analysis_runs": profile.get("analysis_runs", [])[-3:],
        "structured_memory_keys": list((profile.get("structured_memory") or {}).keys()),
        "analysis_contract": state.get("analysis_contract"),
    }
    trace.event("context_retrieve_done", matched_files=ctx.get("matched_files", []), memory_matched_files=memory_files, memory_facts=len(profile.get("stable_facts", [])))
    trace.snapshot(state)
    return state
