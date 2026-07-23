from coding_agent.nodes.analyze_report import _quality, _quality_with_semantic_section_review
from coding_agent.nodes.final_gate import compute_final_gate


def _context_pack():
    return {
        "version": "context_pack_v2.1",
        "evidence_blocks": [
            {
                "path": "src/pkg/data.py",
                "ok": True,
                "evidence_selection": ["role_representative:data_pipeline"],
                "symbols": {"classes": ["BatchDataset"], "functions": ["build_loader"]},
                "content": "1: class BatchDataset:\n2:     pass\n3: def build_loader(): pass",
            },
            {
                "path": "src/pkg/models.py",
                "ok": True,
                "evidence_selection": ["role_representative:model_definition"],
                "symbols": {"classes": ["TinyModel(Module)"], "functions": ["build_model"]},
                "content": "1: class TinyModel(nn.Module):\n2:     pass\n3: def build_model(): pass",
            },
            {
                "path": "src/pkg/losses.py",
                "ok": True,
                "evidence_selection": ["role_representative:loss_definition"],
                "symbols": {"classes": ["FocalLoss"], "functions": ["build_loss"]},
                "content": "1: class FocalLoss:\n2:     pass\n3: def build_loss(): pass",
            },
            {
                "path": "src/pkg/metrics.py",
                "ok": True,
                "evidence_selection": ["role_representative:metric_evaluation"],
                "symbols": {"classes": ["MetricAccumulator"], "functions": ["iou_score"]},
                "content": "1: class MetricAccumulator:\n2:     pass\n3: def iou_score(): pass",
            },
            {
                "path": "experiments/run0/metrics_test.json",
                "ok": True,
                "evidence_selection": ["role_representative:results_or_outputs"],
                "symbols": {},
                "content": '1: {"iou": 0.7, "accuracy": 0.8, "loss": 0.2}',
            },
        ],
    }


def _analysis_context():
    return {
        "selected_files": [
            "src/pkg/data.py",
            "src/pkg/models.py",
            "src/pkg/losses.py",
            "src/pkg/metrics.py",
            "experiments/run0/metrics_test.json",
        ],
        "role_coverage_after": {"coverage_ratio": 1.0, "missing_roles": []},
        "evidence_index": {},
    }


def _contract():
    return {"report_type": "repository_overview", "required_questions": [], "required_sections": []}


class _FakeClient:
    def chat(self, *args, **kwargs):
        return '{"section_coverage":[{"required_section":"Result/Summary Schema","covered":true,"matched_heading":"Collected Output Format","reason":"same meaning"}]}'


class _FakeTrace:
    def event(self, *args, **kwargs):
        return None


def _headings():
    return """# Repository Analysis Report
## 1. Overall Purpose
## 2. Project Type and Evidence Coverage
## 3. Directory Structure
## 4. Main Entry Points
## 5. Data Flow and Inputs/Outputs
## 6. Model / Core Logic
## 7. Losses / Metrics / Evaluation
## 8. Scripts / Workflow / Execution
## 9. Configs, Results, and Artifacts
## 10. Risks, Gaps, and Next Checks
## 11. File Responsibility Table
"""


def test_analysis_quality_v2_rejects_report_that_omits_evidence_symbols_and_fields():
    report = (
        _headings()
        + """
The project has data, model, loss, and metric code in src/pkg/data.py,
src/pkg/models.py, src/pkg/losses.py, and src/pkg/metrics.py.
Results are in experiments/run0/metrics_test.json.
"""
        + "General repository discussion. " * 140
    )

    quality = _quality(report, _analysis_context(), _contract(), context_pack=_context_pack())

    assert quality["ok"] is False
    assert "report missing concrete class/function mentions from core evidence" in quality["warnings"]


def test_semantic_section_review_helper_does_not_recurse():
    contract = {
        "report_type": "repository_overview",
        "required_questions": [],
        "required_sections": ["Result/Summary Schema"],
    }
    report = (
        "# Repository Analysis Report\n"
        "## Collected Output Format\n"
        "Results are stored in experiments/run0/metrics_test.json with fields iou, accuracy, and loss.\n"
        + "Grounded repository discussion with concrete evidence. " * 160
    )

    quality = _quality_with_semantic_section_review(
        report,
        _analysis_context(),
        contract,
        _context_pack(),
        _FakeClient(),
        _FakeTrace(),
    )

    assert "Result/Summary Schema" not in quality["analysis_contract_check"]["missing_sections"]


