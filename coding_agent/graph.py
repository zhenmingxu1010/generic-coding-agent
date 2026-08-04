from __future__ import annotations

try:
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.memory import MemorySaver
except Exception as e:  # pragma: no cover - exercised only when dependency missing
    StateGraph = None
    START = "__start__"
    END = "__end__"
    MemorySaver = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

from .core.state import AgentState
from .nodes.intake import intake_node
from .nodes.supervisor import supervisor_node
from .nodes.repo_scan import repo_scan_node
from .nodes.task_clarify import task_clarify_node, route_after_task_clarify
from .nodes.context_retrieve import context_retrieve_node
from .nodes.analyze_repo import analyze_repo_node
from .nodes.context_compress import context_compress_node
from .nodes.analyze_report import analyze_report_node
from .nodes.plan import plan_node
from .nodes.file_plan import file_plan_node
from .nodes.generate_files import generate_files_node
from .nodes.act import act_node
from .nodes.tool_exec import tool_exec_node
from .nodes.verify import verify_node
from .nodes.diagnose import diagnose_node
from .nodes.failure_owner import failure_owner_node
from .nodes.repair import repair_node
from .nodes.deliverable_review import (
    deliverable_review_needed,
    deliverable_review_node,
    route_after_deliverable_review,
)
from .nodes.strategy_reflection import strategy_reflection_node
from .nodes.report import report_node
from .nodes.final_gate import compute_final_gate
from .core.implementation_batch import implementation_batch_can_continue
from .scope.mode_policy import mode_requires_verification
from .verification.behavior_review import MAX_VERIFICATION_PLAN_ATTEMPTS


def is_analysis_task(state: AgentState) -> bool:
    if is_verify_only_task(state):
        return False
    return (
        bool(state.get("write_locked"))
        or state.get("mode") == "analyze"
        or (bool(state.get("read_only")) and (state.get("task_spec") or {}).get("task_type") == "analyze")
    )


def is_verify_only_task(state: AgentState) -> bool:
    return (
        state.get("mode") == "run_verify"
        or (state.get("task_intent") or {}).get("operation_mode") == "verify_only"
        or (state.get("supervisor") or {}).get("operation_mode") == "verify_only"
    )


def is_greenfield_write_task(state: AgentState) -> bool:
    return state.get("mode") in {"write", "generate_project"} and not bool(state.get("read_only"))


def route_from_start(state: AgentState) -> str:
    """Resume an established contract without asking intake to redefine it."""
    if (
        state.get("resumed_from_checkpoint")
        and state.get("task_spec")
        and state.get("task_contract")
    ):
        return "repo_scan"
    return "intake"


def route_after_retrieve(state: AgentState) -> str:
    if is_analysis_task(state):
        return "analyze_repo"
    # Writing must consume repository evidence before planning or generation.
    # Empty workspaces still produce a small context pack containing task and
    # contract state, so this does not add a free exploration loop.
    return "context_compress"


def route_after_context(state: AgentState) -> str:
    if state.get("stopped_reason") in {"approval_required", "llm_timeout", "runtime_exception"}:
        return "report"
    max_rounds = int(state.get("max_rounds", 12))
    round_budget_exhausted = int(state.get("round_idx", 0)) >= max_rounds
    if is_verify_only_task(state):
        return "verify" if not state.get("verification") else "report"
    if is_analysis_task(state):
        return "analyze_report"
    if state.get("failure") and not (state.get("verification") or {}).get("ok"):
        if round_budget_exhausted:
            state["stopped_reason"] = "max_rounds"
            return "report"
        return "repair"
    if (
        state.get("needs_verification")
        and not round_budget_exhausted
        and implementation_batch_can_continue(state)
    ):
        return "act"
    if round_budget_exhausted and not state.get("needs_verification"):
        state["stopped_reason"] = "max_rounds"
        return "report"
    if is_greenfield_write_task(state) and not state.get("file_plan"):
        return "file_plan"
    # Debug/repair tasks should first reproduce the failure with the project's
    # own verification commands before asking the LLM to patch.
    if state.get("mode") in {"debug", "repair_existing"} and not state.get("verification") and not state.get("failure"):
        return "verify"
    if state.get("needs_verification"):
        return "verify"
    if not state.get("plan"):
        return "plan"
    return "act"


