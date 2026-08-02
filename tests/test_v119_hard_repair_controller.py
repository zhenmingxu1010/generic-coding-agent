from pathlib import Path

from coding_agent.nodes.diagnose import diagnose_node
from coding_agent.nodes.tool_exec import (
    _force_repair_policy_result,
    _invalidate_read_cache_for_path,
    _read_budget_policy_result,
    _record_successful_read_for_budget,
)
from coding_agent.scope.task_intent import classify_task_intent
from coding_agent.repair.failure_analysis import decompose_failure_issues
from coding_agent.repair.traceback_parser import parse_traceback_issues


def _base_state(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True, exist_ok=True)
    return {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
    }


def test_readonly_analysis_with_negative_create_words_stays_readonly():
    task = (
        "只读分析这个项目。请重点说明项目结构、训练入口、模型定义、loss 定义、"
        "metrics/summary/result 文件如何产生，以及 experiments 下已有结果文件的 schema。"
        "禁止创建、修改、删除任何文件。"
    )
    intent = classify_task_intent(task, {"read_only": True})
    assert intent["mode"] == "analyze"
    assert intent["operation_mode"] == "read_only_analysis"
    assert intent["agent_read_only"] is True
    assert intent["create_requested"] is False
    assert intent["modify_requested"] is False


def test_safe_create_with_negative_modify_constraint_still_writes_targets():
    task = (
        "这是已有项目工作区。本轮 Agent 允许创建新文件，但禁止修改任何已有项目文件。"
        "然后创建新脚本 scripts/summarize_metrics.py 和新测试 "
        "tests/test_summarize_metrics.py。新脚本运行时只读取 "
        "data/service_summary.json。"
    )
    intent = classify_task_intent(task)
    assert intent["mode"] == "write"
    assert intent["agent_read_only"] is False
    assert intent["script_read_only"] is True
    assert "scripts/summarize_metrics.py" in intent["create_paths"]
    assert "tests/test_summarize_metrics.py" in intent["create_paths"]
    assert intent["modify_requested"] is False


def test_traceback_parser_extracts_import_and_value_errors():
    text = (
        'File "tests/test_summarize_metrics.py", line 3, in <module>\n'
        "    from scripts.summarize_metrics import parse_summary_file\n"
        "ImportError: cannot import name 'parse_summary_file' from 'scripts.summarize_metrics'\n"
        'File "scripts/summarize_metrics.py", line 88, in find_best_experiments\n'
        "    best_experiments[metric] = max(results, key=lambda x: x[metric])\n"
        "ValueError: max() arg is an empty sequence\n"
    )
    issues = parse_traceback_issues(text)
    assert any(i.get("type") == "import_error_missing_symbol" and i.get("symbol") == "parse_summary_file" for i in issues)
    assert any(i.get("exception_type") == "ValueError" and i.get("file") == "scripts/summarize_metrics.py" for i in issues)


def test_traceback_parser_extracts_cli_unrecognized_arguments():
    text = (
        "E       AssertionError: assert 2 == 0\n"
        "E        +  where 2 = CompletedProcess(args=['python', '-m', 'duration_cli.cli', '--output-json', "
        "'/tmp/out.json'], returncode=2, stderr='usage: cli.py [-h] --input INPUT [--output-json]\\n"
        "cli.py: error: unrecognized arguments: /tmp/out.json\\n').returncode\n"
        "tests/test_cli.py:96: AssertionError\n"
    )

    issues = parse_traceback_issues(text)
    cli_issue = next(i for i in issues if i.get("type") == "cli_unrecognized_arguments")

    assert cli_issue["owner"] == "implementation"
    assert cli_issue["arguments"] == "/tmp/out.json"


def test_failed_non_pytest_verification_command_becomes_failure_issue(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "generated_files": [{"path": "tool.py", "kind": "code"}],
        "verification": {
            "ok": False,
            "results": [
                {
                    "name": "contract",
                    "command": ["python", "tool.py", "input.txt"],
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "",
                    "timed_out": False,
                }
            ],
        },
        "contract_check": {"ok": True, "failures": []},
    }

    issues = decompose_failure_issues(state)

    issue = next(item for item in issues if item.get("type") == "verification_command_failed")
    assert issue["owner"] == "implementation"
    assert issue["target_file"] == "tool.py"


def test_diagnose_populates_interface_and_failure_issues(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / "summarize_metrics.py").write_text(
        "def load_summary(path):\n    return {}\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_summarize_metrics.py").write_text(
        "from scripts.summarize_metrics import parse_summary_file\n\n"
        "def test_parse():\n    assert parse_summary_file('x') == []\n",
        encoding="utf-8",
    )
    state = _base_state(tmp_path)
    state.update({
        "verification": {
            "ok": False,
            "results": [{
                "name": "pytest",
                "command": ["python", "-m", "pytest", "-q"],
                "returncode": 2,
                "stdout": "",
                "stderr": "ImportError: cannot import name 'parse_summary_file' from 'scripts.summarize_metrics'\n",
            }],
        },
        "contract_check": {"ok": False, "failures": []},
    })
    out = diagnose_node(state)
    assert out["interface_check"]["ok"] is False
    assert out["traceback_issues"]
    assert any(i.get("type") == "missing_imported_symbol" for i in out["failure_issues"])


