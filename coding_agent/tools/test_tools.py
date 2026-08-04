from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from coding_agent.core.schemas import ToolResult
from coding_agent.repair.traceback_parser import parse_traceback_issues
from coding_agent.core.utils import coerce_text, truncate
from coding_agent.tools.shell_tools import target_python_executable
from coding_agent.workspace.run_paths import agent_runs_root, is_test_like_path


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _owner_for_path(path: str | None) -> str:
    rel = str(path or "").replace("\\", "/")
    if is_test_like_path(rel):
        return "generated_test_or_external_test"
    if rel:
        return "implementation"
    return "unknown"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _strip_current_dir_prefix(path: str) -> str:
    rel = str(path).replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _normalise_target(workspace: str, target: str) -> str:
    root = Path(workspace).resolve()
    raw = str(target).replace("\\", "/")
    path_part, sep, node_part = raw.partition("::")
    path = Path(path_part)
    if path.is_absolute():
        resolved = path.resolve()
        if not _is_under(resolved, root):
            raise ValueError(f"pytest target is outside workspace: {target}")
        rel = resolved.relative_to(root).as_posix()
    else:
        resolved = (root / path_part).resolve()
        if not _is_under(resolved, root):
            raise ValueError(f"pytest target escapes workspace: {target}")
        rel = _strip_current_dir_prefix(Path(path_part).as_posix())
    return rel + (sep + node_part if sep else "")


def _normalise_pythonpath(workspace: str, pythonpath: list[str] | None) -> list[str]:
    root = Path(workspace).resolve()
    out: list[str] = []
    for item in pythonpath or []:
        raw = str(item)
        path = Path(raw)
        resolved = path.resolve() if path.is_absolute() else (root / raw).resolve()
        if not _is_under(resolved, root):
            raise ValueError(f"pythonpath is outside workspace: {item}")
        rel = resolved.relative_to(root).as_posix()
        if rel not in out:
            out.append(rel)
    return out