def route_after_tool(state: AgentState) -> str:
    max_rounds = int(state.get("max_rounds", 12))
    if state.get("stopped_reason") in {"repair_protocol_blocked", "repair_action_budget_exhausted"}:
        return "report"
    result = state.get("last_tool_result") or {}
    tool = result.get("tool")
    data = result.get("data", {}) or {}
    changed_write = tool in {"write_file", "edit_file", "apply_patch"} and bool(result.get("ok")) and bool(data.get("changed"))
    if int(state.get("round_idx", 0)) >= max_rounds and not changed_write:
        state["stopped_reason"] = "max_rounds"
        return "report"
    # Protocol/tool failures are given back to the LLM as structured feedback.
    if (not result.get("ok")) and (data.get("tool_schema_error") or data.get("missing_args") or data.get("error_type") in {"TypeError"} or str(result.get("message", "")).startswith("unknown tool")):
        return "strategy_reflection"
    if data.get("blocked_by_repair_action_budget"):
        # If LLM repeatedly ignores hard loop-control feedback, terminate with a diagnosable failure.
        force = state.get("force_repair_action") or {}
        if int(force.get("blocked_read_attempts", 0)) >= 3:
            state["stopped_reason"] = "repair_action_budget_exhausted"
            state["failure"] = state.get("failure") or {"failure_type": "repair_action_budget_exhausted", "message": "LLM kept repeating blocked read actions", "signature": "repair_action_budget_exhausted"}
            return "report"
        return "repair"
    if data.get("blocked_by_repeated_action_guard"):
        return "strategy_reflection"
    if data.get("blocked_by_policy"):
        if data.get("read_only_violation") or data.get("approval_required"):
            state["stopped_reason"] = "blocked_by_policy"
            return "report"
        state["failure"] = {
            "failure_type": "write_policy_blocked",
            "priority": 1,
            "message": result.get("message", "write action blocked by policy"),
            "signature": f"write_policy_blocked:{data.get('path') or 'unknown'}",
            "target_file": data.get("path"),
            "raw_excerpt": str(data)[:2000],
            "source": "route_after_tool",
        }
        return "strategy_reflection"
    if tool in {"write_file", "edit_file", "apply_patch"}:
        if result.get("ok") and data.get("changed"):
            # Complete a bounded initial multi-file implementation before
            # verification. Repairs after the first verification are still
            # checked after each edit.
            if (
                int(state.get("round_idx", 0)) < max_rounds
                and implementation_batch_can_continue(state)
            ):
                return "context_compress"
            # A successful file-changing action must still be verified when
            # the LLM round budget has just been reached.
            return "repo_scan"
        # Failed or no-op edits did not create new code to verify. Do not let a
        # stale needs_verification flag from a previous failed verification send
        # the graph through another verify/diagnose cycle.
        if int(state.get("round_idx", 0)) >= max_rounds:
            state["stopped_reason"] = "max_rounds"
            return "report"
        return "strategy_reflection"
    if int(state.get("round_idx", 0)) >= max_rounds:
        state["stopped_reason"] = "max_rounds"
        return "report"
    if tool == "finish":
        if state.get("stopped_reason") == "llm_timeout":
            return "report"
        # Finish is terminal once execution verification has already happened.
        # A direct run_tests/run_shell action is not the authoritative verify
        # node: it does not bind results to requirement atoms or run the final
        # evidence review. Pending requirements must still pass through verify.
        if mode_requires_verification(state.get("mode"), bool(state.get("read_only"))):
            if not state.get("verification"):
                state["needs_verification"] = True
                state["verification_reason"] = "finish requested before execution verification"
                return "verify"
            atom_summary = state.get("requirement_atom_summary") or {}
            pending_requirements = (
                int(atom_summary.get("required_unverified", 0) or 0) > 0
                or any(
                    str(atom.get("status") or "pending") in {"", "pending", "unverified", "unknown"}
                    for atom in (
                        state.get("requirement_atoms")
                        or (state.get("task_contract") or {}).get("requirement_atoms")
                        or []
                    )
                    if isinstance(atom, dict) and atom.get("required", True)
                )
            )
            if (state.get("verification") or {}).get("ok") and pending_requirements:
                state["needs_verification"] = True
                state["verification_reason"] = "finish requested before authoritative requirement verification"
                return "verify"
            state["stopped_reason"] = state.get("stopped_reason") or "finish_requested"
            return "report"
        return "report"
    if (
        tool in {"read_file", "search_text", "filter_files", "list_files"}
        and implementation_batch_can_continue(state)
    ):
        return "context_compress"
    # Verification is a runtime obligation, not another LLM repair round. If
    # the last allowed action changed files at the round limit, still run the
    # deterministic verification path before reporting.
    if state.get("needs_verification"):
        if int(state.get("round_idx", 0)) >= max_rounds:
            state["stopped_reason"] = "max_rounds"
            return "report"
        return "repo_scan"
    if (
        tool in {"run_shell", "run_tests"}
        and not result.get("ok")
        and data.get("failure_kind") == "command_policy"
        and data.get("executed") is False
    ):
        # The candidate command was rejected before execution. This is action
        # feedback, not evidence that the implementation or contract failed.
        # Return to the normal action loop so the LLM can choose an allowed
        # equivalent without spending repair rounds on a nonexistent defect.
        return "context_compress"
    if tool in {"run_shell", "run_tests"} and not result.get("ok"):
        return "diagnose"
    return "context_compress"


