from pathlib import Path

from coding_agent.workspace.artifacts import build_artifact_registry
from coding_agent.nodes.final_gate import compute_final_gate
from coding_agent.nodes.failure_owner import _heuristic_decision
from coding_agent.repair.failure_analysis import summarize_issue_owners


def test_artifact_registry_marks_agent_generated_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_x():\n    assert 1 == 1\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "repo_map": {"files": ["tests/test_main.py"], "py_files": ["tests/test_main.py"]},
        "file_plan": {"files": [{"path": "tests/test_main.py", "kind": "test", "purpose": "generated tests"}]},
        "generated_files": [{"path": "tests/test_main.py", "ok": True}],
        "changed_files": ["tests/test_main.py"],
    }
    reg = build_artifact_registry(state)
    assert reg["agent_generated_tests"] == ["tests/test_main.py"]
    assert reg["by_path"]["tests/test_main.py"]["modifiable_by_agent"] is True


def test_final_gate_rejects_done_when_verification_failed():
    state = {
        "mode": "write",
        "stopped_reason": "done",
        "verification": {"ok": False},
        "needs_verification": True,
        "contract_ok": True,
        "changed_files": ["script.py"],
    }
    gate = compute_final_gate(state)
    assert gate["ok"] is False
    assert "verification_failed" in gate["failures"]
    assert gate["stopped_reason"] != "done"


def test_failure_owner_heuristic_does_not_guess_ambiguous_test_oracle(tmp_path):
    state = {
        "failure": {"failure_type": "test_assertion_error", "target_file": "tests/test_script.py"},
    }
    registry = {
        "by_path": {
            "tests/test_script.py": {"path": "tests/test_script.py", "is_test": True, "origin": "agent_generated_or_modified"}
        }
    }
    files = {"tests/test_script.py": "# Only 1 + 0.5 = 1.5 hours\nassert result == 2.5\n"}
    decision = _heuristic_decision(state, registry, files)
    assert decision.failure_owner == "unknown"
    assert decision.allowed_to_modify_tests is False
    assert decision.strategy == "inspect_more"
    assert decision.test_oracle_status == "ambiguous_requirement"


def test_owner_summary_ignores_unlocated_traceback_duplicates_for_generated_tests():
    issues = [
        {
            "owner": "unknown",
            "type": "assertionerror",
            "file": None,
            "message": "AssertionError: assert 7.0 == 8.0",
            "source": "traceback_parser",
        },
        {
            "owner": "generated_test",
            "type": "assertionerror",
            "file": "tests/test_tool.py",
            "message": "AssertionError: assert 7.0 == 8.0",
            "source": "run_tests_junit",
        },
    ]

    assert summarize_issue_owners(issues) == "generated_test"
