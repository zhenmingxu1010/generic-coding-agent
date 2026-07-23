from __future__ import annotations

from pathlib import Path

from coding_agent.memory.context_pack import write_context_pack
from coding_agent.memory.summary_store import write_context_summary
from coding_agent.core.utils import truncate, write_text_file
from .common import get_trace


def _write_short_term_memory(state: dict, summary: str) -> None:
    path = state.get("short_term_memory_path")
    if not path:
        return
    sections = [
        "# Short-Term Task Memory",
        f"thread_id={state.get('thread_id')} mode={state.get('mode')} round={state.get('round_idx')}",
        f"task={state.get('task')}",
        "\n## Current Summary",
        truncate(summary, 12000),
        "\n## Recent Actions",
        str((state.get("action_history") or [])[-12:])[:6000],
        "\n## Active Failure",
        str(state.get("failure"))[:4000],
    ]
    write_text_file(path, "\n".join(sections))


def context_compress_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("context_compress_start")
    state.setdefault("context_pack_path", str(Path(state["context_summary_path"]).with_name("context_pack.json")))
    pack = write_context_pack(state["context_pack_path"], state, max_chars=28000)
    state["context_pack"] = pack
    summary = write_context_summary(state["context_summary_path"], state, max_chars=28000)
    state["context_summary"] = summary
    _write_short_term_memory(state, summary)
    trace.event(
        "context_compress_done",
        version=pack.get("version", "context_pack_v2.1"),
        chars=len(summary),
        context_pack_path=state.get("context_pack_path"),
        evidence_block_count=(pack.get("budget") or {}).get("evidence_block_count"),
        rendered_chars=(pack.get("budget") or {}).get("rendered_chars"),
        selected_files=[x.get("path") for x in pack.get("selected_files", [])],
        short_term_memory_path=state.get("short_term_memory_path"),
        long_term_memory_path=state.get("long_term_memory_path"),
    )
    trace.snapshot(state)
    return state
