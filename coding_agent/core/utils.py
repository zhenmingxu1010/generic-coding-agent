from __future__ import annotations

import ast
import hashlib
import json
import re
import warnings
from pathlib import Path
from typing import Any


def sha16(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8", errors="replace")
    return hashlib.sha256(text).hexdigest()[:16]


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def normalize_volatile_text(text: Any) -> str:
    """Remove run-specific noise before comparing failure evidence.

    The returned text is only for fingerprints. Human-facing tracebacks keep
    their original paths, timings, and object representations.
    """
    value = str(text or "")
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"(?i)\bat 0x[0-9a-f]+\b", "at <address>", value)
    value = re.sub(
        r"(?i)(?:[a-z]:)?[/\\][^\r\n\"']*?pytest-of-[^/\\\r\n]+[/\\]pytest-\d+",
        "<pytest-temp>",
        value,
    )
    value = re.sub(r"(?i)\bin\s+\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds)\b", "in <duration>", value)
    return value


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def append_jsonl(path: str | Path, obj: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def write_json(path: str | Path, obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json_dumps(obj) + "\n", encoding="utf-8")


def write_text_file(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()



_VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _sanitize_json_like(text: str) -> str:
    r"""Repair common LLM JSON-string mistakes without changing JSON structure.

    LLMs often emit action JSON with file contents inside a string. The most
    frequent failures are literal newlines/tabs inside strings and backslashes
    that are valid in Python/regex/path text but invalid in JSON (for example
    \d, \s, \.). This function only acts while inside JSON strings and converts
    those characters into JSON-safe escapes.
    """
    out: list[str] = []
    in_str = False
    esc = False
    i = 0
    while i < len(text):
        ch = text[i]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            i += 1
            continue

        # Inside a JSON string literal.
        if esc:
            if ch in _VALID_JSON_ESCAPES:
                out.append(ch)
            else:
                # Preserve the literal backslash by escaping it, then keep char.
                out.append('\\')
                out.append(ch)
            esc = False
            i += 1
            continue

        if ch == '\\':
            out.append('\\')
            esc = True
            i += 1
            continue
        if ch == '"':
            out.append(ch)
            in_str = False
            i += 1
            continue
        if ch == '\n':
            out.append('\\n')
            i += 1
            continue
        if ch == '\r':
            out.append('\\r')
            i += 1
            continue
        if ch == '\t':
            out.append('\\t')
            i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _json_loads_repaired(candidate: str) -> dict[str, Any] | None:
    """Try strict JSON, sanitized JSON, then Python-literal fallback.

    The Python-literal fallback is intentionally limited to dicts and is used
    only for common LLM protocol mistakes such as True/False/None or single
    quoted strings. It does not execute code.
    """
    for cand in (candidate, _sanitize_json_like(candidate)):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                obj = ast.literal_eval(cand)
            if isinstance(obj, dict):
                # round-trip through JSON-compatible primitives where possible
                return obj
        except Exception:
            continue
    return None


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from an LLM response with robust fallbacks.

    The agent should not trust LLMs to emit perfect JSON. This function handles
    markdown fences, prose before/after JSON, and multiple candidate JSON spans.
    It deliberately does not fabricate missing braces; if the response is truly
    truncated the caller should retry or use a fallback marked in trace.
    """
    text = _strip_json_fence(text)
    obj0 = _json_loads_repaired(text)
    if obj0 is not None:
        return obj0

    starts = [i for i, ch in enumerate(text) if ch == "{"]
    errors: list[str] = []
    for start in starts:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i+1]
                        obj = _json_loads_repaired(candidate)
                        if obj is not None:
                            return obj
                        try:
                            json.loads(candidate)
                        except Exception as e:
                            errors.append(str(e)[:300])
                        break
    raise ValueError("No valid closed JSON object found in LLM response" + (": " + "; ".join(errors[:3]) if errors else ""))

def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...<truncated {len(text) - limit} chars>"
