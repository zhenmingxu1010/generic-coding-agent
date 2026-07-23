from pathlib import Path

from coding_agent.workspace.repo_map import build_repository_map
from coding_agent.memory.project_memory import (
    load_project_memory,
    update_project_memory_from_repo,
    update_project_memory_from_analysis,
    memory_paths,
    compact_project_memory_for_prompt,
)
from coding_agent.nodes.analyze_repo import _build_evidence_index
from coding_agent.nodes.analyze_report import _quality


def test_project_memory_persists_role_files(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "metrics.py").write_text("def iou_score(): pass\ndef thickness_mae(): pass\n", encoding="utf-8")
    (tmp_path / "run.py").write_text("import argparse\nif __name__ == '__main__': pass\n", encoding="utf-8")
    repo_map = build_repository_map(str(tmp_path))
    state = {"workspace": str(tmp_path), "repo_map": repo_map, "mode": "analyze", "read_only": True}
    profile = update_project_memory_from_repo(state)
    paths = memory_paths(tmp_path)
    assert paths["profile_json"].exists()
    loaded = load_project_memory(tmp_path)
    assert loaded["file_count"] >= 2
    assert loaded["role_files"]["metric_evaluation"]
    assert "run.py" in loaded["known_entrypoints"]
    compact = compact_project_memory_for_prompt(loaded)
    assert "metric_evaluation" in compact


def test_update_project_memory_from_analysis_extracts_evidence_facts(tmp_path: Path):
    (tmp_path / "metrics.py").write_text("def iou(): pass\n", encoding="utf-8")
    repo_map = build_repository_map(str(tmp_path))
    state = {
        "workspace": str(tmp_path),
        "repo_map": repo_map,
        "run_dir": str(tmp_path / ".coding_agent" / "t1"),
        "thread_id": "t1",
        "mode": "analyze",
        "task": "analyze metrics",
        "repo_analysis_context": {"selected_files": ["metrics.py"], "role_coverage_after": {"coverage_ratio": 0.5}},
        "analysis_quality": {"ok": True},
        "analysis_report": "metrics.py defines the concrete IoU metric function and should be used as evidence.",
    }
    profile = update_project_memory_from_analysis(state)
    assert profile["analysis_runs"]
    assert any("metrics.py" in item.get("evidence", []) for item in profile.get("stable_facts", []))


def test_evidence_index_extracts_metric_terms():
    read_result = {"data": {"files": [{"path": "metrics.py", "ok": True, "content": "def iou_score(): pass\ndef thickness_mae(): pass\n"}]}}
    repo_map = {"records": [{"path": "metrics.py", "roles": {"metric_evaluation": 60}, "symbols": {"functions": ["iou_score", "thickness_mae"], "classes": []}}]}
    index = _build_evidence_index(read_result, repo_map)
    terms = index["metric_or_domain_terms_by_file"]["metrics.py"]
    assert "iou_score" in terms
    assert "thickness_mae" in terms


def test_analysis_quality_flags_ungrounded_generic_metrics():
    ctx = {
        "selected_files": ["metrics.py", "train.py", "README.md"],
        "role_coverage_after": {"coverage_ratio": 1.0, "missing_roles": []},
        "evidence_index": {"metric_or_domain_terms_by_file": {"metrics.py": ["iou_score", "thickness_mae"]}},
    }
    report = """# Repository Analysis Report
## 1. Overall Purpose
metrics.py train.py README.md evidence. """ + "x" * 3000 + "\n## 2. Project Type and Evidence Coverage\n## 3. Directory Structure\n## 4. Main Entry Points\n## 5. Data Flow and Inputs/Outputs\n## 6. Model / Core Logic\n## 7. Losses / Metrics / Evaluation\nThe metrics include precision, recall, and F1-score.\n## 8. Scripts / Workflow / Execution\n## 9. Configs, Results, and Artifacts\n## 10. Risks, Gaps, and Next Checks\n## 11. File Responsibility Table\n"
    q = _quality(report, ctx)
    assert q["generic_metric_claims"]
    assert q["ok"] is False
