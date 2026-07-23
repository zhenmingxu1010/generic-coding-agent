from __future__ import annotations

import os
from typing import Any

from coding_agent.core.utils import normalize_volatile_text
from coding_agent.tools.test_tools import run_tests


SOURCE_MODIFY_MODES = {"modify", "debug", "repair_existing"}
NON_COMPARABLE_FAILURE_TYPES = {
    "pytest_invocation_error",
    "pytest_target_not_found",
    "pytest_unavailable",
    "pytest_zero_collected",
}


def _repo_has_tests(repo_map: dict[str, Any]) -> bool:
    if repo_map.get("has_tests"):
        return True
    for rel in repo_map.get("files") or []:
        path = str(rel).replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        if "/tests/" in f"/{path}" or name.startswith("test_") or name.endswith("_test.py"):
            return True
    return False


def _failure_fingerprint(failure: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(failure.get("test") or ""),
        str(failure.get("type") or failure.get("exception_type") or ""),
        normalize_volatile_text(str(failure.get("message") or ""))[:500],
    )


def _compact_test_run(data: dict[str, Any]) -> dict[str, Any]:
    failures = [
        {
            "test": str(item.get("test") or ""),
            "type": str(item.get("type") or item.get("exception_type") or ""),
            "message": normalize_volatile_text(str(item.get("message") or ""))[:1000],
            "owner": str(item.get("owner") or ""),
        }
        for item in (data.get("failures") or [])[:1000]
        if isinstance(item, dict)
    ]
    non_comparable = any(
        item["owner"] in {"environment", "test_collection"}
        or item["type"] in NON_COMPARABLE_FAILURE_TYPES
        for item in failures
    )
    return {
        "version": "test_baseline_v1",
        "captured": True,
        "ok": bool(data.get("ok")),
        "returncode": data.get("returncode"),
        "timed_out": bool(data.get("timed_out")),
        "total": int(data.get("total", 0) or 0),
        "passed": int(data.get("passed", 0) or 0),
        "failed": int(data.get("failed", 0) or 0),
        "errors": int(data.get("errors", 0) or 0),
        "skipped": int(data.get("skipped", 0) or 0),
        "failures": failures,
        "comparable": bool(
            not data.get("timed_out")
            and int(data.get("total", 0) or 0) > 0
            and failures
            and not non_comparable
        ),
    }


def capture_test_baseline(state: dict[str, Any]) -> dict[str, Any]:
    """Capture existing test failures before a source-modification action."""
    existing = state.get("test_baseline")
    if isinstance(existing, dict):
        return existing
    if os.getenv("AGENT_CAPTURE_TEST_BASELINE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {"version": "test_baseline_v1", "captured": False, "reason": "disabled"}
    if state.get("read_only") or state.get("mode") not in SOURCE_MODIFY_MODES:
        return {"version": "test_baseline_v1", "captured": False, "reason": "mode_not_source_modify"}
    if not _repo_has_tests(state.get("repo_map") or {}):
        return {"version": "test_baseline_v1", "captured": False, "reason": "no_existing_tests"}
    timeout = max(10, min(int(os.getenv("AGENT_TEST_BASELINE_TIMEOUT", "90")), 300))
    result = run_tests(
        state["workspace"],
        targets=[],
        kind="pytest",
        timeout_sec=timeout,
        report_dir=state.get("run_dir"),
    )
    data = result.data or {}
    if not data:
        return {
            "version": "test_baseline_v1",
            "captured": True,
            "comparable": False,
            "reason": result.message,
        }
    return _compact_test_run(data)


def compare_with_test_baseline(
    baseline: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    """Accept only an unchanged/subset set of pre-existing test failures."""
    baseline = baseline or {}
    current = current or {}
    result = {
        "version": "test_baseline_comparison_v1",
        "accepted_preexisting_failures": False,
        "reason": "",
        "baseline_total": int(baseline.get("total", 0) or 0),
        "current_total": int(current.get("total", 0) or 0),
        "new_failures": [],
    }
    if not baseline.get("comparable"):
        result["reason"] = "baseline_not_comparable"
        return result
    if current.get("timed_out") or result["current_total"] < result["baseline_total"]:
        result["reason"] = "current_run_timed_out_or_collected_fewer_tests"
        return result
    baseline_keys = {
        _failure_fingerprint(item)
        for item in baseline.get("failures") or []
        if isinstance(item, dict)
    }
    current_items = [
        item for item in current.get("failures") or [] if isinstance(item, dict)
    ]
    current_keys = {_failure_fingerprint(item) for item in current_items}
    new_keys = sorted(current_keys - baseline_keys)
    if new_keys:
        result["reason"] = "new_test_failures"
        result["new_failures"] = [
            {"test": key[0], "type": key[1], "message": key[2]} for key in new_keys[:50]
        ]
        return result
    if not current_keys:
        result["reason"] = "current_tests_pass"
        return result
    result["accepted_preexisting_failures"] = True
    result["reason"] = "no_new_failures_and_test_collection_not_reduced"
    return result
