from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from coding_agent.scope.task_intent import classify_task_intent


TRAILING_PATH_CHARS = " \t\r\n\"'`.,;:，。；：、)]}）】》"

PROJECT_UNDERSTANDING_DEFAULT_PROMPT = """\

[Terminal UI default project-understanding instructions]
This is a project-understanding task. Work read-only unless the user's original task explicitly asks for file changes.
Answer in Chinese by default.
Inspect the repository structure and the most relevant source, script, config, test, metric, summary, and result files that exist in this workspace.
Cover: project purpose, directory layout, main entrypoints, data/config flow, important modules, tests or verification commands, output/result organization, and notable risks or gaps.
Ground claims in concrete file paths from the workspace.
"""


@dataclass
class PreparedTask:
    task: str
    workspace: Path
    original_task: str
    original_workspace: Path
    runtime_instructions: str = ""
    workspace_changed: bool = False
    mentioned_workspace: str | None = None
    defaults_applied: list[str] = field(default_factory=list)
    intent: dict | None = None


def _trim_path_candidate(text: str) -> str:
    return str(text or "").strip().strip(TRAILING_PATH_CHARS)


def _existing_dir_from_candidate(candidate: str, base_workspace: Path) -> Path | None:
    raw = _trim_path_candidate(candidate)
    if not raw:
        return None
    raw_path = Path(raw).expanduser()
    if raw_path.is_absolute():
        candidates: list[tuple[str, Path | None]] = [(raw, None)]
    else:
        candidates = [(str(base_workspace / raw), base_workspace)]

    def within_floor(path: Path, floor: Path | None) -> bool:
        if floor is None:
            return True
        try:
            path.resolve().relative_to(floor.resolve())
            return True
        except ValueError:
            return False

    for item, floor in candidates:
        path = Path(item).expanduser()
        if path.exists() and path.is_dir() and within_floor(path, floor):
            return path.resolve()

        # Users often type a Chinese sentence directly after a path without a
        # separating space, for example "/repo/project这个文件夹". Trim only the
        # trailing suffix until an existing directory is found.
        trimmed = item
        for _ in range(min(80, len(item))):
            if not trimmed:
                break
            trimmed = _trim_path_candidate(trimmed[:-1])
            if not trimmed:
                break
            p2 = Path(trimmed).expanduser()
            if p2.exists() and p2.is_dir() and within_floor(p2, floor):
                return p2.resolve()
    return None


def _path_from_candidate(candidate: str, base_workspace: Path) -> Path | None:
    raw = _trim_path_candidate(candidate)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_workspace / path
    return path.resolve()


def _strip_inline_sentence_suffix_from_path(candidate: str) -> str:
    raw = _trim_path_candidate(candidate)
    if not raw:
        return raw
    sep = "/" if "/" in raw.replace("\\", "/") else "\\"
    normalized = raw.replace("\\", "/")
    head, slash, tail = normalized.rpartition("/")
    if not slash:
        return raw
    match = re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", tail)
    if not match or match.start() == 0:
        return raw
    trimmed = head + "/" + tail[: match.start()]
    if sep == "\\":
        trimmed = trimmed.replace("/", "\\")
    return _trim_path_candidate(trimmed)


def _nonexistent_workspace_from_candidate(candidate: str, base_workspace: Path) -> Path | None:
    path = _path_from_candidate(_strip_inline_sentence_suffix_from_path(candidate), base_workspace)
    if path is None or path.exists():
        return None
    parent = path.parent
    if parent.exists() and parent.is_dir():
        return path
    return None


def _raw_path_candidates(task: str) -> list[str]:
    text = str(task or "")
    raw_candidates: list[str] = []

    for quoted in re.findall(r"[\"'“”‘’]([^\"'“”‘’]+)[\"'“”‘’]", text):
        raw_candidates.append(quoted)

    raw_candidates.extend(re.findall(r"[A-Za-z]:[\\/][^\s\"'`<>|]+", text))
    raw_candidates.extend(
        match.group(1)
        for match in re.finditer(r"(?<![A-Za-z0-9_.-])(/(?:[^\s\"'`<>|]+))", text)
        if not match.group(1).startswith("//")
    )
    raw_candidates.extend(re.findall(r"(?<![\w.-])(?:\.{1,2}/|[A-Za-z0-9_.-]+/)[^\s\"'`<>|]+", text))
    return raw_candidates


