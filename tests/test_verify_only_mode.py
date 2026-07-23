from __future__ import annotations

from pathlib import Path

from coding_agent.graph import is_analysis_task, route_after_context, route_after_tool, route_after_verify
from coding_agent.nodes.final_gate import compute_final_gate
from coding_agent.nodes.supervisor import _should_force_verify_only
from coding_agent.nodes.verify import _default_commands


def test_semantic_verification_without_write_intent_routes_to_verify_only():
    task_intent = {
        "create_requested": False,
        "fix_requested": False,
        "modify_requested": False,
        "source_modify_intent": False,
        "auxiliary_create_intent": False,
        "semantic_write_scope": {
            "source_modification_allowed": False,
            "existing_file_modification_allowed": False,
            "create_paths": [],
            "allowed_modify_paths": [],
        },
        "scope_contract": {"allowed_modify_paths": [], "allowed_create_paths": []},
    }

    assert _should_force_verify_only({"requires_verification": True, "read_only": True}, task_intent) is True


def test_semantic_verification_with_write_intent_does_not_become_verify_only():
    task_intent = {
        "create_requested": False,
        "fix_requested": True,
        "modify_requested": False,
        "source_modify_intent": True,
        "auxiliary_create_intent": False,
        "semantic_write_scope": {"source_modification_allowed": True},
        "scope_contract": {"allowed_modify_paths": ["src/app.py"]},
    }

    assert _should_force_verify_only({"requires_verification": True}, task_intent) is False


def test_run_verify_is_not_routed_as_readonly_analysis():
    state = {
        "mode": "run_verify",
        "read_only": True,
        "write_locked": True,
        "task_spec": {"task_type": "analyze"},
        "task_intent": {"operation_mode": "verify_only"},
    }

    assert is_analysis_task(state) is False
    assert route_after_context(state) == "verify"

    state["verification"] = {"ok": False}
    assert route_after_context(state) == "report"
    assert route_after_verify(state) == "report"
    assert state["stopped_reason"] == "verification_failed"
    assert state["needs_verification"] is False


def test_verify_only_defaults_to_pytest_collection_even_without_tests(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(tmp_path / ".coding_agent" / "t"),
        "thread_id": "t",
        "mode": "run_verify",
        "read_only": True,
        "task_intent": {"operation_mode": "verify_only"},
    }

    commands = _default_commands(state)

    assert ("pytest", ["python", "-m", "pytest", "-q"]) in commands


def test_arbitrary_suggested_commands_are_not_an_executable_verification_source(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(tmp_path / ".coding_agent" / "t"),
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
        "task_contract": {
            "expected_artifacts": ["requested_code_files"],
            "suggested_verify_commands": [["python", "-m", "pytest", "-q", "tests/"]],
        },
        "task_spec": {
            "suggested_verify_commands": [["python", "main.py", "--untrusted-suggestion"]],
        },
        "plan": {
            "verification": {
                "commands": [["echo 'x' > /outside_agent_input.txt"]],
            }
        },
    }

    commands = _default_commands(state)

    assert commands == [("py_compile", ["python", "-m", "compileall", "-q", "."])]
    assert "skipped_verify_commands" not in state


def test_finish_after_failed_verification_reports_instead_of_repeating_verify():
    state = {
        "mode": "generate_project",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 5,
        "needs_verification": True,
        "verification": {"ok": False},
        "last_tool_result": {
            "tool": "finish",
            "ok": True,
            "data": {"message": "done"},
        },
    }

    route = route_after_tool(state)

    assert route == "report"
    assert state["stopped_reason"] == "finish_requested"


def test_final_gate_rejects_verify_only_zero_collected_without_analysis_quality():
    state = {
        "mode": "run_verify",
        "read_only": True,
        "verification": {
            "ok": False,
            "results": [
                {
                    "name": "pytest",
                    "command": ["python", "-m", "pytest", "-q"],
                    "returncode": 5,
                    "stdout": "collected 0 items\n",
                    "stderr": "",
                    "timed_out": False,
                }
            ],
            "test_results": {
                "version": "run_tests_v1",
                "ok": False,
                "runs": [{"name": "pytest", "ok": False, "total": 0, "passed": 0, "failed": 0, "errors": 0}],
                "total": 0,
                "passed": 0,
                "failed": 0,
                "errors": 0,
            },
        },
        "test_results": {
            "version": "run_tests_v1",
            "ok": False,
            "runs": [{"name": "pytest", "ok": False, "total": 0, "passed": 0, "failed": 0, "errors": 0}],
            "total": 0,
        },
        "changed_files": [],
        "generated_files": [],
        "repair_history": [],
        "requirement_atom_summary": {"required_total": 0, "required_failed": 0, "required_unverified": 0},
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "verification_failed" in gate["failures"]
    assert "pytest_zero_tests_collected" in gate["failures"]
    assert "analysis_quality_failed" not in gate["failures"]
    assert gate["stopped_reason"] == "pytest_zero_tests_collected"
