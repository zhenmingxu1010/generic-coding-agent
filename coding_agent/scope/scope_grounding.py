from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.scope.write_scope import normalize_rel


def _repo_files(state: dict[str, Any]) -> set[str]:
    repo_map = state.get("repo_map") or {}
    return {normalize_rel(path) for path in repo_map.get("files", []) if normalize_rel(path)}


def _path_exists_in_repo(state: dict[str, Any], path: str) -> bool:
    rel = normalize_rel(path)
    if not rel:
        return False
    files = _repo_files(state)
    if rel in files:
        return True
    workspace = state.get("workspace")
    return bool(workspace and (Path(workspace) / rel).is_file())


def _is_llm_scope_operation(item: dict[str, Any]) -> bool:
    return str(item.get("source") or "") == "llm_write_scope_intent"


def _is_allow_modify_operation(item: dict[str, Any]) -> bool:
    return str(item.get("operation") or "") in {"allow_modify", "modify_existing"}


def _has_non_llm_allow_modify(scope: dict[str, Any], path: str) -> bool:
    rel = normalize_rel(path)
    for item in scope.get("path_operations") or []:
        if not isinstance(item, dict):
            continue
        if normalize_rel(item.get("path")) != rel:
            continue
        if _is_allow_modify_operation(item) and not _is_llm_scope_operation(item):
            return True
    return False


def _ground_allowed_modify_paths(scope: dict[str, Any], state: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    kept: list[str] = []
    unresolved: list[dict[str, Any]] = []
    for raw in scope.get("allowed_modify_paths") or []:
        rel = normalize_rel(raw)
        if not rel:
            continue
        if _path_exists_in_repo(state, rel) or _has_non_llm_allow_modify(scope, rel):
            if rel not in kept:
                kept.append(rel)
            continue
        unresolved.append(
            {
                "path": rel,
                "source": "llm_write_scope_intent",
                "reason": "LLM-inferred modify target does not exist in the scanned repository",
            }
        )
    return kept, unresolved


def ground_scope_contract_to_repo(state: dict[str, Any]) -> dict[str, Any]:
    """Constrain semantic write-scope paths to real repository files.

    LLMs may correctly decide that a task needs source modification while still
    guessing the wrong filename. Only user-explicit paths or repo-existing paths
    should unlock source writes; unresolved LLM guesses are preserved for audit
    and strategy correction, but removed from allowed_modify_paths.
    """
    scope = dict(state.get("scope_contract") or (state.get("task_intent") or {}).get("scope_contract") or {})
    if not scope:
        return {"version": "scope_grounding_v1", "applied": False, "unresolved_modify_targets": []}

    grounded_paths, unresolved = _ground_allowed_modify_paths(scope, state)
    if not unresolved:
        result = {
            "version": "scope_grounding_v1",
            "applied": True,
            "changed": False,
            "allowed_modify_paths": grounded_paths,
            "unresolved_modify_targets": [],
        }
        state["scope_grounding"] = result
        return result

    unresolved_paths = {item["path"] for item in unresolved}
    updated_ops: list[dict[str, Any]] = []
    for item in scope.get("path_operations") or []:
        if not isinstance(item, dict):
            continue
        rel = normalize_rel(item.get("path"))
        if rel in unresolved_paths and _is_llm_scope_operation(item) and _is_allow_modify_operation(item):
            updated = dict(item)
            updated["operation"] = "unresolved_modify_target"
            updated["grounded"] = False
            updated["grounding_reason"] = "path not present in repo_map files"
            updated_ops.append(updated)
        else:
            updated_ops.append(item)

    scope["allowed_modify_paths"] = grounded_paths
    scope["path_operations"] = updated_ops
    existing_unresolved = [
        item for item in scope.get("unresolved_modify_targets") or []
        if isinstance(item, dict) and normalize_rel(item.get("path")) not in unresolved_paths
    ]
    scope["unresolved_modify_targets"] = existing_unresolved + unresolved
    scope["grounded_to_repo"] = True

    state["scope_contract"] = scope
    task_intent = state.get("task_intent")
    if isinstance(task_intent, dict):
        task_intent["scope_contract"] = scope
        task_intent["allowed_modify_paths"] = [
            normalize_rel(path)
            for path in task_intent.get("allowed_modify_paths") or []
            if normalize_rel(path) not in unresolved_paths
        ]
        task_intent["unresolved_modify_targets"] = scope["unresolved_modify_targets"]
        semantic = task_intent.get("semantic_write_scope")
        if isinstance(semantic, dict):
            semantic["allowed_modify_paths"] = [
                normalize_rel(path)
                for path in semantic.get("allowed_modify_paths") or []
                if normalize_rel(path) not in unresolved_paths
            ]
            semantic["unresolved_modify_targets"] = scope["unresolved_modify_targets"]
            issues = list(semantic.get("consistency_issues") or [])
            issue = "llm_inferred_modify_target_not_found_in_repo"
            if issue not in issues:
                issues.append(issue)
            semantic["consistency_issues"] = issues

    supervisor = state.get("supervisor")
    if isinstance(supervisor, dict):
        supervisor["task_intent"] = state.get("task_intent")

    result = {
        "version": "scope_grounding_v1",
        "applied": True,
        "changed": True,
        "allowed_modify_paths": grounded_paths,
        "unresolved_modify_targets": unresolved,
    }
    state["scope_grounding"] = result
    return result
