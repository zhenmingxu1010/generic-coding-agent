from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coding_agent.core.utils import sha16, truncate
from coding_agent.workspace.run_paths import run_dir_for


def _norm(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _safe_name(path: str) -> str:
    safe = _norm(path).replace("/", "__").replace("\\", "__").strip("._")
    return safe or "unknown"


def _line_window(content: str, lineno: int | None, radius: int = 8) -> str:
    if not lineno:
        return truncate(content, 4000)
    lines = content.splitlines()
    start = max(1, int(lineno) - radius)
    end = min(len(lines), int(lineno) + radius)
    snippet = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1))
    return truncate(snippet, 4000)


def record_failed_write(
    state: dict[str, Any],
    *,
    path: str,
    content: str,
    tool: str,
    result: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    """Persist rejected write content so repair can reason about non-written files."""
    rel = _norm(path)
    run_dir = Path(state.get("run_dir") or run_dir_for(state["workspace"], state.get("thread_id")))
    failed_dir = run_dir / "failed_writes"
    failed_dir.mkdir(parents=True, exist_ok=True)

    records = state.setdefault("failed_writes", [])
    idx = len(records) + 1
    stem = f"{idx:03d}_{_safe_name(rel)}"
    draft_path = failed_dir / stem
    meta_path = failed_dir / f"{stem}.json"
    draft_path.write_text(content, encoding="utf-8")

    data = result.get("data") or {}
    syntax = data.get("syntax_check") or {}
    record = {
        "path": rel,
        "tool": tool,
        "source": source,
        "round_idx": state.get("round_idx", 0),
        "message": result.get("message"),
        "syntax_check": syntax,
        "content_sha16": sha16(content),
        "draft_path": str(draft_path),
        "draft_relpath": str(draft_path.relative_to(run_dir)).replace("\\", "/"),
        "meta_path": str(meta_path),
        "meta_relpath": str(meta_path.relative_to(run_dir)).replace("\\", "/"),
        "content_excerpt": _line_window(content, syntax.get("lineno")),
        "target_file_exists": (Path(state.get("workspace", "")) / rel).exists(),
    }
    meta_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    records.append(record)
    return record


def failed_write_for_path(state: dict[str, Any], path: str | None) -> dict[str, Any] | None:
    rel = _norm(path)
    if not rel:
        return None
    for record in reversed(state.get("failed_writes") or []):
        if isinstance(record, dict) and _norm(record.get("path")) == rel:
            return record
    return None
