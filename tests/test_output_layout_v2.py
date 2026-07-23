from __future__ import annotations

from pathlib import Path

from coding_agent.nodes.verify import _default_commands, _pytest_targets_for_command
from coding_agent.workspace.artifacts import build_artifact_registry
from coding_agent.workspace.run_paths import apply_output_layout, project_memory_dir_for, run_dir_for


def test_existing_project_generated_tests_are_internal_and_code_is_delivered():
    state = {
        "workspace": "/workspace/demo",
        "thread_id": "thread1",
        "mode": "write",
        "task_intent": {},
        "test_policy": {"generate_internal_tests": True},
    }
    files = [
        {"path": "scripts/read_summary.py", "kind": "code"},
        {"path": "tests/test_read_summary.py", "kind": "test"},
    ]

    planned, layout = apply_output_layout(state, files)

    assert planned[0]["path"] == "scripts/read_summary.py"
    assert planned[0]["user_visible"] is True
    assert planned[1]["path"] == ".coding_agent_test/thread1/tests/test_read_summary.py"
    assert planned[1]["agent_internal"] is True
    assert planned[1]["original_path"] == "tests/test_read_summary.py"
    assert layout["agent_test_root"] == ".coding_agent_test/thread1"


def test_greenfield_project_agent_tests_are_internal():
    state = {
        "workspace": "/workspace/new_project",
        "thread_id": "thread2",
        "mode": "generate_project",
        "task_intent": {},
        "test_policy": {"generate_internal_tests": True},
    }
    files = [
        {"path": "src/app.py", "kind": "code"},
        {"path": "tests/test_app.py", "kind": "test"},
    ]

    planned, layout = apply_output_layout(state, files)

    assert [item["path"] for item in planned] == [
        "src/app.py",
        ".coding_agent_test/thread2/tests/test_app.py",
    ]
    assert planned[1]["agent_internal"] is True
    assert planned[1]["user_visible"] is False
    assert layout["agent_tests"] == [
        {"path": ".coding_agent_test/thread2/tests/test_app.py", "original_path": "tests/test_app.py"}
    ]
    assert layout["generated_tests_deliverable"] is False


def test_common_test_support_directories_are_internal_even_if_misclassified():
    state = {
        "workspace": "/workspace/demo",
        "thread_id": "thread-tests",
        "mode": "generate_project",
        "task_intent": {},
        "test_policy": {"generate_internal_tests": True},
    }
    files = [
        {"path": "src/app.py", "kind": "code"},
        {"path": "agent_tests/__init__.py", "kind": "code"},
        {"path": "integration_tests/test_cli.py", "kind": "code"},
        {"path": "unit_tests/fixtures/sample.csv", "kind": "data"},
    ]

    planned, layout = apply_output_layout(state, files)
    paths = [item["path"] for item in planned]

    assert "src/app.py" in paths
    assert "agent_tests/__init__.py" not in paths
    assert "integration_tests/test_cli.py" not in paths
    assert "unit_tests/fixtures/sample.csv" not in paths
    assert ".coding_agent_test/thread-tests/agent_tests/__init__.py" in paths
    assert ".coding_agent_test/thread-tests/integration_tests/test_cli.py" in paths
    assert ".coding_agent_test/thread-tests/unit_tests/fixtures/sample.csv" in paths
    assert all(item["kind"] == "test" for item in planned if item["path"].startswith(".coding_agent_test/"))
    assert layout["deliverables"] == [{"path": "src/app.py", "kind": "code"}]


def test_generated_tests_are_skipped_by_default():
    state = {"workspace": "/workspace/new_project", "thread_id": "thread-default", "mode": "generate_project", "task_intent": {}}
    files = [
        {"path": "src/app.py", "kind": "code"},
        {"path": "tests/test_app.py", "kind": "test"},
    ]

    planned, layout = apply_output_layout(state, files)

    assert [item["path"] for item in planned] == ["src/app.py"]
    assert layout["agent_tests"] == []
    assert layout["skipped_tests"] == [{"path": "tests/test_app.py", "reason": "internal generated tests disabled by default"}]