def test_task_focused_report_does_not_inherit_overview_heading_gate():
    contract = {
        "report_type": "metric_result_summary",
        "required_questions": [
            {
                "id": "metrics_files",
                "question": "Identify metric files.",
                "required_terms": ["metric", "metrics", "iou", "precision", "recall"],
                "required": True,
            },
            {
                "id": "result_summary_files",
                "question": "Identify result files.",
                "required_terms": ["result", "summary", "csv"],
                "required": True,
            },
        ],
        "required_sections": [],
    }
    context_pack = {
        "evidence_blocks": [
            {
                "path": "tools/summarize.py",
                "ok": True,
                "evidence_selection": ["role_representative:metric_evaluation"],
                "symbols": {"classes": [], "functions": ["main"]},
                "content": "1: def main(): pass",
            },
            {
                "path": "src/losses.py",
                "ok": True,
                "evidence_selection": ["role_representative:model_definition"],
                "symbols": {"classes": ["UnusedModel"], "functions": ["forward"]},
                "content": "1: class UnusedModel:\n2:     def forward(self): pass",
            },
            {
                "path": "experiments/run0/test_summary_by_scheme.csv",
                "ok": True,
                "evidence_selection": ["role_representative:results_or_outputs"],
                "symbols": {},
                "content": "scheme,micro_iou,column_precision,column_recall,column_f1\nfixed,0.8,0.7,0.6,0.65\n",
            },
        ]
    }
    ctx = {
        "selected_files": ["tools/summarize.py", "src/losses.py", "experiments/run0/test_summary_by_scheme.csv"],
        "role_coverage_after": {"coverage_ratio": 1.0, "missing_roles": []},
        "evidence_index": {"metric_or_domain_terms_by_file": {"tools/summarize.py": ["micro_iou"]}},
    }
    report = (
        "# Metrics and Result Summary\n"
        "tools/summarize.py defines main and collects metric results. "
        "experiments/run0/test_summary_by_scheme.csv is the result summary csv with micro_iou, "
        "column_precision, column_recall, and column_f1 fields. "
        "The metric comparison ranks micro_iou higher as better and reports precision and recall as real csv fields. "
        + "Focused evidence discussion. " * 120
    )

    quality = _quality(report, ctx, contract, context_pack=context_pack)

    assert quality["ok"] is True
    assert quality["warnings"] == []
    assert quality["analysis_contract_check"]["ok"] is True
    assert quality["analysis_contract_check"]["min_path_mentions"] == 2
    assert "Overall Purpose" in quality["advisory_warnings"][0]


def test_analysis_quality_v2_accepts_report_with_paths_symbols_and_json_fields():
    report = (
        _headings()
        + """
src/pkg/data.py defines BatchDataset and build_loader, which makes it the data pipeline evidence.
src/pkg/models.py defines TinyModel and build_model, which makes it the model definition evidence.
src/pkg/losses.py defines FocalLoss and build_loss, which makes it the loss definition evidence.
src/pkg/metrics.py defines MetricAccumulator and iou_score, which makes it the metric evaluation evidence.
experiments/run0/metrics_test.json stores concrete result fields iou, accuracy, and loss.
"""
        + "Grounded repository discussion with concrete evidence. " * 140
    )

    quality = _quality(report, _analysis_context(), _contract(), context_pack=_context_pack())

    assert quality["ok"] is True
    assert quality["warnings"] == []
    evidence_quality = quality["evidence_quality"]
    assert evidence_quality["ok"] is True
    assert evidence_quality["missing_symbol_mentions"] == []
    assert evidence_quality["missing_structured_field_mentions"] == []


