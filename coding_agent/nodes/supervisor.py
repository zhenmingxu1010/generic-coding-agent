from __future__ import annotations

from typing import Any

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.core.utils import extract_json_object
from coding_agent.contracts.contract import extract_task_contract
from coding_agent.contracts.requirement_atoms import evaluate_requirement_atoms
from coding_agent.scope.mode_policy import classify_mode_heuristic, resolve_read_only, protects_external_tests
from coding_agent.scope.read_only_policy import detect_global_read_only_lock
from coding_agent.scope.task_intent import classify_task_intent
from coding_agent.scope.write_scope import build_write_scope_policy
from .common import get_trace

SUPERVISOR_SYSTEM = """You are the supervisor of a general coding agent.
Read the full prompt and classify the operational mode. Return JSON only.
Schema:
{
  "mode": "analyze|write|modify|debug|generate_project|repair_existing|run_verify",
  "read_only": true,
  "agent_read_only": true,
  "script_read_only": false,
  "scan_first": false,
  "operation_mode": "read_only_analysis|verify_only|safe_create|scoped_modify|agent_artifact_repair|full_workspace",
  "rationale": "short operational rationale",
  "allowed_write": false,
  "requires_verification": true,
  "success_contract": ["..."],
  "primary_agent": "RepoAnalyst|Coder|Debugger|Verifier|Reviewer"
}
Definitions:
- agent_read_only/read_only: this run must not write files.
- script_read_only: the artifact being created is a script that only reads inputs at runtime; this is compatible with mode=write.
- scan_first: inspect/read the repo before creating files.
- analyze: inspect/explain/review only, with no requested new/modified artifacts.
- run_verify: execute project checks/tests without writing files.
- write: create a script or one/few files in the workspace.
- modify/debug: change existing code under a scoped write policy.
Rules:
- Intake already resolved task intent and write scope. Preserve that decision.
- Supervisor chooses routing and the primary agent; it must not redefine write permissions.
- Writing/debug/modify/generate_project require execution-based verification.
"""


