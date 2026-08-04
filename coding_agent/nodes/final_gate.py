from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.scope.write_scope_audit import build_write_scope_audit
from coding_agent.contracts.artifact_constraints import detect_prohibited_artifacts, is_prohibited_artifact_path
from coding_agent.scope.scope_contract import protected_original_output
from coding_agent.workspace.run_paths import is_agent_test_path, is_test_like_path


WRITE_MODES = {"write", "modify", "debug", "generate_project", "repair_existing"}
VERIFY_ONLY_MODES = {"run_verify"}
CONTROLLED_FAILURE_REASONS = {"task_unfulfillable_within_scope"}


def authoritative_requirement_atom_check(state: dict[str, Any]) -> dict[str, Any]:
    """Prefer a completed semantic check over a stale top-level checkpoint view."""
    contract_check = state.get("contract_check") or {}
    candidates = [
        state.get("requirement_atom_check"),
        (state.get("semantic_contract_check") or {}).get("requirement_atom_check"),
        contract_check.get("requirement_atom_check"),
        (contract_check.get("semantic_contract_check") or {}).get("requirement_atom_check"),
    ]
    available = [
        check
        for check in candidates
        if isinstance(check, dict) and isinstance(check.get("summary"), dict)
    ]
    return next((check for check in available if check.get("ok") is True), available[0] if available else {})


