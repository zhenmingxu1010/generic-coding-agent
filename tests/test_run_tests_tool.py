from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from coding_agent.repair.failure_analysis import decompose_failure_issues
from coding_agent.repair.repair_controller import build_repair_controller, force_action_from_controller
from coding_agent.nodes.tool_exec import tool_exec_node
from coding_agent.nodes.verify import _default_commands, _pytest_targets_for_command, verify_node
from coding_agent.core.schemas import ToolResult
from coding_agent.tools import test_tools


def test_run_tests_timeout_preserves_byte_streams_as_text(tmp_path: Path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0] if args else kwargs.get("args"),
            timeout=0.01,
            output=b"partial stdout\n",
            stderr=b"partial stderr\n",
        )

    monkeypatch.setattr(test_tools.subprocess, "run", fake_run)

    result = test_tools.run_tests(
        str(tmp_path),
        timeout_sec=1,
        report_dir=str(tmp_path / "reports"),
    )

    assert result.ok is False
    assert result.data["timed_out"] is True
    assert result.data["stdout"] == "partial stdout\n"
    assert result.data["stderr"] == "partial stderr\n"


def test_source_modify_verification_runs_all_project_tests_even_with_registry(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_existing.py").write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tests" / "test_generated.py").write_text("def test_generated():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(tmp_path / ".coding_agent" / "t"),
        "thread_id": "t",
        "mode": "modify",
        "read_only": False,
        "task_intent": {"source_modify_intent": True, "operation_mode": "scoped_modify"},
        "generated_files": [{"path": "tests/test_generated.py", "kind": "test", "verification_role": "test"}],
    }

    commands = _default_commands(state)
    pytest_commands = [cmd for name, cmd in commands if name == "pytest"]

    assert pytest_commands
    assert pytest_commands[0] == ["python", "-m", "pytest", "-q"]


def test_scope_allowed_modify_verification_runs_all_project_tests_even_when_mode_is_write(tmp_path: Path):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "inventory" / "stock.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_existing.py").write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tests" / "test_generated.py").write_text("def test_generated():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(tmp_path / ".coding_agent" / "t"),
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
        "task_intent": {
            "operation_mode": "safe_create",
            "scope_contract": {"allowed_modify_paths": ["inventory/stock.py"]},
        },
        "scope_contract": {"allowed_modify_paths": ["inventory/stock.py"]},
        "generated_files": [{"path": "tests/test_generated.py", "kind": "test", "verification_role": "test"}],
    }

    commands = _default_commands(state)
    pytest_commands = [cmd for name, cmd in commands if name == "pytest"]

    assert pytest_commands
    assert pytest_commands[0] == ["python", "-m", "pytest", "-q"]


def test_safe_create_verification_runs_only_current_work_tests(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / ".coding_agent_test" / "t" / "tests").mkdir(parents=True)
    (tmp_path / "tests" / "test_tool.py").write_text("def test_root():\n    assert False\n", encoding="utf-8")
    work_test = tmp_path / ".coding_agent_test" / "t" / "tests" / "test_tool.py"
    work_test.write_text("def test_work():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(tmp_path / ".agent_runs" / "t"),
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
        "file_plan": {
            "files": [
                {"path": ".coding_agent_test/t/tests/test_tool.py", "kind": "test", "original_path": "tests/test_tool.py"},
            ]
        },
        "generated_files": [
            {"path": ".coding_agent_test/t/tests/test_tool.py", "kind": "test", "original_path": "tests/test_tool.py"},
        ],
    }

    commands = _default_commands(state)
    pytest_commands = [cmd for name, cmd in commands if name == "pytest"]

    assert pytest_commands
    targets = _pytest_targets_for_command(state, "pytest", pytest_commands[0])
    assert targets == [".coding_agent_test/t/tests/test_tool.py"]
    assert "tests/test_tool.py" not in targets


def test_source_modify_generic_pytest_is_not_retargeted_to_registry(tmp_path: Path):
    (tmp_path / "inventory").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "inventory" / "stock.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_existing.py").write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tests" / "test_generated.py").write_text("def test_generated():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(tmp_path / ".coding_agent" / "t"),
        "thread_id": "t",
        "mode": "modify",
        "read_only": False,
        "task_intent": {"source_modify_intent": True, "operation_mode": "scoped_modify"},
        "generated_files": [{"path": "tests/test_generated.py", "kind": "test", "verification_role": "test"}],
    }
    _default_commands(state)

    targets = _pytest_targets_for_command(state, "pytest", ["python", "-m", "pytest", "-q"])

    assert targets == []


