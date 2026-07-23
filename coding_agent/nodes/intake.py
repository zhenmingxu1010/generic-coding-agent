from __future__ import annotations

import re
from typing import Any

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.core.schemas import TaskSpec
from coding_agent.contracts.contract import extract_task_contract
from coding_agent.contracts.requirement_atoms import evaluate_requirement_atoms
from coding_agent.scope.read_only_policy import detect_global_read_only_lock
from coding_agent.core.utils import extract_json_object
from coding_agent.scope.task_intent import classify_task_intent, is_symbolic_agent_test_path
from coding_agent.scope.semantic_write_scope import WRITE_SCOPE_INTENT_SCHEMA
from .common import ensure_run_artifacts, get_trace


INTAKE_SYSTEM = """You are a coding-agent task intake module.
Read the entire user prompt before classifying the task.
Return JSON only. Do not write prose.
Schema:
{
  "task_type": "analyze|write_script|modify_code|fix_tests|generate_project|other",
  "objective": "clear objective",
  "constraints": ["..."],
  "success_criteria": ["..."],
  "requirements": [
    {
      "id": "short_stable_id",
      "kind": "artifact|behavior|constraint|quality",
      "scope": "deliverable",
      "description": "one independently verifiable requirement",
      "required": true,
      "path": "optional/relative/artifact.path",
      "evidence_mode": "execution|runtime|artifact|analysis",
      "user_evidence": ["exact excerpt copied from the user task"],
      "verification_hint": "what observable evidence would prove it"
    }
  ],
  "workflow_steps": [
    {"id": "short_id", "description": "agent process step such as inspection or verification"}
  ],
  "response_requirements": [
    {"id": "short_id", "description": "what the final response should explain"}
  ],
  "read_only": true,
  "agent_read_only": true,
  "script_read_only": false,
  "scan_first": false,
  "create_paths": ["relative/path.py"],
  "read_reference_paths": ["relative/input.json"],
  "write_scope_intent": {}
}
Definitions:
- agent_read_only/read_only: this run must not write any files.
- script_read_only: the script to be created should only read inputs at runtime; this does NOT make the agent run read-only.
- scan_first: the agent should inspect/read the repository before creating or modifying files.
Rules:
- Split the objective into independently verifiable requirements. Do not encode
  project-specific requirement types; use artifact, behavior, constraint, or quality.
- Requirements are user-visible deliverables, observable behavior, and explicit
  user constraints. Do not turn the agent's workflow into requirements: reading,
  diagnosing, planning, running verification, using tools, recording internal
  traces, or writing the final response are runtime responsibilities.
- Put those runtime responsibilities in workflow_steps. Put requests about the
  final explanation in response_requirements. Only requirements with
  scope=deliverable become final-gate obligations.
- Every deliverable requirement must include user_evidence containing the
  shortest exact excerpt from the user task that directly supports it.
- Do not infer extra deliverables from normal engineering practice. If no exact
  supporting excerpt exists, put the item in workflow_steps or omit it.
- Internal runtime policies are not part of the user's task contract and must
  not appear in create_paths, requirements, constraints, or success_criteria.
- An artifact requirement must name a concrete project-relative deliverable.
- Requirement IDs must be short, stable, unique, and meaningful for this task.
- A behavior requirement must describe an observable result, not an implementation detail.
- Use evidence_mode=execution for public behavior, runtime for constraints the
  agent runtime can observe (for example write scope or artifact placement),
  artifact for required files, and analysis for read-only report requirements.
- Decide source-write permission semantically from the whole task, not from isolated words.
- If the requested result requires changing existing project behavior, fixing an existing failure, or making existing tests pass, set write_scope_intent.source_modification.allowed=true.
- If the requested result is a separate report/script/artifact or existing project files must not be changed, set write_scope_intent.source_modification.allowed=false.
- Phrases such as "script should read" describe script behavior, not global agent permission.
- Scoped constraints like "do not modify training code/existing tests/results" protect those files but do not prevent creating new files.
- Only classify as analyze/read_only when the whole task is inspection/explanation with no requested new/modified artifacts.
""" + WRITE_SCOPE_INTENT_SCHEMA


