from __future__ import annotations

from typing import Any

from coding_agent.scope.task_intent import classify_task_intent

WRITE_MODES = {"write", "modify", "debug", "generate_project", "repair_existing"}
ANALYZE_MODES = {"analyze"}


def mode_requires_verification(mode: str | None, read_only: bool = False) -> bool:
    if read_only and mode == "analyze":
        return False
    return (mode or "") in WRITE_MODES or mode == "run_verify"


def mode_allows_write(mode: str | None, read_only: bool = False) -> bool:
    return (mode or "") in WRITE_MODES and not read_only


def write_tool_used(tool: str | None) -> bool:
    return tool in {"write_file", "edit_file"}


def verify_gate_reason(state: dict[str, Any]) -> str:
    if state.get("needs_verification"):
        return "needs_verification flag is set after a file-changing action"
    if mode_requires_verification(state.get("mode"), bool(state.get("read_only"))) and state.get("decision", {}).get("action", {}).get("tool") == "finish":
        return "finish requested in a write/modify/debug mode; execution verification is required"
    return ""


def explicit_read_only_requested(task: str) -> bool:
    from coding_agent.scope.write_scope import explicit_global_read_only_requested
    return explicit_global_read_only_requested(task)


def classify_mode_heuristic(task: str, current_mode: str | None = None, task_spec: dict | None = None) -> str:
    task_spec = task_spec or {}
    current_mode = current_mode or "auto"
    if current_mode in {"analyze", "write", "modify", "debug", "generate_project", "repair_existing", "run_verify"}:
        return current_mode
    intent = classify_task_intent(task or "", task_spec)
    return intent.get("mode") or ("write" if task_spec.get("task_type") in {"write_script", "generate_project"} else "modify")


def resolve_read_only(task: str, mode: str | None, supervisor_read_only: bool | None = None) -> bool:
    """Resolve global Agent write permission.

    The policy separates three meanings that must not be conflated:
    agent_read_only (this run cannot write), script_read_only (created script only
    reads inputs at runtime), and scan_first (read before create). Only
    agent_read_only maps to state["read_only"].
    """
    intent = classify_task_intent(task or "", {"read_only": supervisor_read_only})
    if intent.get("create_requested") or intent.get("fix_requested") or intent.get("modify_requested"):
        return False
    if intent.get("agent_read_only"):
        return True
    if mode == "analyze":
        return True
    if mode == "run_verify":
        return True
    if (mode or "") in WRITE_MODES:
        return False
    return bool(supervisor_read_only)


def protects_external_tests(task: str) -> bool:
    t = (task or "").lower()
    zh = task or ""
    return any(x in zh for x in ["不要删除测试", "不要改测试", "不能改测试", "不要弱化测试"]) or any(x in t for x in ["do not delete tests", "don't delete tests", "do not weaken tests", "don't weaken tests", "do not modify tests"])