def test_user_requested_tests_are_deliverables():
    state = {
        "workspace": "/workspace/new_project",
        "thread_id": "thread-user-tests",
        "mode": "generate_project",
        "task_intent": {"create_paths": ["tests/test_app.py"]},
    }
    files = [
        {"path": "src/app.py", "kind": "code"},
        {"path": "tests/test_app.py", "kind": "test", "explicit_user_requested": True},
    ]

    planned, layout = apply_output_layout(state, files)

    assert [item["path"] for item in planned] == ["src/app.py", "tests/test_app.py"]
    assert planned[1]["user_visible"] is True
    assert layout["agent_tests"] == []
    assert {"path": "tests/test_app.py", "kind": "test"} in layout["deliverables"]


def test_artifact_registry_classifies_internal_test_support_as_tests(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "mode": "generate_project",
        "repo_map": {"files": []},
        "file_plan": {
            "files": [
                {"path": "src/app.py", "kind": "code"},
                {"path": ".coding_agent_test/t/agent_tests/__init__.py", "kind": "test"},
            ]
        },
        "generated_files": [
            {"path": "src/app.py", "kind": "code"},
            {"path": ".coding_agent_test/t/agent_tests/__init__.py", "kind": "test"},
        ],
        "changed_files": ["src/app.py", ".coding_agent_test/t/agent_tests/__init__.py"],
    }

    registry = build_artifact_registry(state)

    assert registry["by_path"]["src/app.py"]["kind"] == "code"
    assert registry["by_path"][".coding_agent_test/t/agent_tests/__init__.py"]["kind"] == "test"
    assert ".coding_agent_test/t/agent_tests/__init__.py" in registry["agent_generated_tests"]


def test_run_and_project_memory_dirs_are_agent_owned(tmp_path: Path):
    workspace = tmp_path / "project"
    run_dir = run_dir_for(workspace, "thread3")
    memory_dir = project_memory_dir_for(workspace)

    assert ".agent_runs" in run_dir.parts
    assert ".agent_runs" in memory_dir.parts
    assert workspace not in run_dir.parents
    assert workspace not in memory_dir.parents


def test_verify_targets_internal_agent_tests_without_root_test_mix(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / ".coding_agent_test" / "thread4" / "tests").mkdir(parents=True)
    (tmp_path / "scripts" / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_tool.py").write_text("def test_root():\n    assert False\n", encoding="utf-8")
    internal = tmp_path / ".coding_agent_test" / "thread4" / "tests" / "test_tool.py"
    internal.write_text("def test_internal():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "thread_id": "thread4",
        "mode": "write",
        "read_only": False,
        "run_dir": str(run_dir_for(tmp_path, "thread4")),
        "file_plan": {
            "files": [
                {
                    "path": ".coding_agent_test/thread4/tests/test_tool.py",
                    "kind": "test",
                    "original_path": "tests/test_tool.py",
                    "agent_internal": True,
                }
            ]
        },
        "generated_files": [
            {
                "path": ".coding_agent_test/thread4/tests/test_tool.py",
                "kind": "test",
                "original_path": "tests/test_tool.py",
                "agent_internal": True,
                "ok": True,
            }
        ],
    }

    commands = _default_commands(state)
    pytest_cmd = next(cmd for name, cmd in commands if name == "pytest")
    targets = _pytest_targets_for_command(state, "pytest", pytest_cmd)

    assert targets == [".coding_agent_test/thread4/tests/test_tool.py"]


def test_safe_create_verification_ignores_unregistered_project_tests_and_missing_pytest_targets(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / "report.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "tests" / "test_stale.py").write_text("def helper_only():\n    return True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "thread_id": "safe-create",
        "mode": "write",
        "read_only": False,
        "run_dir": str(run_dir_for(tmp_path, "safe-create")),
        "task_contract": {
            "expected_artifacts": ["requested_code_files"],
            "suggested_verify_commands": [["python", "-m", "pytest", "-q", "internal_tests/"]],
        },
        "file_plan": {"files": [{"path": "scripts/report.py", "kind": "code"}]},
        "generated_files": [{"path": "scripts/report.py", "kind": "code"}],
    }

    commands = _default_commands(state)

    assert all(name != "pytest" for name, _cmd in commands)
    assert all(name not in {"contract", "custom", "planned"} for name, _cmd in commands)
