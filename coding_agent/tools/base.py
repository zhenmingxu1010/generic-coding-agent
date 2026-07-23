from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from coding_agent.core.schemas import ToolResult


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: str
    description: str
    parameters: dict[str, Any]
    required: tuple[str, ...] = ()
    required_any: tuple[tuple[str, ...], ...] = ()
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    write: bool = False

    def schema_line(self) -> str:
        return f"- {self.name} {json.dumps(self.parameters, ensure_ascii=False)}"


ToolExecutor = Callable[[str, dict[str, Any], bool], ToolResult]


def _pop_first(args: dict[str, Any], names: list[str], default: Any = None) -> Any:
    for name in names:
        if name in args:
            return args.pop(name)
    return default


class BaseTool:
    """Uniform runtime interface for every agent tool."""

    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def category(self) -> str:
        return self.spec.category

    @property
    def description(self) -> str:
        return self.spec.description

    @property
    def parameters(self) -> dict[str, Any]:
        return dict(self.spec.parameters)

    @property
    def required(self) -> tuple[str, ...]:
        return self.spec.required

    @property
    def required_any(self) -> tuple[tuple[str, ...], ...]:
        return self.spec.required_any

    @property
    def aliases(self) -> dict[str, tuple[str, ...]]:
        return dict(self.spec.aliases)

    @property
    def write(self) -> bool:
        return self.spec.write

    def schema_line(self) -> str:
        return self.spec.schema_line()

    def normalize_args(self, args: dict[str, Any] | None) -> dict[str, Any]:
        normalized: dict[str, Any] = dict(args or {})
        for canonical, aliases in self.spec.aliases.items():
            if canonical in normalized:
                continue
            val = _pop_first(normalized, list(aliases))
            if val is not None:
                normalized[canonical] = val

        if self.name == "filter_files":
            if "glob" not in normalized and "pattern" in normalized and not normalized.get("regex"):
                normalized["glob"] = normalized.pop("pattern")

        return normalized

    def missing_args(self, args: dict[str, Any]) -> list[str]:
        missing = [k for k in self.spec.required if k not in args or args.get(k) is None]
        alternatives = self.spec.required_any
        if alternatives and not any(
            all(key in args and args.get(key) is not None for key in group)
            for group in alternatives
        ):
            # Report the first documented form so correction prompts remain
            # stable, while accepting any complete alternative form.
            missing.extend(
                key
                for key in alternatives[0]
                if key not in args or args.get(key) is None
            )
        return missing

    def validate_args(self, raw_args: dict[str, Any] | None) -> tuple[dict[str, Any], ToolResult | None]:
        raw = dict(raw_args or {})
        normalized = self.normalize_args(raw)

        if normalized != raw:
            return normalized, ToolResult(
                tool=self.name,
                ok=False,
                message="tool argument schema mismatch; regenerate the tool call using the canonical schema",
                data={
                    "tool_schema_error": True,
                    "raw_args": raw,
                    "normalized_args": normalized,
                    "schema_feedback": "Use canonical argument names exactly as documented by the tool schema.",
                },
            )

        missing = self.missing_args(normalized)
        if missing:
            return normalized, ToolResult(
                tool=self.name,
                ok=False,
                message=f"missing required tool args: {missing}",
                data={
                    "tool_schema_error": True,
                    "missing_args": missing,
                    "raw_args": raw,
                    "normalized_args": normalized,
                },
            )

        return normalized, None

    def call(self, workspace: str, args: dict[str, Any], *, read_only: bool = False) -> ToolResult:
        raise NotImplementedError


class FunctionTool(BaseTool):
    def __init__(self, spec: ToolSpec, executor: ToolExecutor) -> None:
        super().__init__(spec)
        self.executor = executor

    def call(self, workspace: str, args: dict[str, Any], *, read_only: bool = False) -> ToolResult:
        return self.executor(workspace, args, read_only)


class ToolRegistry:
    def __init__(self, tools: list[BaseTool] | tuple[BaseTool, ...] | None = None) -> None:
        self._tools: dict[str, BaseTool] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def write_tools(self) -> set[str]:
        return {tool.name for tool in self._tools.values() if tool.write}

    def tools_by_category(self, category: str) -> set[str]:
        return {tool.name for tool in self._tools.values() if tool.category == category}

    def schema_text(self) -> str:
        return "Tool schemas:\n" + "\n".join(tool.schema_line() for tool in self._tools.values())

    def execute(self, workspace: str, name: str, args: dict[str, Any] | None, *, read_only: bool = False) -> ToolResult:
        raw_args = dict(args or {})
        tool = self.get(name)
        if tool is None:
            return ToolResult(tool=name, ok=False, message=f"unknown tool: {name}", data={"normalized_args": raw_args})

        normalized_args, schema_error = tool.validate_args(raw_args)
        if read_only and tool.write:
            return ToolResult(
                tool=name,
                ok=False,
                message=f"read-only policy blocks write tool: {name}",
                data={"blocked_by_policy": True, "raw_args": raw_args, "normalized_args": normalized_args},
            )
        if schema_error is not None:
            return schema_error

        try:
            return tool.call(workspace, normalized_args, read_only=read_only)
        except TypeError as e:
            return ToolResult(
                tool=name,
                ok=False,
                message=f"tool argument schema error: {e}",
                data={"normalized_args": normalized_args, "error_type": "TypeError"},
            )
        except Exception as e:
            return ToolResult(
                tool=name,
                ok=False,
                message=f"tool execution error: {e}",
                data={"normalized_args": normalized_args, "error_type": e.__class__.__name__},
            )
