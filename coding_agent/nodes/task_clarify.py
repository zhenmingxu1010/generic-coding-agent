from __future__ import annotations

from typing import Any

from coding_agent.contracts.contract import extract_task_contract
from coding_agent.contracts.task_completeness import assess_task_completeness
from .common import get_trace


def _apply_inspection_lock(state: dict[str, Any]) -> None:
    intent = dict(state.get("task_intent") or {})
    intent.update({
        "mode": "analyze",
        "operation_mode": "read_only_analysis",
        "agent_read_only": True,
        "source_modify_intent": False,
        "auxiliary_create_intent": False,
    })
    state["task_intent"] = intent
    state["mode"] = "analyze"
    state["read_only"] = True
    supervisor = dict(state.get("supervisor") or {})
    supervisor.update({
        "mode": "analyze",
        "operation_mode": "read_only_analysis",
        "read_only": True,
        "agent_read_only": True,
        "allowed_write": False,
        "requires_verification": False,
        "primary_agent": "RepoAnalyst",
        "task_intent": intent,
    })
    state["supervisor"] = supervisor


def task_clarify_node(state: dict[str, Any]) -> dict[str, Any]:
    """Turn decision-changing ambiguity into a resumable controlled stop."""
    trace = get_trace(state)
    task = str(state.get("task") or "")
    previous = state.get("task_completeness") or {}
    if previous.get("evaluated_task") == task and previous.get("decision") != "clarify":
        trace.event("task_completeness_reused", decision=previous.get("decision"))
        return state

    assessment = assess_task_completeness(
        task,
        state.get("task_spec") or {},
        state.get("task_intent") or {},
        state.get("repo_map") or {},
    )
    assessment["evaluated_task"] = task
    state["task_completeness"] = assessment
    state["assumptions"] = list(assessment.get("assumptions") or [])
    state["implementation_contract"] = dict(assessment.get("implementation_contract") or {})
    state["clarification_questions"] = list(assessment.get("questions") or [])

    if assessment.get("activity") == "inspect":
        _apply_inspection_lock(state)

    task_spec = dict(state.get("task_spec") or {})
    task_spec["task_completeness"] = assessment
    task_spec["assumptions"] = state["assumptions"]
    task_spec["implementation_contract"] = state["implementation_contract"]
    task_spec["implementation_requirements"] = list(assessment.get("implementation_requirements") or [])
    if assessment.get("activity") == "inspect":
        task_spec["task_type"] = "analyze"
        task_spec["read_only"] = True
    state["task_spec"] = task_spec
    state["task_contract"] = extract_task_contract(
        task,
        task_spec,
        state.get("supervisor") or {},
    )
    state["requirement_atoms"] = list((state["task_contract"] or {}).get("requirement_atoms") or [])
    state["requirement_atom_summary"] = dict((state["task_contract"] or {}).get("requirement_atom_summary") or {})

    if assessment.get("decision") == "clarify":
        state["stopped_reason"] = "clarification_required"
        state["outcome"] = "clarification_required"
        state["controlled_failure"] = True
        state["failure"] = {
            "failure_type": "clarification_required",
            "priority": 2,
            "message": "Core task information is missing and cannot be replaced by a safe implementation default.",
            "target_file": None,
            "signature": "clarification_required",
            "raw_excerpt": "\n".join(
                str(item.get("question") or "") for item in state["clarification_questions"]
            )[:4000],
            "source": "task_completeness",
        }
    trace.event(
        "task_completeness_done",
        decision=assessment.get("decision"),
        activity=assessment.get("activity"),
        assumptions=state.get("assumptions"),
        questions=state.get("clarification_questions"),
    )
    trace.snapshot(state)
    return state


def route_after_task_clarify(state: dict[str, Any]) -> str:
    return "report" if state.get("stopped_reason") == "clarification_required" else "context_retrieve"
