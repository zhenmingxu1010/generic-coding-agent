from __future__ import annotations

from typing import Any
from typing import Callable

from coding_agent.core.schemas import ToolResult
from .base import BaseTool, FunctionTool, ToolExecutor, ToolRegistry, ToolSpec
from .file_tools import list_files, filter_files, read_file, read_many_files, write_file, edit_file, search_text
from .shell_tools import run_shell, git_diff
from .test_tools import run_tests
from .python_tools import inspect_python


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_files",
        category="read",
        description="List files under the workspace.",
        parameters={"max_files": 500},
    ),
    ToolSpec(
        name="filter_files",
        category="read",
        description="Find files by glob, regex, suffix, or filename/content hint.",
        parameters={
            "glob": "scripts/*.sh",
            "regex": "^src/.*\\.py$",
            "suffixes": [".py"],
            "contains": "train",
            "max_matches": 100,
        },
    ),
    ToolSpec(
        name="read_file",
        category="read",
        description="Read a bounded slice of one workspace file.",
        parameters={"path": "relative/path.py", "start_line": 1, "limit": 220},
        required=("path",),
        aliases={"path": ("file_path", "filepath", "filename", "file", "target_file", "target_path")},
    ),
    ToolSpec(
        name="read_many_files",
        category="read",
        description="Read bounded content from multiple workspace files.",
        parameters={"paths": ["relative/path.py"], "per_file_chars": 8000, "max_total_chars": 40000},
        required=("paths",),
        aliases={"paths": ("files", "file_paths", "filepaths", "filenames")},
    ),
    ToolSpec(
        name="search_text",
        category="read",
        description="Search workspace text by literal string or regex.",
        parameters={"pattern": "text or regex", "max_matches": 80, "regex": False},
        required=("pattern",),
        aliases={"pattern": ("query", "text", "needle", "search")},
    ),
    ToolSpec(
        name="write_file",
        category="write",
        description="Create or replace a workspace file with full content.",
        parameters={"path": "relative/path.py", "content": "full file content"},
        required=("path", "content"),
        aliases={
            "path": ("file_path", "filepath", "filename", "file", "target_file", "target_path"),
            "content": ("contents", "text", "body", "new_content", "file_content"),
        },
        write=True,
    ),
    ToolSpec(
        name="edit_file",
        category="write",
        description="Atomically replace one or multiple exact text regions in one workspace file.",
        parameters={
            "path": "relative/path.py",
            "old_text": "exact old text",
            "new_text": "replacement text",
            "expected_replacements": 1,
            "replacements": [
                {
                    "old_text": "first exact old text",
                    "new_text": "first replacement text",
                    "expected_replacements": 1,
                },
                {
                    "old_text": "second exact old text",
                    "new_text": "second replacement text",
                    "expected_replacements": 1,
                },
            ],
        },
        required=("path",),
        required_any=(("old_text", "new_text"), ("replacements",)),
        aliases={
            "path": ("file_path", "filepath", "filename", "file", "target_file", "target_path"),
            "old_text": ("old", "before", "find", "search", "target_text", "original_text"),
            "new_text": ("new", "after", "replace", "replacement", "replacement_text", "updated_text"),
            "expected_replacements": ("count", "replace_count", "expected_count"),
            "replacements": ("edits", "changes"),
        },
        write=True,
    ),
    ToolSpec(
        name="run_tests",
        category="verify",
        description="Run tests through a structured test runner and parse results.",
        parameters={"kind": "pytest", "targets": ["tests/test_example.py"], "pythonpath": ["."], "timeout_sec": 120},
        aliases={"targets": ("paths", "files", "test_files", "pytest_targets")},
    ),
    ToolSpec(
        name="run_shell",
        category="execute",
        description="Run a bounded shell command in the workspace.",
        parameters={"command": ["python", "-m", "pytest", "-q"], "timeout_sec": 60},
        required=("command",),
        aliases={"command": ("cmd", "shell", "args")},
    ),
    ToolSpec(
        name="git_diff",
        category="read",
        description="Show git diff for the workspace.",
        parameters={},
    ),
    ToolSpec(
        name="inspect_python",
        category="read",
        description="Parse a Python file and return syntax/symbol information.",
        parameters={"path": "relative/path.py"},
        required=("path",),
        aliases={"path": ("file_path", "filepath", "filename", "file", "target_file", "target_path")},
    ),
    ToolSpec(
        name="finish",
        category="control",
        description="Stop with a concrete message/report.",
        parameters={"message": "...", "report": "..."},
    ),
)

def tool_specs() -> list[ToolSpec]:
    return DEFAULT_TOOL_REGISTRY.specs()


def tool_names() -> list[str]:
    return DEFAULT_TOOL_REGISTRY.names()


def tool_name_union() -> str:
    return "|".join(tool_names())


def tool_schema_text() -> str:
    return DEFAULT_TOOL_REGISTRY.schema_text()


def _plain(func: Callable[..., ToolResult]) -> ToolExecutor:
    def _exec(workspace: str, args: dict[str, Any], read_only: bool) -> ToolResult:
        return func(workspace, **args)
    return _exec


def _run_shell(workspace: str, args: dict[str, Any], read_only: bool) -> ToolResult:
    return run_shell(workspace, read_only=read_only, **args)


def _run_tests(workspace: str, args: dict[str, Any], read_only: bool) -> ToolResult:
    return run_tests(workspace, read_only=read_only, **args)


def _finish(workspace: str, args: dict[str, Any], read_only: bool) -> ToolResult:
    return ToolResult(tool="finish", ok=True, message=str(args.get("message", "finished")), data=args)


TOOL_EXECUTORS: dict[str, ToolExecutor] = {
    "list_files": _plain(list_files),
    "filter_files": _plain(filter_files),
    "read_file": _plain(read_file),
    "read_many_files": _plain(read_many_files),
    "write_file": _plain(write_file),
    "edit_file": _plain(edit_file),
    "search_text": _plain(search_text),
    "run_shell": _run_shell,
    "run_tests": _run_tests,
    "git_diff": _plain(git_diff),
    "inspect_python": _plain(inspect_python),
    "finish": _finish,
}


DEFAULT_TOOL_REGISTRY = ToolRegistry(
    tuple(FunctionTool(spec, TOOL_EXECUTORS[spec.name]) for spec in TOOL_SPECS)
)
TOOL_SPECS_BY_NAME = {spec.name: spec for spec in DEFAULT_TOOL_REGISTRY.specs()}
WRITE_TOOLS = DEFAULT_TOOL_REGISTRY.write_tools()
READ_TOOLS = DEFAULT_TOOL_REGISTRY.tools_by_category("read")


def execute_tool(
    workspace: str,
    tool: str,
    args: dict[str, Any],
    *,
    read_only: bool = False,
    allow_read_only_execution: bool = False,
) -> ToolResult:
    return DEFAULT_TOOL_REGISTRY.execute(
        workspace,
        tool,
        args,
        read_only=read_only,
        allow_read_only_execution=allow_read_only_execution,
    )
