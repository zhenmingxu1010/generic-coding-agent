from __future__ import annotations

from pathlib import Path

from coding_agent.contracts.contract import scan_workspace_contract
from coding_agent.scope.task_intent import classify_task_intent
from coding_agent.scope.write_intent import build_write_intents
from coding_agent.workspace.interface_check import run_interface_consistency_check
from coding_agent.workspace.run_paths import apply_output_layout, agent_test_root_rel, is_agent_test_path


def test_existing_project_layout_keeps_deliverables_visible_and_tests_internal(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t02",
        "mode": "write",
        "task": "Create scripts/report.py and tests/test_report.py without modifying existing project files.",
        "repo_map": {"files": ["src/existing.py"]},
        "test_policy": {"generate_internal_tests": True},
    }
    files = [
        {"path": "scripts/report.py", "kind": "code"},
        {"path": "tests/test_report.py", "kind": "test"},
    ]

    mapped, layout = apply_output_layout(state, files)

    assert [item["path"] for item in mapped] == [
        "scripts/report.py",
        ".coding_agent_test/t02/tests/test_report.py",
    ]
    assert layout["deliverables"] == [{"path": "scripts/report.py", "kind": "code"}]
    assert layout["agent_tests"] == [
        {"path": ".coding_agent_test/t02/tests/test_report.py", "original_path": "tests/test_report.py"}
    ]
    assert is_agent_test_path(".coding_agent_test/t02/tests/test_report.py", state=state)


def test_greenfield_project_layout_keeps_agent_tests_internal(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "thread_id": "greenfield",
        "mode": "generate_project",
        "task_spec": {"task_type": "generate_project"},
        "test_policy": {"generate_internal_tests": True},
    }
    files = [
        {"path": "main.py", "kind": "code"},
        {"path": "tests/test_main.py", "kind": "test"},
    ]

    mapped, layout = apply_output_layout(state, files)

    assert [item["path"] for item in mapped] == [
        "main.py",
        ".coding_agent_test/greenfield/tests/test_main.py",
    ]
    assert layout["agent_tests"] == [
        {"path": ".coding_agent_test/greenfield/tests/test_main.py", "original_path": "tests/test_main.py"}
    ]
    assert layout["generated_tests_deliverable"] is False


def test_write_intents_use_new_layout_paths_without_workdir_mapping(tmp_path: Path):
    task = "Create scripts/report.py and tests/test_report.py. Do not modify src/original.py."
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "original.py").write_text("VALUE = 1\n", encoding="utf-8")
    intent = classify_task_intent(task, {"read_only": False})
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t02",
        "mode": "write",
        "read_only": False,
        "task": task,
        "task_intent": intent,
        "scope_contract": intent["scope_contract"],
        "test_policy": {"generate_internal_tests": True},
    }
    plan_files, _layout = apply_output_layout(
        state,
        [
            {"path": "scripts/report.py", "kind": "code"},
            {"path": "tests/test_report.py", "kind": "test"},
        ],
    )

    intents = build_write_intents(state, {"files": plan_files})

    assert "scripts/report.py" in intents["allowed_write_paths"]
    assert "tests/test_report.py" in intents["allowed_write_paths"]
    assert ".coding_agent_test/t02/tests/test_report.py" not in intents["allowed_write_paths"]
    assert all(not path.startswith(".coding_agent/") for path in intents["allowed_write_paths"])
    assert "src/original.py" in intents["blocked_write_paths"]


def test_contract_scan_ignores_agent_run_records_but_keeps_current_internal_tests(tmp_path: Path):
    (tmp_path / ".coding_agent" / "t02" / "records").mkdir(parents=True)
    (tmp_path / ".coding_agent" / "t02" / "records" / "old.py").write_text("print('old')\n", encoding="utf-8")
    test_root = tmp_path / agent_test_root_rel("t02")
    (test_root / "tests").mkdir(parents=True)
    (test_root / "tests" / "test_report.py").write_text("def test_report():\n    assert True\n", encoding="utf-8")
    state = {"thread_id": "t02", "test_policy": {"generate_internal_tests": True}}

    info = scan_workspace_contract(str(tmp_path), state)

    assert ".coding_agent/t02/records/old.py" not in info["py_files"]
    assert ".coding_agent_test/t02/tests/test_report.py" in info["py_files"]
    assert ".coding_agent_test/t02/tests/test_report.py" in info["test_files"]


def test_interface_check_uses_internal_generated_tests_against_visible_code(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "report.py").write_text("def build_report():\n    return 'ok'\n", encoding="utf-8")
    test_root = tmp_path / agent_test_root_rel("t02")
    (test_root / "tests").mkdir(parents=True)
    (test_root / "tests" / "test_report.py").write_text(
        "from scripts.report import build_report\n\n"
        "def test_report():\n"
        "    assert build_report() == 'ok'\n",
        encoding="utf-8",
    )
    state = {"thread_id": "t02"}

    result = run_interface_consistency_check(str(tmp_path), state)

    assert result["ok"] is True
    assert result["checked_tests"] == [".coding_agent_test/t02/tests/test_report.py"]
