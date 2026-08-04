from __future__ import annotations

import os
import json
import hashlib
import shutil
from pathlib import Path

from coding_agent.workspace.run_paths import agent_repo_root, agent_test_root_rel, internal_generated_tests_enabled, is_agent_test_path
from coding_agent.core.schemas import VerificationResult, CommandResult
from coding_agent.core.utils import normalize_volatile_text
from coding_agent.verification.test_registry import refresh_verification_test_registry, registered_test_paths, normalize_rel
from coding_agent.tools.shell_tools import run_shell
from coding_agent.tools.test_tools import run_tests
from coding_agent.contracts.contract import contract_quality_check
from coding_agent.contracts.semantic_contract import run_semantic_contract_checks
from coding_agent.workspace.interface_check import run_interface_consistency_check
from coding_agent.memory.trace_payloads import requirement_atom_trace_status
from coding_agent.verification.behavior_review import (
    collect_verification_artifacts,
    review_behavior_evidence,
    supplement_verification_steps,
)
from coding_agent.verification.test_baseline import compare_with_test_baseline
from coding_agent.verification.console_entry import adapt_console_command
from coding_agent.safety.path_guard import is_within_workspace
from .common import get_trace


def _update_verification_progress_guard(
    state: dict,
    result_dicts: list[dict],
    contract_check: dict,
) -> None:
    """Detect repeated verification evidence without encoding domain failures."""
    failed_commands = [
        {
            "name": str(result.get("name") or ""),
            "command": [str(part) for part in result.get("command") or []],
            "returncode": int(result.get("returncode", 1) or 0),
            "timed_out": bool(result.get("timed_out")),
            "stdout": normalize_volatile_text(str(result.get("stdout") or "")[-1200:]),
            "stderr": normalize_volatile_text(str(result.get("stderr") or "")[-1200:]),
        }
        for result in result_dicts
        if (int(result.get("returncode", 1) or 0) != 0 or result.get("timed_out"))
        and str(result.get("oracle_status") or "grounded") == "grounded"
    ]
    unresolved_atoms = [
        {
            "id": str(atom.get("id") or ""),
            "status": str(atom.get("status") or ""),
        }
        for atom in (state.get("requirement_atom_check") or {}).get("atoms") or []
        if atom.get("required", True) and str(atom.get("status") or "") in {"failed", "unverified", "pending"}
    ]
    payload = {
        "failed_commands": failed_commands,
        "unresolved_atoms": unresolved_atoms,
        "contract_failures": sorted(normalize_volatile_text(item) for item in contract_check.get("failures") or []),
    }
    if not failed_commands and not unresolved_atoms and not payload["contract_failures"]:
        state.pop("verification_failure_fingerprint", None)
        state["verification_failure_repeat_count"] = 0
        state["verification_stalled"] = False
        return

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    previous = str(state.get("verification_failure_fingerprint") or "")
    repeat_count = int(state.get("verification_failure_repeat_count", 0) or 0) + 1 if previous == fingerprint else 1
    state["verification_failure_fingerprint"] = fingerprint
    state["verification_failure_repeat_count"] = repeat_count
    state["verification_stalled"] = repeat_count >= 3


