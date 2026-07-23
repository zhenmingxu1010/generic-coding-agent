from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from pydantic import ValidationError

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.core.schemas import AgentDecision
from coding_agent.tools.registry import tool_name_union, tool_schema_text
from coding_agent.core import utils as json_utils
from coding_agent.core.utils import truncate
from coding_agent.repair.read_cache import iter_cached_chunks, request_is_cached

AGENT_DECISION_SCHEMA_TEXT = """
{
  "thought_summary": "short operational reason, no hidden chain-of-thought",
  "action": {
    "tool": "__TOOL_NAMES__",
    "args": {}
  },
  "expectation": "what should happen next"
}
""".replace("__TOOL_NAMES__", tool_name_union())

TOOL_SCHEMA_TEXT = tool_schema_text()


def _normalize_rel(path: Any) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _path_from_action_args(args: dict[str, Any] | None) -> str:
    args = args or {}
    return _normalize_rel(
        args.get("path")
        or args.get("file_path")
        or args.get("filepath")
        or args.get("filename")
        or args.get("file")
        or args.get("target_file")
        or args.get("target_path")
        or ""
    )


def _force_allows_one_targeted_read(tool: str, args: dict[str, Any] | None, force: dict[str, Any], state: dict[str, Any]) -> bool:
    if tool != "read_file":
        return False
    attempted = _path_from_action_args(args)
    if not attempted:
        return False
    allowed_paths = {
        _normalize_rel(item)
        for item in [
            force.get("required_path"),
            force.get("path"),
            *(force.get("allowed_target_files") or []),
            *(force.get("allowed_read_files") or []),
        ]
        if item
    }
    if allowed_paths and attempted not in allowed_paths:
        return False
    return not request_is_cached(state, attempted, args)


def _validate_agent_decision(obj: dict[str, Any]) -> AgentDecision:
    """Strictly validate an AgentDecision.

    This intentionally does not silently reinterpret arbitrary JSON data as an
    action. If an LLM emits {"value": 3600} or any other task-data object,
    the guard asks the LLM to correct itself instead of letting the runtime
    crash or fabricating a tool call.
    """
    return AgentDecision(**obj)


def _validate_force_repair_action(decision: AgentDecision, state: dict[str, Any]) -> None:
    force = state.get("force_repair_action") or {}
    if not isinstance(force, dict) or not force:
        return
    tool = decision.action.tool
    if tool == "finish":
        return
    required = str(force.get("required_tool") or "")
    allowed = {str(item) for item in force.get("allowed_tools") or [] if str(item)}
    blocked = {str(item) for item in force.get("blocked_tools") or [] if str(item)}
    if tool in blocked:
        raise ValueError(f"force_repair_action blocks tool {tool}")
    if required and tool != required:
        raise ValueError(f"force_repair_action requires tool {required}, got {tool}")
    if not required and _force_allows_one_targeted_read(tool, decision.action.args or {}, force, state):
        return
    if allowed and tool not in allowed:
        raise ValueError(f"force_repair_action allows only {sorted(allowed)}, got {tool}")
    required_path = str(force.get("required_path") or "")
    if required_path and tool in {"write_file", "edit_file"}:
        args = decision.action.args or {}
        attempted = _path_from_action_args(args)
        allowed_paths = {_normalize_rel(required_path)}
        allowed_paths.update(_normalize_rel(item) for item in force.get("allowed_target_files") or [] if item)
        if attempted not in allowed_paths:
            raise ValueError(
                "force_repair_action requires writing one of "
                f"{sorted(allowed_paths)}, got {attempted or '<missing path>'}"
            )


def _iter_json_objects(text: str) -> Iterator[dict[str, Any]]:
    text = json_utils._strip_json_fence(text)
    whole = json_utils._json_loads_repaired(text)
    if isinstance(whole, dict):
        yield whole

    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for start in starts:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        obj = json_utils._json_loads_repaired(text[start:i + 1])
                        if isinstance(obj, dict):
                            yield obj
                        break


def _extract_agent_decision_object(text: str) -> dict[str, Any]:
    candidates = list(_iter_json_objects(text))
    for obj in candidates:
        if isinstance(obj.get("action"), dict):
            return obj
    if candidates:
        return candidates[0]
    raise ValueError("No valid closed JSON object found in LLM response")


def _format_error(error: Exception, obj: Any | None = None) -> str:
    if isinstance(error, ValidationError):
        return error.errors().__repr__()
    return str(error)


def _repair_control_context(state: dict[str, Any]) -> str:
    force = state.get("force_repair_action")
    cache = state.get("repair_read_cache") or {}
    if not force and not cache:
        return ""
    parts = ["Repair control state:"]
    if force:
        parts.append(f"force_repair_action={force}")
    if cache:
        parts.append("cached_read_files:")
        for key, data in list(iter_cached_chunks(state))[-6:]:
            content = str(data.get("content") or "")
            parts.append(
                f"- {key}: lines {data.get('start_line')}-{data.get('end_line')} "
                f"of {data.get('total_lines')} sha={data.get('sha16')}\n"
                f"{truncate(content, 3500)}"
            )
    recent_edits = []
    for item in (state.get("action_history") or [])[-8:]:
        if not isinstance(item, dict) or item.get("tool") != "edit_file":
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        replacements = args.get("replacements")
        if isinstance(replacements, list):
            recent_edits.append(
                f"- path={args.get('path')} ok={item.get('ok')} changed={item.get('changed')}\n"
                f"  replacements={truncate(str(replacements), 1800)}"
            )
        else:
            recent_edits.append(
                f"- path={args.get('path')} ok={item.get('ok')} changed={item.get('changed')}\n"
                f"  old={truncate(str(args.get('old_text') or ''), 900)}\n"
                f"  new={truncate(str(args.get('new_text') or ''), 900)}"
            )
    if recent_edits:
        parts.append("recent_file_edits:\n" + "\n".join(recent_edits))
    return "\n".join(parts)