def test_pytest_target_preserves_node_selector(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "mode": "modify",
        "task_intent": {"source_modify_intent": True},
    }

    targets = _pytest_targets_for_command(
        state,
        "specific_behavior",
        [
            "python",
            "-m",
            "pytest",
            "tests/test_hooks.py::test_missing_scenario",
        ],
    )

    assert targets == ["tests/test_hooks.py::test_missing_scenario"]


def test_parse_junit_xml_extracts_structured_failure(tmp_path: Path):
    report = tmp_path / "pytest.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2" failures="1" errors="0" skipped="0">
  <testcase classname="tests.test_demo" name="test_ok" file="tests/test_demo.py" line="3" />
  <testcase classname="tests.test_demo" name="test_fail" file="tests/test_demo.py" line="7">
    <failure type="AssertionError" message="assert 1 == 2">tests/test_demo.py:7: AssertionError
E   AssertionError: assert 1 == 2</failure>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    parsed = test_tools._parse_junit_xml(report)

    assert parsed["ok"] is False
    assert parsed["total"] == 2
    assert parsed["passed"] == 1
    assert parsed["failed"] == 1
    assert parsed["testcases"] == [
        {"test": "tests.test_demo::test_ok", "status": "passed", "file": "tests/test_demo.py", "line": 3},
        {"test": "tests.test_demo::test_fail", "status": "failed", "file": "tests/test_demo.py", "line": 7},
    ]
    assert parsed["failures"][0]["test"] == "tests.test_demo::test_fail"
    assert parsed["failures"][0]["file"] == "tests/test_demo.py"
    assert parsed["failures"][0]["line"] == 7
    assert parsed["issues"][0]["owner"] == "generated_test"


def test_build_pytest_command_uses_explicit_targets_and_junit(tmp_path: Path):
    junit = tmp_path / "report.xml"

    command = test_tools._build_pytest_command(
        [".coding_agent_test/t/tests/test_real.py"],
        [".coding_agent_test/t"],
        junit,
    )

    assert command[:2] == [sys.executable, "-c"]
    code = command[2]
    assert "pytest.main" in code
    assert ".coding_agent_test/t/tests/test_real.py" in code
    assert "--junitxml" in code
    assert ".coding_agent_test/t" in code


def test_build_pytest_command_preserves_target_virtualenv_launcher(
    tmp_path: Path,
    monkeypatch,
):
    launcher = tmp_path / "target-python"
    launcher.symlink_to(sys.executable)
    monkeypatch.setenv("AGENT_TARGET_PYTHON", str(launcher))

    command = test_tools._build_pytest_command([], [], tmp_path / "report.xml")

    assert command[0] == str(launcher)


def test_normalise_target_preserves_hidden_agent_directory(tmp_path: Path):
    target = tmp_path / ".coding_agent_test" / "t" / "tests" / "test_real.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_real():\n    assert True\n", encoding="utf-8")

    assert (
        test_tools._normalise_target(str(tmp_path), ".coding_agent_test/t/tests/test_real.py")
        == ".coding_agent_test/t/tests/test_real.py"
    )
    assert (
        test_tools._normalise_target(str(tmp_path), "./.coding_agent_test/t/tests/test_real.py")
        == ".coding_agent_test/t/tests/test_real.py"
    )


def test_run_tests_structures_pytest_invocation_error(tmp_path: Path):
    class FakeCompleted:
        returncode = 4
        stdout = "\n"
        stderr = "ERROR: file or directory not found: .coding_agent_test/t/tests/test_missing.py\n\n"

    old_run = test_tools.subprocess.run
    test_tools.subprocess.run = lambda *args, **kwargs: FakeCompleted()
    try:
        result = test_tools.run_tests(
            str(tmp_path),
            targets=[".coding_agent_test/t/tests/test_missing.py"],
            report_dir=".coding_agent/t/test_reports",
        )
    finally:
        test_tools.subprocess.run = old_run

    assert result.ok is False
    assert result.data["targets"] == [".coding_agent_test/t/tests/test_missing.py"]
    assert result.data["failures"][0]["type"] == "pytest_target_not_found"
    assert result.data["issues"][0]["type"] == "pytest_target_not_found"


