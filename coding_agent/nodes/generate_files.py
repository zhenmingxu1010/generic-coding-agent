from __future__ import annotations

from pathlib import Path

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.verification.test_registry import refresh_verification_test_registry
from coding_agent.tools.file_tools import write_file
from coding_agent.scope.write_guard import prewrite_backup
from coding_agent.scope.write_intent import can_execute_write_intent
from coding_agent.memory.artifact_provenance import record_artifact_event
from coding_agent.workspace.failed_writes import record_failed_write
from coding_agent.core.utils import sha16, truncate
from coding_agent.nodes.file_plan import _planning_evidence
from .common import get_trace


FILE_CONTENT_SYSTEM = """You are the file generation node of a general Coding Agent.
Generate exactly the content for ONE file in a file plan.
Do not output JSON. Do not add explanations. Do not wrap in markdown unless the file itself needs markdown.
The content must be complete and valid for the target file.
Follow the task contract and make the file consistent with the other planned files.
For Python files, produce syntactically valid Python 3.10 code.
For tests, write meaningful pytest tests for the requested behavior, not weak tests.
For tests, exercise the task's public contract such as CLI behavior, documented outputs, and user-requested files. Do not invent or lock down private helper function names/signatures unless the task explicitly requires that API.
Do not turn speculative edge cases or unstated formatting preferences into mandatory assertions. Tests should constrain the requested public behavior, while optional extra cases should remain consistent with the implementation and documentation.
"""


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t + ("\n" if t and not t.endswith("\n") else "")


def _record_patch(state: dict, result: dict) -> None:
    data = result.get("data", {}) or {}
    if not data.get("changed"):
        return
    patches_dir = Path(state.get("patches_dir") or (Path(state["run_dir"]) / "patches"))
    patches_dir.mkdir(parents=True, exist_ok=True)
    idx = len(state.get("repair_history", [])) + 1
    path = data.get("path") or "unknown"
    safe_name = str(path).replace("/", "__").replace("\\", "__")
    diff_path = patches_dir / f"generate_{state.get('round_idx',0):03d}_{idx:03d}_{safe_name}.diff"
    diff = data.get("diff") or ""
    if diff:
        diff_path.write_text(diff, encoding="utf-8")
    state.setdefault("repair_history", []).append({
        "round_idx": state.get("round_idx", 0),
        "mode": state.get("mode"),
        "strategy": "generate_files.write_file",
        "changed": True,
        "files_changed": [path],
        "before_sha16": data.get("before_sha16"),
        "after_sha16": data.get("after_sha16"),
        "diff_path": str(diff_path) if diff else None,
        "message": result.get("message", ""),
    })


