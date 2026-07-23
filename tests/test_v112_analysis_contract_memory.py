from pathlib import Path

from coding_agent.contracts.analysis_contract import (
    build_analysis_contract,
    verify_analysis_report_against_contract,
    extract_structured_memory,
    build_task_focused_file_hints,
)
from coding_agent.nodes.analyze_report import _quality
from coding_agent.memory.project_memory import update_project_memory_from_repo


def test_analysis_contract_detects_metric_summary_followup():
    c = build_analysis_contract("基于已有项目记忆，只读找出 metrics、summary/result、collect_results 相关文件，说明如何比较不同模型，并给出后续新增 summary 分析脚本的设计。")
    assert c["report_type"] == "metric_result_summary"
    ids = {q["id"] for q in c["required_questions"]}
    assert "metrics_files" in ids
    assert "result_summary_files" in ids
    assert "aggregation_flow" in ids
    assert "comparison_method" in ids
    assert "proposed_script_design" in ids


def test_readonly_project_overview_loss_design_does_not_require_script_design():
    c = build_analysis_contract(
        "这是一个只读深度理解任务。禁止修改任何已有项目文件，禁止创建项目文件。"
        "请深入阅读项目结构以及 src、scripts、experiments、summary、metrics、config 相关文件，"
        "输出中文分析报告，说明项目目标、数据流、模型结构、loss 设计、评估指标、"
        "实验结果组织、主要运行入口、复现流程和潜在风险。必须保持只读。"
    )
    ids = {q["id"] for q in c["required_questions"]}

    assert c["report_type"] == "repository_overview"
    assert "proposed_script_design" not in ids
    assert "losses" in ids
    assert "metrics_files" in ids


def test_analysis_contract_checker_rejects_generic_overview_for_focused_task():
    c = build_analysis_contract("找出 metrics、summary/result、collect_results 相关文件，说明如何比较不同模型，并设计 summary 分析脚本")
    ctx = {"selected_files": ["metrics.py", "collect_results.py", "summary.json"], "structured_memory": {"metric_names": ["iou_score", "thickness_mae"]}}
    report = """# Repository Analysis Report
## Overall Purpose
metrics.py collect_results.py summary.json are files. This project trains models.
"""
    q = verify_analysis_report_against_contract(report, c, ctx)
    assert q["ok"] is False
    assert "comparison_method" in q["missing_required_questions"] or "proposed_script_design" in q["missing_required_questions"]


def test_analysis_contract_checker_accepts_task_focused_report():
    c = build_analysis_contract("找出 metrics、summary/result、collect_results 相关文件，说明如何比较不同模型，并设计 summary 分析脚本")
    ctx = {"selected_files": ["metrics.py", "collect_results.py", "summary.json"], "structured_memory": {"metric_names": ["iou_score", "thickness_mae"]}}
    report = """# Metrics / Results / Summary Analysis Report
## 1. Relevant Files
- metrics.py defines iou_score and thickness_mae.
- collect_results.py collects metrics.
- summary.json stores results.
## 2. Metric Schema
iou_score is higher-is-better. thickness_mae is lower-is-better.
## 3. Result/Summary Schema
summary.json contains model and metric fields.
## 4. Existing Aggregation Flow
collect_results.py aggregates metrics from result JSON files.
## 5. Model/Run Comparison Method
Compare runs by model, rank by iou_score descending and thickness_mae ascending, then output a table.
## 6. Proposed Summary Analysis Script Design
Script name: scripts/analyze_summary.py. Input files: summary.json and metrics JSON. Parsed fields: model, loss, iou_score, thickness_mae. Output columns: model, loss, metric, rank. Usage command: python scripts/analyze_summary.py --input summary.json --out table.csv. Sort and rank by best metric.
## 7. Risks and Missing Evidence
Schema may need confirmation.
## 8. Question Coverage Checklist
metrics_files: Answered. result_summary_files: Answered. aggregation_flow: Answered. comparison_method: Answered. proposed_script_design: Answered.
"""
    q = verify_analysis_report_against_contract(report, c, ctx)
    assert q["ok"] is True


