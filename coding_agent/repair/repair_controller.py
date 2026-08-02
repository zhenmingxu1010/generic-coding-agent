from __future__ import annotations

from pathlib import Path
from typing import Any

from coding_agent.verification.test_registry import registered_test_paths
from coding_agent.workspace.failed_writes import failed_write_for_path
from coding_agent.workspace.run_paths import is_test_like_path
from coding_agent.scope.scope_contract import path_allows_modify


def _norm(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    if rel.strip().lower() in {"", "none", "null"}:
        return ""
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _project_rel(state: dict[str, Any], path: str | None) -> str:
    """Normalize an in-workspace absolute traceback path to project-relative."""
    rel = _norm(path)
    if not rel or not Path(rel).is_absolute():
        return rel
    workspace = state.get("workspace")
    if not workspace:
        return rel
    try:
        return _norm(str(Path(rel).resolve().relative_to(Path(workspace).resolve())))
    except (OSError, ValueError):
        return rel


def _is_test_path(path: str | None) -> bool:
    return is_test_like_path(path)


def _generated_code_targets(state: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for source in ((state.get("file_plan") or {}).get("files") or [], state.get("generated_files") or []):
        for item in source:
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "code" and item.get("path"):
                rel = _norm(str(item.get("path")))
                if _is_test_path(rel):
                    continue
                if rel and rel not in out:
                    out.append(rel)
    return out


def _generated_test_targets(state: dict[str, Any]) -> list[str]:
    out: list[str] = []
    registry = state.get("verification_test_registry") or {}
    for rel in registry.get("paths") or []:
        rel = _norm(str(rel))
        if rel and rel not in out:
            out.append(rel)
    for source in ((state.get("file_plan") or {}).get("files") or [], state.get("generated_files") or []):
        for item in source:
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "test" and item.get("path"):
                rel = _norm(str(item.get("path")))
                if rel and rel not in out:
                    out.append(rel)
    return out


def _scope_source_targets(state: dict[str, Any]) -> list[str]:
    scope = (
        state.get("scope_contract")
        or (state.get("task_intent") or {}).get("scope_contract")
        or {}
    )
    out: list[str] = []
    for value in (
        list(scope.get("allowed_modify_paths") or [])
        + list(scope.get("expanded_modify_paths") or [])
    ):
        rel = _norm(str(value))
        if (
            rel
            and not _is_test_path(rel)
            and not any(marker in rel for marker in ("*", "?", "["))
            and rel not in out
        ):
            out.append(rel)
    return out


def _has_unlocalized_behavior_failure(issues: list[dict[str, Any]]) -> bool:
    behavior_types = {
        "semantic_requirement_atom_failed",
        "verification_command_failed",
        "contract_failure",
        "deliverable_consistency_error",
    }
    return any(str(issue.get("type") or "") in behavior_types for issue in issues)


def finalized_controller_for_current_failure(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return the failure-owner controller only while its failure is active."""
    controller = state.get("repair_controller") or {}
    if not controller.get("finalized"):
        return None
    active_signature = str((state.get("failure") or {}).get("signature") or "")
    if str(controller.get("failure_signature") or "") != active_signature:
        return None
    return controller


def _first_generated_code_file(state: dict[str, Any]) -> str | None:
    targets = _generated_code_targets(state)
    return targets[0] if targets else None


def _issue_path(issue: dict[str, Any]) -> str | None:
    return _norm(str(issue.get("target_file") or issue.get("file") or "")) or None


def _import_error_context_targets(state: dict[str, Any], issue: dict[str, Any]) -> list[str]:
    context = state.get("import_error_context") or {}
    if not context.get("present"):
        return []
    module = str(issue.get("missing_module") or issue.get("module") or "").strip()
    out: list[str] = []

    def add(path: str | None) -> None:
        rel = _norm(path)
        if rel and rel not in out:
            out.append(rel)

    for item in context.get("missing_modules") or []:
        if not isinstance(item, dict):
            continue
        if module and str(item.get("module") or "") != module:
            continue
        # Prefer executable/module files before package markers; the marker file
        # is useful evidence but often not the only place that can be repaired.
        candidates = [x for x in item.get("module_path_candidates") or [] if isinstance(x, dict)]
        for candidate in candidates:
            path = str(candidate.get("path") or "")
            if path and not path.endswith("/__init__.py"):
                add(path)
        for candidate in candidates:
            add(str(candidate.get("path") or ""))

    for config_file in ((context.get("project_config") or {}).get("files") or []):
        if str(config_file) in {"pyproject.toml", "setup.cfg", "setup.py"}:
            add(str(config_file))

    for rel in _generated_code_targets(state):
        add(rel)
    return out


def _planned_or_generated_item_for_path(state: dict[str, Any], path: str | None) -> dict[str, Any] | None:
    rel = _norm(path)
    if not rel:
        return None
    for source in (state.get("generated_files") or [], (state.get("file_plan") or {}).get("files") or []):
        for item in source:
            if isinstance(item, dict) and _norm(item.get("path")) == rel:
                return item
    return None


def _is_current_agent_planned_or_generated_path(state: dict[str, Any], path: str | None) -> bool:
    rel = _norm(path)
    if not rel:
        return False
    if _planned_or_generated_item_for_path(state, rel):
        return True
    return False


def _verification_text(state: dict[str, Any], limit: int = 20000) -> str:
    chunks: list[str] = []
    for result in (state.get("verification") or {}).get("results") or []:
        if not isinstance(result, dict):
            continue
        chunks.append(str(result.get("stdout") or ""))
        chunks.append(str(result.get("stderr") or ""))
    return "\n".join(chunks)[:limit]


def _structured_zero_collected(state: dict[str, Any]) -> bool:
    test_results = state.get("test_results") or (state.get("verification") or {}).get("test_results") or {}
    runs = [run for run in test_results.get("runs") or [] if isinstance(run, dict)]
    if not runs:
        return False
    total = sum(int(run.get("total", 0) or 0) for run in runs)
    markers = ["no tests ran", "collected 0 item", "ran 0 tests"]
    return total == 0 and any(
        any(marker in str(run.get("stdout") or run.get("stderr") or run.get("message") or "").lower() for marker in markers)
        or str(run.get("type") or "").lower() == "pytest_zero_collected"
        for run in runs
    )


def pytest_zero_collected(state: dict[str, Any]) -> bool:
    test_results = state.get("test_results") or (state.get("verification") or {}).get("test_results") or {}
    structured_runs = [
        run for run in test_results.get("runs") or []
        if isinstance(run, dict)
    ]
    if structured_runs:
        # The registry controller owns the authoritative project/generated
        # pytest run only. A separate task-specific verification command may
        # guess a nonexistent node selector and collect zero tests, but that is
        # an oracle/evidence problem rather than a broken test registry.
        return _structured_zero_collected(state)
    text = _verification_text(state).lower()
    return any(marker in text for marker in ["no tests ran", "collected 0 items", "ran 0 tests"])


def _syntax_rejected_generated_write_issue_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    failure = state.get("failure") or {}
    if str(failure.get("failure_type") or "") != "syntax_level_error":
        return None
    target = _norm(failure.get("target_file"))
    if not target or not _is_current_agent_planned_or_generated_path(state, target):
        return None

    item = _planned_or_generated_item_for_path(state, target) or {}
    syntax = failure.get("syntax_check") or item.get("syntax_check") or {}
    failed_write = failure.get("failed_write") or item.get("failed_write") or failed_write_for_path(state, target)
    if failed_write:
        syntax = syntax or failed_write.get("syntax_check") or {}
    if not ((syntax or {}).get("checked") and not (syntax or {}).get("ok")):
        raw = str(failure.get("raw_excerpt") or "")
        if "SyntaxError" not in raw and "syntax" not in raw.lower():
            return None

    workspace = state.get("workspace")
    target_exists = bool(workspace and (Path(workspace) / target).exists())
    kind = str(item.get("kind") or "")
    owner = "generated_test" if kind == "test" or _is_test_path(target) else "implementation"
    return {
        "owner": owner,
        "type": "syntax_rejected_generated_file",
        "file": target,
        "target_file": target,
        "target_files": [target],
        "message": failure.get("message") or "generated file write was rejected by Python syntax check",
        "syntax_check": syntax,
        "failed_write": failed_write,
        "target_file_exists": target_exists,
        "repair_hint": "rewrite the full generated file with syntactically valid content; do not read the target if it was never written",
        "source": "repair_controller:syntax_rejected_write",
    }


def _failed_requirement_atom_issues_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check in [
        state.get("requirement_atom_check"),
        (state.get("semantic_contract_check") or {}).get("requirement_atom_check"),
        ((state.get("contract_check") or {}).get("semantic_contract_check") or {}).get("requirement_atom_check"),
    ]:
        if not isinstance(check, dict):
            continue
        for atom in check.get("atoms") or []:
            if not isinstance(atom, dict) or atom.get("status") != "failed":
                continue
            atom_id = str(atom.get("id") or "")
            if not atom_id or atom_id in seen:
                continue
            seen.add(atom_id)
            details = atom.get("details") or {}
            claim = details.get("verification_claim") or {}
            evidence_reason = str(claim.get("reason") or atom.get("verify_hint") or "").strip()
            issues.append({
                "owner": "requirement",
                "type": "semantic_requirement_atom_failed",
                "atom_id": atom_id,
                "atom_type": atom.get("type"),
                "file": None,
                "target_file": None,
                "target_files": [],
                "message": f"{atom_id}: {atom.get('description', '')}",
                "details": details,
                "repair_hint": "use the cited execution evidence to satisfy the stated requirement" + (f"; {evidence_reason}" if evidence_reason else ""),
                "source": "repair_controller:requirement_atom_check",
            })
    return issues


def enrich_failure_issues_for_repair(state: dict[str, Any], issues: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out = []
    for source_issue in (issues if issues is not None else state.get("failure_issues") or []):
        issue = dict(source_issue)
        for key in ("file", "target_file"):
            if issue.get(key):
                issue[key] = _project_rel(state, issue.get(key))
        if issue.get("target_files"):
            issue["target_files"] = [
                _project_rel(state, path)
                for path in issue.get("target_files") or []
                if _project_rel(state, path)
            ]
        out.append(issue)
    syntax_issue = _syntax_rejected_generated_write_issue_from_state(state)
    if syntax_issue and not any(str(issue.get("type") or "") == "syntax_rejected_generated_file" for issue in out):
        out.append(syntax_issue)
    existing_atom_ids = {str(issue.get("atom_id") or "") for issue in out if issue.get("atom_id")}
    for issue in _failed_requirement_atom_issues_from_state(state):
        if str(issue.get("atom_id") or "") not in existing_atom_ids:
            out.append(issue)
            existing_atom_ids.add(str(issue.get("atom_id") or ""))
    if not any(str(issue.get("type") or "") == "pytest_zero_collected" for issue in out) and pytest_zero_collected(state):
        out.append({
            "owner": "generated_test",
            "type": "pytest_zero_collected",
            "file": None,
            "target_file": None,
            "message": "pytest collected zero tests",
            "repair_hint": "repair the test registry, pytest target selection, or generated test collection naming",
            "source": "repair_controller",
        })
    return out


def _missing_symbol_route(issue: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    if str(issue.get("type") or "") not in {"missing_imported_symbol", "import_error_missing_symbol", "modulenotfounderror"}:
        return None
    targets: list[str] = []
    for key in ("file", "target_file"):
        rel = _norm(str(issue.get(key) or ""))
        if rel and rel not in targets:
            targets.append(rel)
    if str(issue.get("type") or "") == "modulenotfounderror":
        for rel in _import_error_context_targets(state, issue):
            if rel and rel not in targets:
                targets.append(rel)
    generated_tests = set(_generated_test_targets(state))
    test_file = _norm(str(issue.get("file") or ""))
    owner = "generated_test" if test_file in generated_tests or _is_test_path(test_file) else "implementation"
    if owner == "generated_test":
        strategy = "fix_generated_test"
        route = "resolve_interface_mismatch"
        allowed = ["edit_file", "write_file", "run_tests", "run_shell", "finish"]
    else:
        strategy = "fix_implementation"
        route = "fix_implementation_import_api"
        allowed = ["edit_file", "write_file", "run_tests", "run_shell", "finish"]
    return {
        "route": route,
        "failure_owner": owner,
        "strategy": strategy,
        "target_files": targets or _generated_code_targets(state)[:1],
        "allowed_tools": allowed,
        "blocked_tools": [],
        "reason": "test imports a missing module/symbol; use structured import context to choose a project-appropriate repair",
        "primary_issue": issue,
    }


def _zero_collected_route(issue: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    if str(issue.get("type") or "") != "pytest_zero_collected":
        return None
    targets = _generated_test_targets(state)
    return {
        "route": "fix_test_registry",
        "failure_owner": "generated_test",
        "strategy": "fix_test_registry",
        "target_files": targets,
        "allowed_tools": ["edit_file", "write_file", "run_tests", "run_shell", "finish"],
        "blocked_tools": ["read_file"],
        "reason": "pytest collected zero tests, so repair test registration/target selection or generated test collection before implementation changes",
        "primary_issue": issue,
    }


def _syntax_rejected_generated_file_route(issue: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    if str(issue.get("type") or "") != "syntax_rejected_generated_file":
        return None
    target = _issue_path(issue)
    owner = "generated_test" if str(issue.get("owner") or "") == "generated_test" or _is_test_path(target) else "implementation"
    return {
        "route": "rewrite_rejected_generated_file",
        "failure_owner": owner,
        "strategy": "fix_generated_test" if owner == "generated_test" else "fix_implementation",
        "target_files": [target] if target else [],
        "allowed_tools": ["write_file", "run_tests", "run_shell", "finish"],
        "blocked_tools": ["read_file", "read_many_files", "search_text", "filter_files", "inspect_python", "edit_file"],
        "required_tool": "write_file",
        "reason": "generated file write was rejected by syntax validation; target may not exist, so repair must rewrite the full file instead of reading or editing it",
        "primary_issue": issue,
    }


def _fallback_route(state: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    impl = [issue for issue in issues if str(issue.get("owner") or "") == "implementation"]
    generated = [issue for issue in issues if "generated_test" in str(issue.get("owner") or "")]
    if generated and not impl:
        return {
            "route": "fix_generated_test",
            "failure_owner": "generated_test",
            "strategy": "fix_generated_test",
            "target_files": [p for p in (_issue_path(issue) for issue in generated) if p],
            "allowed_tools": ["edit_file", "write_file", "run_tests", "run_shell", "finish"],
            "blocked_tools": [],
            "reason": "all actionable issues are owned by current-agent generated tests",
            "primary_issue": generated[0],
        }
    if impl:
        issue_targets = [
            path
            for path in (_issue_path(issue) for issue in impl)
            if path
        ]
        behavior_failure = _has_unlocalized_behavior_failure(issues)
        if behavior_failure:
            # A final audit can only attach a finding to files it was given,
            # and a failed observable scenario often has no traceback file.
            # Preserve the bounded semantic source scope for these multi-file
            # repairs instead of locking onto a noisy or arbitrary first path.
            scope_targets = _scope_source_targets(state)
            if scope_targets:
                scope = (
                    state.get("scope_contract")
                    or (state.get("task_intent") or {}).get("scope_contract")
                    or {}
                )
                issue_targets = [
                    path
                    for path in issue_targets
                    if path_allows_modify(scope, path) and not _is_test_path(path)
                ]
            target_files = list(dict.fromkeys(scope_targets + issue_targets))[:8]
        else:
            target_files = list(dict.fromkeys(issue_targets))
        return {
            "route": "fix_implementation",
            "failure_owner": "implementation" if not generated else "implementation_and_generated_test",
            "strategy": "fix_implementation",
            "target_files": target_files or _generated_code_targets(state)[:1],
            "allowed_tools": ["edit_file", "write_file", "run_tests", "run_shell", "finish"],
            "blocked_tools": [],
            "reason": "implementation issues are present and must be repaired before claiming success",
            "primary_issue": impl[0],
        }
    return {
        "route": "inspect_more",
        "failure_owner": "unknown",
        "strategy": "inspect_more",
        "target_files": [],
        "allowed_tools": ["read_file", "read_many_files", "search_text", "filter_files", "inspect_python", "run_tests", "run_shell", "finish"],
        "blocked_tools": [],
        "reason": "no deterministic repair route matched",
        "primary_issue": issues[0] if issues else None,
    }


def _verification_evidence_route(state: dict[str, Any]) -> dict[str, Any] | None:
    """Keep evidence gaps out of implementation repair.

    An unverified requirement is not evidence that the implementation is
    wrong. Verification replanning is owned by the graph; this route is a
    defense in depth if such a state reaches the repair controller.
    """
    check = state.get("requirement_atom_check") or {}
    atoms = [atom for atom in check.get("atoms") or [] if isinstance(atom, dict) and atom.get("required", True)]
    failed = [atom for atom in atoms if str(atom.get("status") or "") == "failed"]
    unverified = [atom for atom in atoms if str(atom.get("status") or "") == "unverified"]
    results = (state.get("verification") or {}).get("results") or []
    all_commands_passed = bool(results) and all(
        int(result.get("returncode", 1) or 0) == 0 and not result.get("timed_out")
        for result in results
        if isinstance(result, dict)
    )
    if failed or not unverified or not all_commands_passed:
        return None
    atom_ids = [str(atom.get("id")) for atom in unverified if atom.get("id")]
    issue = {
        "owner": "verification_evidence",
        "type": "required_requirement_unverified",
        "atom_ids": atom_ids,
        "message": "required behavior lacks executed evidence",
        "source": "repair_controller:requirement_atom_check",
    }
    return {
        "route": "complete_verification",
        "failure_owner": "verification_evidence",
        "strategy": "inspect_more",
        "target_files": [],
        "allowed_tools": ["read_file", "read_many_files", "search_text", "filter_files", "inspect_python", "run_tests", "run_shell", "finish"],
        "blocked_tools": ["write_file", "edit_file"],
        "reason": "requirements are unverified but none failed; collect execution evidence without changing implementation",
        "primary_issue": issue,
    }


def build_repair_controller(state: dict[str, Any]) -> dict[str, Any]:
    issues = enrich_failure_issues_for_repair(state)
    routes = []
    verification_evidence_route = _verification_evidence_route(state)
    for issue in issues:
        for router in (_syntax_rejected_generated_file_route, _zero_collected_route, _missing_symbol_route):
            route = router(issue, state)
            if route:
                routes.append(route)
                break

    def route_priority(route: dict[str, Any]) -> tuple[int, str]:
        owner = str(route.get("failure_owner") or "")
        route_name = str(route.get("route") or "")
        if route_name == "rewrite_rejected_generated_file":
            return (0, route_name)
        if route_name == "fix_test_registry":
            return (1, route_name)
        if owner in {"implementation", "generated_test", "implementation_and_generated_test"}:
            return (2, route_name)
        return (9, route_name)

    concrete_route = sorted(routes, key=route_priority)[0] if routes else _fallback_route(state, issues)
    # An execution-evidence gap must not hide a newly discovered concrete
    # implementation or generated-test defect. The evidence-only route is the
    # safe fallback only while there is no actionable owner/target to repair.
    selected = (
        concrete_route
        if str(concrete_route.get("route") or "") != "inspect_more"
        else (verification_evidence_route or concrete_route)
    )
    target_files = []
    for rel in selected.get("target_files") or []:
        rel = _norm(rel)
        if rel and rel not in target_files:
            target_files.append(rel)
    selected = dict(selected)
    selected["target_files"] = target_files
    return {
        "version": "repair_controller_v2",
        "failure_signature": str((state.get("failure") or {}).get("signature") or ""),
        "route": selected.get("route"),
        "failure_owner": selected.get("failure_owner"),
        "strategy": selected.get("strategy"),
        "target_files": target_files,
        "allowed_tools": selected.get("allowed_tools") or [],
        "blocked_tools": selected.get("blocked_tools") or [],
        "required_tool": selected.get("required_tool"),
        "primary_issue": selected.get("primary_issue"),
        "issues": issues,
        "routes_considered": routes,
        "registered_test_paths": registered_test_paths(state, existing_only=False),
        "reason": selected.get("reason") or "",
    }


def finalize_repair_controller(
    state: dict[str, Any],
    decision: dict[str, Any],
    *,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn one validated failure-owner decision into the active controller."""
    controller = dict(base or build_repair_controller(state))
    deterministic_routes = {"rewrite_rejected_generated_file", "fix_test_registry", "complete_verification"}
    if str(controller.get("route") or "") not in deterministic_routes:
        owner = str(decision.get("failure_owner") or "unknown")
        strategy = str(decision.get("strategy") or "inspect_more")
        targets: list[str] = []
        for path in decision.get("target_files") or []:
            rel = _project_rel(state, str(path))
            if rel and rel not in targets:
                targets.append(rel)

        if owner == "generated_test":
            generated_tests = set(_generated_test_targets(state))
            targets = [path for path in targets if path in generated_tests]
            if not targets:
                targets = sorted(generated_tests)[:3]
        elif strategy == "fix_implementation":
            scope = (
                state.get("scope_contract")
                or (state.get("task_intent") or {}).get("scope_contract")
                or {}
            )
            scope_targets = _scope_source_targets(state)
            # An LLM owner review may suggest a plausible but unauthorized
            # file. Keep implementation repair inside the established scope.
            if scope_targets:
                targets = [
                    path
                    for path in targets
                    if path_allows_modify(scope, path) and not _is_test_path(path)
                ]
            base_targets = [
                _norm(str(path))
                for path in controller.get("target_files") or []
                if _norm(str(path)) and not _is_test_path(str(path))
            ]
            if scope_targets:
                base_targets = [
                    path for path in base_targets
                    if path_allows_modify(scope, path)
                ]
            issues = [
                issue
                for issue in controller.get("issues") or []
                if isinstance(issue, dict)
            ]
            if _has_unlocalized_behavior_failure(issues):
                targets = list(dict.fromkeys(scope_targets + base_targets + targets))[:8]
            elif not targets:
                targets = base_targets or scope_targets or _generated_code_targets(state)[:1]

        if strategy in {"fix_implementation", "fix_generated_test"}:
            route = strategy
            allowed_tools = ["edit_file", "write_file", "run_tests", "run_shell", "finish"]
            blocked_tools: list[str] = []
            required_tool = None
        elif strategy == "stop_with_reason":
            route = "stop_with_reason"
            allowed_tools = ["finish"]
            blocked_tools = ["edit_file", "write_file"]
            required_tool = "finish"
            targets = []
        else:
            route = "inspect_more"
            allowed_tools = ["read_file", "read_many_files", "search_text", "filter_files", "inspect_python", "run_tests", "run_shell", "finish"]
            blocked_tools = ["edit_file", "write_file"]
            required_tool = None

        controller.update({
            "route": route,
            "failure_owner": owner,
            "strategy": strategy,
            "target_files": targets,
            "allowed_tools": allowed_tools,
            "blocked_tools": blocked_tools,
            "required_tool": required_tool,
            "reason": str(decision.get("reason") or "failure owner selected the repair strategy"),
        })

    controller["failure_signature"] = str((state.get("failure") or {}).get("signature") or "")
    controller["finalized"] = True
    controller["finalized_by"] = "failure_owner"
    return controller


def force_action_from_controller(controller: dict[str, Any]) -> dict[str, Any] | None:
    blocked = set(controller.get("blocked_tools") or [])
    allowed = list(controller.get("allowed_tools") or [])
    required = controller.get("required_tool")
    target_files = [
        _norm(str(item))
        for item in controller.get("target_files") or []
        if _norm(str(item))
    ]
    allowed_read_files = [
        _norm(str(item))
        for item in controller.get("allowed_read_files") or []
        if _norm(str(item))
    ]
    if allowed_read_files and "read_file" not in allowed:
        # The execution broker permits one bounded, uncached read of an active
        # repair target. Expose the same permission to the decision validator
        # so a valid read is not rejected before it reaches that broker.
        allowed.append("read_file")
    target_owner = str(controller.get("failure_owner") or "")
    target_lock_needed = bool(
        target_files
        and str(controller.get("strategy") or "") in {
            "fix_implementation",
            "fix_generated_test",
            "fix_test_registry",
        }
        and target_owner in {
            "implementation",
            "generated_test",
            "implementation_and_generated_test",
        }
    )
    if not blocked and not required and not target_lock_needed:
        return None
    path = target_files[0] if target_files else None
    return {
        "version": "repair_controller_v2",
        "reason": controller.get("reason") or "repair controller selected a non-exploratory repair route",
        "failure_signature": controller.get("failure_signature"),
        "path": path,
        "required_path": path if target_lock_needed else None,
        "allowed_target_files": target_files if target_lock_needed else [],
        "allowed_read_files": allowed_read_files,
        "allowed_tools": allowed,
        "blocked_tools": sorted(blocked),
        "route": controller.get("route"),
        "required_tool": required,
    }