def test_invalidate_read_cache_tolerates_null_force_repair_action():
    state = {
        "force_repair_action": None,
        "repair_read_cache": {
            "sig|scripts/x.py": {"path": "scripts/x.py"},
            "sig|scripts/y.py": {"path": "scripts/y.py"},
        },
    }

    _invalidate_read_cache_for_path(state, "scripts/x.py")

    assert state["force_repair_action"] is None
    assert "sig|scripts/x.py" not in state["repair_read_cache"]
    assert "sig|scripts/y.py" in state["repair_read_cache"]


def test_force_repair_allows_one_targeted_read_before_blocking_repeats(tmp_path: Path):
    target = tmp_path / "scripts" / "tool.py"
    target.parent.mkdir()
    target.write_text("print('x')\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "failure": {"signature": "sig"},
        "force_repair_action": {
            "path": "scripts/tool.py",
            "allowed_tools": ["edit_file", "write_file", "run_tests", "finish"],
        },
        "repair_read_cache": {},
    }
    args = {"path": "scripts/tool.py", "start_line": 1, "limit": 20}

    assert _force_repair_policy_result(state, "read_file", args) is None
    assert _read_budget_policy_result(state, "read_file", args) is None

    _record_successful_read_for_budget(
        state,
        "read_file",
        args,
        {
            "ok": True,
            "data": {
                "path": "scripts/tool.py",
                "start_line": 1,
                "end_line": 1,
                "total_lines": 1,
                "content": "1: print('x')",
            },
        },
    )
    blocked = _read_budget_policy_result(state, "read_file", args)

    assert blocked is not None
    assert blocked.data["blocked_by_repair_action_budget"] is True


def test_force_repair_blocks_full_rewrite_of_existing_project_source(tmp_path: Path):
    target = tmp_path / "scripts" / "tool.py"
    target.parent.mkdir()
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "failure": {"signature": "sig"},
        "force_repair_action": {
            "required_path": "scripts/tool.py",
            "allowed_target_files": ["scripts/tool.py"],
            "allowed_tools": ["edit_file", "write_file", "run_tests", "finish"],
        },
    }

    blocked = _force_repair_policy_result(
        state,
        "write_file",
        {"path": "scripts/tool.py", "content": "def value():\n    return 2\n"},
    )
    allowed = _force_repair_policy_result(
        state,
        "edit_file",
        {
            "path": "scripts/tool.py",
            "old_text": "return 1",
            "new_text": "return 2",
        },
    )

    assert blocked is not None
    assert blocked.data["blocked_by_existing_source_rewrite_policy"] is True
    assert allowed is None


def test_force_repair_allows_bounded_read_of_generated_support_file(tmp_path: Path):
    target = tmp_path / "example.txt"
    target.write_text("one two\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "failure": {"signature": "sig"},
        "force_repair_action": {
            "path": "cli.py",
            "allowed_target_files": ["cli.py", "tests/test_cli.py"],
            "allowed_read_files": ["cli.py", "tests/test_cli.py", "example.txt"],
            "allowed_tools": ["edit_file", "write_file", "run_tests", "finish"],
        },
        "repair_read_cache": {},
    }
    args = {"path": "example.txt", "start_line": 1, "limit": 20}

    assert _force_repair_policy_result(state, "read_file", args) is None
    assert _read_budget_policy_result(state, "read_file", args) is None


def test_read_cache_allows_an_unread_range_of_the_same_file(tmp_path: Path):
    target = tmp_path / "module.py"
    target.write_text("".join(f"line_{index} = {index}\n" for index in range(1, 301)), encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "failure": {"signature": "sig"},
        "force_repair_action": {
            "path": "module.py",
            "allowed_tools": ["edit_file", "write_file", "run_tests", "finish"],
        },
        "repair_read_cache": {},
        "repair_read_budget": {},
    }
    first = {"path": "module.py", "start_line": 1, "limit": 100}
    _record_successful_read_for_budget(
        state,
        "read_file",
        first,
        {
            "ok": True,
            "data": {
                "path": "module.py",
                "start_line": 1,
                "end_line": 100,
                "total_lines": 300,
                "content": "first range",
            },
        },
    )

    unread = {"path": "module.py", "start_line": 200, "limit": 80}
    assert _force_repair_policy_result(state, "read_file", unread) is None
    assert _read_budget_policy_result(state, "read_file", unread) is None

    repeated = _read_budget_policy_result(state, "read_file", first)
    assert repeated is not None
    assert repeated.data["blocked_by_repair_action_budget"] is True
