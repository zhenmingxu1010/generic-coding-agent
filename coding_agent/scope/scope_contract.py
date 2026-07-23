from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from coding_agent.scope.write_guard import path_has_output_intent
from coding_agent.scope.write_scope import extract_mentioned_paths, normalize_rel


ALLOW_MODIFY_MARKERS = [
    "\u5141\u8bb8\u4fee\u6539",
    "\u5141\u8bb8\u7f16\u8f91",
    "\u53ef\u4ee5\u4fee\u6539",
    "\u53ef\u4ee5\u7f16\u8f91",
    "\u53ef\u6539",
    "\u53ea\u5141\u8bb8\u4fee\u6539",
    "allow modifying",
    "allowed to modify",
    "allow modify",
    "may modify",
    "can modify",
    "allowed to edit",
    "may edit",
    "can edit",
]

FORBID_MODIFY_MARKERS = [
    "\u7981\u6b62\u4fee\u6539",
    "\u4e0d\u8981\u4fee\u6539",
    "\u4e0d\u5f97\u4fee\u6539",
    "\u4e0d\u80fd\u4fee\u6539",
    "\u4e0d\u5141\u8bb8\u4fee\u6539",
    "\u7981\u6b62\u7f16\u8f91",
    "\u4e0d\u8981\u7f16\u8f91",
    "\u4e0d\u8981\u6539",
    "\u4e0d\u80fd\u6539",
    "\u7981\u6b62\u6539",
    "\u4e0d\u8981\u5220\u9664",
    "\u7981\u6b62\u5220\u9664",
    "\u4e0d\u8981\u8986\u76d6",
    "\u7981\u6b62\u8986\u76d6",
    "do not modify",
    "don't modify",
    "dont modify",
    "must not modify",
    "cannot modify",
    "do not edit",
    "don't edit",
    "dont edit",
    "must not edit",
    "do not change",
    "don't change",
    "dont change",
    "do not delete",
    "must not delete",
    "do not overwrite",
    "must not overwrite",
    "without modifying",
    "without changing",
    "without editing",
]

READ_REFERENCE_MARKERS = [
    "\u8bfb\u53d6",
    "\u53ea\u8bfb",
    "\u53c2\u8003",
    "\u56de\u9000\u8bfb\u53d6",
    "read",
    "load",
    "input",
    "fallback",
    "reference",
]

EXISTING_TESTS_MARKERS = [
    "\u5df2\u6709\u6d4b\u8bd5",
    "\u73b0\u6709\u6d4b\u8bd5",
    "\u4efb\u4f55\u5df2\u6709\u6d4b\u8bd5",
    "\u4efb\u4f55\u73b0\u6709\u6d4b\u8bd5",
    "existing tests",
    "existing test files",
]

EXISTING_PROJECT_MARKERS = [
    "\u4efb\u4f55\u5df2\u6709\u9879\u76ee\u6587\u4ef6",
    "\u5df2\u6709\u9879\u76ee\u6587\u4ef6",
    "\u4efb\u4f55\u5df2\u6709\u6587\u4ef6",
    "any existing project file",
    "any existing files",
    "existing project files",
]

CLAUSE_BOUNDARIES = [
    "\u3002",
    ". ",
    "\uff1b",
    ";",
    "\n",
    "\uff0c\u4f46",
    "\uff0c\u4f46\u662f",
    "\u4f46\u662f",
    "\u4e0d\u8fc7",
    "\u4f46",
    ", but",
    " but ",
    ", however",
    " however ",
]


def _dedupe(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        rel = normalize_rel(path)
        if rel and rel not in seen:
            seen.add(rel)
            out.append(rel)
    return out


def _contains_any(text: str, markers: list[str]) -> bool:
    low = text.lower()
    return any(marker in text or marker.lower() in low for marker in markers)


def _nearest_marker_distance(text: str, path: str, markers: list[str]) -> int | None:
    """Return the shortest character gap between a path and any marker.

    A clause can contain more than one path operation, for example
    ``do not modify README.md; only modify src/app.py``.  Presence-only checks
    make both paths forbidden.  Associating each path with its nearest marker
    keeps the parser deterministic while preserving forbid-on-tie safety.
    """
    low = text.lower()
    target = path.lower()
    if not target:
        return None

    path_starts: list[int] = []
    start = 0
    while True:
        pos = low.find(target, start)
        if pos < 0:
            break
        path_starts.append(pos)
        start = pos + max(1, len(target))
    if not path_starts:
        return None

    nearest: int | None = None
    for marker in markers:
        needle = marker.lower()
        start = 0
        while True:
            marker_start = low.find(needle, start)
            if marker_start < 0:
                break
            marker_end = marker_start + len(needle)
            for path_start in path_starts:
                path_end = path_start + len(target)
                if marker_end <= path_start:
                    distance = path_start - marker_end
                elif path_end <= marker_start:
                    distance = marker_start - path_end
                else:
                    distance = 0
                nearest = distance if nearest is None else min(nearest, distance)
            start = marker_start + max(1, len(needle))
    return nearest


def _path_clause(task: str, path: str) -> str:
    if not task or not path or path not in task:
        return ""
    idx = task.find(path)
    end_of_path = idx + len(path)
    left = 0
    right = len(task)
    for token in CLAUSE_BOUNDARIES:
        pos = task.rfind(token, 0, idx)
        if pos >= 0:
            left = max(left, pos + len(token))
        pos = task.find(token, end_of_path)
        if pos >= 0:
            right = min(right, pos)
    return task[left:right].strip()


def _path_window(task: str, path: str, window: int = 96) -> str:
    if not task or not path or path not in task:
        return ""
    idx = task.find(path)
    return task[max(0, idx - window): idx + len(path) + window]


def _operation_for_path(task: str, path: str) -> tuple[str, str]:
    clause = _path_clause(task, path)
    window = _path_window(task, path)
    text = clause or window
    forbid_distance = _nearest_marker_distance(text, path, FORBID_MODIFY_MARKERS)
    allow_distance = _nearest_marker_distance(text, path, ALLOW_MODIFY_MARKERS)
    if forbid_distance is not None and allow_distance is not None:
        if allow_distance < forbid_distance:
            return "allow_modify", text
        return "forbid_modify", text
    if forbid_distance is not None:
        return "forbid_modify", text
    if allow_distance is not None:
        return "allow_modify", text
    if path_has_output_intent(task, path):
        return "mention", text
    if _contains_any(text, READ_REFERENCE_MARKERS):
        return "read_reference", text
    return "mention", text


def _path_matches(path: str, patterns: list[str]) -> bool:
    rel = normalize_rel(path)
    for pattern in patterns or []:
        pat = normalize_rel(pattern)
        if not pat:
            continue
        if rel == pat or fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.replace("**/", "*/")):
            return True
    return False


