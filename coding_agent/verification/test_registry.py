from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.workspace.run_paths import is_pytest_collectable_path


def normalize_rel(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _is_test_item(item: dict[str, Any]) -> bool:
    return (
        item.get("kind") == "test"
        or item.get("role") == "test"
        or item.get("verification_role") == "test"
        or item.get("artifact_role") == "test"
    )


def _is_pytest_collectable_path(path: str | None) -> bool:
    return is_pytest_collectable_path(path)


def _add_path(
    rows: list[dict[str, Any]],
    seen: set[str],
    *,
    path: str | None,
    source: str,
    item: dict[str, Any] | None = None,
    workspace: str | None = None,
    existing_only: bool = False,
) -> None:
    rel = normalize_rel(path)
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return
    if not _is_pytest_collectable_path(rel):
        return
    if existing_only and workspace and not (Path(workspace) / rel).is_file():
        return
    if rel in seen:
        return
    item = item or {}
    rows.append(
        {
            "path": rel,
            "source": source,
            "kind": item.get("kind") or item.get("role") or "test",
            "original_path": item.get("original_path"),
            "internal_test": rel.startswith(".coding_agent_test/"),
        }
    )
    seen.add(rel)


def build_verification_test_registry(state: dict[str, Any], *, existing_only: bool = False) -> dict[str, Any]:
    """Return the authoritative list of tests generated/planned for this run.

    Pytest's filename discovery is intentionally not used as the source of truth
    for generated artifacts. A file is a generated test only when the file plan
    or generation metadata marks it as kind=test.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    workspace = state.get("workspace")

    previous = state.get("verification_test_registry") or {}
    for item in previous.get("tests") or []:
        _add_path(rows, seen, path=item.get("path"), source="previous_registry", item=item, workspace=workspace, existing_only=existing_only)

    for item in (state.get("file_plan") or {}).get("files") or []:
        if _is_test_item(item):
            _add_path(rows, seen, path=item.get("path"), source="file_plan", item=item, workspace=workspace, existing_only=existing_only)

    for item in (state.get("file_plan_review") or {}).get("writable_files") or []:
        if _is_test_item(item):
            _add_path(rows, seen, path=item.get("path"), source="file_plan_review", item=item, workspace=workspace, existing_only=existing_only)

    for item in state.get("generated_files") or []:
        if _is_test_item(item):
            _add_path(rows, seen, path=item.get("path"), source="generated_files", item=item, workspace=workspace, existing_only=existing_only)

    return {
        "version": "v1.21",
        "policy": "pytest targets come from explicit file_plan/generated kind=test metadata, not filename discovery alone",
        "tests": rows,
        "paths": [x["path"] for x in rows],
    }


def refresh_verification_test_registry(state: dict[str, Any], *, existing_only: bool = False) -> dict[str, Any]:
    registry = build_verification_test_registry(state, existing_only=existing_only)
    state["verification_test_registry"] = registry
    return registry


def registered_test_paths(state: dict[str, Any], *, existing_only: bool = True) -> list[str]:
    registry = build_verification_test_registry(state, existing_only=existing_only)
    return registry.get("paths") or []


def is_registered_test_path(state: dict[str, Any], path: str | None, *, existing_only: bool = False) -> bool:
    rel = normalize_rel(path)
    if not rel:
        return False
    return rel in set(registered_test_paths(state, existing_only=existing_only))
