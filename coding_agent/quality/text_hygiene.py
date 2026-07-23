from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any


TEXT_SUFFIXES = {
    ".py",
    ".pyi",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".sh",
    ".md",
    ".rst",
    ".txt",
    ".csv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".html",
    ".css",
}

SCRIPT_MARKERS = {
    "CJK": ("CJK ", "HIRAGANA", "KATAKANA", "HANGUL"),
    "CYRILLIC": ("CYRILLIC",),
    "GREEK": ("GREEK",),
    "ARABIC": ("ARABIC",),
    "HEBREW": ("HEBREW",),
    "DEVANAGARI": ("DEVANAGARI",),
    "THAI": ("THAI",),
}

FAIL_IF_UNEXPECTED = {"CYRILLIC", "GREEK", "REPLACEMENT_CHARACTER"}


def _norm(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _script_for_char(ch: str) -> str | None:
    if ch == "\ufffd":
        return "REPLACEMENT_CHARACTER"
    category = unicodedata.category(ch)
    if not category.startswith("L"):
        return None
    name = unicodedata.name(ch, "")
    if not name or "LATIN" in name:
        return None
    for script, markers in SCRIPT_MARKERS.items():
        if any(marker in name for marker in markers):
            return script
    return "OTHER_LETTER"


def scripts_in_text(text: str) -> set[str]:
    out: set[str] = set()
    for ch in text or "":
        script = _script_for_char(ch)
        if script:
            out.add(script)
    return out


def _line_col(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    last_newline = text.rfind("\n", 0, offset)
    col = offset + 1 if last_newline < 0 else offset - last_newline
    return line, col


def unexpected_script_fragments(text: str, *, allowed_scripts: set[str], max_examples: int = 8) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    examples: dict[str, list[dict[str, Any]]] = {}
    for idx, ch in enumerate(text or ""):
        script = _script_for_char(ch)
        if not script or script in allowed_scripts:
            continue
        counts[script] = counts.get(script, 0) + 1
        if len(examples.setdefault(script, [])) < max_examples:
            line, col = _line_col(text, idx)
            examples[script].append({"char": ch, "line": line, "column": col, "codepoint": f"U+{ord(ch):04X}"})
    return [
        {"script": script, "count": count, "examples": examples.get(script, [])}
        for script, count in sorted(counts.items())
    ]


def scan_generated_artifact_text_hygiene(workspace: str, state: dict[str, Any] | None) -> dict[str, Any]:
    """Check generated user-visible text artifacts for accidental mixed-script noise.

    This is intentionally generic: it allows any non-Latin script that appears
    in the task itself, ignores ordinary Latin accents and symbols, and only
    fails for small-script confusable noise in generated source/text files.
    """
    if not state:
        return {"ok": True, "failures": [], "warnings": [], "checks": []}
    root = Path(workspace).resolve()
    allowed_scripts = scripts_in_text(str(state.get("task") or ""))
    failures: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in state.get("generated_files") or []:
        if not isinstance(item, dict) or item.get("agent_internal") or item.get("user_visible") is False:
            continue
        rel = _norm(item.get("path"))
        if not rel or rel in seen:
            continue
        seen.add(rel)
        suffix = Path(rel).suffix.lower()
        if suffix not in TEXT_SUFFIXES:
            continue
        path = (root / rel).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            warnings.append(f"generated artifact text hygiene could not read {rel}: {exc}")
            continue
        unexpected = unexpected_script_fragments(text, allowed_scripts=allowed_scripts)
        if not unexpected:
            checks.append({"path": rel, "ok": True, "unexpected_scripts": []})
            continue
        checks.append({"path": rel, "ok": False, "unexpected_scripts": unexpected})
        hard = [x for x in unexpected if x.get("script") in FAIL_IF_UNEXPECTED]
        target = failures if hard else warnings
        for issue in hard or unexpected:
            first = (issue.get("examples") or [{}])[0]
            target.append(
                "generated artifact contains unexpected "
                f"{issue.get('script')} text in {rel} at line {first.get('line')}, "
                f"column {first.get('column')} ({first.get('codepoint')}); "
                "rewrite the artifact using text/language requested by the task"
            )

    return {"ok": not failures, "failures": failures, "warnings": warnings, "checks": checks}