def _norm(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _is_test_path(path: str | None) -> bool:
    return is_test_like_path(path)


def _is_python_code_path(path: str | None) -> bool:
    rel = _norm(path)
    return rel.endswith(".py") and not _is_test_path(rel)


def _source_modify_requires_code_change(state: dict[str, Any]) -> bool:
    intent = state.get("task_intent") or {}
    scope = state.get("scope_contract") or intent.get("scope_contract") or {}
    if scope.get("allowed_modify_paths") or intent.get("allowed_modify_paths"):
        return True
    if state.get("mode") not in {"modify", "debug", "repair_existing"}:
        return False
    return bool(intent.get("source_modify_intent") or intent.get("operation_mode") == "scoped_modify")


def _source_code_changes(write_scope_audit: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("existing_project_modified_files", "new_project_files", "source_changed_files"):
        for item in write_scope_audit.get(key) or []:
            rel = item.get("path") if isinstance(item, dict) else item
            rel = _norm(str(rel))
            if rel and _is_python_code_path(rel) and rel not in candidates:
                candidates.append(rel)
    return candidates


def _verified_without_implementation_change(state: dict[str, Any], atom_summary: dict[str, Any]) -> bool:
    """Accept a no-op modify task only when executed behavior evidence proves it."""
    verification = state.get("verification") or {}
    if verification.get("ok") is not True or state.get("contract_ok") is not True:
        return False
    if state.get("needs_verification") or state.get("failure_issues"):
        return False
    if int(atom_summary.get("required_total", 0) or 0) <= 0:
        return False
    if int(atom_summary.get("required_failed", 0) or 0) != 0:
        return False
    if int(atom_summary.get("required_unverified", 0) or 0) != 0:
        return False

    infrastructure = set(state.get("verification_infrastructure_step_names") or [])
    for step in state.get("executed_verification_steps") or []:
        if not isinstance(step, dict):
            continue
        name = str(step.get("name") or "")
        if name in infrastructure:
            continue
        if step.get("executed") is False or bool(step.get("timed_out")):
            continue
        try:
            returncode = int(step["returncode"])
        except (KeyError, TypeError, ValueError):
            continue
        if returncode != 0:
            continue
        if step.get("verifies"):
            return True
    # Existing project suites can provide direct requirement evidence through
    # their named passing test cases even when the runtime-added pytest command
    # is an infrastructure step with no explicit `verifies` binding.
    successful_results = {
        str(result.get("name") or "")
        for result in verification.get("results") or []
        if isinstance(result, dict)
        and result.get("executed", True)
        and int(result.get("returncode", 1) or 0) == 0
        and not result.get("timed_out")
    }
    check = authoritative_requirement_atom_check(state)
    for atom in check.get("atoms") or []:
        if not isinstance(atom, dict) or atom.get("status") != "passed":
            continue
        data = atom.get("data") if isinstance(atom.get("data"), dict) else {}
        if str(data.get("evidence_mode") or "") != "execution":
            continue
        if str(atom.get("source") or "") == "agent_implementation_default":
            continue
        claim = (atom.get("details") or {}).get("verification_claim") or {}
        if successful_results.intersection(str(name) for name in claim.get("cited_steps") or []):
            return True
    return False


def _has_required_atoms(state: dict[str, Any], atom_summary: dict[str, Any]) -> bool:
    if int(atom_summary.get("required_total", 0) or 0) > 0:
        return True
    atoms = state.get("requirement_atoms") or (state.get("task_contract") or {}).get("requirement_atoms") or []
    return any(isinstance(atom, dict) and atom.get("required", True) for atom in atoms)


def _test_results(state: dict[str, Any]) -> dict[str, Any]:
    return state.get("test_results") or (state.get("verification") or {}).get("test_results") or {}


def _state_relative_path(state: dict[str, Any], path: str | None) -> str:
    rel = _norm(path)
    if not rel:
        return ""
    try:
        workspace = Path(str(state.get("workspace") or "")).resolve()
        candidate = Path(rel)
        if candidate.is_absolute():
            return _norm(str(candidate.resolve().relative_to(workspace)))
    except Exception:
        pass
    return rel


def _agent_generated_test_paths(state: dict[str, Any]) -> set[str]:
    """Return internal agent test paths, excluding delivered project tests."""
    paths: set[str] = set()
    registry = state.get("artifact_registry") or {}
    for path in registry.get("agent_generated_tests") or []:
        rel = _state_relative_path(state, str(path))
        if rel and is_agent_test_path(rel, state=state):
            paths.add(rel)
    for entry in registry.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("is_test") and (entry.get("agent_internal") or entry.get("user_visible") is False):
            rel = _state_relative_path(state, str(entry.get("path") or ""))
            if rel and is_agent_test_path(rel, state=state):
                paths.add(rel)
    for item in state.get("generated_files") or []:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        kind = str(item.get("kind") or "")
        if path and (item.get("agent_internal") or item.get("user_visible") is False):
            rel = _state_relative_path(state, str(path))
            if rel and (kind == "test" or _is_test_path(rel)) and is_agent_test_path(rel, state=state):
                paths.add(rel)
    return paths


def _path_matches_any(path: str, candidates: set[str]) -> bool:
    rel = _norm(path)
    if not rel:
        return False
    for candidate in candidates:
        item = _norm(candidate)
        if rel == item or rel.endswith("/" + item) or item.endswith("/" + rel):
            return True
    return False


def _failed_test_paths(state: dict[str, Any]) -> list[str]:
    tests = _test_results(state)
    paths: list[str] = []
    for key in ("failures", "errors", "issues"):
        for item in tests.get(key) or []:
            if not isinstance(item, dict):
                continue
            raw = item.get("file") or item.get("path") or item.get("target_file") or item.get("test_file")
            rel = _state_relative_path(state, str(raw or ""))
            if rel and rel not in paths:
                paths.append(rel)
    for item in state.get("failure_issues") or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("file") or item.get("path") or item.get("target_file") or item.get("test_file")
        rel = _state_relative_path(state, str(raw or ""))
        if rel and _is_test_path(rel) and rel not in paths:
            paths.append(rel)
    return paths


def _verification_failure_is_agent_generated_test_advisory(state: dict[str, Any], atom_summary: dict[str, Any]) -> bool:
    verification = state.get("verification") or {}
    if not verification or verification.get("ok") is not False:
        return False
    if state.get("contract_ok") is not True:
        return False
    if int(atom_summary.get("required_failed", 0) or 0) or int(atom_summary.get("required_unverified", 0) or 0):
        return False
    semantic = state.get("semantic_contract_check")
    if isinstance(semantic, dict) and semantic.get("ok") is False:
        return False
    if _zero_test_collection_detected(state):
        return False
    generated_tests = _agent_generated_test_paths(state)
    if not generated_tests:
        return False
    failed_paths = _failed_test_paths(state)
    if not failed_paths:
        return False
    return all(_path_matches_any(path, generated_tests) for path in failed_paths)


def _active_failure_resolved(state: dict[str, Any], atom_summary: dict[str, Any]) -> bool:
    verification = state.get("verification") or {}
    if verification.get("ok") is not True:
        return False
    if int(atom_summary.get("required_failed", 0) or 0):
        return False
    if int(atom_summary.get("required_unverified", 0) or 0):
        return False
    if state.get("failure_issues"):
        return False
    if state.get("mode") in WRITE_MODES | VERIFY_ONLY_MODES and state.get("contract_ok") is not True:
        return False
    return True


def _zero_test_collection_detected(state: dict[str, Any]) -> bool:
    tests = _test_results(state)
    runs = [run for run in tests.get("runs") or [] if isinstance(run, dict)]
    if runs and sum(int(run.get("total", 0) or 0) for run in runs) == 0:
        return True
    if runs and int(tests.get("total", 0) or 0) == 0:
        return True
    haystacks: list[str] = []
    for result in (state.get("verification") or {}).get("results") or []:
        if not isinstance(result, dict):
            continue
        haystacks.append(str(result.get("stdout") or ""))
        haystacks.append(str(result.get("stderr") or ""))
    text = "\n".join(haystacks).lower()
    return any(marker in text for marker in ["collected 0 items", "no tests ran", "ran 0 tests"])


def _controlled_failure_status(
    state: dict[str, Any],
    failures: list[str],
    write_scope_audit: dict[str, Any],
) -> dict[str, Any]:
    stopped = str(state.get("stopped_reason") or "")
    failure = state.get("failure") or {}
    failure_type = str(failure.get("failure_type") or "")
    reason = stopped if stopped in CONTROLLED_FAILURE_REASONS else failure_type
    if reason not in CONTROLLED_FAILURE_REASONS:
        return {
            "controlled_failure": False,
            "outcome": "failed" if failures else "verified_ok",
            "controlled_failure_reason": "",
        }

    safety_failures = [
        failure
        for failure in failures
        if failure in {
            "read_only_violation",
            "source_files_changed_in_safe_create",
            "existing_project_modification_in_safe_create",
        }
        or failure.startswith("protected_path_written:")
        or failure.startswith("prohibited_artifact_created:")
    ]
    if safety_failures:
        return {
            "controlled_failure": False,
            "outcome": "failed",
            "controlled_failure_reason": "controlled failure was invalidated by safety/scope violations",
            "controlled_failure_blockers": safety_failures,
        }

    if write_scope_audit.get("source_changed_files") or write_scope_audit.get("existing_project_modified_files"):
        return {
            "controlled_failure": False,
            "outcome": "failed",
            "controlled_failure_reason": "controlled failure was invalidated by write-scope changes",
            "controlled_failure_blockers": list(write_scope_audit.get("source_changed_files") or [])
            + list(write_scope_audit.get("existing_project_modified_files") or []),
        }

    return {
        "controlled_failure": True,
        "outcome": "unfulfillable_within_scope",
        "controlled_failure_reason": "task cannot be completed without source implementation changes that are outside the allowed scope",
    }


def _created_prohibited_artifacts(state: dict[str, Any], write_scope_audit: dict[str, Any]) -> list[str]:
    prohibited = (
        (state.get("task_contract") or {}).get("prohibited_artifacts")
        or (state.get("task_intent") or {}).get("prohibited_artifacts")
        or detect_prohibited_artifacts(state.get("task", ""))
    )
    if not prohibited:
        return []
    paths: list[str] = []
    for key in ("changed_files", "generated_files", "repair_changed_files", "all_recorded_changes"):
        for item in write_scope_audit.get(key) or []:
            rel = item.get("path") if isinstance(item, dict) else item
            if rel and rel not in paths:
                paths.append(str(rel).replace("\\", "/"))
    for item in state.get("generated_files") or []:
        rel = item.get("path") if isinstance(item, dict) else item
        if rel and rel not in paths:
            paths.append(str(rel).replace("\\", "/"))
    return [p for p in paths if is_prohibited_artifact_path(p, prohibited)]


def _protected_scope_outputs(state: dict[str, Any], write_scope_audit: dict[str, Any]) -> list[str]:
    scope = state.get("scope_contract") or (state.get("task_intent") or {}).get("scope_contract") or {}
    if not scope:
        return []
    paths: list[tuple[str, str | None]] = []
    for key in ("changed_files", "generated_files", "repair_changed_files", "all_recorded_changes"):
        for item in write_scope_audit.get(key) or []:
            rel = item.get("path") if isinstance(item, dict) else item
            if rel:
                rel = str(rel).replace("\\", "/")
                paths.append((rel, None))
    for item in state.get("generated_files") or []:
        if isinstance(item, dict) and item.get("path"):
            if item.get("agent_internal"):
                continue
            paths.append((str(item.get("path")).replace("\\", "/"), item.get("original_path")))
    out: list[str] = []
    for rel, original in paths:
        protected = protected_original_output(scope, rel, original_path=original)
        if protected and protected not in out:
            out.append(protected)
    return out


def _out_of_scope_existing_outputs(
    state: dict[str, Any],
    write_scope_audit: dict[str, Any],
) -> list[str]:
    """Return existing project files changed outside declared/expanded scope."""
    scope = state.get("scope_contract") or (state.get("task_intent") or {}).get("scope_contract") or {}
    allowed = {
        _norm(str(path))
        for path in (
            list(scope.get("allowed_modify_paths") or [])
            + list(scope.get("expanded_modify_paths") or [])
        )
        if _norm(str(path))
    }
    if not scope.get("allowed_modify_paths"):
        return []
    return sorted({
        _norm(str(path))
        for path in write_scope_audit.get("existing_project_modified_files") or []
        if _norm(str(path)) and _norm(str(path)) not in allowed
    })


def compute_final_gate(state: dict[str, Any]) -> dict[str, Any]:
    mode = state.get("mode")
    verification = state.get("verification") or {}
    stopped = state.get("stopped_reason") or ""
    failures: list[str] = []
    warnings: list[str] = []
    changed_files = state.get("changed_files") or []
    generated_files = [
        x for x in (state.get("generated_files") or [])
        if isinstance(x, dict) and x.get("ok") is not False
    ]
    changed_repairs = [
        x for x in (state.get("repair_history") or [])
        if isinstance(x, dict) and x.get("changed")
    ]
    write_scope_audit = build_write_scope_audit(state)
    if stopped == "clarification_required":
        return {
            "ok": False,
            "outcome": "clarification_required",
            "controlled_failure": True,
            "controlled_failure_reason": "clarification_required",
            "controlled_failure_blockers": list(state.get("clarification_questions") or []),
            "failures": ["clarification_required"],
            "warnings": [],
            "stopped_reason": "clarification_required",
            "write_scope_audit": write_scope_audit,
        }
    authoritative_atom_check = authoritative_requirement_atom_check(state)
    atom_summary = authoritative_atom_check.get("summary") or state.get("requirement_atom_summary") or {}
    generated_test_verification_advisory = _verification_failure_is_agent_generated_test_advisory(state, atom_summary)
    if generated_test_verification_advisory:
        warnings.append("agent_generated_tests_failed_but_contract_passed")

    if (state.get("write_locked") or state.get("read_only")) and (changed_files or generated_files or changed_repairs):
        failures.append("read_only_violation")
    safe_create = (state.get("task_intent") or {}).get("operation_mode") == "safe_create"
    if safe_create:
        existing_modified = write_scope_audit.get("existing_project_modified_files") or []
        baseline_unknown_source_changes = (
            not write_scope_audit.get("workspace_baseline_known")
            and bool(write_scope_audit.get("source_changed_files"))
        )
        if existing_modified or baseline_unknown_source_changes:
            failures.append("source_files_changed_in_safe_create")
        if existing_modified or baseline_unknown_source_changes or write_scope_audit.get("no_existing_project_modification") is False:
            failures.append("existing_project_modification_in_safe_create")
    if _source_modify_requires_code_change(state) and not _source_code_changes(write_scope_audit):
        if _verified_without_implementation_change(state, atom_summary):
            warnings.append("requirements_verified_without_implementation_change")
        else:
            failures.append("source_modify_without_code_change")
    prohibited_created = _created_prohibited_artifacts(state, write_scope_audit)
    if prohibited_created:
        failures.append("prohibited_artifact_created:" + ",".join(prohibited_created[:5]))
    protected_outputs = _protected_scope_outputs(state, write_scope_audit)
    if protected_outputs:
        failures.append("protected_path_written:" + ",".join(protected_outputs[:5]))
    out_of_scope_outputs = _out_of_scope_existing_outputs(state, write_scope_audit)
    if out_of_scope_outputs:
        failures.append("out_of_scope_existing_path_written:" + ",".join(out_of_scope_outputs[:5]))

    if mode in WRITE_MODES or mode in VERIFY_ONLY_MODES:
        if not verification:
            failures.append("missing_execution_verification")
        elif not verification.get("ok") and not generated_test_verification_advisory:
            failures.append("verification_failed")
        if mode in WRITE_MODES:
            if state.get("needs_verification"):
                failures.append("needs_verification_true")
            if state.get("contract_ok") is not True:
                failures.append("contract_check_missing_or_failed")
            if mode in {"write", "generate_project"} and not (state.get("changed_files") or state.get("repair_history")):
                failures.append("no_artifact_changes")
    elif mode == "analyze":
        if verification and not verification.get("ok"):
            failures.append("verification_failed")
        analysis_quality = state.get("analysis_quality") or {}
        quality_warnings = list(analysis_quality.get("warnings") or []) + list(verification.get("quality_warnings") or [])
        if not analysis_quality.get("ok"):
            failures.append("analysis_quality_failed")
        if quality_warnings:
            failures.append("analysis_quality_warnings")
        if state.get("contract_ok") is False:
            failures.append("contract_check_failed")

    if stopped in {"max_rounds", "max_rounds_no_artifact"}:
        failures.append(stopped)
    if state.get("failure") and not generated_test_verification_advisory and not _active_failure_resolved(state, atom_summary):
        failures.append("active_failure_present")
    if _has_required_atoms(state, atom_summary) and not atom_summary:
        failures.append("missing_requirement_atom_check")
    if mode in WRITE_MODES or mode in VERIFY_ONLY_MODES or mode == "analyze":
        if atom_summary.get("required_unverified"):
            failures.append(f"required_requirement_atoms_unverified:{atom_summary.get('required_unverified')}")
        if atom_summary.get("required_failed"):
            failures.append(f"required_requirement_atoms_failed:{atom_summary.get('required_failed')}")
    if _zero_test_collection_detected(state):
        failures.append("pytest_zero_tests_collected")

    ok = not failures
    if not ok:
        if "pytest_zero_tests_collected" in failures:
            stopped_reason = "pytest_zero_tests_collected"
        elif "verification_failed" in failures:
            if stopped in {"", "done", "verified_ok"}:
                rounds_exhausted = int(state.get("round_idx", 0) or 0) >= int(state.get("max_rounds", 0) or 0) > 0
                stopped_reason = (
                    "max_rounds_with_unresolved_failure"
                    if rounds_exhausted
                    else "unresolved_verification_failure"
                )
            else:
                stopped_reason = stopped
        elif "read_only_violation" in failures:
            stopped_reason = "read_only_violation"
        elif "missing_execution_verification" in failures:
            stopped_reason = "finished_without_verification"
        elif "needs_verification_true" in failures:
            stopped_reason = "finished_with_pending_verification"
        elif "analysis_quality_failed" in failures or "analysis_quality_warnings" in failures:
            stopped_reason = "analysis_quality_failed"
        else:
            stopped_reason = stopped or "failed_final_gate"
    else:
        successful_stop_reasons = {
            "verified_ok",
            "analysis_complete",
            "verified_with_generated_test_warnings",
        }
        stopped_reason = stopped if stopped in successful_stop_reasons else "verified_ok"
        if generated_test_verification_advisory and stopped_reason != "analysis_complete":
            stopped_reason = "verified_with_generated_test_warnings"

    controlled = _controlled_failure_status(state, failures, write_scope_audit)
    return {
        "ok": ok,
        **controlled,
        "failures": failures,
        "warnings": warnings,
        "stopped_reason": stopped_reason,
        "write_scope_audit": write_scope_audit,
    }
