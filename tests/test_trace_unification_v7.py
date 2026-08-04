from __future__ import annotations

import json
from pathlib import Path

from coding_agent.core.llm_client import OpenAICompatClient
from coding_agent.memory.trace_store import TraceStore
from coding_agent.nodes.report import report_node
from coding_agent.nodes.tool_exec import (
    EMPTY_SHA16,
    _force_repair_policy_result,
    _record_tool_write_artifact_state,
    _normalize_run_tests_args,
    _normalize_write_path_args,
    _repair_target_policy_result,
    tool_exec_node,
)
from coding_agent.nodes.verify import verify_node
from coding_agent.core.schemas import ToolResult


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_trace_store_adds_unified_event_envelope(tmp_path: Path):
    trace_path = tmp_path / "trace.jsonl"

    TraceStore(trace_path).event("tool_call", tool="read_file")

    row = _jsonl(trace_path)[0]
    assert row["schema"] == "trace_event_v2"
    assert row["event"] == "tool_call"
    assert row["event_type"] == "tool_call"
    assert row["channel"] == "tool"
    assert row["source"] == "trace_store"
    assert row["tool"] == "read_file"


def test_message_log_adds_unified_event_envelope(tmp_path: Path):
    messages_path = tmp_path / "messages.jsonl"
    client = OpenAICompatClient(messages_path=messages_path)

    client._append_message_log({"type": "llm_request", "purpose": "unit"})

    row = _jsonl(messages_path)[0]
    assert row["schema"] == "message_event_v2"
    assert row["event"] == "llm_request"
    assert row["event_type"] == "llm_request"
    assert row["channel"] == "llm"
    assert row["source"] == "llm_client"
    assert row["message_kind"] == "llm_call"
    assert row["type"] == "llm_request"


def test_trace_store_suppresses_stream_chunk_events(tmp_path: Path):
    trace_path = tmp_path / "trace.jsonl"

    TraceStore(trace_path).event("llm_stream_chunk", content="partial")

    assert not trace_path.exists()


def test_message_log_suppresses_stream_chunk_events(tmp_path: Path):
    messages_path = tmp_path / "messages.jsonl"
    client = OpenAICompatClient(messages_path=messages_path)

    client._append_message_log({"type": "llm_stream_chunk", "content": "partial"})

    assert not messages_path.exists()


def test_tool_exec_records_tool_call_and_tool_result_events(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    run_dir = tmp_path / ".coding_agent" / "t"
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state_snapshot.json"),
        "mode": "analyze",
        "read_only": False,
        "round_idx": 0,
        "decision": {"action": {"tool": "read_file", "args": {"path": "notes.txt"}}},
    }

    out = tool_exec_node(state)

    events = _jsonl(Path(out["trace_path"]))
    event_names = [row["event"] for row in events]
    assert "tool_call" in event_names
    assert "tool_result" in event_names
    call_event = next(row for row in events if row["event"] == "tool_call")
    result_event = next(row for row in events if row["event"] == "tool_result")
    assert result_event["tool"] == "read_file"
    assert result_event["ok"] is True
    assert result_event["tool_call_id"] == call_event["tool_call_id"]


def test_run_tests_tool_exec_records_verification_result_with_atom_status(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state_snapshot.json"),
        "mode": "write",
        "read_only": False,
        "round_idx": 0,
        "decision": {"action": {"tool": "run_tests", "args": {"targets": ["tests/test_demo.py"]}}},
        "requirement_atom_check": {
            "atoms": [
                {
                    "id": "behavior:demo",
                    "type": "behavior",
                    "required": True,
                    "status": "passed",
                    "description": "demo behavior",
                }
            ],
            "summary": {"required_total": 1, "required_failed": 0, "required_unverified": 0},
        },
    }

    import coding_agent.nodes.tool_exec as tool_exec_mod

    old_execute_tool = tool_exec_mod.execute_tool

    def fake_execute_tool(workspace, tool, args, read_only=False, allow_read_only_execution=False):
        return ToolResult(
            tool="run_tests",
            ok=True,
            message="tests finished",
            data={
                "command": ["python", "-m", "pytest", "tests/test_demo.py"],
                "returncode": 0,
                "stdout": "1 passed\n",
                "stderr": "",
                "timed_out": False,
                "total": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "failures": [],
                "issues": [],
                "ok": True,
            },
        )

    tool_exec_mod.execute_tool = fake_execute_tool
    try:
        out = tool_exec_node(state)
    finally:
        tool_exec_mod.execute_tool = old_execute_tool

    events = _jsonl(Path(out["trace_path"]))
    verification = next(row for row in events if row["event"] == "verification_result")
    assert verification["channel"] == "verification"
    assert verification["source_tool"] == "run_tests"
    assert verification["requirement_atom_status"]["summary"]["required_total"] == 1
    assert verification["requirement_atom_status"]["atoms"][0]["status"] == "passed"