def _dedupe(cmds: list[tuple[str, list[str]]], state: dict | None = None) -> list[tuple[str, list[str]]]:
    """Remove only verification steps with identical execution semantics.

    Equal argv does not imply an equal verification scenario. A command run in
    a disposable sandbox, or bound to different requirement claims, must remain
    a separate step. Otherwise the executor can silently discard the only
    evidence for a required behavior.
    """
    state = state or {}
    sandboxes = state.get("verification_step_sandboxes") or {}
    claims = state.get("verification_step_claims") or {}
    workspaces = state.get("verification_step_execution_workspaces") or {}
    out = []
    seen = set()
    for name, cmd in cmds:
        sandbox_key = json.dumps(
            sandboxes.get(name),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        key = (
            tuple(str(part) for part in cmd),
            sandbox_key,
            str(workspaces.get(name) or "workspace"),
            tuple(sorted(str(atom_id) for atom_id in claims.get(name) or [])),
        )
        if key not in seen:
            out.append((name, cmd))
            seen.add(key)
    return out


def _find_py_files(workspace: str, state: dict | None = None) -> list[str]:
    root = Path(workspace).resolve()
    out = []
    for p in root.rglob("*.py"):
        if not is_within_workspace(root, p):
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        if "__pycache__" in p.parts:
            continue
        if ".coding_agent" in p.parts:
            continue
        if ".coding_agent_test" in p.parts and not is_agent_test_path(rel, state=state or {}):
            continue
        out.append(rel)
    return sorted(out)


def _find_test_files(workspace: str, state: dict | None = None) -> list[str]:
    out: list[str] = []
    for p in _find_py_files(workspace, state):
        name = Path(p).name
        if name.startswith("test_") or name.endswith("_test.py"):
            out.append(p)
    return out


def _is_source_modify_verification(state: dict) -> bool:
    intent = state.get("task_intent") or {}
    scope = state.get("scope_contract") or intent.get("scope_contract") or {}
    has_allowed_source_paths = bool(scope.get("allowed_modify_paths") or intent.get("allowed_modify_paths"))
    if has_allowed_source_paths:
        return True
    if state.get("mode") not in {"modify", "debug", "repair_existing"}:
        return False
    return bool(intent.get("source_modify_intent") or intent.get("operation_mode") == "scoped_modify")


def _is_verify_only_mode(state: dict) -> bool:
    return (
        state.get("mode") == "run_verify"
        or (state.get("task_intent") or {}).get("operation_mode") == "verify_only"
        or (state.get("supervisor") or {}).get("operation_mode") == "verify_only"
    )


def _agent_test_files(workspace: str, state: dict) -> list[str]:
    return [p for p in _find_test_files(workspace, state) if is_agent_test_path(p, state=state)]


def _pytest_targets(cmd: list[str] | str) -> list[str]:
    if not isinstance(cmd, list):
        return []
    parts = [str(x) for x in cmd]
    if len(parts) >= 3 and parts[:3] == ["python", "-m", "pytest"]:
        args = parts[3:]
    elif parts and Path(parts[0]).name in {"pytest", "pytest.exe"}:
        args = parts[1:]
    else:
        return []
    targets = []
    skip_next = False
    options_with_values = {"-k", "-m", "--maxfail", "--tb", "--junitxml", "--cov", "--cov-report", "-o"}
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in options_with_values:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        # Preserve pytest node selectors. Dropping ``::test_name`` silently
        # broadens a missing or misspelled scenario to the whole file and can
        # turn unrelated passing tests into false requirement evidence.
        targets.append(normalize_rel(arg))
    return targets


def _is_pytest_command(cmd: list[str] | str) -> bool:
    if not isinstance(cmd, list) or not cmd:
        return False
    parts = [str(x) for x in cmd]
    return (len(parts) >= 3 and parts[:3] == ["python", "-m", "pytest"]) or Path(parts[0]).name in {"pytest", "pytest.exe"}


def _default_commands(state: dict) -> list[tuple[str, list[str]]]:
    if state.get("read_only") and state.get("mode") == "analyze":
        return []
    workspace = state["workspace"]
    cmds: list[tuple[str, list[str]]] = []
    planned_cmds: list[tuple[str, list[str]]] = []
    verification_rel = f"{agent_test_root_rel(state=state)}/verification"
    verification_dir = Path(workspace).resolve() / verification_rel
    verification_dir.resolve().relative_to((Path(workspace).resolve() / ".coding_agent_test").resolve())
    if verification_dir.exists():
        shutil.rmtree(verification_dir)
    verification_dir.mkdir(parents=True, exist_ok=True)
    state["verification_artifacts_dir"] = str(verification_dir)
    state["verification_step_claims"] = {}
    state["verification_step_timeouts"] = {}
    state["verification_step_stdin"] = {}
    state["verification_step_success_exit_codes"] = {}
    state["verification_step_sandboxes"] = {}
    state["verification_command_adapters"] = {}
    state["verification_infrastructure_step_names"] = []
    for step in (state.get("file_plan") or {}).get("verify_steps") or []:
        if not isinstance(step, dict):
            continue
        name = str(step.get("name") or "").strip()
        sandbox = step.get("sandbox") if isinstance(step.get("sandbox"), dict) else None
        output_dir = ".verification" if sandbox else verification_rel
        command = [str(part).replace("{verification_dir}", output_dir) for part in step.get("command") or []]
        if not name or not command:
            continue
        state["verification_step_claims"][name] = list(step.get("verifies") or [])
        state["verification_step_timeouts"][name] = max(5, min(int(step.get("timeout_sec", 180) or 180), 300))
        state["verification_step_success_exit_codes"][name] = [
            int(code) for code in step.get("success_exit_codes") or [0]
        ]
        if step.get("stdin") is not None:
            state["verification_step_stdin"][name] = str(step.get("stdin"))[:50000]
        if sandbox:
            state["verification_step_sandboxes"][name] = dict(sandbox)
        adapted_command, adapter = adapt_console_command(workspace, command)
        if adapter:
            state["verification_command_adapters"][name] = adapter
        planned_cmds.append((name, adapted_command))
    py_files = _find_py_files(workspace, state)
    refresh_verification_test_registry(state, existing_only=True)
    registry_tests = registered_test_paths(state, existing_only=True)
    all_test_files = _find_test_files(workspace, state)
    agent_tests = [p for p in registry_tests if is_agent_test_path(p, state=state)]
    if not agent_tests and internal_generated_tests_enabled(state):
        agent_tests = _agent_test_files(workspace, state)
    source_modify_verification = _is_source_modify_verification(state)
    if agent_tests:
        test_files = agent_tests
    elif source_modify_verification:
        test_files = all_test_files
    else:
        test_files = registry_tests
    if py_files:
        cmds.append(("py_compile", ["python", "-m", "compileall", "-q", "."]))
        state["verification_infrastructure_step_names"].append("py_compile")
    if test_files:
        cmds.append(("pytest", ["python", "-m", "pytest", "-q"]))
        state["verification_infrastructure_step_names"].append("pytest")
    elif _is_verify_only_mode(state):
        cmds.append(("pytest", ["python", "-m", "pytest", "-q"]))
        state["verification_infrastructure_step_names"].append("pytest")
    cmds.extend(planned_cmds)
    return _dedupe(cmds, state=state)


def _prepare_verification_sandbox(state: dict, name: str, spec: dict) -> Path:
    workspace = Path(state["workspace"]).resolve()
    verification_root = Path(state["verification_artifacts_dir"]).resolve()
    sandbox_root = (verification_root / "sandboxes" / name).resolve()
    sandbox_root.relative_to(verification_root)
    if sandbox_root.exists():
        shutil.rmtree(sandbox_root)
    sandbox_root.mkdir(parents=True, exist_ok=True)
    (sandbox_root / ".verification").mkdir(parents=True, exist_ok=True)

    copied_files = 0
    copied_bytes = 0
    max_files = 500
    max_bytes = 64 * 1024 * 1024
    for pattern in spec.get("copy_paths") or []:
        for source in sorted(workspace.glob(str(pattern))):
            candidates = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.is_file())
            for file_path in candidates:
                rel = file_path.resolve().relative_to(workspace)
                size = file_path.stat().st_size
                copied_files += 1
                copied_bytes += size
                if copied_files > max_files or copied_bytes > max_bytes:
                    raise ValueError("verification sandbox copy budget exceeded")
                target = (sandbox_root / rel).resolve()
                target.relative_to(sandbox_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file_path, target)

    for fixture in spec.get("files") or []:
        if not isinstance(fixture, dict):
            continue
        target = (sandbox_root / str(fixture.get("path") or "")).resolve()
        target.relative_to(sandbox_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(fixture.get("content") or ""), encoding="utf-8")

    for rel in spec.get("omit_paths") or []:
        target = (sandbox_root / str(rel)).resolve()
        target.relative_to(sandbox_root)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    state.setdefault("verification_step_workspaces", {})[name] = str(sandbox_root)
    return sandbox_root