def test_analysis_quality_v2_accepts_same_role_workflow_path_as_alternate_representative():
    context_pack = _context_pack()
    context_pack["selected_files"] = [
        {"path": "scripts/train_cpu.sh", "roles": {"run_workflow": 90}},
        {"path": "scripts/train_gpu.sh", "roles": {"run_workflow": 88}},
    ]
    context_pack["evidence_blocks"] = list(context_pack["evidence_blocks"]) + [
        {
            "path": "scripts/train_cpu.sh",
            "ok": True,
            "evidence_selection": ["role_representative:run_workflow"],
            "symbols": {},
            "content": "1: python -m pkg.train --config configs/cpu.json",
        }
    ]
    report = (
        _headings()
        + """
src/pkg/data.py defines BatchDataset and build_loader, which makes it the data pipeline evidence.
src/pkg/models.py defines TinyModel and build_model, which makes it the model definition evidence.
src/pkg/losses.py defines FocalLoss and build_loss, which makes it the loss definition evidence.
src/pkg/metrics.py defines MetricAccumulator and iou_score, which makes it the metric evaluation evidence.
experiments/run0/metrics_test.json stores concrete result fields iou, accuracy, and loss.
scripts/train_gpu.sh is the cited workflow script for running the same training family.
"""
        + "Grounded repository discussion with concrete evidence. " * 140
    )

    quality = _quality(report, _analysis_context(), _contract(), context_pack=context_pack)

    assert quality["ok"] is True
    workflow_items = [
        item
        for item in quality["evidence_quality"]["path_required"]
        if item["role"] == "run_workflow"
    ]
    assert workflow_items[0]["satisfied_by_alternate_path"] is True
    assert workflow_items[0]["alternate_hits"] == ["scripts/train_gpu.sh"]


def test_analysis_quality_v2_uses_repo_role_assignments_for_workflow_alternates():
    context_pack = _context_pack()
    context_pack["selected_files"] = [
        {"path": "scripts/train_cpu.sh", "roles": {"run_workflow": 90}},
    ]
    context_pack["evidence_blocks"] = list(context_pack["evidence_blocks"]) + [
        {
            "path": "scripts/train_cpu.sh",
            "ok": True,
            "evidence_selection": ["role_representative:run_workflow"],
            "symbols": {},
            "content": "1: python -m pkg.train --config configs/cpu.json",
        }
    ]
    analysis_context = _analysis_context()
    analysis_context["role_coverage_after"] = {
        "coverage_ratio": 1.0,
        "missing_roles": [],
        "role_assignments": {
            "run_workflow": ["scripts/train_cpu.sh", "scripts/train_gpu.sh"],
        },
    }
    report = (
        _headings()
        + """
src/pkg/data.py defines BatchDataset and build_loader, which makes it the data pipeline evidence.
src/pkg/models.py defines TinyModel and build_model, which makes it the model definition evidence.
src/pkg/losses.py defines FocalLoss and build_loss, which makes it the loss definition evidence.
src/pkg/metrics.py defines MetricAccumulator and iou_score, which makes it the metric evaluation evidence.
experiments/run0/metrics_test.json stores concrete result fields iou, accuracy, and loss.
scripts/train_gpu.sh is the cited workflow script discovered in repository role assignments.
"""
        + "Grounded repository discussion with concrete evidence. " * 140
    )

    quality = _quality(report, analysis_context, _contract(), context_pack=context_pack)

    assert quality["ok"] is True
    workflow_items = [
        item
        for item in quality["evidence_quality"]["path_required"]
        if item["role"] == "run_workflow"
    ]
    assert workflow_items[0]["alternate_hits"] == ["scripts/train_gpu.sh"]


def test_analysis_quality_v2_still_requires_exact_core_source_path_mentions():
    report = (
        _headings()
        + """
src/pkg/data.py defines BatchDataset and build_loader, which makes it the data pipeline evidence.
TinyModel and build_model are discussed, but the model source path is not cited.
src/pkg/losses.py defines FocalLoss and build_loss, which makes it the loss definition evidence.
src/pkg/metrics.py defines MetricAccumulator and iou_score, which makes it the metric evaluation evidence.
experiments/run0/metrics_test.json stores concrete result fields iou, accuracy, and loss.
"""
        + "Grounded repository discussion with concrete evidence. " * 140
    )

    quality = _quality(report, _analysis_context(), _contract(), context_pack=_context_pack())

    assert quality["ok"] is False
    assert "report missing required evidence path mentions" in quality["warnings"]
    assert {
        "role": "model_definition",
        "path": "src/pkg/models.py",
    } in quality["evidence_quality"]["missing_path_mentions"]


