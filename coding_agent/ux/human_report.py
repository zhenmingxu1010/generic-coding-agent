from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from coding_agent.ux.language import prefers_chinese
from coding_agent.ux.token_usage import format_token_usage_markdown
from coding_agent.workspace.run_paths import is_test_like_path


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _path_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("path") or "")
    return str(item or "")


def _is_test_path(path: str | None) -> bool:
    return is_test_like_path(path)


def _display_path(path: str) -> str:
    return path.replace("\\", "/")


def _dedupe(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        rel = path.replace("\\", "/")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def _read_optional_text(path: str | None) -> str:
    if not path:
        return ""
    try:
        candidate = Path(path)
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return ""


def _markdown_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    title = ""
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if body:
                sections.append((title, body))
            title = line.strip()
            body = [line.strip()]
            continue
        if body:
            body.append(line)
    if body:
        sections.append((title, body))
    return sections


def extract_answer_summary(final: dict[str, Any], *, max_chars: int = 1800) -> str:
    deliverable_summary = str((final.get("deliverable_review") or {}).get("summary") or "").strip()
    if deliverable_summary:
        return deliverable_summary[:max_chars].rstrip()

    artifacts = final.get("artifacts") or {}
    text = _read_optional_text(artifacts.get("analysis_report"))
    if not text:
        return ""
    sections = _markdown_sections(text)
    preferred = [
        "overall",
        "purpose",
        "summary",
        "conclusion",
        "risk",
        "result",
        "目标",
        "目的",
        "概述",
        "总结",
        "结论",
        "风险",
        "结果",
    ]
    avoid = ["directory structure", "目录结构", "file tree", "文件树"]
    chosen: list[list[str]] = []
    for title, body in sections:
        low = title.lower()
        if any(token in low for token in avoid):
            continue
        if any(token in low for token in preferred):
            chosen.append(body)
        if len(chosen) >= 2:
            break
    if not chosen and sections:
        chosen = [sections[0][1]]
    if not chosen:
        chosen = [[line for line in text.splitlines() if not line.startswith("# ")]]

    out_lines: list[str] = []
    in_code = False
    for body in chosen:
        for line in body:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            if not stripped:
                if out_lines and out_lines[-1] != "":
                    out_lines.append("")
                continue
            if stripped.startswith("|") and len(stripped) > 140:
                continue
            out_lines.append(line.rstrip())
            if len("\n".join(out_lines)) >= max_chars:
                break
        if len("\n".join(out_lines)) >= max_chars:
            break
        if out_lines and out_lines[-1] != "":
            out_lines.append("")
    summary = "\n".join(out_lines).strip()
    if len(summary) > max_chars:
        summary = summary[:max_chars].rstrip() + "\n..."
    return summary


def _test_summary(final: dict[str, Any]) -> dict[str, int]:
    test_results = final.get("test_results") or (final.get("verification") or {}).get("test_results") or {}
    runs = [run for run in test_results.get("runs") or [] if isinstance(run, dict)]
    if runs:
        return {
            "total": sum(int(run.get("total", 0) or 0) for run in runs),
            "passed": sum(int(run.get("passed", 0) or 0) for run in runs),
            "failed": sum(int(run.get("failed", 0) or 0) for run in runs),
            "errors": sum(int(run.get("errors", 0) or 0) for run in runs),
        }
    return {
        "total": int(test_results.get("total", 0) or 0),
        "passed": int(test_results.get("passed", 0) or 0),
        "failed": int(test_results.get("failed", 0) or 0),
        "errors": int(test_results.get("errors", 0) or 0),
    }


def _format_command(command: Any) -> str:
    if isinstance(command, list):
        return shlex.join(str(part) for part in command)
    return str(command or "").strip()


def _verification_evidence(final: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, result in enumerate((final.get("verification") or {}).get("results") or []):
        if not isinstance(result, dict):
            continue
        rows.append(
            {
                "name": str(result.get("name") or f"verification_{index + 1}"),
                "command": _format_command(result.get("command")),
                "returncode": result.get("returncode"),
                "timed_out": bool(result.get("timed_out")),
                "executed": result.get("executed") is not False,
                "stdout": str(result.get("stdout") or "").strip()[:1600],
                "stderr": str(result.get("stderr") or "").strip()[:1600],
            }
        )
    return rows


def build_human_report(final: dict[str, Any], *, show_generated_tests: bool = False) -> dict[str, Any]:
    write_audit = final.get("write_scope_audit") or {}
    generated_files = [
        item
        for item in _as_list(final.get("generated_files"))
        if isinstance(item, dict) and item.get("ok") is not False and item.get("path")
    ]
    generated_test_paths = _dedupe(
        [
            _path_of(item)
            for item in generated_files
            if str(item.get("kind") or "").lower() == "test" or _is_test_path(_path_of(item))
        ]
    )
    generated_non_test_paths = _dedupe(
        [_path_of(item) for item in generated_files if _path_of(item) not in set(generated_test_paths)]
    )
    changed_files = _dedupe([_path_of(item) for item in _as_list(final.get("changed_files"))])
    changed_non_test = _dedupe(
        [path for path in changed_files if show_generated_tests or not _is_test_path(path)]
    )
    source_modified = _dedupe(
        [_path_of(item) for item in _as_list(write_audit.get("existing_project_modified_files"))]
    )
    source_added = _dedupe(
        [_path_of(item) for item in _as_list(write_audit.get("new_project_files"))]
        + [path for path in generated_non_test_paths if path not in set(source_modified)]
    )
    agent_internal_changed = _dedupe(
        [_path_of(item) for item in _as_list(write_audit.get("agent_internal_changed_files"))]
    )

    token_usage = final.get("token_usage") or {}
    token_totals = token_usage.get("totals") or {}
    verification = final.get("verification") or {}
    atom_summary = final.get("requirement_atom_summary") or {}
    gate = final.get("final_gate_status") or {}
    failure_issues = _as_list(final.get("failure_issues"))

    return {
        "task": str(final.get("task") or ""),
        "ok": bool(final.get("ok")),
        "outcome": final.get("outcome") or ("verified_ok" if final.get("ok") else "failed"),
        "stopped_reason": final.get("stopped_reason"),
        "mode": final.get("mode"),
        "workspace": final.get("workspace"),
        "thread_id": final.get("thread_id"),
        "round_idx": final.get("round_idx"),
        "verification_ok": bool(verification.get("ok")) if verification else None,
        "contract_ok": final.get("contract_ok"),
        "analysis_quality_ok": final.get("analysis_quality_ok"),
        "analysis_contract_ok": final.get("analysis_contract_ok"),
        "answer_summary": extract_answer_summary(final),
        "verification_evidence": _verification_evidence(final),
        "final_gate_failures": gate.get("failures") or [],
        "requirement_atom_summary": atom_summary,
        "test_summary": _test_summary(final),
        "source_modified_files": source_modified,
        "source_added_files": source_added,
        "agent_internal_files": agent_internal_changed,
        "generated_non_test_files": generated_non_test_paths,
        "generated_test_count": len(generated_test_paths),
        "generated_test_files": generated_test_paths if show_generated_tests else [],
        "changed_files": changed_non_test,
        "hidden_generated_tests": not show_generated_tests and bool(generated_test_paths),
        "failure_issue_count": len(failure_issues),
        "failure_issues": failure_issues[:8],
        "clarification_questions": _as_list(final.get("clarification_questions")),
        "assumptions": _as_list(final.get("assumptions")),
        "token_usage": token_usage,
        "token_totals": token_totals,
        "artifacts": final.get("artifacts") or {},
    }


def _append_path_section(lines: list[str], title: str, paths: list[str]) -> None:
    lines.append(f"### {title}")
    lines.append("")
    if not paths:
        lines.append("- none")
    else:
        for path in paths:
            lines.append(f"- {_display_path(path)}")
    lines.append("")


def _append_verification_section(
    lines: list[str],
    evidence: list[dict[str, Any]],
    *,
    chinese: bool,
) -> None:
    if not evidence:
        return
    lines.extend(["", f"## {'验证结果' if chinese else 'Verification Evidence'}", ""])
    for row in evidence[:8]:
        if row["timed_out"]:
            status = "超时" if chinese else "timed out"
        elif not row["executed"]:
            status = "未执行" if chinese else "not executed"
        elif row["returncode"] == 0:
            status = "通过" if chinese else "passed"
        else:
            status = "失败" if chinese else "failed"
        lines.append(f"### {row['name']}: {status}")
        lines.append("")
        if row["command"]:
            label = "命令" if chinese else "Command"
            safe_command = row["command"].replace("`", "\\`")
            lines.append(f"- {label}: `{safe_command}`")
        if row["returncode"] is not None:
            lines.append(f"- returncode: {row['returncode']}")
        if row["stdout"]:
            lines.extend(["", "stdout:", "", "```text", row["stdout"].replace("```", "`` `"), "```"])
        if row["stderr"]:
            lines.extend(["", "stderr:", "", "```text", row["stderr"].replace("```", "`` `"), "```"])
        lines.append("")


def format_human_report_markdown(final: dict[str, Any], *, show_generated_tests: bool = False) -> str:
    report = build_human_report(final, show_generated_tests=show_generated_tests)
    chinese = prefers_chinese(report["task"])
    needs_clarification = report["stopped_reason"] == "clarification_required"
    if needs_clarification:
        status = "需要补充信息" if chinese else "Clarification required"
    else:
        status = ("成功" if report["ok"] else "失败") if chinese else ("Success" if report["ok"] else "Failed")
    lines = [
        f"# {'Coding Agent 任务结果' if chinese else 'Coding Agent Result'}",
        "",
        f"**{'任务状态' if chinese else 'Task status'}: {status}**",
    ]
    if needs_clarification:
        lines.extend(["", f"## {'需要你补充' if chinese else 'Questions'}", ""])
        for item in report["clarification_questions"]:
            question = item.get("question") if isinstance(item, dict) else item
            if question:
                lines.append(f"- {question}")
    if report["assumptions"]:
        lines.extend(["", f"## {'采用的实现假设' if chinese else 'Implementation Assumptions'}", ""])
        for item in report["assumptions"]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('field')}: {item.get('value')} ({item.get('rationale')})")
    if report["answer_summary"]:
        lines.extend(
            [
                "",
                f"## {'结果摘要' if chinese else 'Answer Summary'}",
                "",
                report["answer_summary"],
            ]
        )

    lines.extend(
        [
            "",
            f"## {'执行状态' if chinese else 'Execution Status'}",
            "",
            f"- outcome: {report['outcome']}",
            f"- stopped_reason: {report['stopped_reason']}",
            f"- mode: {report['mode']}",
            f"- rounds: {report['round_idx']}",
            f"- verification_ok: {report['verification_ok']}",
            f"- contract_ok: {report['contract_ok']}",
            f"- final_gate_failures: {report['final_gate_failures']}",
        ]
    )
    atom = report["requirement_atom_summary"] or {}
    if atom:
        lines.append(
            "- requirement_atoms: "
            f"failed={atom.get('required_failed', 0)}, "
            f"unverified={atom.get('required_unverified', 0)}, "
            f"total={atom.get('required_total', atom.get('total', 0))}"
        )
    tests = report["test_summary"]
    if any(tests.values()):
        lines.append(
            "- tests: "
            f"total={tests['total']}, passed={tests['passed']}, "
            f"failed={tests['failed']}, errors={tests['errors']}"
        )
    if report["hidden_generated_tests"]:
        lines.append(f"- generated_tests_hidden: {report['generated_test_count']}")

    lines.extend(["", f"## {'文件变更' if chinese else 'File Changes'}", ""])
    _append_path_section(
        lines,
        "修改的已有文件" if chinese else "Modified Existing Files",
        report["source_modified_files"],
    )
    _append_path_section(
        lines,
        "新增文件" if chinese else "Added Files",
        report["source_added_files"],
    )
    _append_path_section(
        lines,
        "Agent 内部文件" if chinese else "Agent Internal Files",
        report["agent_internal_files"],
    )
    if show_generated_tests:
        _append_path_section(
            lines,
            "生成的测试文件" if chinese else "Generated Test Files",
            report["generated_test_files"],
        )

    _append_verification_section(lines, report["verification_evidence"], chinese=chinese)

    if not report["ok"] and report["failure_issues"]:
        lines.extend(["", f"## {'失败原因' if chinese else 'Failure Issues'}", ""])
        for issue in report["failure_issues"]:
            lines.append(
                f"- {issue.get('owner') or 'unknown'} / "
                f"{issue.get('type') or 'issue'}: {issue.get('message') or ''}"
            )

    artifacts = report["artifacts"]
    lines.extend(["", f"## {'结果文件' if chinese else 'Artifacts'}", ""])
    for key in [
        "final_json",
        "final_report",
        "human_report",
        "analysis_report",
        "trace",
        "messages",
        "state_snapshot",
        "context_pack",
    ]:
        if artifacts.get(key):
            lines.append(f"- {key}: {artifacts[key]}")

    lines.append("")
    token_markdown = format_token_usage_markdown(report["token_usage"]).rstrip()
    if chinese:
        token_markdown = token_markdown.replace("## Token Usage", "## Token Usage / Token 使用量", 1)
    lines.append(token_markdown)
    return "\n".join(lines).rstrip() + "\n"


def write_human_report(
    path: str | Path,
    final: dict[str, Any],
    *,
    show_generated_tests: bool = False,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        format_human_report_markdown(final, show_generated_tests=show_generated_tests),
        encoding="utf-8",
    )