def _verification_command_result(
    state: dict,
    name: str,
    cmd: list[str],
    data: dict,
    *,
    fallback_message: str = "",
) -> CommandResult:
    actual = int(data.get("returncode", 1))
    success_codes = [
        int(code)
        for code in (state.get("verification_step_success_exit_codes") or {}).get(name, [0])
    ]
    expectation_met = actual in success_codes and not bool(data.get("timed_out", False))
    return CommandResult(
        name=name,
        command=data.get("command", cmd),
        returncode=0 if expectation_met else actual,
        actual_returncode=actual,
        success_exit_codes=success_codes,
        stdout=data.get("stdout", ""),
        stderr=data.get("stderr", "") or ("" if expectation_met else fallback_message),
        timed_out=bool(data.get("timed_out", False)),
        executed=bool(data.get("executed", True)),
        failure_kind="" if expectation_met else str(data.get("failure_kind") or ""),
    )


def _verification_extra_env(
    state: dict,
    name: str,
    *,
    include_project_workspace: bool = False,
) -> dict[str, str] | None:
    """Return execution environment required by runtime-owned adapters only."""
    adapter = (state.get("verification_command_adapters") or {}).get(name)
    if not adapter:
        if not include_project_workspace:
            return None
        existing = os.environ.get("PYTHONPATH", "")
        value = str(Path(state["workspace"]).resolve())
        if existing:
            value = os.pathsep.join((value, existing))
        return {"PYTHONPATH": value}

    paths: list[str] = []
    if include_project_workspace:
        paths.append(str(Path(state["workspace"]).resolve()))
    paths.append(str(agent_repo_root().resolve()))
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        paths.append(existing)
    return {"PYTHONPATH": os.pathsep.join(paths)}