def route_after_verify(state: AgentState) -> str:
    if is_verify_only_task(state):
        if (state.get("verification") or {}).get("ok"):
            state["stopped_reason"] = state.get("stopped_reason") or "verified_ok"
            state["failure"] = None
        else:
            state["stopped_reason"] = state.get("stopped_reason") or "verification_failed"
        state["needs_verification"] = False
        return "report"
    if (state.get("verification") or {}).get("ok"):
        state["failure"] = None
        state["needs_verification"] = False
        if deliverable_review_needed(state):
            return "deliverable_review"
        state["stopped_reason"] = state.get("stopped_reason") or "verified_ok"
        return "report"
    gate_probe = dict(state)
    gate_probe["needs_verification"] = False
    gate = compute_final_gate(gate_probe)
    if gate.get("ok") and "agent_generated_tests_failed_but_contract_passed" in set(gate.get("warnings") or []):
        state["final_gate_status"] = gate
        state["needs_verification"] = False
        state["stopped_reason"] = "verified_with_generated_test_warnings"
        return "report"
    atom_summary = state.get("requirement_atom_summary") or {}
    required_failed = int(atom_summary.get("required_failed", 0) or 0)
    required_unverified = int(atom_summary.get("required_unverified", 0) or 0)
    verification_results = (state.get("verification") or {}).get("results") or []
    rejected_oracle_steps = set(
        (state.get("verification_oracle_review") or {}).get("rejected_step_names") or []
    )
    executed_results = [
        result
        for result in verification_results
        if isinstance(result, dict)
        and result.get("executed", True)
        and str(result.get("name") or "") not in rejected_oracle_steps
    ]
    all_executed_commands_passed = (
        all(
            int(result.get("returncode", 1) or 0) == 0 and not result.get("timed_out")
            for result in executed_results
        )
        if executed_results
        else bool(rejected_oracle_steps)
    )
    # Repeated *failing* execution should stop a repair loop. Repeatedly
    # missing evidence is different: after bounded replanning it must still
    # reach the deliverable audit, which may identify an incomplete multi-file
    # implementation even when no safe scenario could be constructed.
    evidence_only_gap = (
        required_failed == 0
        and required_unverified > 0
        and all_executed_commands_passed
    )

    def has_unverified_execution_requirement() -> bool:
        check = state.get("requirement_atom_check") or {}
        for atom in check.get("atoms") or []:
            if not isinstance(atom, dict) or str(atom.get("status") or "") != "unverified":
                continue
            data = atom.get("data") if isinstance(atom.get("data"), dict) else {}
            mode = str(data.get("evidence_mode") or "").strip().lower()
            if not mode:
                mode = "runtime" if str(atom.get("type") or "") == "constraint" else "execution"
            if mode == "execution":
                return True
        return False
    if state.get("verification_stalled") and not evidence_only_gap:
        repeat_count = int(state.get("verification_failure_repeat_count", 0) or 0)
        state["needs_verification"] = False
        state["stopped_reason"] = "repeated_verification_failure"
        state["failure_owner"] = "verification_controller"
        state["strategy_decision"] = {
            "strategy": "stop_repeated_verification_failure",
            "reason": "verification evidence did not change across bounded retries",
        }
        state["failure"] = {
            "failure_type": "repeated_verification_failure",
            "priority": 5,
            "message": f"the same verification evidence repeated {repeat_count} times; implementation repair was stopped",
            "target_file": None,
            "signature": str(state.get("verification_failure_fingerprint") or "repeated_verification_failure"),
            "raw_excerpt": str((state.get("verification") or {}).get("results") or [])[:4000],
            "source": "route_after_verify",
        }
        return "report"
    if evidence_only_gap:
        attempts = int(state.get("verification_plan_attempts", 0) or 0)
        # The verification planner can only add execution scenarios. Artifact,
        # runtime, and analysis evidence gaps must proceed to the deliverable
        # audit (or a bounded incomplete report); routing them back to verify
        # cannot increment the planner budget and otherwise forms an infinite
        # verify -> verify loop.
        if has_unverified_execution_requirement() and attempts < MAX_VERIFICATION_PLAN_ATTEMPTS:
            state["needs_verification"] = True
            state["verification_reason"] = "required behavior lacks executed evidence; replan verification without changing implementation"
            return "verify"
        if deliverable_review_needed(state):
            state["needs_verification"] = False
            state["verification_reason"] = "documented behavior remains unverified; audit deliverables against reference contracts"
            return "deliverable_review"
        state["needs_verification"] = False
        state["stopped_reason"] = "verification_evidence_incomplete"
        state["failure"] = {
            "failure_type": "verification_evidence_incomplete",
            "priority": 6,
            "message": f"{required_unverified} required behavior(s) remain unverified after bounded evidence replanning",
            "target_file": None,
            "signature": "verification_evidence_incomplete",
            "raw_excerpt": str(state.get("verification_claims") or {})[:4000],
            "source": "route_after_verify",
        }
        return "report"
    return "diagnose"


