from pathlib import Path

from coding_agent.contracts.contract import extract_task_contract
from coding_agent.nodes.file_plan import file_plan_node
from coding_agent.nodes.final_gate import compute_final_gate
from coding_agent.scope.read_only_policy import detect_global_read_only_lock
from coding_agent.scope.read_only_policy import has_explicit_current_write_intent
from coding_agent.scope.task_intent import classify_task_intent


def _state(tmp_path: Path) -> dict:
    run_dir = tmp_path / ".agent_runs" / "t"
    run_dir.mkdir(parents=True, exist_ok=True)
    return {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state_snapshot.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "thread_id": "t",
        "mode": "analyze",
        "read_only": True,
        "write_locked": True,
        "read_only_policy": {"version": "v1.20", "locked": True},
        "repo_map": {"files": [{"path": "src/example.py"}]},
    }


def test_deep_readonly_prompt_locks_writes():
    task = (
        "This is an existing project. This run is read-only analysis only. "
        "Do not create, modify, or delete any files. Do not write README, "
        "summary, scripts, tests, cache files, or run training. Read src, "
        "scripts, experiments, metrics, and tests, then produce an analysis report."
    )
    policy = detect_global_read_only_lock(task)
    assert policy["locked"] is True

    intent = classify_task_intent(task, {"task_type": "analyze", "read_only": True})
    assert intent["mode"] == "analyze"
    assert intent["operation_mode"] == "read_only_analysis"
    assert intent["agent_read_only"] is True
    assert intent["write_locked"] is True
    assert intent["create_requested"] is False
    assert intent["modify_requested"] is False
    assert intent["create_paths"] == []


def test_safe_create_with_explicit_targets_does_not_lock():
    task = (
        "This is an existing project. The agent may create new files but must "
        "not modify existing project files. Create scripts/summarize_metrics.py "
        "and tests/test_summarize_metrics.py."
    )
    policy = detect_global_read_only_lock(task)
    assert policy["locked"] is False
    assert policy["explicit_current_write_intent"] is True

    intent = classify_task_intent(task)
    assert intent["mode"] == "write"
    assert intent["operation_mode"] == "safe_create"
    assert intent["write_locked"] is False
    assert intent["agent_read_only"] is False
    assert "scripts/summarize_metrics.py" in intent["create_paths"]
    assert "tests/test_summarize_metrics.py" in intent["create_paths"]


def test_safe_create_prompt_with_script_runtime_readonly_is_write():
    task = (
        "This is an existing project. The new script should only read input data "
        "at runtime, but the agent may create scripts/summarize_metrics.py "
        "and tests/test_summarize_metrics.py. Read the project first. "
        "The script reads data/service_summary.json and "
        "falls back to data/runs/*.json. Support --input and "
        "--output-csv."
    )

    assert has_explicit_current_write_intent(task) is True
    policy = detect_global_read_only_lock(task)
    assert policy["locked"] is False
    assert policy["explicit_current_write_intent"] is True

    intent = classify_task_intent(task)
    assert intent["mode"] == "write"
    assert intent["operation_mode"] == "safe_create"
    assert intent["agent_read_only"] is False
    assert intent["write_locked"] is False
    assert intent["script_read_only"] is True
    assert intent["scan_first"] is True
    assert intent["create_requested"] is True
    assert intent["create_paths"] == [
        "scripts/summarize_metrics.py",
        "tests/test_summarize_metrics.py",
    ]
    assert "data/service_summary.json" in intent["read_reference_paths"]


def test_readonly_contract_does_not_request_tests_or_code_artifacts():
    task = (
        "Read-only project analysis. Do not create, modify, or delete files. "
        "Read src, scripts, experiments, metrics, and tests as references."
    )
    contract = extract_task_contract(
        task,
        {"task_type": "analyze", "read_only": True},
        {"mode": "analyze", "read_only": True, "write_locked": True},
    )
    assert contract["read_only"] is True
    assert "requested_code_files" not in contract["expected_artifacts"]
    assert "tests" not in contract["expected_artifacts"]
    assert "pytest_if_tests_exist" not in contract["verification_gates"]
    assert "suggested_verify_commands" not in contract


def test_file_plan_skips_when_write_locked(tmp_path: Path):
    state = _state(tmp_path)
    out = file_plan_node(state)
    assert out["file_plan"]["files"] == []
    assert out["file_plan_review"]["skipped"] is True
    assert out["needs_verification"] is False


def test_final_gate_fails_readonly_violation():
    state = {
        "mode": "analyze",
        "read_only": True,
        "write_locked": True,
        "analysis_quality": {"ok": True},
        "changed_files": ["scripts/should_not_exist.py"],
        "generated_files": [],
        "repair_history": [],
    }
    gate = compute_final_gate(state)
    assert gate["ok"] is False
    assert "read_only_violation" in gate["failures"]
    assert gate["stopped_reason"] == "read_only_violation"
