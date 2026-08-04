from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {
    ".agent_runs",
    ".git",
    ".pytest_cache",
    ".venv",
    "build",
    "dist",
    "htmlcov",
}
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _project_markdown() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in SKIP_PARTS or part == "__pycache__" for part in path.parts)
    )


def test_every_english_markdown_has_an_adjacent_chinese_version():
    missing = []
    invalid = []
    for path in _project_markdown():
        if path.name.endswith(".zh-CN.md"):
            continue
        chinese = path.with_name(f"{path.stem}.zh-CN.md")
        if not chinese.is_file():
            missing.append(path.relative_to(ROOT).as_posix())
            continue
        source_text = path.read_text(encoding="utf-8", errors="replace")
        chinese_text = chinese.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", chinese_text):
            invalid.append({"document": chinese.relative_to(ROOT).as_posix(), "reason": "no CJK text"})
        if source_text.count("```") != chinese_text.count("```"):
            invalid.append({"document": chinese.relative_to(ROOT).as_posix(), "reason": "code-fence mismatch"})

    assert missing == []
    assert invalid == []


def test_local_markdown_links_resolve_in_english_and_chinese_docs():
    missing = []
    for path in _project_markdown():
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw_target in LINK_RE.findall(text):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                missing.append(
                    {
                        "document": path.relative_to(ROOT).as_posix(),
                        "target": raw_target,
                    }
                )

    assert missing == []
