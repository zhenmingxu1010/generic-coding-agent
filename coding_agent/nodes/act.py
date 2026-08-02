from __future__ import annotations

from coding_agent.core.llm_client import OpenAICompatClient, LLMTimeoutError
from coding_agent.core.decision_guard import parse_agent_decision_with_self_correction
from coding_agent.tools.registry import tool_schema_text
from .common import get_trace


ACT_SYSTEM = """You are a specialist coding agent selected by a supervisor.
Choose exactly one next tool action. Return JSON only.
__TOOL_SCHEMA__
Mode behavior:
- analyze: read-only. Never write. Finish with a concrete report.
- write: create requested script/files, then run verification.
- generate_project: create runnable project files, tests, README, demo command, then verify.
- modify: read relevant files, edit implementation via edit_file or write_file, then verify.
- debug/repair_existing: reproduce failure if possible, inspect traceback files, patch real implementation, verify.
Rules:
- Work inside the workspace only.
- Use filter_files for path matching. Use search_text only for file content search.
- Prefer edit_file for small changes; use write_file for new files or full rewrites.
- For repairs, make the smallest evidence-backed change. Preserve return shapes,
  special-case branches, aliases, and adjacent behavior unless the task or a
  failing execution explicitly requires changing them.
- When an initial multi-file implementation batch is open, complete the
  remaining task-required source layers before requesting verification. Do not
  treat a successful edit to one file as completion of the whole task.
- Modify the existing reachable execution path that owns the requested
  behavior. Do not satisfy a change request by adding a new wrapper, alternate
  entry point, or helper that no existing caller invokes. If a wrapper is
  explicitly requested, wire it into the real call graph and preserve the
  original public entry point's contract.
- Do not apply a parameter or normalization to additional branches merely for
  consistency. Unrelated existing branches are compatibility constraints.
- When writing code, write complete valid files.
- Do not weaken tests to hide bugs. If tests are wrong, justify before editing tests.
- In read-only mode, never choose write_file or edit_file.
- If an action repeated with empty/no useful result, choose a different tool.
- For write/modify/debug/generate_project, do not call finish until verification commands have passed or you have explained why verification cannot run.
Schema:
{
  "thought_summary": "short operational reason, no hidden chain-of-thought",
  "action": {"tool": "...", "args": {...}},
  "expectation": "what should happen next"
}
""".replace("__TOOL_SCHEMA__", tool_schema_text())


def act_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("act_start", round_idx=state.get("round_idx", 0), mode=state.get("mode"), read_only=state.get("read_only"))
    client = OpenAICompatClient("configs/model.yaml", messages_path=state["messages_path"])
    recent_obs = state.get("observations", [])[-6:]
    recent_actions = state.get("action_history", [])[-8:]
    user = (
        f"Task: {state.get('task')}\n\n"
        f"Mode: {state.get('mode')}\nSupervisor: {state.get('supervisor')}\nRead-only mode: {state.get('read_only')}\n"
        f"Write scope contract: {state.get('scope_contract')}\n"
        f"Audited scope expansions: {state.get('scope_expansions', [])}\n"
        f"Task contract: {state.get('task_contract')}\nNeeds verification: {state.get('needs_verification')} ({state.get('verification_reason')})\n\n"
        f"Initial implementation batch: open={state.get('implementation_batch_open', False)} "
        f"remaining={state.get('implementation_batch_remaining', [])}\n\n"
        f"Hard invariants:\n" + "\n".join(f"- {x}" for x in state.get("invariants", [])) + "\n\n"
        f"Context summary:\n{state.get('context_summary','')}\n\n"
        f"Plan:\n{state.get('plan')}\n"
        f"plan_step_idx={state.get('plan_step_idx')} completed={state.get('completed_steps')} blocked={state.get('blocked_steps')}\n\n"
        f"Recent actions:\n{recent_actions}\n\n"
        f"Recent observations:\n{recent_obs}\n\n"
        "Choose the next single tool action."
    )
    try:
        text = client.chat([
            {"role": "system", "content": ACT_SYSTEM},
            {"role": "user", "content": user},
        ], purpose=f"act:{state.get('mode')}")
        decision = parse_agent_decision_with_self_correction(
            raw_text=text,
            client=client,
            system_prompt=ACT_SYSTEM,
            state=state,
            trace=trace,
            role="act",
            purpose=f"act:{state.get('mode')}",
            max_attempts=2,
            max_tokens=3000,
        )
    except LLMTimeoutError as e:
        trace.event("act_llm_timeout", error=str(e)[:2000])
        state["failure"] = {"failure_type": "llm_timeout", "priority": 1, "message": str(e), "signature": "llm_timeout:act", "raw_excerpt": str(e)}
        state["stopped_reason"] = "llm_timeout"
        state["decision"] = {"thought_summary": "LLM timed out during act; terminating safely", "action": {"tool": "finish", "args": {"message": "LLM timed out during act", "report": state.get("context_summary", "")}}, "expectation": "final report will record timeout"}
        trace.snapshot(state)
        return state
    if state.get("read_only") and decision.action.tool in {"write_file", "edit_file"}:
        decision.action.tool = "finish"
        decision.action.args = {"message": "Blocked write action because read_only=true. Finishing with current report.", "report": state.get("context_summary", "")}
        decision.thought_summary = "read-only policy converted write action to finish"
    state["decision"] = decision.model_dump()
    trace.event("act_done", decision=state["decision"])
    trace.snapshot(state)
    return state
