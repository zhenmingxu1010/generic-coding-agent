from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from coding_agent.workspace.run_paths import project_memory_dir_for


def _path(workspace: str | Path) -> Path:
    return project_memory_dir_for(workspace) / "reflexions.jsonl"


def append_reflexion(workspace: str | Path, item: dict[str, Any]) -> Path:
    p = _path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(item)
    rec.setdefault("version", "v1.18")
    rec.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


def load_recent_reflexions(workspace: str | Path, limit: int = 8) -> list[dict[str, Any]]:
    p = _path(workspace)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return rows[-limit:]


def build_reflexion_from_state(state: dict[str, Any]) -> dict[str, Any]:
    failure = state.get("failure") or {}
    last = state.get("last_tool_result") or {}
    decision = state.get("strategy_decision") or {}
    issues = state.get("failure_issues") or []
    lesson = "Use verification feedback to choose a concrete repair action."
    avoid: list[str] = []
    next_strategy = "inspect_or_patch"
    if failure.get("failure_type") == "file_write_error" or (last.get("data") or {}).get("blocked_by_policy"):
        lesson = "A write failed due to policy; inspect write_intents and choose an allowed target, or stop with approval_required instead of retrying blocked writes."
        avoid = ["retry_same_blocked_write"]
        next_strategy = "repair_write_intent_or_allowed_target"
    elif state.get("failure_issue_owner_summary") == "implementation_and_generated_test":
        lesson = "The failure involves both implementation and generated test/API mismatch; fix interface consistency and implementation behavior together, not just the test."
        avoid = ["fix_only_generated_test", "repeat_read_without_patch"]
        next_strategy = "patch_implementation_and_generated_test"
    elif failure.get("failure_type") in {"import_level_error", "name_error_impl", "attribute_error_impl"}:
        lesson = "Import/name errors usually require aligning test imports with implementation API or adding the missing public function."
        avoid = ["repeat_pytest_without_code_change"]
        next_strategy = "interface_consistency_patch"
    elif failure.get("failure_type") == "runtime_error":
        lesson = "Runtime errors require changing implementation data handling, not weakening tests."
        avoid = ["repeat_run_without_change"]
        next_strategy = "patch_runtime_logic"
    return {
        "thread_id": state.get("thread_id"),
        "task_excerpt": str(state.get("task", ""))[:500],
        "failure_signature": failure.get("signature"),
        "failure_type": failure.get("failure_type"),
        "target_file": failure.get("target_file"),
        "issue_summary": state.get("failure_issue_owner_summary"),
        "strategy_decision": decision,
        "lesson": lesson,
        "avoid_actions": avoid,
        "next_strategy": next_strategy,
        "last_tool": last.get("tool"),
    }
