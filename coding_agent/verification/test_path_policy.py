from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.scope.write_scope import normalize_rel
from coding_agent.workspace.run_paths import (
    generated_tests_are_deliverable,
    internal_generated_tests_enabled,
    is_test_like_path,
    relocate_generated_test_path,
)


def _workspace_has_tests_dir(workspace: str | None, state: dict[str, Any] | None = None) -> bool:
    if workspace and (Path(workspace) / "tests").is_dir():
        return True
    files = ((state or {}).get("repo_map") or {}).get("files") or []
    return any(str(p).replace("\\", "/").startswith("tests/") for p in files)


def _task_expects_tests(state: dict[str, Any] | None) -> bool:
    state = state or {}
    task = str(state.get("task") or "").lower()
    expected = set((state.get("task_contract") or {}).get("expected_artifacts") or [])
    return "tests" in expected or "pytest" in task or "test" in task or "测试" in str(state.get("task") or "")


def _is_current_agent_generated_test_target(state: dict[str, Any] | None, rel: str) -> bool:
    state = state or {}
    rel = normalize_rel(rel)
    for path in (state.get("artifact_registry") or {}).get("agent_generated_tests") or []:
        if normalize_rel(path) == rel:
            return True
    sources = []
    if isinstance(state.get("file_plan"), dict):
        sources.append((state.get("file_plan") or {}).get("files") or [])
    sources.append(state.get("generated_files") or [])
    for source in sources:
        for item in source or []:
            if not isinstance(item, dict):
                continue
            if normalize_rel(item.get("path")) == rel and str(item.get("kind") or "") == "test":
                return True
    return False


def _available_sibling(root: Path, rel: str) -> str:
    rel = normalize_rel(rel)
    target = root / rel
    if not target.exists():
        return rel
    parent = Path(rel).parent
    stem = Path(rel).stem
    suffix = Path(rel).suffix
    for idx in range(1, 100):
        candidate_name = f"{stem}_generated{'' if idx == 1 else '_' + str(idx)}{suffix}"
        candidate = normalize_rel(str(parent / candidate_name))
        if not (root / candidate).exists():
            return candidate
    return rel


def normalize_generated_test_write_path(
    state: dict[str, Any],
    path: str | None,
) -> tuple[str, dict[str, Any] | None]:
    """Normalize agent-generated verification tests to the internal test root.

    The agent may generate tests to validate its own work, but those tests are
    not project deliverables. Existing external tests remain protected by write
    intents; new test-like Python files are relocated under
    `.coding_agent_test/<thread-id>`.
    """
    rel = normalize_rel(path)
    if not rel or not is_test_like_path(rel):
        return rel, None
    if rel.startswith(".coding_agent_test/") and _is_current_agent_generated_test_target(state, rel):
        return rel, None

    workspace = state.get("workspace")
    root = Path(workspace).resolve() if workspace else Path(".").resolve()
    if not internal_generated_tests_enabled(state):
        return rel, None
    if not generated_tests_are_deliverable(state):
        mapped, original = relocate_generated_test_path(state, rel)
        if mapped and mapped != rel:
            return mapped, {
                "reason": "normalized generated verification test path to agent internal test root",
                "before": rel,
                "after": mapped,
                "original_path": original,
            }
    candidate = rel
    if "/" not in rel and _workspace_has_tests_dir(workspace, state):
        candidate = normalize_rel(f"tests/{Path(rel).name}")
    candidate = _available_sibling(root, candidate)
    if candidate == rel:
        return rel, None
    return candidate, {
        "reason": "normalized inferred generated test path to project test layout",
        "before": rel,
        "after": candidate,
    }
