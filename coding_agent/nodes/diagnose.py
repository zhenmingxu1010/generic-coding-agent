from __future__ import annotations

import hashlib
import re

from coding_agent.core.schemas import FailureInfo
from coding_agent.core.utils import normalize_volatile_text
from coding_agent.repair.failure_analysis import decompose_failure_issues, summarize_issue_owners
from coding_agent.repair.import_error_context import build_import_error_context
from coding_agent.workspace.interface_check import run_interface_consistency_check
from coding_agent.repair.repair_controller import build_repair_controller
from coding_agent.repair.traceback_parser import parse_traceback_issues
from .common import get_trace


def _all_output(verification: dict) -> str:
    results = [
        result
        for result in verification.get("results", [])
        if isinstance(result, dict)
    ]
    failing_results = [
        result
        for result in results
        if int(result.get("returncode", 1) or 0) != 0 or result.get("timed_out")
    ]
    # Verification normalizes accepted baseline failures to returncode=0.
    # Their raw pytest output is useful in reports but must not become the
    # active repair diagnosis when a different task-specific command failed.
    selected_results = failing_results or results
    chunks = []
    for r in selected_results:
        chunks.append(f"===== {r.get('name')} =====")
        chunks.append("COMMAND: " + " ".join(r.get("command", [])))
        chunks.append("RETURNCODE: " + str(r.get("returncode")))
        if r.get("stdout"):
            chunks.append("STDOUT:\n" + r.get("stdout", ""))
        if r.get("stderr"):
            chunks.append("STDERR:\n" + r.get("stderr", ""))
    return "\n".join(chunks)


def _sig(text: str) -> str:
    stable = normalize_volatile_text(text)
    return hashlib.sha256(stable.encode("utf-8", errors="replace")).hexdigest()[:16]


def _first_file(text: str) -> str | None:
    m = re.search(r'File "(?:\./)?([^"\n]+\.py)"', text)
    if m:
        return m.group(1).strip("'\"")
    m = re.search(r'([^\s:\n]+\.py)(?::\d+)?', text)
    return m.group(1).strip("'\"") if m else None


def diagnose_node(state: dict) -> dict:
    trace = get_trace(state)
    trace.event("diagnose_start")
    text = _all_output(state.get("verification", {}))
    contract = state.get("contract_check") or {}
    if not state.get("interface_check"):
        try:
            state["interface_check"] = run_interface_consistency_check(state["workspace"], state)
        except Exception as e:
            state["interface_check"] = {"ok": False, "issues": [], "error": str(e), "source": "diagnose_node"}
    try:
        state["traceback_issues"] = parse_traceback_issues(text)
    except Exception as e:
        state["traceback_issues"] = [{"type": "traceback_parse_error", "message": str(e)}]
    failure_type = "verification_failed"
    priority = 10
    target_file = None
    message = "Verification failed"

    # Tool-level syntax failures have highest priority.
    active = state.get("failure") or {}
    if active.get("source") == "syntax_aware_file_tool":
        failure_type = "syntax_level_error"
        priority = 1
        target_file = active.get("target_file")
        message = active.get("message", "Python syntax check failed")
    else:
        # Python syntax-family errors. IndentationError/TabError are subclasses but appear as their own names.
        m = re.search(r'File "(?:\./)?([^"\n]+\.py)", line \d+.*?\n\s*((?:SyntaxError|IndentationError|TabError|TokenError):[^\n]+)', text, flags=re.S)
        if m:
            failure_type = "syntax_level_error"
            priority = 1
            target_file = m.group(1)
            message = m.group(2)
        else:
            impl_match = None
            for line in text.splitlines():
                if any(x in line for x in ["NameError:", "AttributeError:"]) and ".py" in line:
                    m_line = re.search(r'([^\s:\n]+\.py)(?::\d+)?:.*?((?:NameError|AttributeError):[^\n]+)', line)
                    if m_line and not m_line.group(1).startswith("tests/"):
                        impl_match = (m_line.group(1), m_line.group(2))
                        break
            if impl_match:
                failure_type = "name_error_impl" if impl_match[1].startswith("NameError:") else "attribute_error_impl"
                priority = 1
                target_file = impl_match[0]
                message = impl_match[1]
            elif "ModuleNotFoundError" in text or "ImportError" in text:
                failure_type = "import_level_error"
                priority = 1
                target_file = _first_file(text)
                message = "ImportError/ModuleNotFoundError"
            elif any(x in text for x in ["TypeError:", "ValueError:", "FileNotFoundError:", "KeyError:"]):
                failure_type = "runtime_error"
                priority = 3
                target_file = _first_file(text)
                m2 = re.search(r'((?:TypeError|ValueError|FileNotFoundError|KeyError):[^\n]+)', text)
                message = m2.group(1) if m2 else "runtime error"
            elif any(x in text.lower() for x in ["no tests ran", "collected 0 items", "ran 0 tests"]):
                failure_type = "pytest_zero_collected"
                priority = 4
                target_file = None
                message = "pytest collected zero tests"
            elif "AssertionError" in text or "FAILED" in text:
                failure_type = "test_assertion_error"
                priority = 5
                target_file = _first_file(text)
                message = "pytest assertion failed"
            elif contract.get("failures"):
                failure_type = "contract_error"
                priority = 6
                message = "; ".join(contract.get("failures", [])[:3])

    raw_excerpt = (text + "\n\nCONTRACT_CHECK:\n" + str(contract))[:12000]
    key = f"{failure_type}|{target_file}|{message}|{raw_excerpt[:1000]}"
    failure = FailureInfo(
        failure_type=failure_type,
        priority=priority,
        message=message,
        target_file=target_file,
        signature=_sig(key),
        raw_excerpt=raw_excerpt,
    )
    state["failure"] = failure.model_dump()
    issues = decompose_failure_issues(state)
    state["failure_issues"] = issues
    try:
        state["import_error_context"] = build_import_error_context(state)
    except Exception as e:
        state["import_error_context"] = {"present": False, "error": str(e), "source": "diagnose_node"}
    state["repair_controller"] = build_repair_controller(state)
    owner_summary = summarize_issue_owners(issues)
    if issues and owner_summary != "unknown":
        state["failure_issue_owner_summary"] = owner_summary
    state.setdefault("failure_history", []).append(state["failure"])
    trace.event(
        "diagnose_done",
        failure=state["failure"],
        failure_issues=issues,
        import_error_context=state.get("import_error_context"),
        failure_issue_owner_summary=state.get("failure_issue_owner_summary"),
    )
    trace.snapshot(state)
    return state
