# Tool Module Layout

All agent tools live under `coding_agent/tools`.

## Required Structure

- `base.py`
  - Defines the generic tool protocol:
    `ToolSpec`, `BaseTool`, `FunctionTool`, and `ToolRegistry`.
- `registry.py`
  - Declares built-in tool specs.
  - Binds each spec to an executor.
  - Exposes `DEFAULT_TOOL_REGISTRY`, `execute_tool`, and prompt schema helpers.
- `*_tools.py`
  - Contains concrete tool implementations grouped by domain.
  - Each concrete function must return `ToolResult`.

## Adding A Tool

1. Implement the concrete function in a domain file such as `file_tools.py`,
   `shell_tools.py`, `test_tools.py`, or a new `*_tools.py` file.
2. Add a `ToolSpec` in `registry.py`.
3. Register the executor in `TOOL_EXECUTORS`.
4. Add focused tests for:
   - prompt schema exposure,
   - argument validation,
   - policy metadata such as read/write category,
   - structured `ToolResult` output.

## Rules

- Do not add tool execution branches in graph nodes.
- Do not maintain separate hardcoded tool name sets outside the registry.
- Do not return raw dictionaries from tool implementations.
- Do not silently accept non-canonical argument names. Return structured schema
  feedback so the LLM can repair the tool call.