def build_graph():
    if StateGraph is None:
        raise RuntimeError(
            "LangGraph is not installed. Install requirements first: pip install -r requirements.txt"
        ) from _IMPORT_ERROR

    builder = StateGraph(AgentState)
    builder.add_node("intake", intake_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("repo_scan", repo_scan_node)
    builder.add_node("task_clarify", task_clarify_node)
    builder.add_node("context_retrieve", context_retrieve_node)
    builder.add_node("analyze_repo", analyze_repo_node)
    builder.add_node("context_compress", context_compress_node)
    builder.add_node("analyze_report", analyze_report_node)
    builder.add_node("plan", plan_node)
    builder.add_node("file_plan", file_plan_node)
    builder.add_node("generate_files", generate_files_node)
    builder.add_node("act", act_node)
    builder.add_node("tool_exec", tool_exec_node)
    builder.add_node("verify", verify_node)
    builder.add_node("diagnose", diagnose_node)
    builder.add_node("failure_owner", failure_owner_node)
    builder.add_node("repair", repair_node)
    builder.add_node("deliverable_review", deliverable_review_node)
    builder.add_node("strategy_reflection", strategy_reflection_node)
    builder.add_node("report", report_node)

    builder.add_conditional_edges(START, route_from_start, {
        "intake": "intake",
        "repo_scan": "repo_scan",
    })
    builder.add_edge("intake", "supervisor")
    builder.add_edge("supervisor", "repo_scan")
    builder.add_edge("repo_scan", "task_clarify")
    builder.add_conditional_edges("task_clarify", route_after_task_clarify, {
        "context_retrieve": "context_retrieve",
        "report": "report",
    })
    builder.add_conditional_edges("context_retrieve", route_after_retrieve, {
        "analyze_repo": "analyze_repo",
        "file_plan": "file_plan",
        "context_compress": "context_compress",
    })
    builder.add_edge("analyze_repo", "context_compress")
    builder.add_edge("file_plan", "generate_files")
    builder.add_edge("generate_files", "repo_scan")
    builder.add_conditional_edges("context_compress", route_after_context, {
        "analyze_report": "analyze_report",
        "file_plan": "file_plan",
        "plan": "plan",
        "repair": "repair",
        "verify": "verify",
        "act": "act",
        "report": "report",
    })
    builder.add_edge("analyze_report", "report")
    builder.add_edge("plan", "act")
    builder.add_edge("act", "tool_exec")
    builder.add_conditional_edges("tool_exec", route_after_tool, {
        "repo_scan": "repo_scan",
        "context_compress": "context_compress",
        "verify": "verify",
        "diagnose": "diagnose",
        "repair": "repair",
        "report": "report",
        "strategy_reflection": "strategy_reflection",
    })
    builder.add_conditional_edges("verify", route_after_verify, {
        "report": "report",
        "diagnose": "diagnose",
        "verify": "verify",
        "deliverable_review": "deliverable_review",
    })
    builder.add_conditional_edges("deliverable_review", route_after_deliverable_review, {
        "repair": "repair",
        "report": "report",
    })
    builder.add_edge("diagnose", "failure_owner")
    builder.add_edge("failure_owner", "context_compress")
    builder.add_edge("repair", "tool_exec")
    builder.add_edge("strategy_reflection", "tool_exec")
    builder.add_edge("report", END)

    return builder.compile(checkpointer=MemorySaver())