def _normalize_grounding_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _user_evidence_quotes(item: dict[str, Any]) -> list[str]:
    raw = item.get("user_evidence")
    if raw is None:
        raw = item.get("source_quotes")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(value).strip() for value in raw if str(value).strip()]


def _requirement_is_user_grounded(task: str, item: dict[str, Any]) -> tuple[bool, list[str]]:
    task_text = _normalize_grounding_text(task)
    valid_quotes = [
        quote
        for quote in _user_evidence_quotes(item)
        if _normalize_grounding_text(quote)
        and _normalize_grounding_text(quote) in task_text
    ]
    return bool(valid_quotes), valid_quotes


def _ground_deliverable_requirements(
    task: str,
    requirements: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for index, raw in enumerate(requirements):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        if is_symbolic_agent_test_path(item.get("path")):
            dropped.append({
                "id": str(item.get("id") or f"requirement_{index + 1}"),
                "description": str(item.get("description") or ""),
                "reason": "symbolic internal test location is a runtime policy, not a deliverable artifact",
            })
            continue
        grounded, quotes = _requirement_is_user_grounded(task, item)
        if not grounded:
            dropped.append({
                "id": str(item.get("id") or f"requirement_{index + 1}"),
                "description": str(item.get("description") or ""),
                "reason": "no exact user-task evidence supports this hard requirement",
            })
            continue
        item["user_evidence"] = quotes
        accepted.append(item)
    return accepted, {
        "version": "requirement_grounding_v1",
        "accepted_ids": [str(item.get("id") or "") for item in accepted],
        "dropped": dropped,
    }


def _resolve_intake(task: str, obj: dict) -> dict:
    intent = classify_task_intent(task, obj)
    read_only_policy = intent.get("read_only_policy") or detect_global_read_only_lock(task)
    obj = dict(obj or {})
    deliverable_candidates: list[dict] = []
    workflow_steps = list(obj.get("workflow_steps") or [])
    response_requirements = list(obj.get("response_requirements") or [])
    for item in obj.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "deliverable").lower()
        if scope == "workflow":
            workflow_steps.append(item)
        elif scope == "response":
            response_requirements.append(item)
        elif scope == "deliverable":
            deliverable_candidates.append(item)
    deliverable_requirements, requirement_grounding = _ground_deliverable_requirements(
        task,
        deliverable_candidates,
    )
    obj["requirements"] = deliverable_requirements
    obj["requirement_grounding"] = requirement_grounding
    obj["workflow_steps"] = workflow_steps
    obj["response_requirements"] = response_requirements
    obj["success_criteria"] = [
        str(item.get("description") or "")
        for item in deliverable_requirements
        if str(item.get("description") or "").strip()
    ]
    obj["read_only"] = bool(intent["agent_read_only"])
    obj["agent_read_only"] = bool(intent["agent_read_only"])
    obj["write_locked"] = bool(intent.get("write_locked"))
    obj["read_only_policy"] = read_only_policy
    obj["script_read_only"] = bool(intent["script_read_only"])
    obj["scan_first"] = bool(intent["scan_first"])
    obj["create_paths"] = intent.get("create_paths", [])
    obj["read_reference_paths"] = intent.get("read_reference_paths", [])
    obj["write_scope_intent"] = obj.get("write_scope_intent") or intent.get("semantic_write_scope", {})
    obj["prohibited_artifacts"] = intent.get("prohibited_artifacts", [])
    if intent["mode"] == "analyze":
        obj["task_type"] = "analyze"
    elif intent["mode"] == "write":
        obj["task_type"] = "write_script" if obj.get("task_type") != "generate_project" else "generate_project"
    elif intent["mode"] == "debug":
        obj["task_type"] = "modify_code"
    elif intent["mode"] == "modify":
        obj["task_type"] = "modify_code"
    obj.setdefault("objective", task)
    obj.setdefault("constraints", [])
    obj.setdefault("success_criteria", [])
    obj.setdefault("requirements", [])
    obj.setdefault("workflow_steps", [])
    obj.setdefault("response_requirements", [])
    return obj, intent


