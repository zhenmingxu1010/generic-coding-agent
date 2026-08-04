from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coding_agent.core.decision_guard import parse_agent_decision_with_self_correction
from coding_agent.core.llm_client import LLMTimeoutError, OpenAICompatClient
from coding_agent.core.utils import truncate
from coding_agent.memory.reflexion_store import load_recent_reflexions
from coding_agent.repair.import_error_context import build_import_error_context
from coding_agent.repair.read_cache import iter_cached_chunks
from coding_agent.repair.repair_controller import (
    build_repair_controller,
    finalized_controller_for_current_failure,
    force_action_from_controller,
)
from coding_agent.tools.registry import tool_name_union, tool_schema_text
from .common import get_trace


REPAIR_SYSTEM = """You are the repair node of a general coding agent.
Return exactly one AgentDecision JSON object and choose one tool action.
Available tools: __TOOL_NAMES__.
__TOOL_SCHEMA__

Rules:
- Repair the failed task requirement, not merely a test symptom.
- Treat executed command results, requirement claims, traceback data, and file
  contents as evidence. Do not invent a domain schema or project convention.
- Project/user tests are constraints. Agent-owned internal checks may be fixed
  only when the evidence shows their oracle is wrong.
- Use structured read tools for source inspection. Reuse cached reads when a
  file has not changed; repeated exploration is blocked by the runtime.
- Prefer edit_file for small changes and write_file for a complete rewrite of
  a current-agent generated file. When one file needs multiple known changes,
  use edit_file.replacements to apply all exact replacements atomically before
  verification.
- Never use write_file to replace a pre-existing project source file. Preserve
  its interfaces and use edit_file with exact, minimal replacements instead.
- Minimize the behavioral diff. Preserve existing special-case branches,
  return shapes, aliases, and adjacent control flow unless the failed
  requirement or execution evidence directly implicates them.
- Static deliverable-review findings are secondary to explicit task behavior
  and executed evidence. Never remove behavior required by the task merely to
  resolve a redundancy, dead-code, or unused-import observation. If behavior
  is implemented in the wrong layer, preserve it while relocating the owning
  implementation and remove only the incorrect duplicate.
- A new helper or wrapper is not a repair when the real public entry point
  never calls it. Patch the existing reachable execution path identified by
  the task or failure evidence; remove an unrequested disconnected alternate
  entry point after relocating its required behavior.
- When a subprocess prints the expected error but reports the wrong process
  exit code, inspect the launcher, module entry point, and console wrapper as
  well as the handler; a returned status must reach the operating system.
- Do not broaden a fix to additional branches merely for symmetry or
  consistency. An untouched branch is a compatibility constraint when no
  evidence says its behavior should change.
- When exact target file contents are already supplied, patch them directly;
  do not spend another action reading the same file.
- After a file-changing action, the graph runs verification automatically.
- Obey force_repair_action exactly. Finish with a concrete reason when the task
  cannot be completed within the allowed scope.

Schema:
{
  "thought_summary": "short operational reason",
  "action": {"tool": "one available tool", "args": {}},
  "expectation": "observable next result"
}
""".replace("__TOOL_NAMES__", tool_name_union()).replace("__TOOL_SCHEMA__", tool_schema_text())