def _as_list(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _report_dir(workspace: str, report_dir: str | None) -> Path:
    root = Path(workspace).resolve()
    if report_dir:
        raw = Path(report_dir)
        path = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
        runs_root = agent_runs_root().resolve()
        if not (_is_under(path, root) or _is_under(path, runs_root)):
            raise ValueError(f"test report directory is outside workspace: {report_dir}")
    else:
        path = agent_runs_root() / "test_reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_pytest_command(targets: list[str], pythonpath: list[str], junit_path: Path, quiet: bool = True) -> list[str]:
    args = ["-q"] if quiet else []
    args.extend(targets)
    args.extend(["--junitxml", str(junit_path)])
    path_inserts = "".join(f"sys.path.insert(0, {p!r}); " for p in reversed(pythonpath))
    code = (
        "import sys; "
        f"{path_inserts}"
        "import pytest; "
        f"raise SystemExit(pytest.main({args!r}))"
    )
    return [target_python_executable(), "-c", code]


def _first_path_line(text: str) -> tuple[str | None, int | None]:
    text = text or ""
    m = re.search(r"(?m)^([^\s:\n]+\.py):(\d+):", text)
    if m:
        return m.group(1).replace("\\", "/"), int(m.group(2))
    m = re.search(r'File "(?:\./)?([^"\n]+\.py)", line (\d+)', text)
    if m:
        return m.group(1).replace("\\", "/"), int(m.group(2))
    return None, None


def _testcase_id(case: ET.Element) -> str:
    classname = case.attrib.get("classname") or ""
    name = case.attrib.get("name") or ""
    if classname and name:
        return f"{classname}::{name}"
    return name or classname or "<unknown>"


def _parse_junit_xml(path: Path, workspace: str = "") -> dict[str, Any]:
    if not path.exists():
        return {
            "ok": False,
            "reason": "missing_junit_xml",
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "testcases": [],
            "failures": [],
            "issues": [],
        }
    try:
        root = ET.parse(path).getroot()
    except Exception as e:
        return {
            "ok": False,
            "reason": "junit_xml_parse_error",
            "error": str(e),
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "testcases": [],
            "failures": [],
            "issues": [],
        }

    cases = [el for el in root.iter() if _strip_namespace(el.tag) == "testcase"]
    testcases: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    failed = errors = skipped = 0
    for case in cases:
        status = "passed"
        problem: ET.Element | None = None
        for child in list(case):
            tag = _strip_namespace(child.tag)
            if tag in {"failure", "error", "skipped"}:
                problem = child
                status = "failed" if tag == "failure" else tag
                break
        if status == "failed":
            failed += 1
        elif status == "error":
            errors += 1
        elif status == "skipped":
            skipped += 1

        raw_line = case.attrib.get("line")
        try:
            case_line = int(raw_line) if raw_line is not None else None
        except (TypeError, ValueError):
            case_line = None
        testcases.append({
            "test": _testcase_id(case),
            "status": status,
            "file": (case.attrib.get("file") or "").replace("\\", "/") or None,
            "line": case_line,
        })

        if problem is None or status == "skipped":
            continue

        text = problem.text or ""
        attr_file = case.attrib.get("file")
        attr_line = case.attrib.get("line")
        parsed_file, parsed_line = _first_path_line(text)
        file_path = (attr_file or parsed_file or "").replace("\\", "/") or None
        try:
            line_no = int(attr_line) if attr_line is not None else parsed_line
        except Exception:
            line_no = parsed_line
        message = problem.attrib.get("message") or (text.strip().splitlines()[-1] if text.strip() else status)
        exc_type = problem.attrib.get("type") or ("AssertionError" if status == "failed" else "Error")
        item = {
            "test": _testcase_id(case),
            "classname": case.attrib.get("classname"),
            "name": case.attrib.get("name"),
            "file": file_path,
            "line": line_no,
            "status": status,
            "type": exc_type,
            "message": truncate(message, 1000),
            "text": truncate(text, 4000),
            "owner": _owner_for_path(file_path),
        }
        failures.append(item)
        issues.append({
            "owner": "generated_test" if item["owner"] == "generated_test_or_external_test" else item["owner"],
            "type": str(exc_type).split(".", 1)[-1].lower(),
            "exception_type": exc_type,
            "file": file_path,
            "line": line_no,
            "message": truncate(message, 1000),
            "test": item["test"],
            "repair_hint": "inspect test oracle and implementation API" if item["owner"] == "generated_test_or_external_test" else "fix implementation runtime behavior",
            "source": "run_tests_junit",
        })
        for parsed in parse_traceback_issues(text):
            issue = dict(parsed)
            if issue.get("owner") == "generated_test_or_external_test":
                issue["owner"] = "generated_test"
            issue.setdefault("test", item["test"])
            issue["source"] = f"run_tests_junit:{issue.get('source', 'traceback_parser')}"
            issues.append(issue)

    total = len(cases)
    passed = max(total - failed - errors - skipped, 0)
    return {
        "ok": failed == 0 and errors == 0,
        "reason": "",
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "testcases": testcases,
        "failures": failures,
        "issues": issues,
    }


def run_tests(
    workspace: str,
    targets: list[str] | str | None = None,
    kind: str = "pytest",
    timeout_sec: int = 120,
    pythonpath: list[str] | str | None = None,
    report_dir: str | None = None,
    max_output_chars: int = 12000,
    read_only: bool = False,
) -> ToolResult:
    """Run tests and return structured results.

    The current implementation intentionally supports pytest only. It keeps
    stdout/stderr as diagnostic evidence, while the primary output is parsed
    JUnit XML so later diagnose/repair code can consume structured failures.
    """
    if read_only:
        return ToolResult(
            tool="run_tests",
            ok=False,
            message="read-only analysis blocks project test execution",
            data={"kind": kind, "targets": targets or [], "blocked_by_policy": True, "read_only_execution_blocked": True},
        )
    if kind != "pytest":
        return ToolResult(tool="run_tests", ok=False, message=f"unsupported test kind: {kind}", data={"kind": kind})
    try:
        normalised_targets = [_normalise_target(workspace, t) for t in _as_list(targets)]
        normalised_pythonpath = _normalise_pythonpath(workspace, _as_list(pythonpath))
        report_root = _report_dir(workspace, report_dir)
    except Exception as e:
        return ToolResult(tool="run_tests", ok=False, message=str(e), data={"kind": kind, "targets": targets or []})

    junit_path = report_root / f"pytest_{uuid.uuid4().hex}.xml"
    command = _build_pytest_command(normalised_targets, normalised_pythonpath, junit_path)
    env = os.environ.copy()
    try:
        proc = subprocess.run(
            command,
            cwd=str(Path(workspace).resolve()),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            env=env,
        )
        timed_out = False
        returncode = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        timed_out = True
        returncode = -1
        stdout = coerce_text(e.stdout)
        stderr = coerce_text(e.stderr)

    parsed = _parse_junit_xml(junit_path, workspace)
    stream_issues = parse_traceback_issues((stdout or "") + "\n" + (stderr or ""))
    for issue in stream_issues:
        if issue.get("owner") == "generated_test_or_external_test":
            issue["owner"] = "generated_test"
        issue["source"] = f"run_tests_stream:{issue.get('source', 'traceback_parser')}"
    issues = list(parsed.get("issues") or []) + stream_issues
    failures = list(parsed.get("failures") or [])

    stream_text = (stdout or "") + "\n" + (stderr or "")
    stream_low = stream_text.lower()
    zero_collected = int(parsed.get("total", 0) or 0) == 0 and (
        returncode == 5
        or "no tests ran" in stream_low
        or "collected 0 item" in stream_low
        or "ran 0 tests" in stream_low
    )
    if zero_collected:
        message = (stdout or stderr or "pytest collected zero tests").strip()
        failures.append({
            "test": "<pytest_collection>",
            "file": None,
            "line": None,
            "status": "error",
            "type": "pytest_zero_collected",
            "message": truncate(message, 1000),
            "text": truncate(stream_text, 4000),
            "owner": "test_collection",
        })
        issues.append({
            "owner": "test_collection",
            "type": "pytest_zero_collected",
            "exception_type": "PytestZeroCollected",
            "file": None,
            "line": None,
            "message": truncate(message, 1000),
            "repair_hint": "inspect pytest configuration, selected targets, and generated test names before changing code or tests",
            "source": "run_tests",
        })

    if "No module named 'pytest'" in stderr:
        failures.append({
            "test": "<pytest_import>",
            "file": None,
            "line": None,
            "status": "error",
            "type": "ModuleNotFoundError",
            "message": "pytest is not installed in the active Python environment",
            "text": truncate(stderr, 4000),
            "owner": "environment",
        })
        issues.append({
            "owner": "environment",
            "type": "pytest_unavailable",
            "exception_type": "ModuleNotFoundError",
            "file": None,
            "line": None,
            "message": "pytest is not installed in the active Python environment",
            "repair_hint": "install pytest or run in the project test environment",
            "source": "run_tests",
        })

    if returncode != 0 and not timed_out and not failures and not issues:
        message = (stderr or stdout or f"pytest exited with return code {returncode}").strip()
        owner = "environment"
        issue_type = "pytest_invocation_error"
        missing = re.search(r"file or directory not found:\s*([^\n]+)", message)
        file_path = missing.group(1).strip().replace("\\", "/") if missing else None
        if file_path:
            owner = _owner_for_path(file_path)
            issue_type = "pytest_target_not_found"
        failures.append({
            "test": "<pytest_invocation>",
            "file": file_path,
            "line": None,
            "status": "error",
            "type": issue_type,
            "message": truncate(message, 1000),
            "text": truncate((stdout or "") + "\n" + (stderr or ""), 4000),
            "owner": owner,
        })
        issues.append({
            "owner": "generated_test" if owner == "generated_test_or_external_test" else owner,
            "type": issue_type,
            "exception_type": "PytestInvocationError",
            "file": file_path,
            "line": None,
            "message": truncate(message, 1000),
            "repair_hint": "fix pytest target selection or test registration" if file_path else "inspect pytest invocation failure",
            "source": "run_tests",
        })

    ok = bool(returncode == 0 and not timed_out and parsed.get("ok", False))
    workspace_root = Path(workspace).resolve()
    try:
        report_path = str(junit_path.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        report_path = str(junit_path).replace("\\", "/")

    data = {
        "version": "run_tests_v1",
        "kind": kind,
        "ok": ok,
        "targets": normalised_targets,
        "pythonpath": normalised_pythonpath,
        "report": report_path,
        "command": command,
        "returncode": returncode,
        "stdout": truncate(stdout, max_output_chars),
        "stderr": truncate(stderr, max_output_chars),
        "timed_out": timed_out,
        "total": parsed.get("total", 0),
        "passed": parsed.get("passed", 0),
        "failed": parsed.get("failed", 0),
        "errors": parsed.get("errors", 0),
        "skipped": parsed.get("skipped", 0),
        "testcases": list(parsed.get("testcases") or [])[:5000],
        "failures": failures,
        "issues": issues,
        "junit_parse": {k: v for k, v in parsed.items() if k not in {"failures", "issues", "testcases"}},
    }
    return ToolResult(tool="run_tests", ok=ok, message="tests finished", data=data)
