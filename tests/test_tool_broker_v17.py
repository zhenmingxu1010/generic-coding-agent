from pathlib import Path

from coding_agent.tools.registry import execute_tool
from coding_agent.core.decision_guard import parse_agent_decision_with_self_correction


class DummyTrace:
    def __init__(self):
        self.events = []
    def event(self, name, **kwargs):
        self.events.append((name, kwargs))


class DummyClient:
    def __init__(self, response):
        self.response = response
        self.calls = []
    def chat(self, messages, purpose=None, max_tokens=None):
        self.calls.append({"messages": messages, "purpose": purpose, "max_tokens": max_tokens})
        return self.response


def test_tool_broker_rejects_alias_and_returns_feedback(tmp_path: Path):
    res = execute_tool(str(tmp_path), "write_file", {"filename": "b.py", "content": "print(1)\n"})
    assert not res.ok
    assert res.data["tool_schema_error"] is True
    assert res.data["normalized_args"]["path"] == "b.py"
    assert not (tmp_path / "b.py").exists()


def test_tool_broker_missing_args_structured_failure(tmp_path: Path):
    res = execute_tool(str(tmp_path), "edit_file", {"path": "x.py"})
    assert not res.ok
    assert res.data["tool_schema_error"] is True
    assert res.data["missing_args"] == ["old_text", "new_text"]


