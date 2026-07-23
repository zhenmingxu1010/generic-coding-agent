from __future__ import annotations

from typing import Any


_TERMINAL_FIELDS = {
    "stopped_reason", "final_ok", "final_gate_status", "runtime_ok",
    "outcome", "controlled_failure",
}

_CLARIFICATION_DERIVED_FIELDS = {
    "task_spec", "task_intent", "task_contract", "task_completeness",
    "assumptions", "implementation_contract", "clarification_questions",
    "supervisor", "scope_contract", "scope_grounding", "read_only_policy",
    "write_locked", "read_only", "repo_map", "relevant_context", "context_pack",
    "context_summary", "plan", "file_plan", "decision", "verification",
    "verification_claims", "verification_grounding", "verification_plan_update",
    "contract_check", "contract_ok", "semantic_contract_check",
    "requirement_atoms", "requirement_atom_summary", "requirement_atom_check",
    "failure", "failure_issues", "failure_owner", "strategy_decision",
    "analysis_report", "analysis_quality", "analysis_contract_check",
    "needs_verification", "verification_reason", "route_next",
}


def prepare_resumed_state(
    state: dict[str, Any],
    *,
    max_rounds: int,
    max_repair_calls: int,
    clarification_answer: str | None = None,
) -> dict[str, Any]:
    """Reopen a checkpoint, optionally resolving a clarification stop."""
    state = dict(state)
    previous_stop = state.get("stopped_reason")
    if previous_stop:
        state["resumed_from_stopped_reason"] = previous_stop
    for key in _TERMINAL_FIELDS:
        state.pop(key, None)

    answer = str(clarification_answer or "").strip()
    if answer:
        if previous_stop != "clarification_required":
            raise ValueError("A clarification answer can only resume a clarification_required checkpoint.")
        original_task = str(state.get("original_task") or state.get("user_task") or state.get("task") or "").strip()
        questions = list(state.get("clarification_questions") or [])
        history = list(state.get("clarification_history") or [])
        history.append({"questions": questions, "answer": answer})
        state["original_task"] = original_task
        state["clarification_answer"] = answer
        state["clarification_history"] = history
        state["task"] = f"{original_task}\n\nUser clarification:\n{answer}".strip()
        state["user_task"] = state["task"]
        for key in _CLARIFICATION_DERIVED_FIELDS:
            state.pop(key, None)
        state["round_idx"] = 0
        state["plan_step_idx"] = 0
        state["completed_steps"] = []
        state["blocked_steps"] = []
        state["mode"] = "auto"

    state["max_rounds"] = max_rounds
    state["max_repair_llm_calls"] = max_repair_calls
    state["resumed_from_checkpoint"] = True
    return state
