from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any


DEFAULT_AUDIT_DIR = Path(".agent_runs/regression-audits")
DEFAULT_BUNDLE_NAME = "full_regression_audit_bundle.zip"
EXPECTED_CASE_COUNT = 11


def _load_final(audit_zip: Path) -> tuple[dict[str, Any], set[str]]:
    with zipfile.ZipFile(audit_zip) as z:
        names = z.namelist()
        final_name = "final.json" if "final.json" in names else next(
            name for name in names if name.endswith("final.json")
        )
        return json.loads(z.read(final_name)), set(names)


def _expected_ok(audit_zip: Path) -> bool:
    return not audit_zip.name.startswith(("t05_", "t10_"))


def _get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _workspace_exists(names: set[str], rel: str) -> bool:
    wanted = f"workspace/{rel}".rstrip("/")
    return wanted in names or any(name.startswith(wanted + "/") for name in names)


def _result_named(final: dict[str, Any], fragment: str) -> dict[str, Any] | None:
    fragment = fragment.lower()
    return next(
        (
            result
            for result in _get(final, "verification.results", []) or []
            if isinstance(result, dict) and fragment in str(result.get("name") or "").lower()
        ),
        None,
    )


def _case_id(audit_zip: Path) -> str:
    match = re.match(r"(t\d{2})_", audit_zip.name.lower())
    return match.group(1).upper() if match else ""


def _validate_case(case_id: str, final: dict[str, Any], names: set[str]) -> list[str]:
    """Validate every machine-checkable capability promised by the matrix."""
    failures: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    source_changed = list(_get(final, "write_scope_audit.source_changed_files", []) or [])
    existing_modified = list(_get(final, "write_scope_audit.existing_project_modified_files", []) or [])
    test_total = int(_get(final, "verification.test_results.total", 0) or 0)
    required_failed = int(_get(final, "requirement_atom_summary.required_failed", 0) or 0)
    required_unverified = int(_get(final, "requirement_atom_summary.required_unverified", 0) or 0)

    if case_id == "T01":
        require(final.get("mode") == "analyze", "mode is not analyze")
        require(final.get("read_only") is True, "read_only is not true")
        require(final.get("analysis_quality_ok") is True, "analysis quality did not pass")
        require(source_changed == [], "read-only analysis changed source files")
    elif case_id == "T02":
        require(final.get("contract_ok") is True, "contract did not pass")
        require(existing_modified == [], "an existing project file was modified")
        require(source_changed == ["scripts/summarize_metrics.py"], "unexpected source changes")
        require(_workspace_exists(names, "scripts/summarize_metrics.py"), "script is missing from workspace audit")
        fallback = _result_named(final, "fallback")
        fallback_output = str((fallback or {}).get("stdout") or "")
        require(bool(fallback), "fallback scenario was not executed")
        require(all(token in fallback_output for token in ("worker", "800", "0.02", "240")), "fallback output lacks required non-placeholder values")
    elif case_id == "T03":
        require(final.get("contract_ok") is True, "contract did not pass")
        require(required_failed == 0 and required_unverified == 0, "required atoms are incomplete")
        require(test_total > 0, "pytest did not execute tests")
        require(_workspace_exists(names, "scripts/summarize_inventory.py"), "inventory script is missing")
    elif case_id == "T04":
        require(_get(final, "verification.ok") is True, "verification did not pass")
        require(test_total >= 2, "fewer than two project tests passed")
        require("timecalc.py" in existing_modified, "timecalc.py was not recorded as modified")
        require("tests/test_timecalc.py" not in existing_modified, "protected project test was modified")
        require(not (_get(final, "interface_check.missing_imported_symbols", []) or []), "imported symbols remain missing")
    elif case_id == "T05":
        require(final.get("ok") is False, "negative zero-test case unexpectedly passed")
        require(final.get("stopped_reason") == "pytest_zero_tests_collected", "zero-test stop reason is missing")
        require(test_total == 0, "negative case unexpectedly collected tests")
        require(source_changed == [], "negative verify-only case changed source")
    elif case_id == "T06":
        expected = {"scripts/inspect_event_schema.py", "docs/event_schema.md"}
        require(existing_modified == [], "an existing project file was modified")
        require(set(source_changed) == expected and len(source_changed) == 2, "source changes are not exactly the two declared deliverables")
        require(all(_workspace_exists(names, rel) for rel in expected), "one or more declared deliverables are missing")
    elif case_id == "T07":
        require(_get(final, "verification.ok") is True, "verification did not pass")
        require(test_total >= 2, "fewer than two project tests passed")
        require("calculator.py" in existing_modified, "calculator.py was not recorded as modified")
        require("tests/test_calculator.py" not in existing_modified, "protected project test was modified")
    elif case_id == "T08":
        require(final.get("mode") in {"write", "generate_project"}, "unexpected generation mode")
        require(_get(final, "verification.ok") is True, "verification did not pass")
        require(required_failed == 0 and required_unverified == 0, "required atoms are incomplete")
        require(test_total > 0, "generated project tests did not run")
        require(_workspace_exists(names, "README.md"), "README is missing")
        require(any(name.startswith("workspace/tests/test_") and name.endswith(".py") for name in names), "pytest file is missing")
    elif case_id == "T09":
        require(_get(final, "task_completeness.decision") == "proceed", "short prompt did not proceed with safe defaults")
        assumption_ids = {str(item.get("id") or "") for item in final.get("assumptions") or [] if isinstance(item, dict)}
        require("implementation_language" in assumption_ids, "implementation-language assumption is missing")
        require("representative_verification" in assumption_ids, "representative-verification assumption is missing")
        require(_get(final, "verification.ok") is True, "short-prompt verification did not pass")
        require(required_failed == 0 and required_unverified == 0, "short-prompt atoms are incomplete")
    elif case_id == "T10":
        require(final.get("outcome") == "clarification_required", "outcome is not clarification_required")
        require(final.get("controlled_failure") is True, "clarification is not a controlled stop")
        require(bool(final.get("clarification_questions")), "clarification question is missing")
        require(source_changed == [], "clarification case changed source before receiving an answer")
    elif case_id == "T11":
        require(final.get("mode") == "analyze", "mode is not analyze")
        require(final.get("read_only") is True, "read_only is not true")
        require(final.get("analysis_quality_ok") is True, "analysis quality did not pass")
        require(source_changed == [], "colloquial inspection changed source")
        require(int(_get(final, "project_memory.task_summaries_count", 0) or 0) >= 1, "project memory was not updated")
    else:
        failures.append("audit filename does not identify a known T01-T11 case")
    return failures