def test_run_tests_structures_zero_collected_pytest(tmp_path: Path):
    class FakeCompleted:
        returncode = 5
        stdout = "\nno tests ran in 0.02s\n"
        stderr = ""

    old_run = test_tools.subprocess.run
    test_tools.subprocess.run = lambda *args, **kwargs: FakeCompleted()
    try:
        result = test_tools.run_tests(
            str(tmp_path),
            targets=["tests/test_empty.py"],
            report_dir=".coding_agent/t/test_reports",
        )
    finally:
        test_tools.subprocess.run = old_run

    assert result.ok is False
    assert result.data["total"] == 0
    assert result.data["failures"][0]["type"] == "pytest_zero_collected"
    assert result.data["issues"][0]["type"] == "pytest_zero_collected"
    assert result.data["issues"][0]["owner"] == "test_collection"


def test_run_tests_treats_pytest_returncode_5_as_zero_collected(tmp_path: Path):
    class FakeCompleted:
        returncode = 5
        stdout = "\n"
        stderr = ""

    old_run = test_tools.subprocess.run
    old_parse = test_tools._parse_junit_xml
    test_tools.subprocess.run = lambda *args, **kwargs: FakeCompleted()
    test_tools._parse_junit_xml = lambda *args, **kwargs: {
        "ok": True,
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "failures": [],
        "issues": [],
    }
    try:
        result = test_tools.run_tests(
            str(tmp_path),
            targets=[],
            report_dir=".coding_agent/t/test_reports",
        )
    finally:
        test_tools.subprocess.run = old_run
        test_tools._parse_junit_xml = old_parse

    assert result.ok is False
    assert result.data["total"] == 0
    assert result.data["failures"][0]["type"] == "pytest_zero_collected"
    assert result.data["issues"][0]["type"] == "pytest_zero_collected"


def test_verify_node_uses_run_tests_for_registered_pytest(tmp_path: Path):
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / ".coding_agent_test" / "t" / "tests").mkdir(parents=True)
    (tmp_path / "scripts" / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / ".coding_agent_test" / "t" / "tests" / "test_demo.py").write_text("def test_demo():\n    assert 1 == 1\n", encoding="utf-8")
    run_dir = tmp_path / ".agent_runs" / "t"
    calls: list[dict] = []

    def fake_run_tests(workspace, **kwargs):
        calls.append({"workspace": workspace, **kwargs})
        return ToolResult(
            tool="run_tests",
            ok=True,
            message="tests finished",
            data={
                "version": "run_tests_v1",
                "kind": "pytest",
                "ok": True,
                "targets": kwargs.get("targets") or [],
                "pythonpath": kwargs.get("pythonpath") or [],
                "report": ".agent_runs/t/test_reports/pytest.xml",
                "command": ["python", "-c", "pytest.main(...)"],
                "returncode": 0,
                "stdout": ". [100%]\n",
                "stderr": "",
                "timed_out": False,
                "total": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "failures": [],
                "issues": [],
            },
        )

    import coding_agent.nodes.verify as verify_mod

    old_run_tests = verify_mod.run_tests
    verify_mod.run_tests = fake_run_tests
    try:
        state = {
            "workspace": str(tmp_path),
            "run_dir": str(run_dir),
            "thread_id": "t",
            "mode": "write",
            "read_only": False,
            "task": "create a script and tests",
            "task_contract": {},
            "file_plan": {
                "files": [
                    {"path": "scripts/demo.py", "kind": "code"},
                    {"path": ".coding_agent_test/t/tests/test_demo.py", "kind": "test"},
                ]
            },
            "trace_path": str(run_dir / "trace.jsonl"),
            "state_snapshot_path": str(run_dir / "state.json"),
        }
        out = verify_node(state)
    finally:
        verify_mod.run_tests = old_run_tests

    assert calls
    assert calls[0]["targets"] == [".coding_agent_test/t/tests/test_demo.py"]
    assert calls[0]["pythonpath"] == ["."]
    assert out["verification"]["ok"] is True
    assert out["verification"]["test_results"]["total"] == 1
    assert out["test_results"]["runs"][0]["name"] == "pytest"


