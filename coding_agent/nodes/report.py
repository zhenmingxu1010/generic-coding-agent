from __future__ import annotations

from pathlib import Path

from coding_agent.core.utils import write_json, write_text_file
from coding_agent.nodes.final_gate import authoritative_requirement_atom_check, compute_final_gate
from coding_agent.memory.project_memory import update_project_memory_from_final
from coding_agent.scope.write_scope_audit import build_write_scope_audit
from coding_agent.repair.failure_analysis import decompose_failure_issues
from coding_agent.contracts.requirement_atoms import evaluate_requirement_atoms
from coding_agent.ux.human_report import format_human_report_markdown
from coding_agent.ux.token_usage import format_token_usage_markdown, summarize_token_usage
from coding_agent.workspace.run_paths import project_memory_dir_for
from .common import get_trace


def _ensure_analysis_requirement_atom_check(state: dict) -> None:
    if state.get("mode") != "analyze":
        return
    atoms = (
        state.get("requirement_atoms")
        or (state.get("task_contract") or {}).get("requirement_atoms")
        or ((state.get("requirement_atom_check") or {}).get("atoms"))
        or []
    )
    if not atoms or not state.get("workspace"):
        return
    check = evaluate_requirement_atoms(str(state.get("workspace")), list(atoms), state=state)
    state["requirement_atom_check"] = check
    state["requirement_atoms"] = check.get("atoms", [])
    state["requirement_atom_summary"] = check.get("summary", {})