def build_scope_contract(task: str, paths: list[str] | None = None) -> dict[str, Any]:
    """Extract generic path operation constraints from task text."""
    task = task or ""
    paths = _dedupe(paths if paths is not None else extract_mentioned_paths(task))
    allowed_modify_paths: list[str] = []
    forbidden_modify_paths: list[str] = []
    protected_existing_paths: list[str] = []
    read_reference_paths: list[str] = []
    operations: list[dict[str, Any]] = []

    for path in paths:
        operation, evidence = _operation_for_path(task, path)
        row = {"path": path, "operation": operation, "evidence": evidence[:400]}
        operations.append(row)
        if operation == "forbid_modify":
            forbidden_modify_paths.append(path)
            protected_existing_paths.append(path)
        elif operation == "allow_modify":
            allowed_modify_paths.append(path)
        elif operation == "read_reference":
            read_reference_paths.append(path)

    protected_existing_globs: list[str] = []
    if _contains_any(task, FORBID_MODIFY_MARKERS) and _contains_any(task, EXISTING_TESTS_MARKERS):
        protected_existing_globs.extend(["tests/**", "test_*.py", "**/test_*.py", "**/*_test.py"])
    if _contains_any(task, FORBID_MODIFY_MARKERS) and _contains_any(task, EXISTING_PROJECT_MARKERS):
        protected_existing_globs.append("**")

    return {
        "version": "scope_contract_v1",
        "path_operations": operations,
        "allowed_modify_paths": _dedupe(allowed_modify_paths),
        "forbidden_modify_paths": _dedupe(forbidden_modify_paths),
        "protected_existing_paths": _dedupe(protected_existing_paths),
        "protected_existing_globs": _dedupe(protected_existing_globs),
        "read_reference_paths": _dedupe(read_reference_paths),
    }


def path_operation(scope: dict[str, Any] | None, path: str) -> str:
    rel = normalize_rel(path)
    for item in (scope or {}).get("path_operations") or []:
        if normalize_rel(item.get("path")) == rel:
            return str(item.get("operation") or "mention")
    return "mention"


def path_allows_modify(scope: dict[str, Any] | None, path: str, *, original_path: str | None = None) -> bool:
    candidates = [normalize_rel(path), normalize_rel(original_path)]
    allowed = {normalize_rel(p) for p in (scope or {}).get("allowed_modify_paths") or []}
    return any(candidate and candidate in allowed for candidate in candidates)


def path_is_protected(
    scope: dict[str, Any] | None,
    path: str,
    *,
    original_path: str | None = None,
    include_globs: bool = True,
) -> bool:
    candidates = [normalize_rel(path), normalize_rel(original_path)]
    if path_allows_modify(scope, path, original_path=original_path):
        return False
    exact = {normalize_rel(p) for p in (scope or {}).get("protected_existing_paths") or []}
    exact |= {normalize_rel(p) for p in (scope or {}).get("forbidden_modify_paths") or []}
    if any(candidate and candidate in exact for candidate in candidates):
        return True
    if include_globs:
        globs = [normalize_rel(p) for p in (scope or {}).get("protected_existing_globs") or []]
        return any(candidate and _path_matches(candidate, globs) for candidate in candidates)
    return False


def protected_original_output(scope: dict[str, Any] | None, path: str, *, original_path: str | None = None) -> str:
    candidates = [normalize_rel(original_path), normalize_rel(path)]
    if path_allows_modify(scope, path, original_path=original_path):
        return ""
    exact = {normalize_rel(p) for p in (scope or {}).get("protected_existing_paths") or []}
    exact |= {normalize_rel(p) for p in (scope or {}).get("forbidden_modify_paths") or []}
    for candidate in candidates:
        if candidate and candidate in exact:
            return candidate
    return ""