def parse_agent_decision_with_self_correction(
    *,
    raw_text: str,
    client: OpenAICompatClient,
    system_prompt: str,
    state: dict[str, Any],
    trace: Any,
    role: str,
    purpose: str,
    max_attempts: int = 2,
    max_tokens: int = 1400,
) -> AgentDecision:
    """Parse an LLM action response, asking the LLM to repair invalid output.

    Handles both invalid JSON and valid JSON with the wrong schema. The key
    behavior is agentic: the runtime does not invent business decisions. It
    reports the protocol error and asks the LLM to output a corrected tool
    decision under the exact schema.
    """
    last_text = raw_text
    last_error: Exception | None = None
    last_obj: Any | None = None

    for attempt in range(max_attempts + 1):
        try:
            obj = _extract_agent_decision_object(last_text)
            last_obj = obj
            decision = _validate_agent_decision(obj)
            _validate_force_repair_action(decision, state)
            if attempt > 0:
                trace.event(
                    f"{role}_decision_self_corrected",
                    attempt=attempt,
                    corrected_decision=decision.model_dump(),
                )
            return decision
        except Exception as e:
            last_error = e
            trace.event(
                f"{role}_decision_invalid",
                attempt=attempt,
                error_type=e.__class__.__name__,
                error=_format_error(e, last_obj),
                parsed_obj=last_obj,
                raw_preview=truncate(last_text, 4000),
            )
            if attempt >= max_attempts:
                break

            correction_prompt = (
                "Your previous response cannot be used by the Coding Agent runtime.\n"
                "This is a protocol/schema error, not a request to solve the task data directly.\n\n"
                f"Role: {role}\n"
                f"Task: {state.get('task')}\n"
                f"Mode: {state.get('mode')}\n"
                f"Read-only: {state.get('read_only')}\n"
                f"Task contract: {state.get('task_contract')}\n"
                f"Active failure: {state.get('failure')}\n\n"
                f"{_repair_control_context(state)}\n\n"
                "Required output schema is exactly this AgentDecision JSON object:\n"
                f"{AGENT_DECISION_SCHEMA_TEXT}\n\n"
                f"{TOOL_SCHEMA_TEXT}\n\n"
                "Common mistakes to fix:\n"
                "- If you output task data such as {\"value\": 3600}, replace it with a tool action.\n"
                "- If you output only {\"tool\": ..., \"args\": ...}, wrap it under action.\n"
                "- If a field is missing, add it.\n"
                "- If force_repair_action is present, obey its allowed_tools/required_tool; use cached_read_files instead of requesting another read.\n"
                "- If you choose write_file, keep the content concise enough that the entire JSON object is complete and closed.\n"
                "- Return JSON only. No markdown. No explanations.\n\n"
                f"Validation/parsing error:\n{_format_error(e, last_obj)}\n\n"
                f"Parsed object if any:\n{last_obj}\n\n"
                f"Original/previous response:\n{truncate(last_text, 6000)}\n\n"
                "Now output one corrected AgentDecision JSON object."
            )
            last_text = client.chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": correction_prompt},
                ],
                purpose=f"{purpose}:decision_self_correction:{attempt+1}",
                max_tokens=max_tokens,
            )

    # Exhausted correction attempts. Do not crash; turn the protocol failure into
    # a controlled finish action that the reporter can expose.
    signature = f"llm_{role}_decision_schema_error"
    state["failure"] = {
        "failure_type": "llm_decision_schema_error",
        "priority": 1,
        "message": f"LLM failed to produce valid AgentDecision after {max_attempts + 1} attempts: {_format_error(last_error) if last_error else 'unknown'}",
        "target_file": None,
        "signature": signature,
        "raw_excerpt": truncate(last_text, 6000),
        "source": "DecisionGuard",
    }
    if state.get("force_repair_action"):
        state["failure"]["failure_type"] = "repair_protocol_blocked"
        state["failure"]["message"] = (
            "LLM repeatedly chose actions blocked by force_repair_action: "
            + state["failure"]["message"]
        )
        state["stopped_reason"] = "repair_protocol_blocked"
        state["needs_verification"] = False
    trace.event(f"{role}_decision_self_correction_failed", failure=state["failure"])
    return AgentDecision(
        thought_summary="LLM action schema could not be repaired after retries",
        action={"tool": "finish", "args": {"message": "Stopped because LLM action schema was invalid", "report": truncate(last_text, 2000)}},
        expectation="report schema failure",
    )