def report_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("report_start")
    verification = state.get("verification") or {}
    _ensure_analysis_requirement_atom_check(state)
    authoritative_atom_check = authoritative_requirement_atom_check(state)
    if authoritative_atom_check:
        state["requirement_atom_check"] = authoritative_atom_check
        state["requirement_atoms"] = authoritative_atom_check.get("atoms", state.get("requirement_atoms") or [])
        state["requirement_atom_summary"] = authoritative_atom_check.get("summary", state.get("requirement_atom_summary") or {})
    gate = compute_final_gate(state)
    state["final_gate_status"] = gate
    trace.event(
        "final_gate_result",
        ok=gate.get("ok"),
        outcome=gate.get("outcome"),
        controlled_failure=gate.get("controlled_failure"),
        failures=gate.get("failures", []),
        warnings=gate.get("warnings", []),
        stopped_reason=gate.get("stopped_reason"),
        requirement_atom_summary=state.get("requirement_atom_summary"),
    )
    final_ok = bool(gate.get("ok"))
    outcome = gate.get("outcome") or ("verified_ok" if final_ok else "failed")
    controlled_failure = bool(gate.get("controlled_failure"))
    stopped = gate.get("stopped_reason") or state.get("stopped_reason") or "done"
    runtime_ok = final_ok

    if state.get("mode") == "analyze":
        analysis_quality_ok = bool((state.get("analysis_quality") or {}).get("ok"))
    else:
        analysis_quality_ok = None

    analysis_quality = state.get("analysis_quality") or {}
    analysis_contract_check = state.get("analysis_contract_check") or analysis_quality.get("analysis_contract_check")
    analysis_contract_ok = bool(analysis_contract_check.get("ok")) if isinstance(analysis_contract_check, dict) else None

    state["final_ok"] = final_ok
    state["outcome"] = outcome
    state["controlled_failure"] = controlled_failure
    state["runtime_ok"] = runtime_ok
    state["analysis_quality_ok"] = bool(analysis_quality_ok) if analysis_quality_ok is not None else False
    state["stopped_reason"] = stopped
    write_scope_audit = gate.get("write_scope_audit") or build_write_scope_audit(state)
    state["write_scope_audit"] = write_scope_audit
    if not final_ok and not state.get("failure_issues"):
        try:
            state["failure_issues"] = decompose_failure_issues(state)
        except Exception as e:
            trace.event("report_failure_issue_decompose_failed", error=str(e)[:2000])
    requirement_atom_check = state.get("requirement_atom_check") or ((state.get("semantic_contract_check") or {}).get("requirement_atom_check") or {})
    requirement_atoms = state.get("requirement_atoms") or requirement_atom_check.get("atoms")
    requirement_atom_summary = state.get("requirement_atom_summary") or requirement_atom_check.get("summary")
    active_failure_owner = None if final_ok else state.get("failure_owner")
    active_failure_issues = [] if final_ok else state.get("failure_issues")
    active_strategy_decision = None if final_ok else state.get("strategy_decision")
    active_verification_reason = "" if final_ok else state.get("verification_reason")
    resolved_repair = None
    if final_ok and (state.get("failure_owner") or state.get("strategy_decision") or state.get("repair_controller")):
        controller = state.get("repair_controller") or {}
        strategy = state.get("strategy_decision") or {}
        resolved_repair = {
            "status": "resolved",
            "previous_failure_owner_present": bool(state.get("failure_owner")),
            "previous_strategy_present": bool(state.get("strategy_decision")),
            "previous_repair_route": controller.get("route") or strategy.get("repair_controller_route"),
            "verification_reason_before_resolution": state.get("verification_reason") or "",
        }
    # Persist the final run outcome into long-term project memory.
    try:
        state["project_memory"] = update_project_memory_from_final(state)
    except Exception as e:
        trace.event("project_memory_final_update_failed", error=str(e)[:2000])

    analysis_report_path = str(Path(state["run_dir"]) / "analysis_report.md") if state.get("analysis_report") else None
    token_usage = summarize_token_usage(state.get("messages_path"))
    state["token_usage"] = token_usage
    final = {
        "ok": final_ok,
        "outcome": outcome,
        "controlled_failure": controlled_failure,
        "controlled_failure_reason": gate.get("controlled_failure_reason") or "",
        "controlled_failure_blockers": gate.get("controlled_failure_blockers") or [],
        "runtime_ok": runtime_ok,
        "analysis_quality_ok": analysis_quality_ok,
        "stopped_reason": stopped,
        "final_gate_status": gate,
        "task": state.get("task"),
        "workspace": state.get("workspace"),
        "thread_id": state.get("thread_id"),
        "round_idx": state.get("round_idx"),
        "mode": state.get("mode"),
        "supervisor": state.get("supervisor"),
        "read_only": state.get("read_only"),
        "write_locked": state.get("write_locked"),
        "read_only_policy": state.get("read_only_policy"),
        "scope_grounding": state.get("scope_grounding"),
        "scope_contract": state.get("scope_contract") or (state.get("task_intent") or {}).get("scope_contract"),
        "task_spec": state.get("task_spec"),
        "task_intent": state.get("task_intent"),
        "task_contract": state.get("task_contract"),
        "task_completeness": state.get("task_completeness"),
        "assumptions": state.get("assumptions") or [],
        "implementation_contract": state.get("implementation_contract") or {},
        "clarification_questions": state.get("clarification_questions") or [],
        "clarification_history": state.get("clarification_history") or [],
        "artifact_registry": state.get("artifact_registry"),
        "test_oracle_review": state.get("test_oracle_review"),
        "failure_owner": active_failure_owner,
        "failure_issues": active_failure_issues,
        "resolved_repair": resolved_repair,
        "traceback_issues": state.get("traceback_issues"),
        "force_repair_action": state.get("force_repair_action"),
        "repair_action_budget": state.get("repair_action_budget"),
        "repair_llm_call_budget": state.get("repair_llm_call_budget"),
        "repair_prompt_stats": state.get("repair_prompt_stats"),
        "verification_review_mode": state.get("verification_review_mode"),
        "verification_review_prompt_chars": state.get("verification_review_prompt_chars"),
        "repair_read_cache": state.get("repair_read_cache"),
        "interface_check": state.get("interface_check"),
        "strategy_decision": active_strategy_decision,
        "repair_controller": state.get("repair_controller"),
        "contract_check": state.get("contract_check"),
        "contract_ok": state.get("contract_ok"),
        "semantic_contract_check": state.get("semantic_contract_check"),
        "analysis_contract_ok": analysis_contract_ok,
        "analysis_contract_check": analysis_contract_check,
        "requirement_atoms": requirement_atoms,
        "requirement_atom_summary": requirement_atom_summary,
        "requirement_atom_check": requirement_atom_check,
        "sample_data_review": state.get("sample_data_review"),
        "semantic_checks": state.get("semantic_checks"),
        "verification_claims": state.get("verification_claims"),
        "verification_grounding": state.get("verification_grounding"),
        "verification_oracle_review": state.get("verification_oracle_review"),
        "verification_artifacts": state.get("verification_artifacts"),
        "verification_plan_update": state.get("verification_plan_update"),
        "skipped_file_plan_verify_steps": state.get("skipped_file_plan_verify_steps"),
        "needs_verification": state.get("needs_verification"),
        "verification_reason": active_verification_reason,
        "verification": state.get("verification"),
        "test_results": state.get("test_results") or (state.get("verification") or {}).get("test_results"),
        "test_baseline": state.get("test_baseline"),
        "test_baseline_comparison": state.get("test_baseline_comparison"),
        "file_plan": state.get("file_plan"),
        "file_plan_review": state.get("file_plan_review"),
        "verification_test_registry": state.get("verification_test_registry"),
        "write_intents": state.get("write_intents"),
        "write_scope_policy": state.get("write_scope_policy"),
        "write_scope_audit": write_scope_audit,
        "source_changed_files": write_scope_audit.get("source_changed_files", []),
        "agent_test_changed_files": write_scope_audit.get("agent_test_changed_files", []),
        "recent_reflexions": state.get("recent_reflexions"),
        "reflexion_memory_path": state.get("reflexion_memory_path"),
        "approval_required": state.get("approval_required"),
        "prewrite_backups": state.get("prewrite_backups"),
        "failed_writes": state.get("failed_writes"),
        "restore_manifest": state.get("restore_manifest_path"),
        "generated_files": state.get("generated_files"),
        "changed_files": state.get("changed_files"),
        "analysis_quality": state.get("analysis_quality"),
        "deliverable_review": state.get("deliverable_review"),
        "project_memory": {
            "path": state.get("project_profile_path"),
            "artifact_provenance_path": state.get("artifact_provenance_path"),
            "long_term_memory_path": state.get("long_term_memory_path"),
            "stable_facts_count": len((state.get("project_memory") or {}).get("stable_facts", [])),
            "task_summaries_count": len((state.get("project_memory") or {}).get("task_summaries", [])),
            "analysis_runs_count": len((state.get("project_memory") or {}).get("analysis_runs", [])),
        },
        "quality_warnings": state.get("quality_warnings") or (state.get("verification") or {}).get("quality_warnings", []),
        "token_usage": token_usage,
        "failure": None if final_ok else state.get("failure"),
        "failure_history": state.get("failure_history", []),
        "repair_history": state.get("repair_history", []),
        "action_history_tail": state.get("action_history", [])[-20:],
        "artifacts": {
            "trace": state.get("trace_path"),
            "messages": state.get("messages_path"),
            "context_pack": state.get("context_pack_path"),
            "context_summary": state.get("context_summary_path"),
            "state_snapshot": state.get("state_snapshot_path"),
            "analysis_report": analysis_report_path,
            "patches_dir": state.get("patches_dir"),
            "failed_writes_dir": str(Path(state["run_dir"]) / "failed_writes"),
            "restore_manifest": state.get("restore_manifest_path"),
            "repository_map": state.get("repository_map_path"),
            "short_term_memory": state.get("short_term_memory_path"),
            "long_term_memory": state.get("long_term_memory_path"),
            "project_profile": state.get("project_profile_path"),
            "artifact_provenance": state.get("artifact_provenance_path"),
            "workspace_baseline": str(Path(state.get("project_memory_dir") or project_memory_dir_for(state["workspace"])) / "workspace_baseline.json"),
            "reflexions": str(Path(state.get("project_memory_dir") or project_memory_dir_for(state["workspace"])) / "reflexions.jsonl"),
        },
    }
    report_md = Path(state["run_dir"]) / "final_report.md"
    human_report_md = Path(state["run_dir"]) / "final_report_human.md"
    final["artifacts"].update(
        {
            "final_json": str(state["final_path"]),
            "final_report": str(report_md),
            "human_report": str(human_report_md),
        }
    )
    write_json(state["final_path"], final)
    human_report_text = format_human_report_markdown(final)
    write_text_file(human_report_md, human_report_text)
    token_usage_md = "\n" + format_token_usage_markdown(token_usage)
    if state.get("analysis_report"):
        write_text_file(report_md, state["analysis_report"].rstrip() + "\n" + token_usage_md)
    else:
        write_text_file(report_md, human_report_text)
    trace.event("report_done", final=final)
    trace.snapshot(state)
    return state