def _pytest_pythonpath_for_targets(state: dict, targets: list[str]) -> list[str]:
    if any(is_agent_test_path(t.split("::", 1)[0], state=state) for t in targets):
        return ["."]
    return []


def _pytest_targets_for_command(state: dict, name: str, cmd: list[str]) -> list[str]:
    targets = _pytest_targets(cmd)
    if targets:
        return targets
    if name == "pytest":
        if _is_source_modify_verification(state):
            return []
        registry_tests = registered_test_paths(state, existing_only=True)
        if registry_tests:
            return registry_tests
    return []


def _run_pytest_step(state: dict, name: str, cmd: list[str]) -> tuple[CommandResult, dict]:
    targets = _pytest_targets_for_command(state, name, cmd)
    pythonpath = _pytest_pythonpath_for_targets(state, targets)
    report_dir = str(Path(state["run_dir"]) / "test_reports") if state.get("run_dir") else None
    res = run_tests(
        state["workspace"],
        targets=targets,
        kind="pytest",
        timeout_sec=180,
        pythonpath=pythonpath,
        report_dir=report_dir,
    )
    data = res.data or {}
    run = {
        "name": name,
        **data,
    }
    result = CommandResult(
        name=name,
        command=data.get("command", cmd),
        returncode=int(data.get("returncode", 1)),
        stdout=data.get("stdout", ""),
        stderr=data.get("stderr", ""),
        timed_out=bool(data.get("timed_out", False)),
    )
    return result, run