def test_chinese_task_focused_analysis_can_pass_with_structured_evidence_paths():
    task = (
        "\u53ea\u8bfb\u7406\u89e3\u8fd9\u4e2a\u9879\u76ee\uff0c\u7981\u6b62\u4fee\u6539\u6587\u4ef6\u3002"
        "\u8bf7\u9605\u8bfb experiments\u3001scripts\u3001src\u3001summary\u3001metrics \u76f8\u5173\u6587\u4ef6\uff0c"
        "\u8f93\u51fa\u4e2d\u6587\u9879\u76ee\u7406\u89e3\u62a5\u544a\uff0c\u5305\u62ec\u9879\u76ee\u76ee\u6807\u3001\u8bad\u7ec3/\u8bc4\u4f30\u5165\u53e3\u3001"
        "\u5b9e\u9a8c\u7ed3\u679c\u6587\u4ef6\u3001\u5173\u952e\u6307\u6807\u3001\u53ef\u8fd0\u884c\u547d\u4ee4\u548c\u6f5c\u5728\u98ce\u9669\u3002"
    )
    contract = build_analysis_contract(task)
    ctx = {
        "selected_files": [f"experiments/run{i}/metrics_test.json" for i in range(12)],
        "role_coverage_after": {"coverage_ratio": 1.0, "missing_roles": []},
        "structured_memory": {
            "metric_names": ["eight_class_accuracy", "num_layers_accuracy", "thickness_mae_km", "iou"],
            "metric_files": ["src/pkg/metrics.py", "experiments/run0/metrics_test.json"],
            "summary_files": ["experiments/summary.json"],
            "collector_files": ["src/pkg/collect_results.py"],
            "script_files": ["scripts/01_train.sh", "scripts/07_collect_results.sh"],
            "config_files": ["experiments/run0/config.json"],
        },
        "evidence_index": {
            "metric_or_domain_terms_by_file": {
                "src/pkg/metrics.py": ["eight_class_accuracy", "num_layers_accuracy", "thickness_mae_km", "iou"]
            }
        },
    }
    report = (
        "# \u4e2d\u6587\u9879\u76ee\u7406\u89e3\u62a5\u544a\n"
        "## 1. \u9879\u76ee\u76ee\u6807\n"
        "\u8fd9\u662f project overview\uff0c\u7528\u4e8e\u8bf4\u660e\u9879\u76ee\u76ee\u6807\u3001directory\u3001entry\u3001script workflow \u548c risk\u3002\n"
        "## 2. \u9879\u76ee\u7c7b\u578b\n"
        "\u6839\u636e `src/pkg/metrics.py`\u3001`experiments/run0/metrics_test.json` \u548c `experiments/summary.json`\uff0c\u5b83\u662f\u5b9e\u9a8c\u7ed3\u679c\u5206\u6790\u9879\u76ee\u3002\n"
        "## 3. \u76ee\u5f55\u7ed3\u6784\n"
        "`src/pkg/metrics.py` \u5c5e\u4e8e src\uff0c`experiments/run0/config.json` \u548c `experiments/run0/metrics_test.json` \u5c5e\u4e8e experiments\uff0c"
        "`scripts/01_train.sh` \u548c `scripts/07_collect_results.sh` \u5c5e\u4e8e scripts\u3002\n"
        "## 4. \u4e3b\u8981\u5165\u53e3\n"
        "`scripts/01_train.sh` \u662f train entry\uff0c`scripts/07_collect_results.sh` \u662f collect workflow run command\u3002\n"
        "## 5. \u6570\u636e\u6d41\n"
        "\u6570\u636e\u4ece experiments \u7684 metrics_test JSON \u8fdb\u5165 `src/pkg/collect_results.py`\uff0c\u7136\u540e\u6c47\u603b\u5230 summary result artifact\u3002\n"
        "## 6. \u6a21\u578b\n"
        "\u6a21\u578b\u7ef4\u5ea6\u6765\u81ea metrics/result \u4e2d\u7684 model/loss/experiment \u5b57\u6bb5\uff0c\u8be6\u7ec6 model file \u9700\u7ee7\u7eed\u6838\u5bf9\u3002\n"
        "## 7. \u635f\u5931\u3001\u6307\u6807\u3001\u8bc4\u4f30\n"
        "eight_class_accuracy\u3001num_layers_accuracy\u3001iou \u6309 higher/best \u7406\u89e3\uff0cthickness_mae_km \u6309 lower/best \u7406\u89e3\uff0c"
        "metric metrics accuracy iou mae loss score \u90fd\u662f\u672c\u62a5\u544a\u8986\u76d6\u7684\u6307\u6807\u8bcd\u3002\n"
        "## 8. \u811a\u672c\u3001\u5de5\u4f5c\u6d41\u3001\u53ef\u8fd0\u884c\u547d\u4ee4\n"
        "script workflow run command \u5305\u62ec `scripts/01_train.sh` \u548c `scripts/07_collect_results.sh`\u3002\n"
        "## 9. \u914d\u7f6e\u3001\u7ed3\u679c\u3001\u4ea7\u7269\n"
        "config experiment matrix \u6765\u81ea `experiments/run0/config.json`\uff0cresult/summary/metrics_test/artifact/json \u6765\u81ea `experiments/summary.json`\u3002\n"
        "## 10. \u98ce\u9669\u3001\u4e0b\u4e00\u6b65\n"
        "\u98ce\u9669\u662f schema \u53ef\u80fd\u5728\u4e0d\u540c\u5b9e\u9a8c\u95f4\u4e0d\u5b8c\u5168\u4e00\u81f4\uff0c\u9700\u8981\u62bd\u6837\u68c0\u67e5\u3002\n"
        "## 11. \u6587\u4ef6\u804c\u8d23\n"
        "`src/pkg/metrics.py` \u8d1f\u8d23\u6307\u6807\uff0c`src/pkg/collect_results.py` \u8d1f\u8d23 collect aggregate summary\uff0c"
        "`experiments/run0/metrics_test.json` \u8d1f\u8d23\u5b58\u50a8\u5355\u6b21\u8bc4\u4f30\u7ed3\u679c\u3002\n"
        "## 12. \u95ee\u9898\u8986\u76d6\u68c0\u67e5\n"
        "project_overview: \u5df2\u56de\u7b54\u3002metrics_files: \u5df2\u56de\u7b54\u3002result_summary_files: \u5df2\u56de\u7b54\u3002aggregation_flow: \u5df2\u56de\u7b54\u3002"
        "comparison_method: compare comparison rank ranking higher lower best table \u5df2\u56de\u7b54\u3002entrypoints_workflow: train entry script workflow run \u5df2\u56de\u7b54\u3002"
        "configs_experiment_matrix: config experiment matrix \u5df2\u56de\u7b54\u3002\n"
        + "\u8bc1\u636e\u8865\u5145\u3002" * 500
    )

    quality = _quality(report, ctx, contract)

    assert contract["report_type"] == "repository_overview"
    assert quality["ok"] is True
    assert quality["analysis_contract_check"]["ok"] is True
    assert quality["analysis_contract_check"]["path_mentions_count"] >= 5


