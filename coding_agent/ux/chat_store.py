from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from coding_agent.core.utils import append_jsonl, now_iso, read_json, write_json
from coding_agent.workspace.run_paths import agent_runs_root, safe_id


def default_chat_store_dir() -> Path:
    return agent_runs_root() / "chats"


def make_chat_session_id() -> str:
    return safe_id(f"chat_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}")


def chat_session_paths(store_dir: str | Path, session_id: str) -> dict[str, Path]:
    root = Path(store_dir) / safe_id(session_id)
    return {
        "root": root,
        "meta": root / "meta.json",
        "conversation": root / "conversation.jsonl",
        "llm_messages": root / "llm_messages.jsonl",
    }


def title_from_text(text: str, *, max_chars: int = 36) -> str:
    compact = " ".join(str(text or "").strip().split())
    if not compact:
        return "新对话"
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def create_chat_session(
    store_dir: str | Path,
    *,
    workspace: str | Path | None,
    model: str | None,
    base_url: str | None,
    title: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    sid = safe_id(session_id or make_chat_session_id())
    paths = chat_session_paths(store_dir, sid)
    paths["root"].mkdir(parents=True, exist_ok=True)
    now = now_iso()
    meta = {
        "id": sid,
        "title": title or "新对话",
        "created_at": now,
        "updated_at": now,
        "workspace": str(workspace) if workspace else None,
        "model": model,
        "base_url": base_url,
        "turns": 0,
    }
    write_json(paths["meta"], meta)
    return meta


def load_chat_meta(store_dir: str | Path, session_id: str) -> dict[str, Any] | None:
    path = chat_session_paths(store_dir, session_id)["meta"]
    if not path.exists():
        return None
    data = read_json(path)
    return data if isinstance(data, dict) else None


def save_chat_meta(store_dir: str | Path, meta: dict[str, Any]) -> None:
    session_id = str(meta.get("id") or "")
    if not session_id:
        raise ValueError("chat meta missing id")
    write_json(chat_session_paths(store_dir, session_id)["meta"], meta)


def list_chat_sessions(store_dir: str | Path, *, limit: int = 12, include_empty: bool = False) -> list[dict[str, Any]]:
    root = Path(store_dir)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for meta_path in root.glob("*/meta.json"):
        try:
            data = read_json(meta_path)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("id"):
            if not include_empty and int(data.get("turns") or 0) <= 0:
                continue
            out.append(data)
    out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return out[:limit]


def append_chat_turn(
    store_dir: str | Path,
    session_id: str,
    *,
    user_text: str,
    assistant_text: str,
) -> None:
    paths = chat_session_paths(store_dir, session_id)
    paths["root"].mkdir(parents=True, exist_ok=True)
    append_jsonl(paths["conversation"], {"ts": now_iso(), "role": "user", "content": user_text})
    append_jsonl(paths["conversation"], {"ts": now_iso(), "role": "assistant", "content": assistant_text})
    meta = load_chat_meta(store_dir, session_id) or {"id": session_id}
    if not meta.get("title") or meta.get("title") == "新对话":
        meta["title"] = title_from_text(user_text)
    meta["updated_at"] = now_iso()
    meta["turns"] = int(meta.get("turns") or 0) + 1
    save_chat_meta(store_dir, meta)


def load_chat_history(store_dir: str | Path, session_id: str, *, max_messages: int = 20) -> list[dict[str, str]]:
    path = chat_session_paths(store_dir, session_id)["conversation"]
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = str(data.get("role") or "")
            content = str(data.get("content") or "")
            if role in {"user", "assistant"} and content:
                rows.append({"role": role, "content": content})
    return rows[-max_messages:]


def resolve_chat_session_choice(store_dir: str | Path, choice: str) -> dict[str, Any] | None:
    raw = str(choice or "").strip()
    sessions = list_chat_sessions(store_dir, limit=30)
    if not raw:
        return sessions[0] if sessions else None
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]
    for item in sessions:
        if raw == str(item.get("id") or ""):
            return item
    return None