def collect(audit_dir: Path, out: Path, pattern: str) -> dict[str, Any]:
    audits = sorted(audit_dir.glob(pattern))
    rows: list[dict[str, Any]] = []
    for audit_zip in audits:
        try:
            final, names = _load_final(audit_zip)
            ok = final.get("ok")
            stopped_reason = final.get("stopped_reason")
            round_idx = final.get("round_idx")
            read_error = None
            condition_failures = _validate_case(_case_id(audit_zip), final, names)
        except Exception as exc:
            ok = None
            stopped_reason = None
            round_idx = None
            read_error = f"{type(exc).__name__}: {exc}"
            condition_failures = ["audit could not be validated"]
        expected = _expected_ok(audit_zip)
        rows.append(
            {
                "name": audit_zip.name,
                "ok": ok,
                "expected_ok": expected,
                "passed": ok is expected and read_error is None and not condition_failures,
                "stopped_reason": stopped_reason,
                "round_idx": round_idx,
                "read_error": read_error,
                "condition_failures": condition_failures,
            }
        )

    missing_count = max(0, EXPECTED_CASE_COUNT - len(audits))
    summary = {
        "ok": bool(rows) and missing_count == 0 and all(row["passed"] for row in rows),
        "audit_dir": str(audit_dir),
        "bundle": str(out),
        "pattern": pattern,
        "files": len(audits),
        "missing_count": missing_count,
        "rows": rows,
        "failures": [row for row in rows if not row["passed"]],
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("regression_summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        for audit_zip in audits:
            bundle.write(audit_zip, audit_zip.name)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bundle regression audit zips and include a machine-readable summary.")
    parser.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR), help="Directory containing t01-t11 audit zips.")
    parser.add_argument("--out", help="Output bundle path. Defaults to AUDIT_DIR/full_regression_audit_bundle.zip.")
    parser.add_argument("--pattern", default="t*_regression_*_audit.zip", help="Glob pattern for regression audit zips.")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when any regression result is unexpected.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    audit_dir = Path(args.audit_dir)
    out = Path(args.out) if args.out else audit_dir / DEFAULT_BUNDLE_NAME
    summary = collect(audit_dir, out, args.pattern)
    for row in summary["rows"]:
        status = "PASS" if row["passed"] else "FAIL"
        print(
            f"{status} {row['name']}: ok={row['ok']}, expected_ok={row['expected_ok']}, "
            f"stopped_reason={row['stopped_reason']}, round={row['round_idx']}"
        )
        for failure in row["condition_failures"]:
            print(f"  - {failure}")
    print({"bundle": summary["bundle"], "files": summary["files"], "ok": summary["ok"]})
    if args.strict and not summary["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
