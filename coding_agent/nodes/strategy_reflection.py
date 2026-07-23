from __future__ import annotations

from coding_agent.core.llm_client import OpenAICompatClient, LLMTimeoutError
from coding_agent.core.decision_guard import parse_agent_decision_with_self_correction
from coding_agent.tools.registry import tool_name_union, tool_schema_text
from .common import get_trace
from coding_agent.memory.reflexion_store import load_recent_reflexions


STRATEGY_REFLECTION_SYSTEM = """You are the StrategyReflection node of a general Coding Agent.
The runtime has detected that the previous action/repair strategy is not making progress, or a tool/action schema was invalid.
Your job is to self-correct the strategy and output exactly one next tool action as valid JSON.

Return JSON only. Do not write prose.

Available tools: __TOOL_NAMES__.
__TOOL_SCHEMA__

Schema:
{
  "thought_summary": "short operational reason, no hidden chain-of-thought",
  "action": {"tool": "...", "args": {...}},
  "expectation": "what should happen next"
}

Rules:
- Do not repeat a tool call that the runtime says was no-op, empty, blocked, or schema-invalid.
- If a tool argument schema failed, output the same intended action with the exact tool schema.
- If local edit made no progress, inspect the file or rewrite the target file with write_file.
- If the runtime has blocked repeated reads or force_repair_action is active, do NOT choose read_file/read_many_files/filter_files/search_text.
- If force_repair_action.required_tool is set, choose that tool unless you finish with a concrete reason.
- If a repair changed code but created syntax errors, inspect the full target file before another local edit, or rewrite the full file.
- If tests and implementation disagree, inspect both before changing either.
- If a write was blocked because the target path is not allowed or not found, do not retry that path; use repo_map/context to choose a real existing source file, create only an allowed new test/artifact, or finish with a concrete blocker.
- The runtime, not you, decides success after verification.
""".replace("__TOOL_NAMES__", tool_name_union()).replace("__TOOL_SCHEMA__", tool_schema_text())


def strategy_reflection_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event(
        "strategy_reflection_start",
        mode=state.get("mode"),
        failure=state.get("failure"),
        last_tool_result=state.get("last_tool_result"),
    )
    client = OpenAICompatClient("configs/model.yaml", messages_path=state["messages_path"])
    banned = state.get("banned_actions", [])[-8:]
    recent_reflexions = load_recent_reflexions(state.get("workspace", ""), limit=8)
    state["recent_reflexions"] = recent_reflexions
    user = (
        f"Task:\n{state.get('task')}\n\n"
        f"Mode: {state.get('mode')}\n"
        f"Read-only: {state.get('read_only')}\n"
        f"Task contract:\n{state.get('task_contract')}\n\n"
        f"Current failure:\n{state.get('failure')}\n\n"
        f"Last tool result:\n{state.get('last_tool_result')}\n\n"
        f"Scope grounding:\n{state.get('scope_grounding')}\n\n"
        f"Scope contract:\n{state.get('scope_contract')}\n\n"
        f"Repository map summary:\n{ {k: (state.get('repo_map') or {}).get(k) for k in ['project_types', 'py_files', 'has_tests']} }\n\n"
        f"Recent action history:\n{state.get('action_history', [])[-10:]}\n\n"
        f"Recent repair history:\n{state.get('repair_history', [])[-8:]}\n\n"
        f"Recent observations:\n{state.get('observations', [])[-6:]}\n\n"
        f"Banned/recently failed action keys:\n{banned}\n\n"
        f"Force repair action:\n{state.get('force_repair_action')}\n"
        f"Repair action budget:\n{state.get('repair_action_budget')}\n"
        f"Failure issues:\n{state.get('failure_issues')}\n"
        f"Traceback issues:\n{state.get('traceback_issues')}\n"
        f"Interface check:\n{state.get('interface_check')}\n"
        f"Recent Reflexion lessons:\n{recent_reflexions}\n\n"
        f"Context summary:\n{state.get('context_summary', '')}\n\n"
        "The previous strategy is not acceptable. Explain briefly in thought_summary what changed in your strategy, then choose ONE different valid tool action."
    )
    try:
        text = client.chat(
            [
                {"role": "system", "content": STRATEGY_REFLECTION_SYSTEM},
                {"role": "user", "content": user},
            ],
            purpose=f"strategy_reflection:{state.get('mode')}",
            max_tokens=3000,
        )
        decision = parse_agent_decision_with_self_correction(
            raw_text=text,
            client=client,
            system_prompt=STRATEGY_REFLECTION_SYSTEM,
            state=state,
            trace=trace,
            role="strategy_reflection",
            purpose=f"strategy_reflection:{state.get('mode')}",
            max_attempts=2,
            max_tokens=3000,
        )
    except LLMTimeoutError as e:
        trace.event("strategy_reflection_llm_timeout", error=str(e)[:2000])
        state["failure"] = {"failure_type": "llm_timeout", "priority": 1, "message": str(e), "signature": "llm_timeout:strategy_reflection", "raw_excerpt": str(e)}
        state["stopped_reason"] = "llm_timeout"
        state["decision"] = {"thought_summary": "LLM timed out during strategy reflection; terminating safely", "action": {"tool": "finish", "args": {"message": "LLM timed out during strategy reflection", "report": state.get("context_summary", "")}}, "expectation": "final report will record timeout"}
        trace.snapshot(state)
        return state
    if state.get("read_only") and decision.action.tool in {"write_file", "edit_file"}:
        decision.action.tool = "finish"
        decision.action.args = {"message": "Blocked write action because read_only=true", "report": state.get("context_summary", "")}
        decision.thought_summary = "read-only policy converted write action to finish"
    state["decision"] = decision.model_dump()
    trace.event("strategy_reflection_done", decision=state["decision"])
    trace.snapshot(state)
    return state
