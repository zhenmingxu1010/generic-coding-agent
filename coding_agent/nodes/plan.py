from __future__ import annotations

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.tools.registry import tool_name_union
from coding_agent.core.utils import extract_json_object
from coding_agent.verification.test_baseline import capture_test_baseline
from .common import get_trace


PLAN_SYSTEM = """You are the planning node of a multi-mode repo-level coding agent.
Return JSON only. Do not write prose.
Available tools: __TOOL_NAMES__.
Important distinction:
- filter_files searches file paths.
- search_text searches file contents.
Planning requirements by mode:
- analyze: read-only evidence gathering and report.
- write: create requested script/files, add minimal tests or demo input, run compile/pytest/demo.
- generate_project: create project files, tests, README, demo command, verify.
- modify: identify edit scope, read files, patch implementation, run verification.
- debug/repair_existing: reproduce failure, diagnose, patch implementation, verify.
Schema:
{
  "mode": "analyze|write|modify|debug|generate_project|repair_existing|run_verify",
  "steps": [
    {"id": 1, "goal": "...", "tool_hint": "__TOOL_NAMES__"}
  ],
  "verification": {"commands": [["python", "-m", "compileall", "-q", "."]], "notes": "..."},
  "risk_notes": ["..."]
}
Rules:
- Do not assume every task is project generation.
- Do not plan writes when read_only=true.
- For code-writing modes, include verification steps.
- Prefer patch/edit scope minimization: read before writing.
""".replace("__TOOL_NAMES__", tool_name_union())


def _default_verify_for_mode(mode: str, repo_map: dict) -> list[list[str]]:
    cmds: list[list[str]] = []
    if repo_map.get("py_files"):
        cmds.append(["python", "-m", "compileall", "-q", "."])
    if repo_map.get("has_tests"):
        cmds.append(["python", "-m", "pytest", "-q"])
    if mode in {"write", "generate_project"} and not cmds:
        cmds.append(["python", "-m", "compileall", "-q", "."])
    return cmds


def plan_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("plan_start", mode=state.get("mode"))
    client = OpenAICompatClient("configs/model.yaml", messages_path=state["messages_path"])
    user = (
        f"Task:\n{state.get('task')}\n\n"
        f"Mode: {state.get('mode')}\nSupervisor: {state.get('supervisor')}\nRead-only: {state.get('read_only')}\nTask contract: {state.get('task_contract')}\n\n"
        f"Repo map summary: files={len((state.get('repo_map') or {}).get('files', []))}, py_files={len((state.get('repo_map') or {}).get('py_files', []))}, project_types={(state.get('repo_map') or {}).get('project_types')}\n\n"
        f"Context summary:\n{state.get('context_summary','')}\n\n"
        "Create a concise execution plan for this mode."
    )
    try:
        text = client.chat([
            {"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": user},
        ], purpose=f"plan:{state.get('mode')}")
        plan = extract_json_object(text)
    except Exception as e:
        trace.event("plan_llm_failed", error=str(e)[:2000])
        plan = {"mode": state.get("mode"), "steps": [{"id": 1, "goal": "inspect files", "tool_hint": "list_files"}], "verification": {"commands": _default_verify_for_mode(state.get("mode", "modify"), state.get("repo_map") or {}), "notes": "fallback plan"}, "risk_notes": ["LLM plan failed; using fallback"]}
    plan["mode"] = state.get("mode")
    if state.get("read_only"):
        for step in plan.get("steps", []):
            if step.get("tool_hint") in {"write_file", "edit_file"}:
                step["tool_hint"] = "finish"
                step["goal"] = "read-only mode blocks writes; finish with a report"
        plan.setdefault("risk_notes", []).append("read_only=true: write_file/edit_file are blocked by policy")
    if state.get("mode") != "analyze":
        plan.setdefault("verification", {})
        if not plan["verification"].get("commands"):
            plan["verification"]["commands"] = _default_verify_for_mode(state.get("mode", "modify"), state.get("repo_map") or {})
    state["plan"] = plan
    state["plan_step_idx"] = 0
    state["completed_steps"] = []
    state["blocked_steps"] = []
    state["test_baseline"] = capture_test_baseline(state)
    trace.event("plan_done", plan=state["plan"], test_baseline=state["test_baseline"])
    trace.snapshot(state)
    return state