def _generated_sibling_context(
    state: dict,
    current_path: str,
    *,
    max_chars: int = 12000,
) -> str:
    """Read already-written planned siblings so later files share one contract."""
    workspace = Path(state["workspace"])
    blocks: list[str] = []
    used = 0
    for item in state.get("generated_files") or []:
        path = str(item.get("path") or "")
        if not path or path == current_path or item.get("ok") is False:
            continue
        full = workspace / path
        if not full.is_file():
            continue
        content = full.read_text(encoding="utf-8", errors="replace")
        header = f"===== {path} (kind={item.get('kind') or 'unknown'}) =====\n"
        remaining = max_chars - used - len(header)
        if remaining <= 0:
            break
        snippet = truncate(content, min(5000, remaining))[:remaining]
        block = header + snippet
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def generate_files_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("generate_files_start", file_plan=state.get("file_plan"), mode=state.get("mode"), stopped_reason=state.get("stopped_reason"))
    if state.get("write_locked") or state.get("read_only"):
        state["failure"] = {
            "failure_type": "read_only_violation",
            "priority": 0,
            "message": "generate_files was reached during a read-only/write-locked task",
            "signature": "read_only_generate_files",
            "raw_excerpt": str({"mode": state.get("mode"), "read_only_policy": state.get("read_only_policy")})[:2000],
            "source": "generate_files_node",
        }
        state["stopped_reason"] = "read_only_violation"
        state["needs_verification"] = False
        trace.event("generate_files_blocked_read_only", failure=state.get("failure"))
        trace.snapshot(state)
        return state
    if state.get("stopped_reason") in {"file_plan_no_writable_targets", "approval_required", "runtime_exception", "llm_timeout"}:
        trace.event("generate_files_skipped", reason=state.get("stopped_reason"), failure=state.get("failure"))
        trace.snapshot(state)
        return state
    client = OpenAICompatClient("configs/model.yaml", messages_path=state["messages_path"])
    plan = state.get("file_plan") or {}
    files = [item for item in plan.get("files") or [] if isinstance(item, dict)]
    files = sorted(enumerate(files), key=lambda pair: (pair[1].get("kind") == "test", pair[0]))
    files = [item for _, item in files]
    generated = state.setdefault("generated_files", [])
    changed_files = state.setdefault("changed_files", [])

    for item in files:
        path = item.get("path")
        if not path:
            continue
        original_path = item.get("original_path") or path
        existing = ""
        full = Path(state["workspace"]) / path
        if full.exists() and full.is_file():
            existing = full.read_text(encoding="utf-8", errors="replace")
        sibling_context = _generated_sibling_context(state, str(path))
        prompt = (
            f"Task:\n{state.get('task')}\n\n"
            f"Mode: {state.get('mode')}\n"
            f"Task contract:\n{state.get('task_contract')}\n\n"
            f"Complete file plan:\n{plan}\n\n"
            f"Task-relevant repository evidence:\n{_planning_evidence(state, max_chars=14000)}\n\n"
            f"Generate content for this file only:\npath={path}\nkind={item.get('kind')}\npurpose={item.get('purpose')}\n\n"
            f"Original requested project-relative path: {original_path}\n"
            f"Already generated sibling files (their actual content is authoritative):\n{sibling_context or 'none yet'}\n\n"
            "Generated verification tests may be placed under .coding_agent_test/<thread-id>; those tests are agent-owned and should exercise the public behavior of delivered project files. "
            "When generating such tests, locate delivered code relative to the workspace root from Path(__file__).resolve(), not by importing private helper APIs unless explicitly required.\n\n"
            f"Existing content sha16={sha16(existing) if existing else 'new-file'}; existing preview:\n{truncate(existing, 2000)}\n\n"
            "Return only the file content."
        )
        try:
            text = client.chat([
                {"role": "system", "content": FILE_CONTENT_SYSTEM},
                {"role": "user", "content": prompt},
            ], purpose=f"generate_file:{path}", max_tokens=3200)
            content = _strip_code_fence(text)
        except Exception as e:
            trace.event("generate_file_llm_failed", path=path, error=str(e))
            state["failure"] = {
                "failure_type": "llm_file_generation_error",
                "priority": 2,
                "message": f"LLM failed to generate {path}: {e}",
                "target_file": path,
                "signature": "llm_file_generation_error",
                "raw_excerpt": str(e),
            }
            continue
        target = Path(state["workspace"]) / path
        allowed, reason, details = can_execute_write_intent(state, str(path), exists=target.exists())
        if not allowed:
            result = {
                "tool": "write_file",
                "ok": False,
                "message": reason,
                "data": {"blocked_by_policy": True, "approval_required": bool(details.get("approval_required")), "path": path, **details},
            }
        else:
            backup = prewrite_backup(state, str(path)) if target.exists() else None
            res = write_file(state["workspace"], path, content)
            result = res.model_dump()
            result.setdefault("data", {})["write_intent"] = details.get("write_intent")
            if backup:
                result.setdefault("data", {})["prewrite_backup"] = backup
        failed_write = None
        data = result.get("data", {}) or {}
        if (not result.get("ok")) and data.get("rejected_write") and (data.get("syntax_check") or {}).get("checked"):
            failed_write = record_failed_write(
                state,
                path=str(path),
                content=content,
                tool="write_file",
                result=result,
                source="generate_files_node",
            )
            result.setdefault("data", {})["failed_write"] = failed_write
        generated.append({
            "path": path,
            "ok": result.get("ok"),
            "message": result.get("message"),
            "syntax_check": (result.get("data") or {}).get("syntax_check"),
            "failed_write": failed_write,
            "kind": item.get("kind"),
            "original_path": item.get("original_path"),
            "verification_role": "test" if item.get("kind") == "test" else None,
        })
        if (result.get("data") or {}).get("changed"):
            changed_files.append(path)
            _record_patch(state, result)
            data = result.get("data", {}) or {}
            try:
                record_artifact_event(
                    state["workspace"],
                    path=path,
                    thread_id=state.get("thread_id"),
                    task=state.get("task"),
                    action="generate_files.write_file",
                    origin="agent_modified" if backup else "agent_generated",
                    kind=item.get("kind"),
                    before_sha16=data.get("before_sha16"),
                    after_sha16=data.get("after_sha16"),
                )
            except Exception:
                pass
        refresh_verification_test_registry(state, existing_only=False)
        if not result.get("ok"):
            data = result.get("data", {}) or {}
            if data.get("approval_required"):
                state["approval_required"] = {"path": path, "reason": result.get("message"), "data": data}
                state["stopped_reason"] = "approval_required"
            state["failure"] = {
                "failure_type": "approval_required" if data.get("approval_required") else ("syntax_level_error" if (data.get("syntax_check") or {}).get("checked") else "file_write_error"),
                "priority": 1,
                "message": result.get("message", "file generation failed"),
                "target_file": path,
                "signature": data.get("after_sha16") or "file_generation_failed",
                "raw_excerpt": str(data.get("syntax_check") or result),
                "failed_write": failed_write,
                "source": "generate_files_node",
            }
    state["needs_verification"] = True
    state["verification_reason"] = "generate_files completed; execution verification is required"
    trace.event("generate_files_done", generated_files=generated, changed_files=changed_files, failure=state.get("failure"), needs_verification=state.get("needs_verification"), prewrite_backups=state.get("prewrite_backups"))
    trace.snapshot(state)
    return state
