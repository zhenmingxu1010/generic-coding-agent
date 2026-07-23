from __future__ import annotations

import re


def contains_cjk(text: str | None) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text or ""))


def prefers_chinese(text: str | None) -> bool:
    raw = text or ""
    low = raw.lower()
    return "\u4e2d\u6587" in raw or "\u6c49\u8bed" in raw or "chinese" in low or contains_cjk(raw)


def _cjk_count(text: str | None) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text or ""))


def _latin_alpha_count(text: str | None) -> int:
    return len(re.findall(r"[A-Za-z]", text or ""))


def response_language_quality(prompt: str | None, response: str | None, *, artifact: str = "response") -> dict:
    if not prefers_chinese(prompt):
        return {"ok": True, "expected": "same_as_user", "warning": None}

    cjk = _cjk_count(response)
    latin = _latin_alpha_count(response)
    ratio = cjk / max(1, cjk + latin)
    ok = cjk >= 80 or (cjk >= 30 and ratio >= 0.08)
    warning = None
    if not ok:
        warning = (
            f"The user's prompt is Chinese, but the {artifact} does not contain enough Chinese prose. "
            "Rewrite it in Chinese while preserving file paths, commands, API names, and code identifiers."
        )
    return {
        "ok": ok,
        "expected": "chinese",
        "cjk_chars": cjk,
        "latin_alpha_chars": latin,
        "cjk_ratio": ratio,
        "warning": warning,
    }


def language_instruction_for_text(text: str | None, *, artifact: str = "response") -> str:
    if prefers_chinese(text):
        return (
            f"The user's prompt contains Chinese. Write the {artifact} in Chinese by default, "
            "while keeping file paths, commands, API names, and code identifiers unchanged."
        )
    return (
        f"Use the same natural language as the user's task when it is clear; "
        "keep file paths, commands, API names, and code identifiers unchanged."
    )
