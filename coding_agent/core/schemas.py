from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class TaskSpec(BaseModel):
    task_type: str = Field(default="coding")
    objective: str
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    implementation_requirements: list[dict[str, Any]] = Field(default_factory=list)
    implementation_contract: dict[str, Any] = Field(default_factory=dict)
    task_completeness: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[dict[str, Any]] = Field(default_factory=list)
    requirement_grounding: dict[str, Any] = Field(default_factory=dict)
    workflow_steps: list[dict[str, Any]] = Field(default_factory=list)
    response_requirements: list[dict[str, Any]] = Field(default_factory=list)
    read_only: bool = False
    create_paths: list[str] = Field(default_factory=list)
    read_reference_paths: list[str] = Field(default_factory=list)
    write_scope_intent: dict[str, Any] = Field(default_factory=dict)


class ToolAction(BaseModel):
    tool: Literal[
        "list_files",
        "filter_files",
        "read_file",
        "read_many_files",
        "write_file",
        "edit_file",
        "search_text",
        "run_tests",
        "run_shell",
        "git_diff",
        "inspect_python",
        "finish",
    ]
    args: dict[str, Any] = Field(default_factory=dict)


class AgentDecision(BaseModel):
    thought_summary: str = ""
    action: ToolAction
    expectation: str = ""


class TaskContract(BaseModel):
    mode: str = "unknown"
    read_only: bool = False
    objective: str = ""
    constraints: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    required_behaviors: list[str] = Field(default_factory=list)
    verification_gates: list[str] = Field(default_factory=list)
    quality_gates: list[str] = Field(default_factory=list)
    prohibited_actions: list[str] = Field(default_factory=list)
    requirement_atoms: list[dict[str, Any]] = Field(default_factory=list)
    requirement_atom_summary: dict[str, Any] = Field(default_factory=dict)
    implementation_contract: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[dict[str, Any]] = Field(default_factory=list)
    source: str = ""


class ToolResult(BaseModel):
    tool: str
    ok: bool
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class CommandResult(BaseModel):
    name: str
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    executed: bool = True
    failure_kind: str = ""
    actual_returncode: int | None = None
    success_exit_codes: list[int] = Field(default_factory=lambda: [0])


class VerificationResult(BaseModel):
    ok: bool = False
    results: list[CommandResult] = Field(default_factory=list)
    test_results: dict[str, Any] = Field(default_factory=dict)
    compile_ok: bool | None = None
    pytest_ok: bool | None = None
    analysis_ok: bool | None = None
    quality_warnings: list[str] = Field(default_factory=list)


class FailureInfo(BaseModel):
    failure_type: str
    priority: int = 10
    message: str = ""
    target_file: str | None = None
    signature: str
    raw_excerpt: str = ""


class PatchRecord(BaseModel):
    round_idx: int
    strategy: str
    changed: bool
    files_changed: list[str] = Field(default_factory=list)
    message: str = ""


class FilePlanItem(BaseModel):
    path: str
    purpose: str = ""
    kind: Literal["code", "test", "readme", "config", "data", "other"] = "other"


class VerificationStep(BaseModel):
    name: str
    command: list[str]
    verifies: list[str] = Field(default_factory=list)
    basis: list[dict[str, str]] = Field(default_factory=list)
    expected: str = ""
    timeout_sec: int = 180
    success_exit_codes: list[int] = Field(default_factory=lambda: [0])
    stdin: str | None = None
    sandbox: dict[str, Any] | None = None


class FilePlan(BaseModel):
    files: list[FilePlanItem] = Field(default_factory=list)
    verify_steps: list[VerificationStep] = Field(default_factory=list)
    rationale: str = ""