def _cached_repair_reads_prompt(state: dict[str, Any], limit: int = 7000) -> str:
    chunks = list(iter_cached_chunks(state))[-6:]
    if not chunks:
        return ""
    per_chunk = max(1200, limit // len(chunks))
    parts: list[str] = []
    for key, data in chunks:
        parts.append(
            f"Cached read {key}: lines {data.get('start_line')}-{data.get('end_line')} "
            f"of {data.get('total_lines')} sha={data.get('sha16')}\n"
            f"{truncate(str(data.get('content') or ''), per_chunk)}"
        )
    return truncate("\n\n".join(parts), limit)


def _recent_actions_prompt(state: dict[str, Any], limit: int = 6000) -> str:
    """Keep repair decisions aware of their own recent edits without replaying files."""
    parts: list[str] = []
    for item in (state.get("action_history") or [])[-8:]:
        if not isinstance(item, dict):
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        tool = str(item.get("tool") or "")
        path = args.get("path") or args.get("file_path") or ""
        line = (
            f"round={item.get('round_idx')} tool={tool} path={path or '<none>'} "
            f"ok={item.get('ok')} changed={item.get('changed')} message={item.get('message')}"
        )
        if tool == "edit_file":
            replacements = args.get("replacements")
            if isinstance(replacements, list):
                line += "\nreplacements=" + truncate(
                    json.dumps(replacements, ensure_ascii=False),
                    2800,
                )
            else:
                line += (
                    f"\nold_text={truncate(str(args.get('old_text') or ''), 1200)}"
                    f"\nnew_text={truncate(str(args.get('new_text') or ''), 1200)}"
                )
        parts.append(line)
    return truncate("\n\n".join(parts), limit)


def _compact_json(value: Any, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    return truncate(text, limit)


def _failed_verification_prompt(state: dict[str, Any], limit: int = 9000) -> str:
    verification = state.get("verification") or {}
    failed_results: list[dict[str, Any]] = []
    for result in verification.get("results") or []:
        if not isinstance(result, dict):
            continue
        if int(result.get("returncode", 1) or 0) == 0 and not result.get("timed_out"):
            continue
        failed_results.append({
            "name": result.get("name"),
            "command": result.get("command"),
            "returncode": result.get("returncode"),
            "timed_out": bool(result.get("timed_out")),
            "stdout": truncate(str(result.get("stdout") or ""), 4500),
            "stderr": truncate(str(result.get("stderr") or ""), 2500),
        })
    tests = verification.get("test_results") or state.get("test_results") or {}
    payload = {
        "failed_results": failed_results,
        "test_summary": {
            key: tests.get(key)
            for key in ("total", "passed", "failed", "errors", "skipped")
            if key in tests
        },
        "test_failures": (tests.get("failures") or [])[:12],
        "test_issues": (tests.get("issues") or [])[:12],
    }
    return _compact_json(payload, limit)


def _failed_requirement_prompt(state: dict[str, Any], limit: int = 5000) -> str:
    checks = [
        state.get("requirement_atom_check"),
        (state.get("semantic_contract_check") or {}).get("requirement_atom_check"),
    ]
    atoms: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check in checks:
        if not isinstance(check, dict):
            continue
        for atom in check.get("atoms") or []:
            if not isinstance(atom, dict) or str(atom.get("status") or "") not in {"failed", "unverified"}:
                continue
            atom_id = str(atom.get("id") or "")
            if atom_id in seen:
                continue
            seen.add(atom_id)
            atoms.append({
                "id": atom_id,
                "type": atom.get("type"),
                "status": atom.get("status"),
                "description": atom.get("description"),
                "details": atom.get("details"),
            })
    return _compact_json(atoms, limit)


def _repair_target_contents_prompt(
    state: dict[str, Any],
    controller: dict[str, Any],
    limit: int = 22000,
) -> str:
    workspace = Path(str(state.get("workspace") or ".")).resolve()
    parts: list[str] = []
    used = 0
    targets = [
        str(rel or "").replace("\\", "/")
        for rel in controller.get("target_files") or []
        if str(rel or "").strip()
    ]
    last_result = state.get("last_tool_result") or {}
    last_data = last_result.get("data") if isinstance(last_result.get("data"), dict) else {}
    failed_path = str(last_data.get("path") or "").replace("\\", "/")
    if last_result.get("ok") is False and failed_path in targets:
        targets.remove(failed_path)
        targets.insert(0, failed_path)

    def head_tail(content: str, max_chars: int) -> str:
        if len(content) <= max_chars:
            return content
        head_size = max_chars // 2
        tail_size = max_chars - head_size
        return (
            content[:head_size]
            + "\n\n... <middle omitted; current file continues> ...\n\n"
            + content[-tail_size:]
        )

    for rel in targets:
        rel = str(rel or "").replace("\\", "/")
        if not rel:
            continue
        try:
            path = (workspace / rel).resolve()
            path.relative_to(workspace)
        except (OSError, ValueError):
            continue
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        remaining = limit - used
        if remaining <= 300:
            break
        block = (
            f"===== {rel} =====\n"
            f"{head_tail(content, min(10000, remaining - 100))}"
        )
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


def _finish_repair_call_budget(state: dict[str, Any], limit: int) -> dict[str, Any]:
    message = (
        f"Repair stopped after {limit} LLM repair decisions without verified success. "
        "Review the recorded failure evidence or rerun with a larger --max-repair-calls budget."
    )
    state["failure"] = {
        "failure_type": "repair_llm_call_budget_exhausted",
        "priority": 2,
        "message": message,
        "signature": "repair_llm_call_budget_exhausted",
        "raw_excerpt": str(state.get("failure") or {})[:3000],
        "source": "repair_node",
    }
    state["stopped_reason"] = "repair_llm_call_budget_exhausted"
    state["needs_verification"] = False
    state["decision"] = {
        "thought_summary": "repair LLM call budget exhausted",
        "action": {"tool": "finish", "args": {"message": message}},
        "expectation": "the final report exposes the bounded repair stop",
    }
    return state


def repair_node(state: dict[str, Any]) -> dict[str, Any]:
    trace = get_trace(state)
    trace.event("repair_start", failure=state.get("failure"), mode=state.get("mode"))
    controller = finalized_controller_for_current_failure(state)
    controller_reused = controller is not None
    if controller is None:
        controller = build_repair_controller(state)
    generated_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in state.get("generated_files") or []
        if isinstance(item, dict) and item.get("path")
    }
    existing_project_targets = [
        str(path).replace("\\", "/")
        for path in controller.get("target_files") or []
        if (
            path
            and (Path(str(state.get("workspace") or ".")) / str(path)).is_file()
            and str(path).replace("\\", "/") not in generated_paths
        )
    ]
    if existing_project_targets:
        controller["allowed_tools"] = [
            tool
            for tool in controller.get("allowed_tools") or []
            if tool != "write_file"
        ]
    generated_read_files = [
        str(item.get("path") or "").replace("\\", "/")
        for item in state.get("generated_files") or []
        if isinstance(item, dict) and item.get("path") and item.get("ok") is not False
    ]
    controller["allowed_read_files"] = list(dict.fromkeys(
        list(controller.get("target_files") or []) + generated_read_files
    ))[:24]
    state["repair_controller"] = controller
    state["repair_controller_reused"] = controller_reused
    if not state.get("import_error_context"):
        try:
            state["import_error_context"] = build_import_error_context(state)
        except Exception as exc:
            state["import_error_context"] = {"present": False, "error": str(exc), "source": "repair_node"}

    repair_call_limit = max(
        1,
        int(state.get("max_repair_llm_calls") or min(int(state.get("max_rounds", 12) or 12), 6)),
    )
    repair_call_count = int(state.get("repair_llm_call_count", 0) or 0)
    state["repair_llm_call_budget"] = {
        "limit": repair_call_limit,
        "used": repair_call_count,
        "remaining": max(0, repair_call_limit - repair_call_count),
    }
    if repair_call_count >= repair_call_limit:
        _finish_repair_call_budget(state, repair_call_limit)
        trace.event("repair_llm_call_budget_exhausted", budget=state["repair_llm_call_budget"], failure=state["failure"])
        trace.snapshot(state)
        return state

    force = force_action_from_controller(controller)
    if force:
        state["force_repair_action"] = force
        state["repair_action_budget"] = {
            "version": "repair_controller_v2",
            "active": True,
            "force_repair_action": force,
            "read_cache": state.get("repair_read_cache", {}),
            "read_budget": state.get("repair_read_budget", {}),
        }
    else:
        state.pop("force_repair_action", None)
        state.pop("repair_action_budget", None)

    recent_reflexions = load_recent_reflexions(state.get("workspace", ""), limit=8)
    state["recent_reflexions"] = recent_reflexions
    target_contents = _repair_target_contents_prompt(state, controller)
    prompt = (
        f"Task:\n{state.get('task')}\n\n"
        f"Mode and scope:\nmode={state.get('mode')} read_only={state.get('read_only')}\n"
        f"scope_contract={_compact_json(state.get('scope_contract'), 3500)}\n\n"
        f"Task contract:\n{_compact_json(state.get('task_contract'), 5000)}\n\n"
        f"Active failure:\n{_compact_json(state.get('failure'), 4500)}\n"
        f"Structured failure issues:\n{_compact_json(state.get('failure_issues'), 5000)}\n"
        f"Failed or unverified requirements:\n{_failed_requirement_prompt(state)}\n"
        f"Failed verification evidence:\n{_failed_verification_prompt(state)}\n"
        f"Import context when relevant:\n{_compact_json(state.get('import_error_context'), 3500)}\n\n"
        f"Failure owner and strategy:\n{state.get('failure_owner')}\n{_compact_json(state.get('strategy_decision'), 2500)}\n"
        f"Repair controller:\n{_compact_json({key: controller.get(key) for key in ('route', 'failure_owner', 'strategy', 'target_files', 'primary_issue', 'reason')}, 4500)}\n"
        f"Force repair action:\n{state.get('force_repair_action')}\n\n"
        f"Exact target file contents (patch directly when present):\n{target_contents}\n\n"
        f"Cached reads:\n{_cached_repair_reads_prompt(state, limit=12000)}\n\n"
        f"Last tool result (use current edit context after an exact-match failure):\n"
        f"{_compact_json(state.get('last_tool_result'), 6000)}\n\n"
        f"Recent tool actions (including exact edits):\n{_recent_actions_prompt(state)}\n\n"
        f"Recent reflexions:\n{_compact_json(recent_reflexions[-3:], 2500)}\n\n"
        "Choose one grounded next action. If one target file needs several known changes, "
        "send one edit_file action with a replacements array so verification runs only after all changes are applied."
    )
    state["repair_prompt_stats"] = {
        "chars": len(prompt),
        "target_content_chars": len(target_contents),
        "failure_signature": str((state.get("failure") or {}).get("signature") or ""),
    }

    client = OpenAICompatClient("configs/model.yaml", messages_path=state.get("messages_path"))
    state["repair_llm_call_count"] = repair_call_count + 1
    state["repair_llm_call_budget"] = {
        "limit": repair_call_limit,
        "used": repair_call_count + 1,
        "remaining": max(0, repair_call_limit - repair_call_count - 1),
    }
    try:
        raw = client.chat(
            [
                {"role": "system", "content": REPAIR_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            purpose=f"repair:{state.get('mode')}",
            max_tokens=3000,
        )
        decision = parse_agent_decision_with_self_correction(
            raw_text=raw,
            client=client,
            system_prompt=REPAIR_SYSTEM,
            state=state,
            trace=trace,
            role="repair",
            purpose=f"repair:{state.get('mode')}",
            max_attempts=1,
            max_tokens=3000,
        )
    except LLMTimeoutError as exc:
        state["failure"] = {
            "failure_type": "llm_timeout",
            "priority": 1,
            "message": str(exc),
            "signature": "llm_timeout:repair",
            "raw_excerpt": str(exc),
        }
        state["stopped_reason"] = "llm_timeout"
        state["decision"] = {
            "thought_summary": "LLM timed out during repair",
            "action": {"tool": "finish", "args": {"message": str(exc)}},
            "expectation": "the final report records the timeout",
        }
        trace.snapshot(state)
        return state

    if state.get("read_only") and decision.action.tool in {"write_file", "edit_file"}:
        decision.action.tool = "finish"
        decision.action.args = {"message": "repair write blocked because the run is read-only"}
    state["decision"] = decision.model_dump()
    trace.event("repair_decision_done", decision=state["decision"], repair_controller=controller)
    trace.snapshot(state)
    return state
