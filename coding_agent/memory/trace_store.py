from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.core.utils import append_jsonl, now_iso, write_json


STREAM_CHUNK_EVENT_MARKERS = (
    "stream_chunk",
    "stream_delta",
    "message_chunk",
    "llm_delta",
)


def _is_stream_chunk_event(name: str) -> bool:
    low = str(name or "").lower()
    return any(marker in low for marker in STREAM_CHUNK_EVENT_MARKERS)


def _channel_for_event(name: str) -> str:
    low = str(name or "").lower()
    if low in {"tool_call", "tool_result"} or low.startswith("tool_"):
        return "tool"
    if low in {"verification_result"} or low.startswith("verify_"):
        return "verification"
    if low.startswith("final_gate"):
        return "final_gate"
    if low.startswith("llm_"):
        return "llm"
    if low.startswith("report_"):
        return "report"
    return "agent"


class TraceStore:
    def __init__(self, trace_path: str | Path, snapshot_path: str | Path | None = None):
        self.trace_path = Path(trace_path)
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None

    def event(self, name: str, **data: Any) -> None:
        if _is_stream_chunk_event(name):
            return
        payload = dict(data)
        reserved_payload = {}
        for key in ("ts", "schema", "event", "event_type", "channel", "source"):
            if key in payload:
                reserved_payload[key] = payload.pop(key)
        row = {
            "ts": now_iso(),
            "schema": "trace_event_v2",
            "event": name,
            "event_type": name,
            "channel": _channel_for_event(name),
            "source": "trace_store",
            **payload,
        }
        if reserved_payload:
            row["reserved_payload"] = reserved_payload
        append_jsonl(
            self.trace_path,
            row,
        )

    def snapshot(self, state: dict[str, Any]) -> None:
        if self.snapshot_path:
            write_json(self.snapshot_path, state)