def _aggregate_test_runs(runs: list[dict]) -> dict:
    totals = {
        "total": sum(int(run.get("total", 0) or 0) for run in runs),
        "passed": sum(int(run.get("passed", 0) or 0) for run in runs),
        "failed": sum(int(run.get("failed", 0) or 0) for run in runs),
        "errors": sum(int(run.get("errors", 0) or 0) for run in runs),
        "skipped": sum(int(run.get("skipped", 0) or 0) for run in runs),
    }
    return {
        "version": "run_tests_v1",
        "ok": all(bool(run.get("ok")) for run in runs) if runs else None,
        "runs": runs,
        "total": totals["total"],
        "passed": totals["passed"],
        "failed": totals["failed"],
        "errors": totals["errors"],
        "skipped": totals["skipped"],
        "failures": [failure for run in runs for failure in (run.get("failures") or [])],
        "issues": [issue for run in runs for issue in (run.get("issues") or [])],
    }


def verify_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("verify_start", mode=state.get("mode"), read_only=state.get("read_only"), needs_verification=state.get("needs_verification"), task_contract=state.get("task_contract"))
    if state.get("read_only") and state.get("mode") == "analyze":
        report = ((state.get("last_tool_result") or {}).get("data") or {}).get("report") or state.get("analysis_report") or ""
        quality = state.get("analysis_quality") or {}
        ok = bool(quality.get("ok", len(report) >= 1000))
        ver = VerificationResult(ok=ok, analysis_ok=ok, quality_warnings=quality.get("warnings", [])).model_dump()
        state["verification"] = ver
        trace.event(
            "verification_result",
            verification=state["verification"],
            ok=state["verification"].get("ok"),
            requirement_atom_check=state.get("requirement_atom_check"),
            requirement_atom_summary=state.get("requirement_atom_summary"),
            requirement_atom_status=requirement_atom_trace_status(state),
            contract_check=state.get("contract_check"),
        )
        trace.event("verify_done", verification=state["verification"])
        trace.snapshot(state)
        return state

    state["verification_plan_update"] = supplement_verification_steps(state)
    results = []
    test_runs = []
    commands = _default_commands(state)
    for name, cmd in commands:
        input_text = (state.get("verification_step_stdin") or {}).get(name)
        sandbox_spec = (state.get("verification_step_sandboxes") or {}).get(name)
        if sandbox_spec:
            try:
                execution_workspace = _prepare_verification_sandbox(state, name, sandbox_spec)
                timeout = int((state.get("verification_step_timeouts") or {}).get(name, 180) or 180)
                res = run_shell(
                    str(execution_workspace),
                    cmd,
                    timeout_sec=timeout,
                    input_text=input_text,
                    extra_env=_verification_extra_env(
                        state,
                        name,
                        include_project_workspace=True,
                    ),
                )
                data = res.data or {}
                results.append(_verification_command_result(
                    state, name, cmd, data, fallback_message=res.message,
                ))
            except Exception as exc:
                results.append(CommandResult(
                    name=name,
                    command=cmd,
                    returncode=1,
                    stderr=f"verification sandbox setup failed: {exc}",
                    executed=False,
                    failure_kind="sandbox_setup",
                ))
        elif name == "pytest" or _is_pytest_command(cmd):
            result, run = _run_pytest_step(state, name, cmd)
            results.append(result)
            test_runs.append(run)
        else:
            timeout = int((state.get("verification_step_timeouts") or {}).get(name, 180) or 180)
            res = run_shell(
                state["workspace"],
                cmd,
                timeout_sec=timeout,
                input_text=input_text,
                extra_env=_verification_extra_env(state, name),
            )
            data = res.data or {}
            results.append(_verification_command_result(
                state, name, cmd, data, fallback_message=res.message,
            ))
    state["test_results"] = _aggregate_test_runs(test_runs)
    baseline_comparison = compare_with_test_baseline(
        state.get("test_baseline"),
        state.get("test_results"),
    )
    state["test_baseline_comparison"] = baseline_comparison
    if baseline_comparison.get("accepted_preexisting_failures"):
        state["test_results"]["raw_ok"] = state["test_results"].get("ok")
        state["test_results"]["ok"] = True
        state["test_results"]["accepted_preexisting_failures"] = True
        for result in results:
            if result.name != "pytest" or result.returncode == 0:
                continue
            result.actual_returncode = result.returncode
            result.returncode = 0
            result.failure_kind = "preexisting_test_failures"
    executed_steps = []
    executed_requirement_ids: set[str] = set()
    for (name, cmd), result in zip(commands, results):
        verifies = [
            str(atom_id)
            for atom_id in (state.get("verification_step_claims") or {}).get(name) or []
            if atom_id
        ]
        executed_requirement_ids.update(verifies)
        executed_steps.append({
            "name": name,
            "command": list(cmd),
            "verifies": verifies,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "executed": result.executed,
            "failure_kind": result.failure_kind,
            "execution_workspace": (state.get("verification_step_workspaces") or {}).get(name, state["workspace"]),
            "sandboxed": name in (state.get("verification_step_sandboxes") or {}),
            "command_adapter": (state.get("verification_command_adapters") or {}).get(name),
        })
    state["executed_verification_steps"] = executed_steps
    planned_steps = [
        step
        for step in (state.get("file_plan") or {}).get("verify_steps") or []
        if isinstance(step, dict) and step.get("name")
    ]
    executed_names = {item["name"] for item in executed_steps}
    plan_update = dict(state.get("verification_plan_update") or {})
    plan_update.update({
        "executed_steps": [item["name"] for item in executed_steps],
        "unexecuted_planned_steps": [
            str(step.get("name"))
            for step in planned_steps
            if str(step.get("name")) not in executed_names
        ],
        "executed_requirement_ids": sorted(executed_requirement_ids),
    })
    state["verification_plan_update"] = plan_update
    # Only runtime-owned compile/test checks are hard gates. Task behavior is
    # judged through requirement-bound execution evidence below.
    infrastructure_names = set(state.get("verification_infrastructure_step_names") or [])
    infrastructure_results = [r for r in results if r.name in infrastructure_names]
    commands_ok = (
        all(r.returncode == 0 and not r.timed_out for r in infrastructure_results)
        if infrastructure_results
        else True
    )
    warnings = []
    if baseline_comparison.get("accepted_preexisting_failures"):
        warnings.append(
            "pre-existing test failures remain unchanged; no new failures and no reduction in collected tests"
        )
    if not results:
        warnings.append("no verification commands were available; cannot claim success")

    result_dicts = [r.model_dump() for r in results]
    artifacts = collect_verification_artifacts(state)
    state["verification_artifacts"] = artifacts
    state["verification_claims"] = review_behavior_evidence(state, result_dicts, artifacts)
    oracle_steps = (state.get("verification_oracle_review") or {}).get("steps") or {}
    for result in result_dicts:
        review = oracle_steps.get(str(result.get("name") or ""))
        if review:
            result["oracle_status"] = review.get("status")
            result["oracle_reason"] = review.get("reason")
    contract_check = contract_quality_check(state["workspace"], state.get("task_contract") or {}, verification={"results": result_dicts}, state=state)
    semantic_check = run_semantic_contract_checks(state["workspace"], state.get("task_contract") or {}, task=state.get("task", ""), state=state)
    interface_check = run_interface_consistency_check(state["workspace"], state)
    state["interface_check"] = interface_check
    state["semantic_contract_check"] = semantic_check
    state["sample_data_review"] = semantic_check.get("sample_data_review")
    requirement_atom_check = semantic_check.get("requirement_atom_check") or {}
    state["requirement_atom_check"] = requirement_atom_check
    state["requirement_atoms"] = requirement_atom_check.get("atoms", state.get("requirement_atoms") or (state.get("task_contract") or {}).get("requirement_atoms", []))
    state["requirement_atom_summary"] = requirement_atom_check.get("summary", state.get("requirement_atom_summary") or (state.get("task_contract") or {}).get("requirement_atom_summary", {}))
    state["semantic_checks"] = semantic_check.get("semantic_checks", [])

    def _internal_interface_issue(issue: dict[str, Any]) -> bool:
        test_file = str(issue.get("test_file") or issue.get("file") or "").replace("\\", "/")
        return test_file.startswith(".coding_agent_test/") or bool(issue.get("internal_test"))

    semantic_ok = bool(semantic_check.get("ok"))
    interface_issues = list(interface_check.get("issues", []) or [])
    public_interface_failures = [
        x.get("message", str(x))
        for x in interface_issues
        if not (semantic_ok and _internal_interface_issue(x))
    ]
    internal_interface_warnings = [
        "generated test interface issue: " + str(x.get("message", x))
        for x in interface_issues
        if semantic_ok and _internal_interface_issue(x)
    ]
    merged_contract_check = dict(contract_check)
    merged_failures = list(contract_check.get("failures", [])) + list(semantic_check.get("failures", [])) + public_interface_failures
    merged_warnings = list(contract_check.get("warnings", [])) + list(semantic_check.get("warnings", [])) + internal_interface_warnings
    merged_contract_check["failures"] = merged_failures
    merged_contract_check["warnings"] = merged_warnings
    merged_contract_check["semantic_contract_check"] = semantic_check
    merged_contract_check["interface_check"] = interface_check
    interface_public_ok = not public_interface_failures
    merged_contract_check["ok"] = bool(contract_check.get("ok") and semantic_check.get("ok") and interface_public_ok)

    state["contract_check"] = merged_contract_check
    state["contract_ok"] = bool(merged_contract_check.get("ok"))
    warnings.extend(merged_contract_check.get("warnings", []))
    if merged_contract_check.get("failures"):
        warnings.extend(["contract failure: " + f for f in merged_contract_check.get("failures", [])])

    ok = bool(commands_ok and results and merged_contract_check.get("ok"))
    ver = VerificationResult(
        ok=ok,
        results=results,
        test_results=state.get("test_results") or {},
        compile_ok=next((r.returncode == 0 for r in results if r.name == "py_compile"), None),
        pytest_ok=bool((state.get("test_results") or {}).get("ok")) if test_runs else next((r.returncode == 0 for r in results if r.name == "pytest"), None),
        quality_warnings=warnings,
    )
    state["verification"] = ver.model_dump()
    state["needs_verification"] = not ok
    _update_verification_progress_guard(state, result_dicts, merged_contract_check)
    if ok:
        # A successful verification resolves the failure that triggered repair.
        # Keeping stale failure fields makes the final gate report a false
        # active failure even when contract and runtime checks now pass.
        # LangGraph merges node output into the prior state. Removing a key
        # from the local dict does not emit a deletion, so stale failures can
        # survive a successful repair. Explicit neutral values are required.
        state["failure"] = None
        state["failure_issues"] = []
        state["failure_owner"] = None
        state["strategy_decision"] = None
        state["repair_controller"] = None
        state["force_repair_action"] = None
    trace.event(
        "verification_result",
        verification=state["verification"],
        ok=state["verification"].get("ok"),
        test_results=state.get("test_results"),
        contract_check=state.get("contract_check"),
        semantic_contract_check=state.get("semantic_contract_check"),
        requirement_atom_check=state.get("requirement_atom_check"),
        requirement_atom_summary=state.get("requirement_atom_summary"),
        requirement_atom_status=requirement_atom_trace_status(state),
        interface_check=state.get("interface_check"),
        verification_claims=state.get("verification_claims"),
        verification_artifacts=state.get("verification_artifacts"),
        verification_plan_update=state.get("verification_plan_update"),
    )
    trace.event(
        "verify_done",
        verification=state["verification"],
        test_results=state.get("test_results"),
        contract_check=state.get("contract_check"),
        semantic_contract_check=state.get("semantic_contract_check"),
        requirement_atom_summary=state.get("requirement_atom_summary"),
        interface_check=state.get("interface_check"),
        verification_claims=state.get("verification_claims"),
        verification_artifacts=state.get("verification_artifacts"),
    )
    trace.snapshot(state)
    return state