def test_failure_analysis_consumes_structured_test_issues():
    state = {
        "verification": {"results": [], "test_results": {}},
        "test_results": {
            "runs": [
                {
                    "issues": [
                        {
                            "owner": "generated_test_or_external_test",
                            "type": "assertionerror",
                            "file": "tests/test_demo.py",
                            "line": 5,
                            "message": "AssertionError",
                            "source": "run_tests_junit",
                        }
                    ],
                    "failures": [],
                }
            ]
        },
    }

    issues = decompose_failure_issues(state)

    assert any(issue.get("source") == "run_tests_junit" for issue in issues)
    assert any(issue.get("owner") == "generated_test" for issue in issues)


def test_failure_analysis_decomposes_failed_requirement_atoms():
    state = {
        "semantic_contract_check": {
            "requirement_atom_check": {
                "atoms": [
                    {
                        "id": "requirement:alternate_runtime_case",
                        "type": "behavior",
                        "description": "The requested alternate runtime case must succeed.",
                        "required": True,
                        "status": "failed",
                        "details": {
                            "verification_claim": {
                                "reason": "the executed command returned a non-zero status"
                            }
                        },
                    }
                ]
            }
        },
        "verification": {"results": []},
    }

    issues = decompose_failure_issues(state)

    issue = next(x for x in issues if x.get("type") == "semantic_requirement_atom_failed")
    assert issue["owner"] == "requirement"
    assert issue["atom_id"] == "requirement:alternate_runtime_case"
    assert issue["target_file"] is None
    assert "non-zero status" in issue["repair_hint"]


def test_failure_analysis_assigns_collection_import_error_to_generated_test():
    test_path = ".coding_agent_test/t/tests/test_analyze_model_comparison.py"
    state = {
        "verification": {"results": [], "test_results": {}},
        "test_results": {
            "runs": [
                {
                    "issues": [
                        {
                            "owner": "generated_test",
                            "type": "modulenotfounderror",
                            "file": test_path,
                            "message": "ModuleNotFoundError: No module named 'analyze_model_comparison'",
                            "source": "run_tests_stream:traceback_parser",
                        }
                    ],
                    "failures": [
                        {
                            "owner": "implementation",
                            "type": "Error",
                            "file": "../../../python/importlib/__init__.py",
                            "line": 126,
                            "message": "collection failure",
                            "text": (
                                "ImportError while importing test module '/workspace/"
                                + test_path
                                + "'.\nE   ModuleNotFoundError: No module named 'analyze_model_comparison'"
                            ),
                        }
                    ],
                }
            ]
        },
    }

    issues = decompose_failure_issues(state)

    collection = next(x for x in issues if x.get("source") == "run_tests_failure")
    assert collection["owner"] == "generated_test"
    assert collection["repair_hint"] == "inspect test oracle and implementation API"


def test_failed_exact_edit_on_current_agent_file_forces_full_rewrite(tmp_path: Path):
    run_dir = tmp_path / ".agent_runs" / "t"
    target = tmp_path / "script.py"
    run_dir.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "decision": {
            "action": {
                "tool": "edit_file",
                "args": {
                    "path": "script.py",
                    "old_text": "MISSING = 1\n",
                    "new_text": "VALUE = 2\n",
                },
            }
        },
        "failure": {
            "failure_type": "contract_error",
            "signature": "sig",
            "message": "implementation failed contract",
        },
        "generated_files": [{"path": "script.py", "kind": "code"}],
        "write_intents": {
            "by_path": {
                "script.py": {
                    "allowed": True,
                    "reason": "current agent generated artifact",
                }
            }
        },
    }

    out = tool_exec_node(state)

    assert out["last_tool_result"]["ok"] is False
    assert out["last_tool_result"]["message"] == "old_text not found"
    assert "VALUE = 1" in (
        out["last_tool_result"]["data"]["nearest_current_context"]
    )
    assert out["last_tool_result"]["data"]["failed_old_text"] == "MISSING = 1\n"
    force = out["force_repair_action"]
    assert force["required_tool"] == "write_file"
    assert force["path"] == "script.py"
    assert "write_file" in force["allowed_tools"]