def extract_existing_workspace_path(task: str, base_workspace: str | Path) -> Path | None:
    """Return the most specific existing directory mentioned in a user task."""

    base = Path(base_workspace).expanduser().resolve()
    raw_candidates = _raw_path_candidates(task)

    found: list[Path] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        path = _existing_dir_from_candidate(raw, base)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    if not found:
        return None
    found.sort(key=lambda p: len(str(p)), reverse=True)
    return found[0]


def extract_new_workspace_path(task: str, base_workspace: str | Path) -> Path | None:
    """Return a not-yet-created workspace path when the task intends project creation."""

    base = Path(base_workspace).expanduser().resolve()
    found: list[Path] = []
    seen: set[str] = set()
    for raw in _raw_path_candidates(task):
        path = _nonexistent_workspace_from_candidate(raw, base)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    if not found:
        return None
    found.sort(key=lambda p: len(str(p)), reverse=True)
    return found[0]


def task_intent_flags(task: str) -> dict:
    try:
        return classify_task_intent(task or "")
    except Exception:
        return {}


def is_project_understanding_task(task: str, *, has_existing_workspace_path: bool = False) -> bool:
    stripped = str(task or "").strip()
    intent = task_intent_flags(stripped)
    write_like = bool(intent.get("create_requested") or intent.get("fix_requested") or intent.get("modify_requested"))
    if write_like:
        return False
    if intent.get("analyze_requested") or intent.get("operation_mode") == "read_only_analysis":
        return True
    return bool(has_existing_workspace_path and stripped)


def should_auto_route_chat_to_code(task: str, base_workspace: str | Path) -> bool:
    intent = task_intent_flags(task or "")
    allow_new_workspace = bool(
        intent.get("create_requested")
        or intent.get("operation_mode") == "safe_create"
        or intent.get("mode") in {"write", "generate_project"}
    )
    target = extract_new_workspace_path(task, base_workspace) if allow_new_workspace else None
    target = target or extract_existing_workspace_path(task, base_workspace)
    if target is None:
        return False
    return allow_new_workspace or is_project_understanding_task(task, has_existing_workspace_path=True)


def prepare_task_for_agent(
    task: str,
    *,
    base_workspace: str | Path,
    mode: str,
) -> PreparedTask:
    original_workspace = Path(base_workspace).expanduser().resolve()
    defaults: list[str] = []
    enriched = str(task or "").strip()
    runtime_parts: list[str] = []
    intent = task_intent_flags(enriched)
    allow_new_workspace = bool(
        intent.get("create_requested")
        or intent.get("operation_mode") == "safe_create"
        or intent.get("mode") in {"write", "generate_project"}
    )
    target_workspace = extract_new_workspace_path(enriched, original_workspace) if allow_new_workspace else None
    target_workspace = target_workspace or extract_existing_workspace_path(enriched, original_workspace)
    workspace = target_workspace or original_workspace

    if mode == "code" and is_project_understanding_task(enriched, has_existing_workspace_path=target_workspace is not None):
        runtime_parts.append(PROJECT_UNDERSTANDING_DEFAULT_PROMPT.strip())
        defaults.append("project_understanding")

    if target_workspace is not None and target_workspace != original_workspace:
        target_kind = "new or existing" if not target_workspace.exists() else "existing"
        runtime_parts.append(
            "[Terminal UI resolved target workspace]\n"
            f"The user mentioned a {target_kind} directory. Use this directory as the target workspace for this run: {target_workspace}"
        )
        defaults.append("mentioned_workspace")

    return PreparedTask(
        task=enriched,
        workspace=workspace,
        original_task=str(task or "").strip(),
        original_workspace=original_workspace,
        runtime_instructions="\n\n".join(part for part in runtime_parts if part),
        workspace_changed=workspace != original_workspace,
        mentioned_workspace=str(target_workspace) if target_workspace else None,
        defaults_applied=defaults,
        intent=intent,
    )