def test_structured_memory_extracts_metric_result_files():
    repo_map = {
        "files": ["src/metrics.py", "src/collect_results.py", "experiments/summary.json", "scripts/run.sh"],
        "records": [
            {"path": "src/metrics.py", "roles": {"metric_evaluation": 60}, "signals": {}, "importance_score": 10},
            {"path": "src/collect_results.py", "roles": {}, "signals": {}, "importance_score": 10},
            {"path": "experiments/summary.json", "roles": {"results_or_outputs": 70}, "signals": {"is_result_like": True}, "importance_score": 10},
            {"path": "scripts/run.sh", "roles": {"run_workflow": 60}, "signals": {}, "importance_score": 10},
        ],
    }
    ctx = {"evidence_index": {"metric_or_domain_terms_by_file": {"src/metrics.py": ["iou_score", "thickness_mae"]}}}
    m = extract_structured_memory({"repo_map": repo_map, "repo_analysis_context": ctx})
    assert "src/metrics.py" in m["metric_files"]
    assert "experiments/summary.json" in m["summary_files"]
    assert "src/collect_results.py" in m["collector_files"]
    assert "iou_score" in m["metric_names"]


def test_task_focused_file_hints_prefers_structured_memory():
    repo_map = {"files": ["metrics.py", "collect_results.py", "README.md"], "records": []}
    c = build_analysis_contract("找 metrics summary collect_results 并说明比较模型")
    profile = {"structured_memory": {"metric_files": ["metrics.py"], "collector_files": ["collect_results.py"]}}
    hints = build_task_focused_file_hints(c, repo_map, profile, max_files=5)
    assert hints[:2] == ["metrics.py", "collect_results.py"]
