from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "regression_matrix" / "matrix.json"
RENDER_SCRIPT = ROOT / "scripts" / "render_regression_matrix.py"


def _matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_regression_matrix_declares_t01_to_t11():
    matrix = _matrix()
    cases = matrix["cases"]

    assert [case["id"] for case in cases] == [f"T{i:02d}" for i in range(1, 12)]
    assert matrix["version"].startswith("opensource_regression_matrix")


def test_each_regression_case_has_commands_and_expected_conditions():
    for case in _matrix()["cases"]:
        assert case["thread_id"].startswith("regression_")
        assert case["title_zh"]
        assert case["purpose_zh"]
        assert case["workspace"]["template"]
        assert case.get("task") or case.get("repair_existing")
        assert isinstance(case.get("max_rounds"), int)
        assert case["max_rounds"] > 0
        assert case["expected_conditions"]


def test_regression_matrix_has_required_capability_labels():
    titles = {case["id"]: case["title_zh"] for case in _matrix()["cases"]}

    assert titles["T01"] == "只读项目理解"
    assert titles["T02"] == "隔离生成分析脚本"
    assert titles["T03"] == "CLI 参数语义验证"
    assert titles["T04"] == "生成测试不一致修复"
    assert titles["T05"] == "0 tests 检测"
    assert titles["T06"] == "禁止污染原项目"
    assert titles["T07"] == "修改已有小项目 bug"
    assert titles["T08"] == "生成一个小型 CLI 项目"
    assert titles["T09"] == "短 Prompt 安全默认值"
    assert titles["T10"] == "模糊 Prompt 受控追问"
    assert titles["T11"] == "口语化只读项目查看"


def test_short_prompt_cases_cover_proceed_clarify_and_readonly():
    cases = {case["id"]: case for case in _matrix()["cases"]}
    assert cases["T09"]["task"] == "写个脚本统计文本行数"
    assert any("clarification_required" in item for item in cases["T10"]["expected_conditions"])
    assert cases["T11"]["task"] == "看看这个项目"


def test_t02_requires_direct_fallback_evidence_and_complete_values():
    case = next(case for case in _matrix()["cases"] if case["id"] == "T02")
    conditions = "\n".join(case["expected_conditions"])

    assert "no-argument fallback scenario" in conditions
    assert "default summary absent" in conditions
    assert "non-placeholder values" in conditions


def test_t06_allows_only_declared_new_deliverables():
    case = next(case for case in _matrix()["cases"] if case["id"] == "T06")
    conditions = case["expected_conditions"]

    assert "final.write_scope_audit.source_changed_files == []" not in conditions
    assert any(
        "source_changed_files contains only" in condition
        and "scripts/inspect_event_schema.py" in condition
        and "docs/event_schema.md" in condition
        for condition in conditions
    )


def test_render_regression_case_outputs_run_and_audit_commands():
    proc = subprocess.run(
        [
            sys.executable,
            str(RENDER_SCRIPT),
            "--case",
            "T03",
            "--agent-repo",
            "/agent",
            "--audit-dir",
            "/audit",
            "--work-root",
            "/work",
        ],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    out = proc.stdout

    assert "THREAD=regression_t03_cli_contract" in out
    assert "WORKSPACE=/work/t03_cli_contract" in out
    assert "python -m coding_agent.main" in out
    assert "python -m coding_agent.export_audit" in out
    assert "--output-csv" in out
    assert "Expected conditions:" in out


def test_render_all_regression_cases_outputs_all_thread_ids():
    proc = subprocess.run(
        [sys.executable, str(RENDER_SCRIPT), "--all"],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    out = proc.stdout

    for case in _matrix()["cases"]:
        assert case["thread_id"] in out


def test_render_regression_matrix_uses_model_config_by_default():
    proc = subprocess.run(
        [sys.executable, str(RENDER_SCRIPT), "--case", "T01"],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    out = proc.stdout

    assert "configs/model.local.yaml" in out
    assert "export AGENT_LLM_API_KEY" not in out
    assert "AGENT_LLM_API_KEY:-EMPTY" not in out


def test_render_regression_matrix_can_include_legacy_llm_env_defaults():
    proc = subprocess.run(
        [sys.executable, str(RENDER_SCRIPT), "--case", "T01", "--include-llm-env"],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    out = proc.stdout

    assert "export AGENT_LLM_API_KEY=\"${AGENT_LLM_API_KEY:-EMPTY}\"" in out