def test_verify_node_records_requirement_atom_status_for_analysis(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state_snapshot.json"),
        "mode": "analyze",
        "read_only": True,
        "analysis_report": "analysis " * 200,
        "analysis_quality": {"ok": True, "warnings": []},
        "requirement_atom_check": {
            "atoms": [{"id": "analysis:summary", "type": "analysis", "required": True, "status": "passed"}],
            "summary": {"required_total": 1, "required_failed": 0, "required_unverified": 0},
        },
    }

    out = verify_node(state)

    events = _jsonl(Path(out["trace_path"]))
    verification = next(row for row in events if row["event"] == "verification_result")
    assert verification["ok"] is True
    assert verification["requirement_atom_status"]["atoms"][0]["id"] == "analysis:summary"


def test_report_node_records_final_gate_result_event(tmp_path: Path):
    run_dir = tmp_path / ".coding_agent" / "t"
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state_snapshot.json"),
        "final_path": str(run_dir / "final.json"),
        "project_memory_dir": str(tmp_path / ".coding_agent" / "project_memory"),
        "mode": "write",
        "read_only": False,
        "write_locked": False,
        "task": "create script",
        "task_contract": {},
        "task_intent": {},
        "verification": {"ok": True, "results": [], "test_results": {"total": 1, "runs": [{"total": 1}]}},
        "test_results": {"total": 1, "runs": [{"total": 1}]},
        "contract_ok": True,
        "semantic_contract_check": {"ok": True},
        "requirement_atom_summary": {"required_total": 1, "required_failed": 0, "required_unverified": 0},
        "changed_files": ["script.py"],
        "generated_files": [{"path": "script.py", "kind": "code"}],
        "repair_history": [],
        "needs_verification": False,
    }

    out = report_node(state)

    events = _jsonl(Path(out["trace_path"]))
    final_gate = next(row for row in events if row["event"] == "final_gate_result")
    assert final_gate["channel"] == "final_gate"
    assert final_gate["ok"] is True
    assert final_gate["outcome"] == "verified_ok"


def test_tool_write_records_generated_test_and_registry(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_path = tests_dir / "test_generated.py"
    test_path.write_text("def test_generated():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "generated_files": [],
        "changed_files": [],
    }
    result = {
        "tool": "write_file",
        "ok": True,
        "message": "written",
        "data": {
            "path": "tests/test_generated.py",
            "changed": True,
            "existed_before": False,
            "before_sha16": EMPTY_SHA16,
            "after_sha16": "after",
            "syntax_check": {"checked": True, "ok": True},
        },
    }

    _record_tool_write_artifact_state(state, "write_file", result)

    assert state["changed_files"] == ["tests/test_generated.py"]
    assert state["generated_files"][0]["path"] == "tests/test_generated.py"
    assert state["generated_files"][0]["kind"] == "test"
    assert state["generated_files"][0]["verification_role"] == "test"
    assert state["verification_test_registry"]["paths"] == ["tests/test_generated.py"]


def test_successful_tool_write_clears_stale_failed_write_for_same_generated_file(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_path = tests_dir / "test_generated.py"
    test_path.write_text("def test_generated():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "generated_files": [
            {
                "path": "tests/test_generated.py",
                "kind": "test",
                "ok": False,
                "failed_write": {"draft_relpath": "failed_writes/001_test_generated.py"},
            }
        ],
        "changed_files": [],
    }
    result = {
        "tool": "write_file",
        "ok": True,
        "message": "written",
        "data": {
            "path": "tests/test_generated.py",
            "changed": True,
            "existed_before": True,
            "before_sha16": "before",
            "after_sha16": "after",
            "syntax_check": {"checked": True, "ok": True},
        },
    }

    _record_tool_write_artifact_state(state, "write_file", result)

    assert state["generated_files"][0]["ok"] is True
    assert state["generated_files"][0]["failed_write"] is None


def test_run_tests_args_map_original_test_path_to_agent_internal_test_path(tmp_path: Path):
    work = tmp_path / ".coding_agent_test" / "t"
    (work / "tests").mkdir(parents=True)
    (work / "tests" / "test_tool.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "generated_files": [
            {
                "path": ".coding_agent_test/t/tests/test_tool.py",
                "original_path": "tests/test_tool.py",
                "kind": "test",
                "verification_role": "test",
            }
        ],
    }

    args, normalization = _normalize_run_tests_args(state, {"targets": ["tests/test_tool.py"], "pythonpath": ["."]})

    assert args["targets"] == [".coding_agent_test/t/tests/test_tool.py"]
    assert args["pythonpath"] == ["."]
    assert normalization["changes"][0]["reason"] == "mapped requested test path to agent internal test root"


