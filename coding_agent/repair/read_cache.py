from __future__ import annotations

from typing import Any, Iterable


def normalize_rel(path: Any) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def failure_signature(state: dict[str, Any]) -> str:
    failure = state.get("failure") or {}
    return str(failure.get("signature") or failure.get("failure_type") or "no_failure")


def requested_line_range(args: dict[str, Any] | None) -> tuple[int, int]:
    args = args or {}
    try:
        start = max(1, int(args.get("start_line", 1) or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        limit = max(1, int(args.get("limit", 220) or 220))
    except (TypeError, ValueError):
        limit = 220
    return start, start + limit - 1


def cache_key(state: dict[str, Any], rel: str) -> str:
    return f"{failure_signature(state)}|{normalize_rel(rel)}"


def read_chunks(entry: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(entry, dict):
        return []
    return [dict(item) for item in entry.get("reads") or [] if isinstance(item, dict)]


def _covered_end(requested_end: int, chunks: Iterable[dict[str, Any]]) -> int:
    totals = []
    for chunk in chunks:
        try:
            total = int(chunk.get("total_lines") or 0)
        except (TypeError, ValueError):
            total = 0
        if total > 0:
            totals.append(total)
    return min(requested_end, max(totals)) if totals else requested_end


def request_is_cached(
    state: dict[str, Any],
    rel: str,
    args: dict[str, Any] | None,
    *,
    current_sha16: str | None = None,
) -> bool:
    """Return true only when the requested line interval is already cached.

    Cache reuse is scoped to the active failure signature and the current file
    version. Reading a different part of the same file is new evidence, not a
    repeated action.
    """
    entry = (state.get("repair_read_cache") or {}).get(cache_key(state, rel))
    if not isinstance(entry, dict):
        return False
    if current_sha16 and entry.get("sha16") != current_sha16:
        return False

    chunks = read_chunks(entry)
    if not chunks:
        return False
    requested_start, requested_end = requested_line_range(args)
    requested_end = _covered_end(requested_end, chunks)

    ranges: list[tuple[int, int]] = []
    for chunk in chunks:
        try:
            start = int(chunk.get("start_line") or 0)
            end = int(chunk.get("end_line") or 0)
        except (TypeError, ValueError):
            continue
        if start > 0 and end >= start:
            ranges.append((start, end))
    if not ranges:
        return False

    cursor = requested_start
    for start, end in sorted(ranges):
        if end < cursor:
            continue
        if start > cursor:
            return False
        cursor = max(cursor, end + 1)
        if cursor > requested_end:
            return True
    return cursor > requested_end


def append_read_chunk(
    state: dict[str, Any],
    rel: str,
    chunk: dict[str, Any],
    *,
    current_sha16: str | None,
    source_tool: str,
) -> dict[str, Any]:
    cache = state.setdefault("repair_read_cache", {})
    key = cache_key(state, rel)
    previous = cache.get(key) if isinstance(cache.get(key), dict) else {}
    same_version = not previous or not current_sha16 or previous.get("sha16") == current_sha16
    chunks = read_chunks(previous) if same_version else []

    identity = (chunk.get("start_line"), chunk.get("end_line"), chunk.get("sha16"))
    if identity not in {
        (item.get("start_line"), item.get("end_line"), item.get("sha16"))
        for item in chunks
    }:
        chunks.append(dict(chunk))

    entry = {
        "path": normalize_rel(rel),
        "failure_signature": failure_signature(state),
        "sha16": current_sha16 or chunk.get("sha16"),
        "round_idx": state.get("round_idx", 0),
        "read_count": len(chunks),
        "blocked_repeats": int(previous.get("blocked_repeats", 0) or 0),
        "source_tool": source_tool,
        "reads": chunks,
    }
    cache[key] = entry
    return entry


def iter_cached_chunks(state: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for key, entry in (state.get("repair_read_cache") or {}).items():
        for chunk in read_chunks(entry if isinstance(entry, dict) else {}):
            yield str(key), chunk
