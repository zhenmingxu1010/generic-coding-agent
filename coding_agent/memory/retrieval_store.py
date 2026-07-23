from __future__ import annotations

from pathlib import Path

from coding_agent.tools.file_tools import search_text, read_file


def retrieve_by_task_terms(workspace: str, task: str, max_files: int = 8) -> dict:
    # Deliberately simple first version: keyword search over task tokens.
    tokens = [t for t in task.replace("/", " ").replace("_", " ").split() if len(t) >= 4]
    seen: list[str] = []
    for tok in tokens[:8]:
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