def _heuristic_mode(
    task: str,
    current_mode: str,
    task_spec: dict[str, Any],
    resolved_intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = dict(resolved_intent) if resolved_intent else classify_task_intent(task, task_spec)
    mode = classify_mode_heuristic(task, current_mode, task_spec)
    if intent.get("write_locked"):
        mode = "analyze"
    elif intent.get("source_modify_intent"):
        mode = "debug" if intent.get("fix_requested") else "modify"
    # If deterministic intent found a requested creation, it wins over an LLM/heuristic analyze label.
    elif intent.get("create_requested"):
        mode = "write"
    read_only = bool(intent.get("agent_read_only"))
    return {
        "mode": mode,
        "read_only": read_only,
        "agent_read_only": read_only,
        "script_read_only": bool(intent.get("script_read_only")),
        "scan_first": bool(intent.get("scan_first")),
        "operation_mode": intent.get("operation_mode"),
        "allowed_write": not read_only,
        "requires_verification": mode != "analyze",
        "success_contract": list(task_spec.get("success_criteria") or []),
        "primary_agent": {"analyze":"RepoAnalyst", "write":"Coder", "modify":"Coder", "debug":"Debugger", "repair_existing":"Debugger", "generate_project":"Coder", "run_verify":"Verifier"}.get(mode, "Coder"),
        "rationale": "deterministic task_intent supervisor fallback",
        "task_intent": intent,
        "read_only_policy": intent.get("read_only_policy"),
        "write_locked": bool(intent.get("write_locked")),
    }


def _has_resolved_intake_intent(intent: dict[str, Any] | None) -> bool:
    if not isinstance(intent, dict):
        return False
    semantic = intent.get("semantic_write_scope") or {}
    return bool(semantic.get("available") and semantic.get("valid"))


def _compact_supervisor_context(task_spec: dict[str, Any], task_intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_type": task_spec.get("task_type"),
        "objective": task_spec.get("objective"),
        "constraints": task_spec.get("constraints") or [],
        "success_criteria": task_spec.get("success_criteria") or [],
        "resolved_task_intent": {
            "mode": task_intent.get("mode"),
            "operation_mode": task_intent.get("operation_mode"),
            "agent_read_only": task_intent.get("agent_read_only"),
            "source_modify_intent": task_intent.get("source_modify_intent"),
            "create_requested": task_intent.get("create_requested"),
            "allowed_modify_paths": task_intent.get("allowed_modify_paths") or [],
            "create_paths": task_intent.get("create_paths") or [],
            "protected_existing_paths": task_intent.get("protected_existing_paths") or [],
        },
    }


def _has_write_intent(task_intent: dict[str, Any]) -> bool:
    scope = task_intent.get("semantic_write_scope") or {}
    contract = task_intent.get("scope_contract") or {}
    return bool(
        task_intent.get("create_requested")
        or task_intent.get("fix_requested")
        or task_intent.get("modify_requested")
        or task_intent.get("source_modify_intent")
        or task_intent.get("auxiliary_create_intent")
        or scope.get("source_modification_allowed")
        or scope.get("existing_file_modification_allowed")
        or scope.get("create_paths")
        or scope.get("allowed_modify_paths")
        or contract.get("allowed_modify_paths")
        or contract.get("allowed_create_paths")
    )


def _should_force_verify_only(supervisor: dict[str, Any], task_intent: dict[str, Any]) -> bool:
    """Route semantic verification requests to a non-writing verifier mode.

    This intentionally relies on the supervisor/task-intent semantic fields
    instead of keyword matching. The hard rule is only consistency: if the
    model says verification is required and no write intent is present, the
    run should verify, not produce an analysis report.
    """
    if _has_write_intent(task_intent):
        return False
    if supervisor.get("mode") == "run_verify":
        return True
    if supervisor.get("operation_mode") == "verify_only":
        return True
    verifier_signal = supervisor.get("primary_agent") == "Verifier"
    readonly_signal = bool(
        supervisor.get("read_only")
        or supervisor.get("agent_read_only")
        or task_intent.get("agent_read_only")
        or task_intent.get("write_locked")
    )
    return bool(supervisor.get("requires_verification") and (verifier_signal or readonly_signal))


def supervisor_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("supervisor_start", task=state.get("task"), incoming_mode=state.get("mode"))
    current_mode = state.get("mode", "auto")
    task_spec = state.get("task_spec") or {}
    incoming_intent = state.get("task_intent") or {}
    intake_intent_resolved = _has_resolved_intake_intent(incoming_intent)
    fallback = _heuristic_mode(
        state.get("task", ""),
        current_mode,
        task_spec,
        resolved_intent=incoming_intent if intake_intent_resolved else None,
    )
    obj = fallback
    # Intake owns semantic intent. A second LLM classification adds cost and
    # can corrupt a valid scope when its JSON is truncated. Only use the
    # supervisor LLM when intake could not produce a valid semantic decision.
    if intake_intent_resolved:
        trace.event("supervisor_reused_intake_intent", task_intent=incoming_intent)
    else:
        try:
            client = OpenAICompatClient("configs/model.yaml", messages_path=state["messages_path"])
            context = _compact_supervisor_context(task_spec, incoming_intent)
            text = client.chat([
                {"role": "system", "content": SUPERVISOR_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Task:\n{state.get('task')}\n\n"
                        f"Resolved intake context:\n{context}\n\n"
                        f"Current mode: {current_mode}\n"
                        f"Routing suggestion: {fallback}"
                    ),
                },
            ], purpose="supervisor", max_tokens=900)
            cand = extract_json_object(text)
            if cand.get("mode") in {"analyze", "write", "modify", "debug", "generate_project", "repair_existing", "run_verify"}:
                obj = {**fallback, **cand}
        except Exception as e:
            trace.event("supervisor_llm_failed", error=str(e)[:2000], fallback=fallback)
            obj = fallback

    # The LLM performs first-pass intent analysis, while deterministic consistency
    # resolver prevents conflating script_read_only/scan_first with global agent_read_only.
    if intake_intent_resolved:
        task_intent = dict(incoming_intent)
    else:
        intent_input = dict(task_spec)
        for key in ("write_scope_intent", "read_only", "create_paths", "read_reference_paths"):
            if key in obj:
                intent_input[key] = obj[key]
        task_intent = classify_task_intent(state.get("task", ""), intent_input)
    read_only_policy = task_intent.get("read_only_policy") or state.get("read_only_policy") or detect_global_read_only_lock(state.get("task", ""))
    resolved_mode = obj.get("mode", fallback["mode"])
    force_verify_only = _should_force_verify_only(obj, task_intent)
    if force_verify_only:
        task_intent = dict(task_intent)
        task_intent["mode"] = "run_verify"
        task_intent["operation_mode"] = "verify_only"
        task_intent["agent_read_only"] = True
        task_intent["source_modify_intent"] = False
        task_intent["auxiliary_create_intent"] = False
        resolved_mode = "run_verify"
    elif task_intent.get("write_locked") or read_only_policy.get("locked"):
        resolved_mode = "analyze"
    elif task_intent.get("source_modify_intent"):
        resolved_mode = "debug" if task_intent.get("fix_requested") else "modify"
    elif task_intent.get("create_requested"):
        resolved_mode = "generate_project" if resolved_mode == "generate_project" else "write"
    elif task_intent.get("fix_requested") and resolved_mode == "analyze":
        resolved_mode = "debug"
    elif task_intent.get("modify_requested") and resolved_mode == "analyze":
        resolved_mode = "modify"
    resolved_read_only = bool(task_intent.get("agent_read_only") or task_intent.get("write_locked") or read_only_policy.get("locked"))
    obj["mode"] = resolved_mode
    obj["read_only"] = resolved_read_only
    obj["agent_read_only"] = resolved_read_only
    obj["script_read_only"] = bool(task_intent.get("script_read_only"))
    obj["scan_first"] = bool(task_intent.get("scan_first"))
    obj["operation_mode"] = task_intent.get("operation_mode")
    obj["task_intent"] = task_intent
    obj["read_only_policy"] = read_only_policy
    obj["write_locked"] = bool(task_intent.get("write_locked") or read_only_policy.get("locked"))
    obj["allowed_write"] = not resolved_read_only
    if resolved_mode == "run_verify":
        obj["operation_mode"] = "verify_only"
        obj["requires_verification"] = True
        obj["primary_agent"] = "Verifier"
        obj["allowed_write"] = False
    if obj["write_locked"] and resolved_mode != "run_verify":
        obj["operation_mode"] = "read_only_analysis"
        obj["requires_verification"] = False
    obj["external_tests_protected"] = protects_external_tests(state.get("task", "")) or resolved_mode in {"debug", "modify", "repair_existing"}
    state["mode"] = resolved_mode
    state["read_only"] = resolved_read_only
    state["write_locked"] = bool(obj["write_locked"])
    state["read_only_policy"] = read_only_policy
    state["task_intent"] = task_intent
    state["scope_contract"] = task_intent.get("scope_contract") or {}
    state["supervisor"] = obj
    state["task_contract"] = extract_task_contract(state.get("task", ""), state.get("task_spec", {}), obj)
    contract_atoms = (state.get("task_contract") or {}).get("requirement_atoms", [])
    if state.get("resumed_from_checkpoint") and state.get("verification_claims") and state.get("workspace"):
        resumed_check = evaluate_requirement_atoms(str(state["workspace"]), list(contract_atoms), state=state)
        state["requirement_atom_check"] = resumed_check
        state["requirement_atoms"] = resumed_check.get("atoms", contract_atoms)
        state["requirement_atom_summary"] = resumed_check.get("summary", {})
    else:
        state["requirement_atoms"] = contract_atoms
        state["requirement_atom_summary"] = (state.get("task_contract") or {}).get("requirement_atom_summary", {})
    state["write_scope_policy"] = build_write_scope_policy(state.get("task", ""), resolved_mode, resolved_read_only, state.get("file_plan"))
    invariants = state.setdefault("invariants", [])
    mode_invariant = f"Current mode is {state['mode']}; read_only={state['read_only']}; allowed_write={obj.get('allowed_write')}"
    if mode_invariant not in invariants:
        invariants.append(mode_invariant)
    trace.event(
        "supervisor_done",
        supervisor=obj,
        task_contract=state.get("task_contract"),
        requirement_atom_summary=state.get("requirement_atom_summary"),
        write_scope_policy=state.get("write_scope_policy"),
    )
    trace.snapshot(state)
    return state
