from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from coding_agent.export_audit import export_audit_bundle
from coding_agent.ux.human_report import build_human_report, format_human_report_markdown
from coding_agent.workspace.run_paths import run_dir_for


def _final() -> dict:
    return {
        "ok": True,
        "outcome": "verified_ok",
        "stopped_reason": "verified_ok",
        "mode": "write",
        "thread_id": "t",
        "round_idx": 2,
        "verification": {"ok": True, "test_results": {"runs": [{"total": 2, "passed": 2, "failed": 0, "errors": 0}]}},
        "contract_ok": True,
        "final_gate_status": {"failures": []},
        "requirement_atom_summary": {"required_failed": 0, "required_unverified": 0, "required_total": 3},
        "write_scope_audit": {
            "existing_project_modified_files": ["src/app.py"],
            "new_project_files": ["scripts/report.py"],
            "agent_test_changed_files": [".coding_agent_test/t/tests/test_report.py"],
        },
        "changed_files": [
            "src/app.py",
            "scripts/report.py",
            ".coding_agent_test/t/tests/test_report.py",
        ],
        "generated_files": [
            {"path": "scripts/report.py", "kind": "code", "ok": True},
            {"path": ".coding_agent_test/t/tests/test_report.py", "kind": "test", "ok": True},
        ],
        "token_usage": {
            "available": True,
            "totals": {
                "calls": 1,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "reasoning_tokens": 2,
                "total_tokens": 15,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 10,
            },
            "by_purpose": {},
        },
        "artifacts": {"trace": "trace.jsonl", "messages": "messages.jsonl"},
    }


def test_human_report_hides_generated_tests_by_default():
    report = build_human_report(_final())
    markdown = format_human_report_markdown(_final())

    assert report["generated_test_count"] == 1
    assert report["generated_test_files"] == []
    assert report["hidden_generated_tests"] is True
    assert ".coding_agent_test/t/tests/test_report.py" not in markdown
    assert "generated_tests_hidden: 1" in markdown
    assert "| TOTAL | 1 | 10 | 5 | 2 | 15 |" in markdown


def test_human_report_can_show_generated_tests():
    report = build_human_report(_final(), show_generated_tests=True)
    markdown = format_human_report_markdown(_final(), show_generated_tests=True)

    assert report["generated_test_files"] == [".coding_agent_test/t/tests/test_report.py"]
    assert ".coding_agent_test/t/tests/test_report.py" in markdown


def test_human_report_presents_clarification_as_a_question_not_a_crash():
    final = _final()
    final.update({
        "ok": False,
        "outcome": "clarification_required",
        "stopped_reason": "clarification_required",
        "task": "写个脚本",
        "clarification_questions": [{"question": "希望它完成什么核心功能？"}],
        "assumptions": [],
    })

    markdown = format_human_report_markdown(final)

    assert "任务状态: 需要补充信息" in markdown
    assert "## 需要你补充" in markdown
    assert "希望它完成什么核心功能？" in markdown


def test_export_audit_includes_human_report(tmp_path: Path):
    run_dir = run_dir_for(tmp_path, "t")
    run_dir.mkdir(parents=True)
    (run_dir / "final.json").write_text("{}", encoding="utf-8")
    (run_dir / "final_report_human.md").write_text("human", encoding="utf-8")
    out = tmp_path / "audit.zip"

    export_audit_bundle(tmp_path, "t", out)

    with zipfile.ZipFile(out) as zf:
        assert "agent/final_report_human.md" in zf.namelist()
        assert "audit_manifest.json" in zf.namelist()


def test_export_audit_redacts_local_paths_and_common_api_keys(tmp_path: Path):
    run_dir = run_dir_for(tmp_path, "redact")
    run_dir.mkdir(parents=True)
    fake_key = "test-token-that-must-be-redacted"
    (run_dir / "final.json").write_text(
        '{"workspace": "' + str(tmp_path) + '", "api_key": "' + fake_key + '"}',
        encoding="utf-8",
    )
    out = tmp_path / "audit.zip"

    export_audit_bundle(tmp_path, "redact", out)

    with zipfile.ZipFile(out) as zf:
        text = zf.read("agent/final.json").decode("utf-8")
        assert str(tmp_path) not in text
        assert fake_key not in text
        assert "<WORKSPACE>" in text
        assert "<REDACTED>" in text


def test_export_audit_rejects_missing_run_directory(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="run directory does not exist"):
        export_audit_bundle(tmp_path, "missing_thread", tmp_path / "audit.zip")


def test_export_audit_rejects_run_without_final_json(tmp_path: Path):
    run_dir_for(tmp_path, "t").mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="final.json does not exist"):
        export_audit_bundle(tmp_path, "t", tmp_path / "audit.zip")


def test_human_report_extracts_analysis_answer_summary(tmp_path: Path):
    analysis = tmp_path / "analysis_report.md"
    analysis.write_text(
        "# Repository Analysis Report\n\n"
        "## 1. Overall Purpose\n\n"
        "这个项目用于分析服务运行结果，比较不同配置的效果。\n\n"
        "核心结论是新缓存策略在关键指标上更优。\n\n"
        "## 2. Directory Structure\n\n"
        "```text\n"
        "very large tree\n"
        "```\n",
        encoding="utf-8",
    )
    final = _final()
    final["artifacts"]["analysis_report"] = str(analysis)

    report = build_human_report(final)
    markdown = format_human_report_markdown(final)

    assert "这个项目用于分析服务运行结果" in report["answer_summary"]
    assert "新缓存策略" in report["answer_summary"]
    assert "very large tree" not in report["answer_summary"]
    assert "## Answer Summary" in markdown
    assert "这个项目用于分析服务运行结果" in markdown


def test_human_report_summarizes_chinese_code_change_and_runtime_verification():
    final = _final()
    final.update(
        {
            "task": "修复程序中空行被计入非空行数量的问题，并验证运行结果。",
            "deliverable_review": {
                "summary": (
                    "修复了 src/app.py 中空行被计入 non_empty 的问题，"
                    "现在只统计去除空白后仍有内容的行。"
                )
            },
            "verification": {
                "ok": True,
                "results": [
                    {
                        "name": "behavior_check",
                        "command": ["python", "src/app.py", "sample.txt"],
                        "returncode": 0,
                        "stdout": '{"non_empty": 2, "characters": 9}\n',
                        "stderr": "",
                    }
                ],
            },
        }
    )

    markdown = format_human_report_markdown(final)

    assert "# Coding Agent 任务结果" in markdown
    assert "## 结果摘要" in markdown
    assert "修复了 src/app.py" in markdown
    assert "## 文件变更" in markdown
    assert "src/app.py" in markdown
    assert "## 验证结果" in markdown
    assert "python src/app.py sample.txt" in markdown
    assert '{"non_empty": 2, "characters": 9}' in markdown
