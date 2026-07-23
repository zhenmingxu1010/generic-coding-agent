from pathlib import Path

from coding_agent.nodes.file_plan import _required_create_targets
from coding_agent.memory.artifact_provenance import load_artifact_provenance, record_artifact_event
from coding_agent.workspace.artifacts import build_artifact_registry
from coding_agent.scope.write_guard import review_file_plan
from coding_agent.scope.write_scope import build_write_scope_policy


def test_required_create_targets_distinguish_script_from_input_summary():
    task = (
        "新增一个只读分析脚本 scripts/summarize_metrics.py。"
        "脚本应读取 data/service_summary.json。"
        "写一个最小 pytest 或 smoke test。"
    )
    targets = _required_create_targets(task, {"expected_artifacts": ["tests"]})
    paths = [x["path"] for x in targets]
    assert "scripts/summarize_metrics.py" in paths
    assert "tests/test_summarize_metrics.py" not in paths
    assert "data/service_summary.json" not in paths


def test_historical_agent_artifact_is_modifiable_but_summary_is_reference(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "scripts" / "summarize_metrics.py").write_text("print('old')\n", encoding="utf-8")
    (tmp_path / "data" / "service_summary.json").write_text("{}\n", encoding="utf-8")
    record_artifact_event(
        tmp_path,
        path="scripts/summarize_metrics.py",
        thread_id="old_thread",
        task="old agent generated script",
        action="write_file",
        origin="agent_generated",
        kind="code",
    )
    state = {
        "workspace": str(tmp_path),
        "task": "新增 scripts/summarize_metrics.py，读取 data/service_summary.json。",
        "mode": "write",
        "read_only": False,
        "repo_map": {"files": ["scripts/summarize_metrics.py", "data/service_summary.json"]},
        "file_plan": {
            "files": [
                {"path": "scripts/summarize_metrics.py", "kind": "code", "purpose": "target script"},
                {"path": "data/service_summary.json", "kind": "data", "purpose": "input summary"},
            ]
        },
        "generated_files": [],
        "changed_files": [],
    }
    state["write_scope_policy"] = build_write_scope_policy(state["task"], state["mode"], False, state["file_plan"])
    registry = build_artifact_registry(state)
    script = registry["by_path"]["scripts/summarize_metrics.py"]
    assert script["historical_agent_artifact"] is True
    assert script["modifiable_by_agent"] is True
    review = review_file_plan(state, state["file_plan"])
    writable = [x["path"] for x in review["writable_files"]]
    read_refs = [x["path"] for x in review["read_reference_files"]]
    assert "scripts/summarize_metrics.py" in writable
    assert "data/service_summary.json" in read_refs


def test_load_artifact_provenance_ignores_legacy_state_snapshot(tmp_path):
    run = tmp_path / ".coding_agent" / "old_thread"
    run.mkdir(parents=True)
    (run / "state_snapshot.json").write_text(
        '{"thread_id":"old_thread","task":"x","generated_files":[{"path":"scripts/foo.py"}],"changed_files":["tests/test_foo.py"]}',
        encoding="utf-8",
    )
    store = load_artifact_provenance(tmp_path)
    assert store["artifacts"] == {}
