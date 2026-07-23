from pathlib import Path

import pytest

from coding_agent.core.schemas import ToolResult
from coding_agent.tools.base import BaseTool, FunctionTool, ToolRegistry, ToolSpec
from coding_agent.tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    WRITE_TOOLS,
    execute_tool,
    tool_names,
)


def test_default_registry_exposes_uniform_tool_objects():
    for name in tool_names():
        tool = DEFAULT_TOOL_REGISTRY.get(name)
        assert isinstance(tool, BaseTool)
        assert tool.name == name
        assert tool.description
        assert isinstance(tool.parameters, dict)
        assert callable(tool.call)

    assert DEFAULT_TOOL_REGISTRY.write_tools() == WRITE_TOOLS


def test_tool_package_public_api_exports_core_contracts():
    import coding_agent.tools as tools

    assert tools.BaseTool is BaseTool
    assert tools.FunctionTool is FunctionTool
    assert tools.ToolRegistry is ToolRegistry
    assert tools.ToolSpec is ToolSpec
    assert tools.DEFAULT_TOOL_REGISTRY is DEFAULT_TOOL_REGISTRY


def test_registry_rejects_duplicate_tool_names():
    tool = DEFAULT_TOOL_REGISTRY.get("finish")
    assert tool is not None

    registry = ToolRegistry([tool])
    with pytest.raises(ValueError):
        registry.register(tool)


def test_registry_executes_custom_function_tool(tmp_path: Path):
    spec = ToolSpec(
        name="echo_path",
        category="read",
        description="Return the requested path.",
        parameters={"path": "relative/path.py"},
        required=("path",),
        aliases={"path": ("filename",)},
    )

    def _echo(workspace: str, args: dict, read_only: bool) -> ToolResult:
        return ToolResult(tool="echo_path", ok=True, message="ok", data={"workspace": workspace, "path": args["path"]})

    registry = ToolRegistry([FunctionTool(spec, _echo)])

    ok = registry.execute(str(tmp_path), "echo_path", {"path": "a.py"})
    assert ok.ok
    assert ok.data["path"] == "a.py"

    schema_error = registry.execute(str(tmp_path), "echo_path", {"filename": "a.py"})
    assert not schema_error.ok
    assert schema_error.data["tool_schema_error"] is True
    assert schema_error.data["normalized_args"] == {"path": "a.py"}


def test_execute_tool_delegates_to_default_registry(tmp_path: Path):
    res = execute_tool(str(tmp_path), "write_file", {"filename": "x.py", "content": "VALUE = 1\n"})
    assert not res.ok
    assert res.data["tool_schema_error"] is True
    assert not (tmp_path / "x.py").exists()


def test_read_only_policy_uses_tool_metadata(tmp_path: Path):
    res = DEFAULT_TOOL_REGISTRY.execute(
        str(tmp_path),
        "write_file",
        {"path": "x.py", "content": "VALUE = 1\n"},
        read_only=True,
    )
    assert not res.ok
    assert res.data["blocked_by_policy"] is True
    assert not (tmp_path / "x.py").exists()
