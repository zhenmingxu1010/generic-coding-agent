from __future__ import annotations

import re

from coding_agent.tools.file_tools import search_text, read_file


def _task_search_terms(task: str, limit: int = 16) -> list[str]:
    """Extract bounded search terms while prioritizing code-like identifiers."""
    raw_terms = re.findall(r"[A-Za-z_][A-Za-z0-9_./-]*|[\u3400-\u4dbf\u4e00-\u9fff]+", task or "")
    candidates: list[str] = []
    for raw in raw_terms:
        term = raw.strip("._/-")
        if len(term) < 4 and not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", term):
            continue
        if term not in candidates:
            candidates.append(term)

    def priority(term: str) -> tuple[int, int]:
        code_like = any(marker in term for marker in ("_", ".", "/", "-"))
        return (0 if code_like else 1, candidates.index(term))

    return sorted(candidates, key=priority)[: max(1, limit)]


def retrieve_by_task_terms(workspace: str, task: str, max_files: int = 8) -> dict:
    seen: list[str] = []
    for tok in _task_search_terms(task):
        res = search_text(workspace, tok, max_matches=20)
        for m in res.data.get("matches", []):
            p = m.get("path")
            if p and p not in seen:
                seen.append(p)
            if len(seen) >= max_files:
                break
        if len(seen) >= max_files:
            break
    snippets = []
    for p in seen[:max_files]:
        snippets.append(read_file(workspace, p, start_line=1, limit=120).model_dump())
    return {"matched_files": seen[:max_files], "snippets": snippets}
