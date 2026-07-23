from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TOKEN_FIELDS = [
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "total_tokens",
]


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_token_usage(raw_usage: dict[str, Any] | None) -> dict[str, int]:
    usage = raw_usage or {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    completion_details = (
        usage.get("completion_tokens_details")
        if isinstance(usage.get("completion_tokens_details"), dict)
        else {}
    )
    out = {
        "prompt_tokens": _as_int(usage.get("prompt_tokens")),
        "completion_tokens": _as_int(usage.get("completion_tokens")),
        "reasoning_tokens": _as_int(completion_details.get("reasoning_tokens")),
        "cached_tokens": _as_int(prompt_details.get("cached_tokens")),
        "prompt_cache_hit_tokens": _as_int(usage.get("prompt_cache_hit_tokens")),
        "prompt_cache_miss_tokens": _as_int(usage.get("prompt_cache_miss_tokens")),
        "total_tokens": _as_int(usage.get("total_tokens")),
    }
    if out["total_tokens"] <= 0:
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


def empty_token_totals() -> dict[str, int]:
    return {"calls": 0, **{field: 0 for field in TOKEN_FIELDS}}


def _add_usage(target: dict[str, int], usage: dict[str, int]) -> None:
    target["calls"] = _as_int(target.get("calls")) + 1
    for field in TOKEN_FIELDS:
        target[field] = _as_int(target.get(field)) + _as_int(usage.get(field))


def summarize_token_usage(messages_path: str | Path | None) -> dict[str, Any]:
    path = Path(messages_path) if messages_path else None
    summary: dict[str, Any] = {
        "version": "token_usage_v1",
        "source": str(path) if path else None,
        "available": False,
        "totals": empty_token_totals(),
        "by_purpose": {},
        "calls": [],
        "missing_usage_calls": 0,
    }
    if not path or not path.is_file():
        return summary

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") != "llm_response" and row.get("type") != "llm_response":
            continue
        purpose = str(row.get("purpose") or "unknown")
        usage = row.get("token_usage")
        raw_usage = row.get("raw_usage")
        if isinstance(usage, dict):
            usage = {field: _as_int(usage.get(field)) for field in TOKEN_FIELDS}
            if usage["total_tokens"] <= 0:
                usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        else:
            usage = normalize_token_usage(raw_usage if isinstance(raw_usage, dict) else None)
        if not usage or _as_int(usage.get("total_tokens")) <= 0:
            summary["missing_usage_calls"] += 1
        normalized = usage
        call = {
            "purpose": purpose,
            "attempt": _as_int(row.get("attempt")),
            "model": row.get("model"),
            "llm_call_id": row.get("llm_call_id"),
            **normalized,
        }
        summary["calls"].append(call)
        summary["available"] = True
        _add_usage(summary["totals"], normalized)
        by_purpose = summary["by_purpose"]
        if purpose not in by_purpose:
            by_purpose[purpose] = empty_token_totals()
        _add_usage(by_purpose[purpose], normalized)
    return summary


def format_token_usage_markdown(summary: dict[str, Any] | None) -> str:
    summary = summary or {}
    if not summary.get("available"):
        return "## Token Usage\n\nNo LLM token usage was recorded.\n"
    totals = summary.get("totals") or {}
    lines = [
        "## Token Usage",
        "",
        "| Scope | Calls | Input | Output | Reasoning | Total | Cache Hit | Cache Miss |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        _usage_row("TOTAL", totals),
    ]
    for purpose, usage in sorted((summary.get("by_purpose") or {}).items()):
        lines.append(_usage_row(str(purpose), usage))
    return "\n".join(lines) + "\n"


def _usage_row(label: str, usage: dict[str, Any]) -> str:
    return (
        f"| {label} | {_as_int(usage.get('calls'))} | "
        f"{_as_int(usage.get('prompt_tokens'))} | "
        f"{_as_int(usage.get('completion_tokens'))} | "
        f"{_as_int(usage.get('reasoning_tokens'))} | "
        f"{_as_int(usage.get('total_tokens'))} | "
        f"{_as_int(usage.get('prompt_cache_hit_tokens'))} | "
        f"{_as_int(usage.get('prompt_cache_miss_tokens'))} |"
    )
