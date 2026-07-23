from typing import get_args

from coding_agent.core.decision_guard import AGENT_DECISION_SCHEMA_TEXT, TOOL_SCHEMA_TEXT
from coding_agent.core.schemas import ToolAction
from coding_agent.tools.registry import TOOL_EXECUTORS, tool_names, tool_schema_text, tool_specs


def test_tool_registry_matches_agent_decision_literal():
    literal_names = set(get_args(ToolAction.model_fields["tool"].annotation))
    registry_names = set(tool_names())
    assert registry_names == literal_names


def test_tool_registry_has_executor_for_every_tool():
    assert set(TOOL_EXECUTORS) == set(tool_names())


def test_decision_guard_tool_schema_is_generated_from_registry():
    assert TOOL_SCHEMA_TEXT == tool_schema_text()
    for name in tool_names():
        assert name in AGENT_DECISION_SCHEMA_TEXT
        assert f"- {name} " in TOOL_SCHEMA_TEXT or f"- {name} {{" in TOOL_SCHEMA_TEXT


def test_tool_specs_expose_required_args_and_write_category():
    specs = {spec.name: spec for spec in tool_specs()}
    assert specs["write_file"].write is True
    assert specs["edit_file"].write is True
    assert specs["read_file"].write is False
    assert specs["write_file"].required == ("path", "content")
    assert specs["edit_file"].required_any == (("old_text", "new_text"), ("replacements",))
    assert "filename" in specs["write_file"].aliases["path"]
