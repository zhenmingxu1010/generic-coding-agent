from __future__ import annotations

from pathlib import Path

from coding_agent.nodes.tool_exec import _redirect_external_test_edit_to_generated_test
from coding_agent.verification.test_path_policy import normalize_generated_test_write_path
from coding_agent.scope.write_intent import can_execute_write_intent


def test_external_test_edit_is_redirected_to_new_generated_test(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    original = "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    (tests_dir / "test_calculator.py").write_text(original, encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "mode": "modify",
        "read_only": False,
        "task": "Add divide(a,b) and verify with pytest.",
        "task_contract": {"expected_artifacts": ["tests"]},
        "repo_map": {"files": ["calculator.py", "tests/test_calculator.py"]},
        "test_policy": {"generate_internal_tests": True},
    }
    tool, args, info = _redirect_external_test_edit_to_generated_test(
        "edit_file",
        state,
        {
            "path": "tests/test_calculator.py",
            "old_text": original.rstrip("\n"),
            "new_text": original + "\n\ndef test_divide():\n    assert divide(4, 2) == 2\n",
            "expected_replacements": 1,
        },
    )

    assert tool == "write_file"
    assert args["path"] == ".coding_agent_test/default/tests/test_calculator.py"
    assert "test_divide" in args["content"]
    assert info["source_path"] == "tests/test_calculator.py"


def test_modify_mode_allows_new_generated_test_without_write_intent(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    state = {
        "workspace": str(tmp_path),
        "mode": "modify",
        "read_only": False,
        "task": "Add divide(a,b) and verify with pytest.",
        "task_contract": {"expected_artifacts": ["tests"]},
        "test_policy": {"generate_internal_tests": True},
    }

    ok, reason, detail = can_execute_write_intent(state, ".coding_agent_test/default/tests/test_calculator_generated.py", exists=False)

    assert ok is True
    assert "new test file allowed" in reason
    assert detail["implicit_new_test"] is True


def test_current_agent_generated_test_can_be_rewritten_in_place(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_timecalc.py").write_text("def test_old():\n    assert False\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "mode": "generate_project",
        "read_only": False,
        "task": "Create a small CLI project and pytest tests.",
        "task_contract": {"expected_artifacts": ["tests"]},
        "file_plan": {"files": [{"path": "tests/test_timecalc.py", "kind": "test"}]},
        "generated_files": [{"path": "tests/test_timecalc.py", "kind": "test"}],
        "repo_map": {"files": ["timecalc.py", "tests/test_timecalc.py"]},
        "test_policy": {"generate_internal_tests": True},
    }

    path, info = normalize_generated_test_write_path(state, "tests/test_timecalc.py")

    assert path == ".coding_agent_test/default/tests/test_timecalc.py"
    assert info is not None
    assert info["original_path"] == "tests/test_timecalc.py"