def test_analysis_quality_v2_does_not_fail_missing_tests_role_without_test_evidence():
    analysis_context = _analysis_context()
    analysis_context["role_coverage_after"] = {
        "coverage_ratio": 0.9,
        "missing_roles": ["tests"],
        "role_assignments": {},
    }
    analysis_context["compact_repo_map"] = {"has_tests": False, "candidates_by_role": {}}
    report = (
        _headings()
        + """
src/pkg/data.py defines BatchDataset and build_loader, which makes it the data pipeline evidence.
src/pkg/models.py defines TinyModel and build_model, which makes it the model definition evidence.
src/pkg/losses.py defines FocalLoss and build_loss, which makes it the loss definition evidence.
src/pkg/metrics.py defines MetricAccumulator and iou_score, which makes it the metric evaluation evidence.
experiments/run0/metrics_test.json stores concrete result fields iou, accuracy, and loss.
"""
        + "Grounded repository discussion with concrete evidence. " * 140
    )

    quality = _quality(report, analysis_context, _contract(), context_pack=_context_pack())

    assert quality["ok"] is True
    assert quality["missing_roles"] == []
    assert quality["raw_missing_roles"] == ["tests"]


def test_analysis_quality_v2_scales_coverage_and_path_gate_to_small_repository():
    analysis_context = {
        "selected_files": ["README.md", "converter.py"],
        "role_coverage_after": {
            "coverage_ratio": 0.3,
            "missing_roles": [
                "data_pipeline",
                "model_definition",
                "loss_definition",
                "metric_evaluation",
                "run_workflow",
                "results_or_outputs",
                "tests",
            ],
            "role_assignments": {
                "project_overview": ["README.md"],
                "entrypoint": ["converter.py"],
                "config_or_arguments": ["converter.py"],
            },
        },
        "compact_repo_map": {
            "has_tests": False,
            "candidates_by_role": {
                "project_overview": [{"path": "README.md"}],
                "entrypoint": [{"path": "converter.py"}],
                "config_or_arguments": [{"path": "converter.py"}],
            },
        },
        "evidence_index": {},
    }
    context_pack = {
        "evidence_blocks": [
            {
                "path": "README.md",
                "ok": True,
                "evidence_selection": ["role_representative:project_overview"],
                "symbols": {},
                "content": "Temperature Converter usage documentation.",
            },
            {
                "path": "converter.py",
                "ok": True,
                "evidence_selection": ["role_representative:entrypoint"],
                "symbols": {
                    "functions": ["celsius_to_fahrenheit", "fahrenheit_to_celsius", "main"],
                    "classes": [],
                },
                "content": "def celsius_to_fahrenheit(value): ...\ndef main(): ...",
            },
        ]
    }
    report = (
        _headings()
        + """
README.md documents the public command-line usage and the repository purpose.
converter.py provides main, celsius_to_fahrenheit, and fahrenheit_to_celsius as the core conversion logic.
"""
        + "Grounded repository discussion with concrete evidence. " * 140
    )

    quality = _quality(report, analysis_context, _contract(), context_pack=context_pack)

    assert quality["ok"] is True
    assert quality["raw_coverage_ratio"] == 0.3
    assert quality["coverage_ratio"] == 1.0
    assert quality["relevant_roles"] == ["project_overview", "entrypoint", "config_or_arguments"]
    assert quality["required_path_mentions"] == 2
    assert quality["path_mentions_count"] == 2
    assert quality["weak_evidence"] is False


def test_final_gate_rejects_analysis_quality_warnings():
    state = {
        "mode": "analyze",
        "read_only": True,
        "write_locked": True,
        "verification": {"ok": True, "analysis_ok": True, "quality_warnings": ["report too shallow"]},
        "analysis_quality": {"ok": True, "warnings": ["report too shallow"]},
        "changed_files": [],
        "generated_files": [],
        "repair_history": [],
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "analysis_quality_warnings" in gate["failures"]
    assert gate["stopped_reason"] == "analysis_quality_failed"
