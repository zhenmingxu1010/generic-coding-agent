from pathlib import Path

from coding_agent.scope.write_guard import review_file_plan, is_protected_existing_file
from coding_agent.scope.write_scope import build_write_scope_policy
from coding_agent.nodes.file_plan import file_plan_node


def _base_state(tmp_path: Path, task: str):
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True, exist_ok=True)
    return {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "task": task,
        "mode": "write",
        "read_only": False,
        "task_contract": {"expected_artifacts": ["requested_code_files", "tests"]},
        "repo_map": {"files": []},
    }


def test_existing_summary_json_is_protected_reference(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "service_summary.json").write_text('{"experiments": []}\n', encoding="utf-8")
    task = "新增 scripts/analyze.py。脚本应读取 data/service_summary.json。"
    state = _base_state(tmp_path, task)
    state["write_scope_policy"] = build_write_scope_policy(task, "write", False, None)
    plan = {
        "files": [
            {"path": "scripts/analyze.py", "kind": "code", "purpose": "new script"},
            {"path": "data/service_summary.json", "kind": "data", "purpose": "input summary"},
        ]
    }
    review = review_file_plan(state, plan)
    assert any(x["path"] == "scripts/analyze.py" for x in review["writable_files"])
    assert any(x["path"] == "data/service_summary.json" for x in review["read_reference_files"])
    assert not any(x["path"] == "data/service_summary.json" for x in review["writable_files"])


def test_protected_existing_result_file_detection():
    assert is_protected_existing_file("experiments/run1/metrics_test.json")
    assert is_protected_existing_file("results/summary.csv")
    assert is_protected_existing_file("checkpoints/best.pt")
    assert not is_protected_existing_file("scripts/summarize_metrics.py")


def test_file_plan_filters_mentioned_input_json_from_outputs(tmp_path: Path, monkeypatch):
    # Force fallback plan by making the LLM client fail, then ensure user-mentioned
    # reference JSON is not auto-appended as a generated artifact.
    task = "新增 scripts/summarize_metrics.py。脚本应读取 data/service_summary.json。"
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "service_summary.json").write_text('{"experiments": []}\n', encoding="utf-8")
    state = _base_state(tmp_path, task)

    class BadClient:
        def __init__(self, *a, **kw):
            pass
        def chat(self, *a, **kw):
            raise RuntimeError("no llm")

    monkeypatch.setattr("coding_agent.nodes.file_plan.OpenAICompatClient", BadClient)
    out = file_plan_node(state)
    paths = [f["path"] for f in out["file_plan"]["files"]]
    assert "data/service_summary.json" not in paths
