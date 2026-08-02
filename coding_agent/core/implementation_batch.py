from __future__ import annotations

from typing import Any


MAX_IMPLEMENTATION_BATCH_ACTIONS = 6


def _normalize_rel_path(value: object) -> str:
    rel = str(value or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def update_implementation_batch(state: dict[str, Any]) -> bool:
    """Persist bounded initial multi-file implementation state.

    This must run inside a graph node, not a conditional router: LangGraph
    routes on router mutations but does not commit them as node state.
    """
    if (
        state.get("mode") not in {"modify", "debug", "repair_existing"}
        or state.get("read_only")
        or state.get("verification")
    ):
        state["implementation_batch_open"] = False
        state["implementation_batch_remaining"] = []
        return False
    allowed = list(dict.fromkeys(
        _normalize_rel_path(path)
        for path in (state.get("scope_contract") or {}).get("allowed_modify_paths") or []
        if _normalize_rel_path(path)
    ))
    changed = {
        _normalize_rel_path(path)
        for path in state.get("changed_files") or []
        if _normalize_rel_path(path)
    }
    remaining = [path for path in allowed if path not in changed]
    if len(allowed) < 2 or not remaining:
        state["implementation_batch_open"] = False
        state["implementation_batch_remaining"] = []
        return False
    if "implementation_batch_started_round" not in state:
        state["implementation_batch_started_round"] = int(state.get("round_idx", 0) or 0)
    elapsed = (
        int(state.get("round_idx", 0) or 0)
        - int(state.get("implementation_batch_started_round", 0) or 0)
    )
    state["implementation_batch_remaining"] = remaining
    state["implementation_batch_open"] = elapsed < MAX_IMPLEMENTATION_BATCH_ACTIONS
    return bool(state["implementation_batch_open"])


def implementation_batch_can_continue(state: dict[str, Any]) -> bool:
    if not state.get("implementation_batch_open") or state.get("verification"):
        return False
    elapsed = (
        int(state.get("round_idx", 0) or 0)
        - int(state.get("implementation_batch_started_round", 0) or 0)
    )
    return (
        elapsed < MAX_IMPLEMENTATION_BATCH_ACTIONS
        and bool(state.get("implementation_batch_remaining"))
    )