def intake_node(state: dict) -> dict:
    state = ensure_run_artifacts(state)
    trace = get_trace(state)
    trace.event("intake_start", task=state.get("task"))
    client = OpenAICompatClient("configs/model.yaml", messages_path=state["messages_path"])
    state.setdefault("original_task", state.get("task", ""))
    runtime_instructions = str(state.get("task_runtime_instructions") or "").strip()
    user = f"Task:\n{state['task']}"
    if runtime_instructions:
        user += (
            "\n\nAgent runtime defaults (these are not user requirements and must not be "
            f"reported as such):\n{runtime_instructions}"
        )
    try:
        text = client.chat([
            {"role": "system", "content": INTAKE_SYSTEM},
            {"role": "user", "content": user},
        ], purpose="intake")
        obj = extract_json_object(text)
        obj, task_intent = _resolve_intake(state.get("task", ""), obj)
        spec = TaskSpec(**obj)
    except Exception as e:
        trace.event("intake_llm_failed", error=str(e)[:2000], fallback=True)
        obj, task_intent = _resolve_intake(state.get("task", ""), {})
        read_only = bool(obj.get("read_only"))
        task_type = obj.get("task_type") or ("analyze" if read_only else "write_script")
        spec = TaskSpec(
            task_type=task_type,
            objective=state.get("task", ""),
            constraints=[],
            success_criteria=[],
            read_only=read_only,
        )
    # Always compute deterministic consistency-resolved intent, even when the LLM succeeds.
    try:
        task_intent
    except NameError:
        task_intent = classify_task_intent(state.get("task", ""), spec.model_dump())
    state["task_intent"] = task_intent
    state["scope_contract"] = task_intent.get("scope_contract") or {}
    state["task_spec"] = spec.model_dump()
    state["read_only_policy"] = task_intent.get("read_only_policy") or detect_global_read_only_lock(state.get("task", ""))
    state["write_locked"] = bool(task_intent.get("write_locked") or (state.get("read_only_policy") or {}).get("locked"))
    state["read_only"] = bool(task_intent.get("agent_read_only", spec.read_only))
    if state["write_locked"]:
        state["read_only"] = True
        state["mode"] = "analyze"
        state["task_spec"]["task_type"] = "analyze"
        state["task_spec"]["read_only"] = True
    state.setdefault("round_idx", 0)
    state.setdefault("observations", [])
    state.setdefault("action_history", [])
    state.setdefault("repair_history", [])
    state.setdefault("failure_history", [])
    state.setdefault("completed_steps", [])
    state.setdefault("blocked_steps", [])
    state.setdefault("plan_step_idx", 0)
    state.setdefault("repeated_action_count", 0)
    state["mode"] = "analyze" if state.get("write_locked") else state.get("mode", "auto")
    state["task_contract"] = extract_task_contract(state.get("task", ""), state.get("task_spec", {}), {})
    contract_atoms = (state.get("task_contract") or {}).get("requirement_atoms", [])
    if state.get("resumed_from_checkpoint") and state.get("verification_claims") and state.get("workspace"):
        resumed_check = evaluate_requirement_atoms(str(state["workspace"]), list(contract_atoms), state=state)
        state["requirement_atom_check"] = resumed_check
        state["requirement_atoms"] = resumed_check.get("atoms", contract_atoms)
        state["requirement_atom_summary"] = resumed_check.get("summary", {})
    else:
        state["requirement_atoms"] = contract_atoms
        state["requirement_atom_summary"] = (state.get("task_contract") or {}).get("requirement_atom_summary", {})
    state.setdefault("needs_verification", False)
    state.setdefault("verification_reason", "")
    trace.event(
        "intake_done",
        task_spec=state["task_spec"],
        task_intent=state.get("task_intent"),
        read_only_policy=state.get("read_only_policy"),
        write_locked=state.get("write_locked"),
        task_contract=state.get("task_contract"),
        requirement_atom_summary=state.get("requirement_atom_summary"),
        read_only=state["read_only"],
    )
    trace.snapshot(state)
    return state