def test_edit_file_applies_multiple_replacements_atomically(tmp_path: Path):
    target = tmp_path / "module.py"
    target.write_text("VALUE = 1\nLABEL = 'old'\n", encoding="utf-8")

    res = execute_tool(
        str(tmp_path),
        "edit_file",
        {
            "path": "module.py",
            "replacements": [
                {"old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                {"old_text": "LABEL = 'old'", "new_text": "LABEL = 'new'"},
            ],
        },
    )

    assert res.ok is True
    assert res.data["changed"] is True
    assert len(res.data["replacement_results"]) == 2
    assert target.read_text(encoding="utf-8") == "VALUE = 2\nLABEL = 'new'\n"


def test_edit_file_batch_rolls_back_when_any_replacement_is_invalid(tmp_path: Path):
    target = tmp_path / "module.py"
    original = "VALUE = 1\nLABEL = 'old'\n"
    target.write_text(original, encoding="utf-8")

    res = execute_tool(
        str(tmp_path),
        "edit_file",
        {
            "path": "module.py",
            "replacements": [
                {"old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                {"old_text": "MISSING = True", "new_text": "MISSING = False"},
            ],
        },
    )

    assert res.ok is False
    assert res.data["changed"] is False
    assert res.data["failed_replacement"] == 1
    assert target.read_text(encoding="utf-8") == original


def test_decision_guard_asks_llm_to_fix_valid_json_wrong_schema(tmp_path):
    trace = DummyTrace()
    client = DummyClient('{"thought_summary":"fix schema","action":{"tool":"read_file","args":{"path":"script.py"}},"expectation":"inspect file"}')
    state = {"task":"sum durations", "mode":"write", "read_only":False, "task_contract":{}}
    decision = parse_agent_decision_with_self_correction(
        raw_text='{"duration": 3600}',
        client=client,
        system_prompt="Return AgentDecision JSON only.",
        state=state,
        trace=trace,
        role="repair",
        purpose="unit",
        max_attempts=1,
    )
    assert decision.action.tool == "read_file"
    assert decision.action.args["path"] == "script.py"
    assert client.calls, "LLM should be asked to correct wrong schema"
    assert any(name == "repair_decision_invalid" for name, _ in trace.events)


def test_decision_guard_prefers_agent_decision_over_inner_empty_dict(tmp_path):
    trace = DummyTrace()
    client = DummyClient("{}")
    state = {"task": "x", "mode": "write", "read_only": False, "task_contract": {}}
    raw = (
        '{"thought_summary":"truncated","action":{"tool":"write_file","args":{"path":"x.py",'
        '"content":"def f():\\n    return {}\\n"}}'
        '\n{"thought_summary":"fallback","action":{"tool":"read_file","args":{"path":"script.py"}},"expectation":"inspect"}'
    )

    decision = parse_agent_decision_with_self_correction(
        raw_text=raw,
        client=client,
        system_prompt="Return AgentDecision JSON only.",
        state=state,
        trace=trace,
        role="repair",
        purpose="unit",
        max_attempts=0,
    )

    assert decision.action.tool == "read_file"
    assert decision.action.args["path"] == "script.py"
    assert not client.calls


def test_decision_guard_rejects_action_blocked_by_force_repair(tmp_path):
    trace = DummyTrace()
    client = DummyClient(
        '{"thought_summary":"forced rewrite",'
        '"action":{"tool":"write_file","args":{"path":"script.py","content":"print(1)\\n"}},'
        '"expectation":"file rewritten"}'
    )
    state = {
        "task": "x",
        "mode": "write",
        "read_only": False,
        "task_contract": {},
        "force_repair_action": {
            "required_tool": "write_file",
            "allowed_tools": ["write_file", "run_tests", "finish"],
        },
    }

    decision = parse_agent_decision_with_self_correction(
        raw_text='{"thought_summary":"inspect","action":{"tool":"list_files","args":{}},"expectation":"inspect"}',
        client=client,
        system_prompt="Return AgentDecision JSON only.",
        state=state,
        trace=trace,
        role="strategy_reflection",
        purpose="unit",
        max_attempts=1,
    )

    assert decision.action.tool == "write_file"
    assert client.calls
    assert any(name == "strategy_reflection_decision_invalid" for name, _ in trace.events)


def test_decision_guard_rejects_force_repair_wrong_write_path(tmp_path):
    trace = DummyTrace()
    client = DummyClient(
        '{"thought_summary":"write required target",'
        '"action":{"tool":"write_file","args":{"path":"scripts/tool.py","content":"print(1)\\n"}},'
        '"expectation":"target rewritten"}'
    )
    state = {
        "task": "x",
        "mode": "write",
        "read_only": False,
        "task_contract": {},
        "force_repair_action": {
            "required_path": "scripts/tool.py",
            "allowed_target_files": ["scripts/tool.py"],
            "allowed_tools": ["write_file", "edit_file", "run_tests", "finish"],
        },
    }

    decision = parse_agent_decision_with_self_correction(
        raw_text=(
            '{"thought_summary":"wrong path",'
            '"action":{"tool":"write_file","args":{"path":".coding_agent_test/t/tests/test_tool.py","content":"def test_x(): pass\\n"}},'
            '"expectation":"test rewritten"}'
        ),
        client=client,
        system_prompt="Return AgentDecision JSON only.",
        state=state,
        trace=trace,
        role="repair",
        purpose="unit",
        max_attempts=1,
    )

    assert decision.action.tool == "write_file"
    assert decision.action.args["path"] == "scripts/tool.py"
    assert client.calls
    assert any(name == "repair_decision_invalid" for name, _ in trace.events)


def test_decision_guard_honors_force_blocked_tools_even_if_allowed(tmp_path):
    trace = DummyTrace()
    client = DummyClient(
        '{"thought_summary":"rewrite target",'
        '"action":{"tool":"write_file","args":{"path":"scripts/tool.py","content":"print(1)\\n"}},'
        '"expectation":"target rewritten"}'
    )
    state = {
        "task": "x",
        "mode": "write",
        "read_only": False,
        "task_contract": {},
        "force_repair_action": {
            "allowed_tools": ["write_file", "run_shell", "finish"],
            "blocked_tools": ["run_shell"],
            "required_path": "scripts/tool.py",
            "allowed_target_files": ["scripts/tool.py"],
        },
    }

    decision = parse_agent_decision_with_self_correction(
        raw_text='{"thought_summary":"probe","action":{"tool":"run_shell","args":{"command":"python -c \\"print(1)\\""}},"expectation":"probe"}',
        client=client,
        system_prompt="Return AgentDecision JSON only.",
        state=state,
        trace=trace,
        role="repair",
        purpose="unit",
        max_attempts=1,
    )

    assert decision.action.tool == "write_file"
    assert client.calls
    assert any(name == "repair_decision_invalid" for name, _ in trace.events)


def test_decision_guard_turns_unrecoverable_schema_error_into_finish(tmp_path):
    trace = DummyTrace()
    client = DummyClient('{"still":"wrong"}')
    state = {"task":"x", "mode":"write", "read_only":False, "task_contract":{}}
    decision = parse_agent_decision_with_self_correction(
        raw_text='{"duration": 3600}',
        client=client,
        system_prompt="Return AgentDecision JSON only.",
        state=state,
        trace=trace,
        role="repair",
        purpose="unit",
        max_attempts=1,
    )
    assert decision.action.tool == "finish"
    assert state["failure"]["failure_type"] == "llm_decision_schema_error"


def test_decision_guard_marks_unrecoverable_force_repair_violation_blocked(tmp_path):
    trace = DummyTrace()
    client = DummyClient(
        '{"thought_summary":"still inspect",'
        '"action":{"tool":"read_file","args":{"path":"script.py"}},'
        '"expectation":"inspect"}'
    )
    state = {
        "task": "x",
        "mode": "write",
        "read_only": False,
        "task_contract": {},
        "needs_verification": True,
        "failure": {"signature": "sig"},
        "force_repair_action": {
            "allowed_tools": ["write_file", "run_tests", "finish"],
            "path": "script.py",
        },
        "repair_read_cache": {
            "sig|script.py": {
                "sha16": "abc",
                "reads": [{
                    "path": "script.py",
                    "start_line": 1,
                    "end_line": 2,
                    "total_lines": 2,
                    "sha16": "abc",
                    "content": "1: print('old')\n",
                }],
            }
        },
    }

    decision = parse_agent_decision_with_self_correction(
        raw_text='{"thought_summary":"inspect","action":{"tool":"read_file","args":{"path":"script.py"}},"expectation":"inspect"}',
        client=client,
        system_prompt="Return AgentDecision JSON only.",
        state=state,
        trace=trace,
        role="repair",
        purpose="unit",
        max_attempts=1,
    )

    assert decision.action.tool == "finish"
    assert state["failure"]["failure_type"] == "repair_protocol_blocked"
    assert state["stopped_reason"] == "repair_protocol_blocked"
    assert state["needs_verification"] is False
