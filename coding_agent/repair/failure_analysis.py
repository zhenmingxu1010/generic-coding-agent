from __future__ import annotations

import re
from typing import Any

from coding_agent.repair.traceback_parser import parse_traceback_issues
from coding_agent.repair.repair_controller import enrich_failure_issues_for_repair
from coding_agent.workspace.run_paths import is_test_like_path


def _is_test_path(path: str | None) -> bool:
    return is_test_like_path(path)


def _verification_text(state: dict[str, Any], limit: int = 20000) -> str:
    parts: list[str] = []
    rejected = set(
        (state.get("verification_oracle_review") or {}).get("rejected_step_names") or []
    )
    for r in (state.get("verification") or {}).get("results", []) or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("name") or "") in rejected:
            continue
        if int(r.get("returncode", 1) or 0) == 0 and not r.get("timed_out"):
            continue
        parts.append(f"===== {r.get('name')} =====")
        parts.append("COMMAND: " + " ".join(r.get("command", [])))
        parts.append("RETURNCODE: " + str(r.get("returncode")))
        if r.get("stdout"):
            parts.append("STDOUT:\n" + r.get("stdout", ""))
        if r.get("stderr"):
            parts.append("STDERR:\n" + r.get("stderr", ""))
    text = "\n".join(parts)
    return text[:limit]


def _structured_test_runs(state: dict[str, Any]) -> list[dict[str, Any]]:
    test_results = state.get("test_results") or (state.get("verification") or {}).get("test_results") or {}
    if test_results.get("accepted_preexisting_failures"):
        return []
    return list(test_results.get("runs") or [])


def _first_generated_code_file(state: dict[str, Any]) -> str | None:
    for item in (state.get("file_plan") or {}).get("files") or []:
        if item.get("kind") == "code" and item.get("path"):
            return str(item.get("path")).replace("\\", "/")
    for item in state.get("generated_files") or []:
        if item.get("kind") == "code" and item.get("path"):
            return str(item.get("path")).replace("\\", "/")
    return None