def test_write_path_args_leave_project_paths_unchanged(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "thread_id": "real_thread",
    }

    args, normalization = _normalize_write_path_args(
        state,
        "write_file",
        {"path": "scripts/tool.py", "content": "VALUE = 1\n"},
    )

    assert args["path"] == "scripts/tool.py"
    assert normalization is None


def test_repair_target_policy_does_not_hard_lock_controller_suggestions(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "failure": {"failure_type": "contract_error", "signature": "sig"},
        "repair_controller": {
            "target_files": ["scripts/tool.py"],
            "route": "fix_implementation",
        },
        "strategy_decision": {
            "target_files": ["scripts/tool.py"],
        },
    }

    allowed = _repair_target_policy_result(
        state,
        "write_file",
        {"path": "docs/notes.md", "content": "notes\n"},
    )

    assert allowed is None


def test_repair_target_policy_blocks_only_required_force_path(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "failure": {"failure_type": "contract_error", "signature": "sig"},
        "force_repair_action": {
            "required_path": "scripts/tool.py",
            "allowed_target_files": ["scripts/tool.py"],
            "allowed_tools": ["write_file", "edit_file", "run_tests", "finish"],
        },
    }

    blocked = _repair_target_policy_result(
        state,
        "write_file",
        {"path": "docs/notes.md", "content": "notes\n"},
    )
    allowed = _repair_target_policy_result(
        state,
        "write_file",
        {"path": "scripts/tool.py", "content": "VALUE = 1\n"},
    )

    assert blocked is not None
    assert blocked.data["blocked_by_repair_target_policy"] is True
    assert blocked.data["allowed_target_files"] == ["scripts/tool.py"]
    assert blocked.data["force_repair_action"]["required_path"] == "scripts/tool.py"
    assert state["force_repair_action"]["trigger"] == "write_outside_repair_target"
    assert allowed is None


def test_force_repair_policy_blocks_wrong_write_path(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "force_repair_action": {
            "path": "scripts/tool.py",
            "required_path": "scripts/tool.py",
            "allowed_target_files": ["scripts/tool.py"],
            "allowed_tools": ["write_file", "edit_file", "run_tests", "finish"],
        },
    }

    blocked = _force_repair_policy_result(
        state,
        "write_file",
        {"path": "tests/test_tool.py", "content": "def test_bad():\n    assert False\n"},
    )
    allowed = _force_repair_policy_result(
        state,
        "write_file",
        {"path": "scripts/tool.py", "content": "VALUE = 1\n"},
    )

    assert blocked is not None
    assert blocked.data["blocked_by_force_repair_path"] is True
    assert blocked.data["attempted_path"] == "tests/test_tool.py"
    assert blocked.data["allowed_target_files"] == ["scripts/tool.py"]
    assert allowed is None


def test_tool_write_uses_explicit_file_plan_test_kind_without_filename_guessing(tmp_path: Path):
    path = tmp_path / "test_generated.py"
    path.write_text("def test_generated():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "file_plan": {"files": [{"path": "test_generated.py", "kind": "test"}]},
        "generated_files": [],
        "changed_files": [],
    }
    result = {
        "tool": "write_file",
        "ok": True,
        "message": "written",
        "data": {
            "path": "test_generated.py",
            "changed": True,
            "existed_before": False,
            "before_sha16": EMPTY_SHA16,
            "after_sha16": "after",
            "syntax_check": {"checked": True, "ok": True},
        },
    }

    _record_tool_write_artifact_state(state, "write_file", result)

    assert state["generated_files"][0]["kind"] == "test"
    assert state["verification_test_registry"]["paths"] == ["test_generated.py"]
