from pathlib import Path
import json

from coding_agent.workspace.interface_check import run_interface_consistency_check
from coding_agent.repair.failure_analysis import decompose_failure_issues, summarize_issue_owners
from coding_agent.nodes.tool_exec import tool_exec_node


def test_interface_check_detects_test_importing_missing_symbol(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / "analyze_model_comparison.py").write_text(
        "def extract_experiment_data():\n    return []\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_analyze_model_comparison.py").write_text(
        "from analyze_model_comparison import parse_experiment_data\n\n"
        "def test_x():\n    assert True\n",
        encoding="utf-8",
    )
    out = run_interface_consistency_check(str(tmp_path), {})
    assert out["ok"] is False
    issue = out["issues"][0]
    assert issue["type"] == "missing_imported_symbol"
    assert issue["test_file"] == "tests/test_analyze_model_comparison.py"
    assert issue["target_file"] == "scripts/analyze_model_comparison.py"
    assert issue["missing_symbols"] == ["parse_experiment_data"]


def test_interface_check_accepts_top_level_reexports_and_aliases(tmp_path: Path):
    (tmp_path / "package").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "package" / "api.py").write_text(
        "from .errors import PublicError\n"
        "from .errors import InternalError as RenamedError\n"
        "import collections.abc as abc\n"
        "answer: int = 42\n",
        encoding="utf-8",
    )
    (tmp_path / "package" / "errors.py").write_text(
        "class PublicError(Exception):\n    pass\n"
        "class InternalError(Exception):\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_api.py").write_text(
        "from package.api import PublicError, RenamedError, abc, answer\n",
        encoding="utf-8",
    )

    out = run_interface_consistency_check(str(tmp_path), {})

    assert out["ok"] is True
    assert out["issues"] == []


def test_failure_decomposer_combines_interface_and_runtime_contract():
    state = {
        "interface_check": {"issues": [{"message": "test imports missing function", "test_file": "tests/test_a.py", "target_file": "a.py"}]},
        "contract_check": {"failures": ["contract command failed with ValueError"]},
        "verification": {"results": []},
    }
    issues = decompose_failure_issues(state)
    owners = summarize_issue_owners(issues)
    assert any(x["type"] == "missing_imported_symbol" for x in issues)
    assert any(x["type"] == "contract_failure" for x in issues)
    assert owners == "implementation_and_generated_test"


def test_repeated_read_cache_blocks_second_unchanged_file_read(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    base = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "round_idx": 0,
        "mode": "debug",
        "read_only": False,
        "failure": {"failure_type": "runtime_error", "signature": "sig1"},
        "repo_map": {"files": ["a.py"]},
    }
    state = dict(base)
    state.update({"round_idx": 0, "decision": {"action": {"tool": "read_file", "args": {"path": "a.py"}}}})
    out = tool_exec_node(state)
    base["repair_read_budget"] = out.get("repair_read_budget")
    base["repair_read_cache"] = out.get("repair_read_cache")
    assert out["last_tool_result"]["ok"] is True

    state = dict(base)
    state.update({"round_idx": 1, "decision": {"action": {"tool": "read_file", "args": {"path": "a.py"}}}})
    out = tool_exec_node(state)
    assert out["last_tool_result"]["ok"] is False
    assert out["last_tool_result"]["data"].get("blocked_by_repair_action_budget") is True
    assert out["last_tool_result"]["data"].get("blocked_by_read_cache") is True
    assert out.get("force_repair_action")


