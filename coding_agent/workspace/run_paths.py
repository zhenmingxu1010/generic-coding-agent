from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any


def normalize_rel(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.strip("/")


def safe_id(value: str | None, *, default: str = "default", limit: int = 96) -> str:
    raw = str(value or default)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
    return safe[:limit] or default


def agent_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def agent_runs_root() -> Path:
    configured = os.getenv("AGENT_RUNS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return agent_repo_root() / ".agent_runs"


def workspace_run_key(workspace: str | Path) -> str:
    root = Path(workspace).expanduser().resolve()
    digest = hashlib.sha1(str(root).encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{safe_id(root.name or 'workspace', limit=48)}_{digest}"


def run_dir_for(workspace: str | Path, thread_id: str | None) -> Path:
    return agent_runs_root() / workspace_run_key(workspace) / safe_id(thread_id)


def project_memory_dir_for(workspace: str | Path) -> Path:
    return agent_runs_root() / workspace_run_key(workspace) / "project_memory"


def agent_test_root_rel(thread_id: str | None = None, state: dict[str, Any] | None = None) -> str:
    tid = safe_id(thread_id or ((state or {}).get("thread_id")))
    return f".coding_agent_test/{tid}"


def is_agent_test_path(path: str | None, *, thread_id: str | None = None, state: dict[str, Any] | None = None) -> bool:
    rel = normalize_rel(path)
    parts = rel.split("/")
    if len(parts) < 2 or parts[0] != ".coding_agent_test":
        return False
    tid = thread_id or ((state or {}).get("thread_id"))
    return tid is None or parts[1] == safe_id(tid)


def is_test_like_path(path: str | None) -> bool:
    """Return whether a path is a test or test-support artifact.

    This is intentionally based on common Python project conventions rather
    than a project-specific directory name. Files under `tests/`,
    `unit_tests/`, `integration_tests/`, `agent_tests/`, etc. are validation
    assets, including fixtures such as CSV/JSON files. Pytest-style root files
    such as `test_tool.py` and `tool_test.py` are also tests.
    """
    rel = normalize_rel(path)
    if not rel:
        return False
    if is_agent_test_path(rel):
        return True
    parts = rel.split("/")
    parent_parts = parts[:-1]
    if any(part == "tests" or part.endswith("_tests") for part in parent_parts):
        return True
    name = Path(rel).name
    return name.startswith("test_") or name.endswith("_test.py")


def is_under_test_support_dir(path: str | None) -> bool:
    """Return whether a path is inside a conventional test-support directory."""
    rel = normalize_rel(path)
    if not rel:
        return False
    if is_agent_test_path(rel):
        return True
    parts = rel.split("/")
    return any(part == "tests" or part.endswith("_tests") for part in parts[:-1])


def is_pytest_collectable_path(path: str | None) -> bool:
    rel = normalize_rel(path)
    if not rel or Path(rel).suffix != ".py":
        return False
    name = Path(rel).name
    return name.startswith("test_") or name.endswith("_test.py")


def internal_generated_tests_enabled(state: dict[str, Any] | None = None) -> bool:
    """Return whether the agent may create its own hidden verification tests.

    The default is false. Verification should prefer compile/run/semantic
    probes and existing project tests. Hidden pytest files are useful for some
    development workflows, but they should not be the default because a bad
    generated test can block otherwise-correct delivered code.
    """
    state = state or {}
    policy = state.get("test_policy") or {}
    if "generate_internal_tests" in policy:
        return bool(policy.get("generate_internal_tests"))
    raw = os.getenv("AGENT_GENERATE_INTERNAL_TESTS", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _contract_requests_tests(state: dict[str, Any] | None = None) -> bool:
    state = state or {}
    contract = state.get("task_contract") or {}
    expected = {str(x).strip().lower() for x in contract.get("expected_artifacts") or []}
    if expected.intersection({"test", "tests", "pytest"}):
        return True
    for atom in contract.get("requirement_atoms") or []:
        if not isinstance(atom, dict):
            continue
        data = atom.get("data") or {}
        if atom.get("type") == "artifact_exists" and str(data.get("kind") or "").lower() == "test":
            return True
    return False


def test_item_is_user_requested(state: dict[str, Any] | None, item: dict[str, Any]) -> bool:
    """Return whether a test-like item is a user/contract deliverable.

    This is about ownership, not project type. If the task contract or task
    intent explicitly asks for a test artifact, it is a normal deliverable.
    Otherwise a test-like file proposed by the model is considered agent-owned
    verification and is disabled by default.
    """
    state = state or {}
    path = normalize_rel(item.get("path"))
    original = normalize_rel(item.get("original_path") or path)
    if not (is_test_like_path(path) or is_test_like_path(original) or str(item.get("kind") or "") == "test"):
        return False
    if item.get("explicit_user_requested") or item.get("user_requested") or item.get("contract_required"):
        return True
    create_paths = {
        normalize_rel(p)
        for p in (state.get("task_intent") or {}).get("create_paths") or []
        if normalize_rel(p)
    }
    if original in create_paths and is_test_like_path(original):
        return True
    return _contract_requests_tests(state)


def should_keep_planned_test_item(state: dict[str, Any] | None, item: dict[str, Any]) -> bool:
    path = normalize_rel(item.get("path"))
    original = normalize_rel(item.get("original_path") or path)
    is_test = str(item.get("kind") or "") == "test" or is_test_like_path(path) or is_test_like_path(original)
    if not is_test:
        return True
    return test_item_is_user_requested(state, item) or internal_generated_tests_enabled(state)


def relocate_generated_test_path(state: dict[str, Any], path: str | None) -> tuple[str, str | None]:
    """Return the internal test path and original requested path.

    Tests generated by the agent for its own verification are never user
    deliverables. They live under `.coding_agent_test/<thread-id>/...` so they
    can be executed without changing the project tree seen by the user.
    """
    original = normalize_rel(path or "")
    if not original:
        return "", None
    if is_agent_test_path(original, state=state):
        return original, None
    root = agent_test_root_rel(state=state)
    rel = original
    if rel.startswith(".coding_agent_test/"):
        parts = rel.split("/")
        rel = "/".join(parts[2:]) if len(parts) > 2 else Path(rel).name
    mapped = f"{root}/{rel}"
    return normalize_rel(mapped), normalize_rel(original)


def generated_tests_are_deliverable(state: dict[str, Any]) -> bool:
    """Return whether agent-owned verification tests should be delivered.

    Agent-owned verification tests are not deliverables by default. Tests that
    are explicitly requested by the user are handled per item in
    `apply_output_layout` and do not use this function.
    """
    return bool((state.get("test_policy") or {}).get("deliver_internal_tests", False))


def apply_output_layout(state: dict[str, Any], files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    deliver_tests = generated_tests_are_deliverable(state)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    agent_tests: list[dict[str, str]] = []
    deliverables: list[dict[str, str]] = []
    skipped_tests: list[dict[str, str]] = []
    for item in files:
        row = dict(item)
        path = normalize_rel(row.get("path"))
        if not path:
            continue
        kind = str(row.get("kind") or "")
        is_test = kind == "test" or is_test_like_path(path)
        if is_test and test_item_is_user_requested(state, row):
            row["path"] = path
            row["kind"] = "test"
            row["user_visible"] = True
            row["agent_internal"] = False
            deliverables.append({"path": path, "kind": "test"})
        elif is_test and not internal_generated_tests_enabled(state):
            skipped_tests.append({"path": path, "reason": "internal generated tests disabled by default"})
            continue
        elif is_test and not deliver_tests:
            mapped, original = relocate_generated_test_path(state, path)
            if not mapped:
                continue
            row["path"] = mapped
            row["kind"] = "test"
            row["verification_role"] = "test"
            row["agent_internal"] = True
            row["user_visible"] = False
            if original:
                row["original_path"] = original
            agent_tests.append({"path": mapped, "original_path": original or path})
        else:
            row["path"] = path
            row.setdefault("user_visible", True)
            deliverables.append({"path": path, "kind": str(row.get("kind") or "other")})
        if row["path"] in seen:
            continue
        seen.add(row["path"])
        out.append(row)
    layout = {
        "version": "output_layout_v2",
        "policy": "deliverable files stay in the workspace; generated verification tests live under .coding_agent_test/<thread-id>; run artifacts live under the agent repo .agent_runs",
        "agent_test_root": agent_test_root_rel(state=state),
        "run_dir": state.get("run_dir"),
        "generated_tests_deliverable": deliver_tests,
        "internal_generated_tests_enabled": internal_generated_tests_enabled(state),
        "agent_tests": agent_tests,
        "deliverables": deliverables,
        "skipped_tests": skipped_tests,
    }
    return out, layout


def mapped_agent_test_for_original(state: dict[str, Any], path: str | None) -> str | None:
    rel = normalize_rel(path or "")
    if not rel:
        return None
    if is_agent_test_path(rel, state=state):
        return rel
    for source in (
        state.get("generated_files") or [],
        (state.get("file_plan") or {}).get("files") or [],
        (state.get("file_plan_review") or {}).get("writable_files") or [],
    ):
        for item in source if isinstance(source, list) else []:
            if not isinstance(item, dict):
                continue
            item_path = normalize_rel(item.get("path"))
            original = normalize_rel(item.get("original_path"))
            if item.get("kind") == "test" and original == rel and is_agent_test_path(item_path, state=state):
                return item_path
    mapped, _original = relocate_generated_test_path(state, rel)
    if mapped and (Path(state.get("workspace", "")) / mapped).exists():
        return mapped
    return None