def test_edit_file_with_full_content_is_coerced_to_write_file(tmp_path: Path):
    run_dir = tmp_path / ".agent_runs" / "t"
    target = tmp_path / "script.py"
    run_dir.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "decision": {
            "action": {
                "tool": "edit_file",
                "args": {
                    "path": "script.py",
                    "content": "VALUE = 2\n",
                },
            }
        },
        "generated_files": [{"path": "script.py", "kind": "code"}],
        "write_intents": {
            "by_path": {
                "script.py": {
                    "allowed": True,
                    "reason": "current agent generated artifact",
                }
            }
        },
    }

    out = tool_exec_node(state)

    assert out["last_tool_result"]["tool"] == "write_file"
    assert out["last_tool_result"]["ok"] is True
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert out["action_history"][-1]["tool"] == "write_file"


def test_force_full_rewrite_blocks_more_edit_attempts(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    target = run_dir / "work" / "script.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "decision": {
            "action": {
                "tool": "edit_file",
                "args": {
                    "path": "script.py",
                    "old_text": "VALUE = 1\n",
                    "new_text": "VALUE = 2\n",
                },
            }
        },
        "failure": {"failure_type": "contract_error", "signature": "sig"},
        "force_repair_action": {
            "reason": "exact edit failed",
            "failure_signature": "sig",
            "path": "script.py",
            "required_tool": "write_file",
            "allowed_tools": ["write_file", "run_tests", "run_shell", "finish"],
        },
    }

    out = tool_exec_node(state)

    assert out["last_tool_result"]["ok"] is False
    assert out["last_tool_result"]["data"]["blocked_by_repair_action_budget"] is True
    assert out["last_tool_result"]["data"]["required_tool"] == "write_file"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_syntax_rejected_write_records_failed_draft_and_blocks_read_loop(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True)
    target = ".coding_agent_test/t/tests/test_bad.py"
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "decision": {
            "action": {
                "tool": "write_file",
                "args": {
                    "path": target,
                    "content": "def test_bad(:\n    assert True\n",
                },
            }
        },
        "file_plan": {"files": [{"path": target, "kind": "test"}]},
        "generated_files": [{"path": target, "kind": "test"}],
    }

    out = tool_exec_node(state)

    assert out["last_tool_result"]["ok"] is False
    assert out["failure"]["failure_type"] == "syntax_level_error"
    assert out["failed_writes"]
    failed = out["failed_writes"][0]
    assert failed["path"] == target
    assert failed["syntax_check"]["checked"] is True
    assert "def test_bad" in Path(failed["draft_path"]).read_text(encoding="utf-8")
    assert not (tmp_path / target).exists()

    controller = build_repair_controller(out)
    force = force_action_from_controller(controller)
    assert controller["route"] == "rewrite_rejected_generated_file"
    assert force is not None
    assert force["required_tool"] == "write_file"

    out["force_repair_action"] = force
    out["repair_action_budget"] = {
        "version": "repair_controller_v2",
        "active": True,
        "force_repair_action": force,
    }
    out["decision"] = {
        "action": {
            "tool": "read_file",
            "args": {"path": target, "start_line": 1, "limit": 20},
        }
    }

    blocked = tool_exec_node(out)

    assert blocked["last_tool_result"]["ok"] is False
    assert blocked["last_tool_result"]["data"]["blocked_by_repair_action_budget"] is True
    assert blocked["last_tool_result"]["data"]["required_tool"] == "write_file"
    assert "file not found" not in blocked["last_tool_result"]["message"]


def test_force_repair_blocks_explicitly_blocked_shell_probe(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True)
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "failure": {"failure_type": "contract_error", "signature": "sig"},
        "force_repair_action": {
            "version": "repair_controller_v2",
            "reason": "schema facts are already available; fix implementation",
            "failure_signature": "sig",
            "path": "scripts/report.py",
            "required_path": "scripts/report.py",
            "allowed_target_files": ["scripts/report.py"],
            "allowed_tools": ["edit_file", "write_file", "run_tests", "finish"],
            "blocked_tools": ["run_shell", "read_file"],
        },
        "repair_action_budget": {"active": True},
        "decision": {
            "action": {
                "tool": "run_shell",
                "args": {"command": "python -c \"import json; print(json.load(open('data/summary.json')).keys())\""},
            }
        },
    }

    blocked = tool_exec_node(state)

    assert blocked["last_tool_result"]["ok"] is False
    assert blocked["last_tool_result"]["data"]["blocked_by_repair_action_budget"] is True
    assert blocked["last_tool_result"]["data"]["blocked_by_force_blocked_tool"] is True
    assert blocked["last_tool_result"]["data"]["attempted_tool"] == "run_shell"