def test_repeated_read_many_files_blocks_after_budget_is_spent(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True)
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    base = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "mode": "debug",
        "read_only": False,
        "failure": {"failure_type": "runtime_error", "signature": "sig-many"},
        "repo_map": {"files": ["a.py", "b.py"]},
    }

    state = dict(base)
    state.update({"round_idx": 0, "decision": {"action": {"tool": "read_many_files", "args": {"paths": ["a.py", "b.py"]}}}})
    out = tool_exec_node(state)
    assert out["last_tool_result"]["ok"] is True
    base["repair_read_budget"] = out.get("repair_read_budget")
    base["repair_read_cache"] = out.get("repair_read_cache")

    state = dict(base)
    state.update({"round_idx": 1, "decision": {"action": {"tool": "read_many_files", "args": {"paths": ["b.py", "a.py"]}}}})
    out = tool_exec_node(state)
    assert out["last_tool_result"]["ok"] is True
    base["repair_read_budget"] = out.get("repair_read_budget")
    base["repair_read_cache"] = out.get("repair_read_cache")

    state = dict(base)
    state.update({"round_idx": 2, "decision": {"action": {"tool": "read_many_files", "args": {"paths": ["a.py", "b.py"]}}}})
    out = tool_exec_node(state)

    assert out["last_tool_result"]["ok"] is False
    assert out["last_tool_result"]["data"].get("blocked_by_repair_action_budget") is True
    assert out["last_tool_result"]["data"].get("read_counts") == {"a.py": 2, "b.py": 2}
    assert out.get("force_repair_action")


def test_repeated_run_shell_command_blocks_without_file_change(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True)
    (tmp_path / "probe.py").write_text("print('probe')\n", encoding="utf-8")
    args = {"command": ["python", "probe.py"], "timeout_sec": 10}
    key = json.dumps({"tool": "run_shell", "args": args}, ensure_ascii=False, sort_keys=True, default=str)
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "round_idx": 2,
        "mode": "debug",
        "read_only": False,
        "failure": {"failure_type": "runtime_error", "signature": "sig-shell"},
        "repo_map": {"files": []},
        "action_history": [
            {"tool": "run_shell", "action_key": key, "failure_signature": "sig-shell", "changed": False},
            {"tool": "run_shell", "action_key": key, "failure_signature": "sig-shell", "changed": False},
        ],
        "decision": {"action": {"tool": "run_shell", "args": args}},
    }

    out = tool_exec_node(state)

    result = out["last_tool_result"]
    assert result["ok"] is False
    assert result["data"]["blocked_by_repeated_action_guard"] is True
    assert "run_shell" not in out["force_repair_action"]["allowed_tools"]


def test_repeated_run_shell_budget_resets_after_file_change(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True)
    (tmp_path / "probe.py").write_text("print('probe')\n", encoding="utf-8")
    args = {"command": ["python", "probe.py"], "timeout_sec": 10}
    key = json.dumps({"tool": "run_shell", "args": args}, ensure_ascii=False, sort_keys=True, default=str)
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "round_idx": 3,
        "mode": "debug",
        "read_only": False,
        "failure": {"failure_type": "runtime_error", "signature": "sig-shell"},
        "repo_map": {"files": []},
        "action_history": [
            {"tool": "run_shell", "action_key": key, "failure_signature": "sig-shell", "changed": False},
            {"tool": "write_file", "action_key": "write", "failure_signature": "sig-shell", "changed": True},
            {"tool": "run_shell", "action_key": key, "failure_signature": "sig-shell", "changed": False},
        ],
        "decision": {"action": {"tool": "run_shell", "args": args}},
    }

    out = tool_exec_node(state)

    assert out["last_tool_result"]["ok"] is True


def test_required_repair_tool_blocks_run_shell_escape(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True)
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "round_idx": 0,
        "mode": "debug",
        "read_only": False,
        "failure": {"failure_type": "runtime_error", "signature": "sig-force"},
        "repo_map": {"files": []},
        "force_repair_action": {
            "reason": "must rewrite generated file",
            "failure_signature": "sig-force",
            "required_tool": "write_file",
            "allowed_tools": ["write_file", "finish"],
        },
        "decision": {
            "action": {
                "tool": "run_shell",
                "args": {"command": ["python", "-c", "print('escape')"], "timeout_sec": 10},
            }
        },
    }

    out = tool_exec_node(state)

    result = out["last_tool_result"]
    assert result["ok"] is False
    assert result["data"]["blocked_by_repair_action_budget"] is True
    assert result["data"]["required_tool"] == "write_file"
