from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.verification.test_registry import registered_test_paths
from coding_agent.contracts.requirement_atoms import extract_requirement_atoms, summarize_requirement_atoms
from coding_agent.contracts.artifact_constraints import detect_prohibited_artifacts, tests_creation_prohibited, is_test_artifact_path
from coding_agent.workspace.run_paths import internal_generated_tests_enabled, is_agent_test_path, is_pytest_collectable_path
from coding_agent.quality.text_hygiene import scan_generated_artifact_text_hygiene


def _structured_create_paths(supervisor: dict[str, Any], task_spec: dict[str, Any]) -> list[str]:
    intent = supervisor.get("task_intent") or {}
    paths = list(intent.get("create_paths") or [])
    for item in (task_spec.get("file_plan") or {}).get("files") or []:
        if isinstance(item, dict) and item.get("path"):
            paths.append(str(item.get("path")))
    return [str(path).replace("\\", "/") for path in paths if path]


def _tests_requested(task_spec: dict[str, Any], supervisor: dict[str, Any]) -> bool:
    if "tests" in set(task_spec.get("expected_artifacts") or []):
        return True
    create_paths = _structured_create_paths(supervisor, task_spec)
    return any(is_test_artifact_path(path) for path in create_paths)
def extract_task_contract(task: str, task_spec: dict[str, Any] | None = None, supervisor: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a generic task contract from user text and supervisor output.

    This is intentionally not project-specific. It extracts operational
    obligations without inferring domain behavior from task keywords.
    """
    task_spec = task_spec or {}
    supervisor = supervisor or {}
    mode = supervisor.get("mode") or task_spec.get("task_type") or "unknown"

    from coding_agent.scope.read_only_policy import detect_global_read_only_lock
    from coding_agent.scope.write_scope import explicit_global_read_only_requested
    read_only_policy = supervisor.get("read_only_policy") or task_spec.get("read_only_policy") or detect_global_read_only_lock(task)
    read_only = bool(
        task_spec.get("read_only")
        or supervisor.get("read_only")
        or supervisor.get("write_locked")
        or task_spec.get("write_locked")
        or read_only_policy.get("locked")
        or explicit_global_read_only_requested(task)
    )
    expected_artifacts: list[str] = []
    required_behaviors: list[str] = []
    verification_gates: list[str] = []
    quality_gates: list[str] = []
    prohibited_actions: list[str] = []
    prohibited_artifacts = detect_prohibited_artifacts(task)
    prohibit_tests = tests_creation_prohibited(task)

    verify_only = mode == "run_verify" or supervisor.get("operation_mode") == "verify_only"

    if read_only and not verify_only:
        prohibited_actions.extend(["modify_user_source", "delete_files", "run_expensive_training"])
        quality_gates.extend(["report_has_evidence_paths", "report_covers_requested_topics"])
    elif verify_only:
        prohibited_actions.extend(["modify_user_source", "delete_files", "run_expensive_training"])
        verification_gates.extend(["execution_based_verification", "pytest_collection_checked"])
    else:
        verification_gates.append("py_compile_if_python")
        quality_gates.append("changes_recorded_as_diff")

        verification_gates.append("execution_based_verification")

        if (not prohibit_tests) and _tests_requested(task_spec, supervisor):
            expected_artifacts.append("tests")
            verification_gates.append("pytest_if_tests_exist")
            quality_gates.append("tests_assert_task_behavior")
        elif prohibit_tests:
            prohibited_actions.append("create_tests")

    requirement_atoms = extract_requirement_atoms(task, task_spec, supervisor)

    # De-duplicate while preserving order.
    def dedupe(xs: list[str]) -> list[str]:
        out = []
        seen = set()
        for x in xs:
            if x not in seen:
                out.append(x)
                seen.add(x)
        return out

    return {
        "mode": mode,
        "read_only": read_only,
        "objective": task_spec.get("objective") or task,
        "constraints": dedupe(list(task_spec.get("constraints") or []) + list(supervisor.get("success_contract") or [])),
        "expected_artifacts": dedupe(expected_artifacts),
        "required_behaviors": dedupe(required_behaviors),
        "verification_gates": dedupe(verification_gates),
        "quality_gates": dedupe(quality_gates),
        "prohibited_actions": dedupe(prohibited_actions),
        "prohibited_artifacts": prohibited_artifacts,
        "read_only_policy": read_only_policy,
        "requirement_atoms": requirement_atoms,
        "requirement_atom_summary": summarize_requirement_atoms(requirement_atoms),
        "implementation_contract": task_spec.get("implementation_contract") or {},
        "assumptions": list(task_spec.get("assumptions") or []),
        "source": "llm_structured_contract_v2",
    }


def _include_file(rel: str, parts: tuple[str, ...], state: dict[str, Any] | None = None) -> bool:
    if "__pycache__" in parts:
        return False
    if ".coding_agent" in parts or ".agent_runs" in parts:
        return False
    if ".coding_agent_test" in parts:
        return bool(
            state
            and internal_generated_tests_enabled(state)
            and is_agent_test_path(rel, state=state)
        )
    return True


def _is_pytest_test_case_path(rel: str) -> bool:
    if Path(rel).name == "conftest.py":
        return False
    return is_pytest_collectable_path(rel)


def scan_workspace_contract(workspace: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(workspace).resolve()
    py_files = []
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(root)).replace("\\", "/")
        if _include_file(rel, p.parts, state):
            py_files.append(rel)
    py_files = sorted(py_files)
    registry_tests = registered_test_paths(state or {}, existing_only=True) if state else []
    test_files = registry_tests or [p for p in py_files if _is_pytest_test_case_path(p)]
    readmes = [str(p.relative_to(root)) for p in root.glob("README*") if p.is_file()]
    return {
        "py_files": py_files,
        "test_files": test_files,
        "readmes": readmes,
        "has_readme": bool(readmes),
    }


def contract_quality_check(workspace: str, contract: dict[str, Any], verification: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generic contract check. It avoids project-specific file names.

    It does not replace execution tests. It adds warnings/failures when a task
    asks for artifacts or behaviors that are obviously missing.
    """
    info = scan_workspace_contract(workspace, state)
    warnings: list[str] = []
    failures: list[str] = []
    # Test quality: weak but generic. Do not fail projects that did not request tests.
    if info["test_files"]:
        weak_tests = []
        root = Path(workspace).resolve()
        for rel in info["test_files"]:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            if "assert True" in text or text.count("assert") == 0:
                weak_tests.append(rel)
        if weak_tests:
            warnings.append("weak tests detected: " + ", ".join(weak_tests[:5]))

    text_hygiene = scan_generated_artifact_text_hygiene(workspace, state)
    failures.extend(text_hygiene.get("failures") or [])
    warnings.extend(text_hygiene.get("warnings") or [])

    return {
        "ok": not failures,
        "failures": failures,
        "warnings": warnings,
        "workspace_contract_scan": info,
        "generated_text_hygiene": text_hygiene,
    }