def _generated_test_paths_from_issues(issues: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for issue in issues:
        if "generated_test" not in str(issue.get("owner") or ""):
            continue
        for key in ("file", "target_file"):
            path = str(issue.get(key) or "").replace("\\", "/")
            if path:
                out.add(path)
    return out


def _failure_owner_from_collection_text(failure: dict[str, Any], generated_test_paths: set[str]) -> str | None:
    text = str(failure.get("text") or failure.get("message") or "")
    file_path = str(failure.get("file") or "").replace("\\", "/")
    normalized_text = text.replace("\\", "/")
    if file_path and _is_test_path(file_path):
        return "generated_test"
    if ".coding_agent_test/" in normalized_text or ".coding_agent_test." in normalized_text:
        return "generated_test"
    if str(failure.get("message") or "").lower() == "collection failure" or "importing test module" in text:
        for path in generated_test_paths:
            if path and path in text:
                return "generated_test"
        if "ModuleNotFoundError" in text and any(_is_test_path(path) for path in generated_test_paths):
            return "generated_test"
    return None


def _requirement_atom_checks(state: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for check in [
        state.get("requirement_atom_check"),
        (state.get("semantic_contract_check") or {}).get("requirement_atom_check"),
        ((state.get("contract_check") or {}).get("semantic_contract_check") or {}).get("requirement_atom_check"),
    ]:
        if isinstance(check, dict) and isinstance(check.get("atoms"), list):
            checks.append(check)
    return checks


def _semantic_atom_repair_hint(atom: dict[str, Any]) -> str:
    details = atom.get("details") or {}
    claim = details.get("verification_claim") or {}
    evidence_reason = str(claim.get("reason") or "").strip()
    verify_hint = str(atom.get("verify_hint") or "").strip()
    guidance = evidence_reason or verify_hint or "use the cited execution evidence"
    return f"satisfy the stated requirement and rerun its observable verification; {guidance}"


def _failed_requirement_atom_issues(state: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check in _requirement_atom_checks(state):
        for atom in check.get("atoms") or []:
            if not isinstance(atom, dict):
                continue
            if str(atom.get("status") or "") != "failed":
                continue
            atom_id = str(atom.get("id") or "")
            if not atom_id or atom_id in seen:
                continue
            seen.add(atom_id)
            issues.append({
                "owner": "requirement",
                "type": "semantic_requirement_atom_failed",
                "atom_id": atom_id,
                "atom_type": atom.get("type"),
                "file": None,
                "target_file": None,
                "target_files": [],
                "message": f"{atom_id}: {atom.get('description', '')}",
                "details": atom.get("details") or {},
                "repair_hint": _semantic_atom_repair_hint(atom),
                "source": "requirement_atom_check",
            })
    return issues


def decompose_failure_issues(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Split a verification failure into actionable generic issues."""
    issues: list[dict[str, Any]] = []

    # Static interface issues found before/alongside pytest.
    iface = state.get("interface_check") or {}
    for issue in iface.get("issues", []) or []:
        issues.append({
            "owner": "generated_test" if _is_test_path(issue.get("test_file")) else "unknown",
            "type": "missing_imported_symbol",
            "file": issue.get("test_file"),
            "target_file": issue.get("target_file"),
            "message": issue.get("message"),
            "repair_hint": "make an agent-owned test exercise the real public API, or fix the implementation when the imported API is required",
            "source": "interface_check",
        })

    text = _verification_text(state)

    for item in parse_traceback_issues(text):
        issue = dict(item)
        if issue.get("owner") == "generated_test_or_external_test":
            issue["owner"] = "generated_test"
        issues.append(issue)

    rejected_steps = set(
        (state.get("verification_oracle_review") or {}).get("rejected_step_names") or []
    )
    for result in (state.get("verification") or {}).get("results", []) or []:
        if not isinstance(result, dict):
            continue
        name = str(result.get("name") or "command")
        if name in rejected_steps:
            continue
        if name in {"py_compile", "pytest", "run_tests"}:
            continue
        returncode = int(result.get("returncode", 0) or 0)
        if returncode == 0 and not result.get("timed_out"):
            continue
        issues.append({
            "owner": "implementation",
            "type": "verification_command_failed",
            "file": _first_generated_code_file(state),
            "target_file": _first_generated_code_file(state),
            "command_name": name,
            "command": result.get("command") or [],
            "returncode": returncode,
            "timed_out": bool(result.get("timed_out")),
            "stdout": str(result.get("stdout") or "")[:1200],
            "stderr": str(result.get("stderr") or "")[:1200],
            "message": f"verification command {name} failed with return code {returncode}",
            "repair_hint": "inspect the command, working directory, input paths, stdout/stderr, and implementation behavior before changing code or tests",
            "source": "verification_result",
        })

    for run in _structured_test_runs(state):
        for issue in run.get("issues") or []:
            item = dict(issue)
            if item.get("owner") == "generated_test_or_external_test":
                item["owner"] = "generated_test"
            item.setdefault("source", "run_tests")
            issues.append(item)
        generated_test_paths = _generated_test_paths_from_issues(issues)
        for failure in run.get("failures") or []:
            owner = "generated_test" if failure.get("owner") == "generated_test_or_external_test" else failure.get("owner", "unknown")
            owner = _failure_owner_from_collection_text(failure, generated_test_paths) or owner
            item = {
                "owner": owner,
                "type": str(failure.get("type") or failure.get("status") or "test_failure").split(".", 1)[-1].lower(),
                "exception_type": failure.get("type"),
                "file": failure.get("file"),
                "line": failure.get("line"),
                "message": failure.get("message"),
                "test": failure.get("test"),
                "repair_hint": "inspect test oracle and implementation API" if owner == "generated_test" else "fix implementation runtime behavior",
                "source": "run_tests_failure",
            }
            issues.append(item)

    issues.extend(_failed_requirement_atom_issues(state))

    # ImportError: cannot import name 'X' from 'Y'
    for m in re.finditer(r"ImportError:\s+cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]", text):
        issues.append({
            "owner": "generated_test_or_interface",
            "type": "import_error_missing_symbol",
            "symbol": m.group(1),
            "module": m.group(2),
            "message": m.group(0),
            "repair_hint": "align generated tests and generated implementation API",
            "source": "pytest_import_error",
        })

    # Runtime errors with traceback file targets.
    runtime_rx = re.compile(r'File "(?:\./)?([^"\n]+\.py)", line \d+.*?\n\s*((?:ValueError|TypeError|KeyError|NameError|AttributeError|FileNotFoundError):[^\n]+)', re.S)
    for m in runtime_rx.finditer(text):
        path, msg = m.group(1).replace("\\", "/"), m.group(2).strip()
        owner = "generated_test" if _is_test_path(path) else "implementation"
        issues.append({
            "owner": owner,
            "type": msg.split(":", 1)[0].lower(),
            "file": path,
            "message": msg,
            "repair_hint": "fix implementation runtime behavior" if owner == "implementation" else "fix generated test oracle/API usage",
            "source": "traceback",
        })

    # Contract failures should be explicit issues too.
    for failure in ((state.get("contract_check") or {}).get("failures") or []):
        issues.append({
            "owner": "implementation",
            "type": "contract_failure",
            "message": str(failure),
            "repair_hint": "make implementation satisfy task contract and rerun verification",
            "source": "contract_check",
        })

    # Dedupe by type/file/message.
    issues = enrich_failure_issues_for_repair(state, issues)
    deduped: list[dict[str, Any]] = []
    seen = set()
    for item in issues:
        key = (item.get("type"), item.get("file"), item.get("target_file"), item.get("message"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _is_unlocated_traceback_duplicate(issue: dict[str, Any]) -> bool:
    owner = str(issue.get("owner") or "")
    source = str(issue.get("source") or "")
    return (
        owner in {"", "unknown"}
        and not issue.get("file")
        and not issue.get("target_file")
        and "traceback_parser" in source
    )


def _canonical_issue_owner(owner: str) -> str:
    if owner in {"generated_test_or_interface", "generated_test_or_external_test"}:
        return "generated_test"
    return owner


def summarize_issue_owners(issues: list[dict[str, Any]]) -> str:
    owners = {
        _canonical_issue_owner(str(x.get("owner")))
        for x in issues
        if x.get("owner") and not _is_unlocated_traceback_duplicate(x)
    }
    if not owners:
        return "unknown"
    if len(owners) == 1:
        return next(iter(owners))
    if "implementation" in owners and any("test" in o for o in owners):
        return "implementation_and_generated_test"
    return "+".join(sorted(owners))
