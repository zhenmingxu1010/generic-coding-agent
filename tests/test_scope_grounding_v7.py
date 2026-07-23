from __future__ import annotations

from pathlib import Path

from coding_agent.graph import route_after_tool
from coding_agent.scope.scope_grounding import ground_scope_contract_to_repo
from coding_agent.verification.plan_grounding import build_grounding_sources, validate_step_grounding


def test_llm_inferred_missing_modify_path_is_not_allowed_after_repo_scan(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "repo_map": {"files": ["src/app.py"], "py_files": ["src/app.py"]},
        "scope_contract": {
            "version": "scope_contract_v1",
            "allowed_modify_paths": ["relative/calc.py"],
            "path_operations": [
                {
                    "path": "relative/calc.py",
                    "operation": "allow_modify",
                    "evidence": "LLM guessed implementation path",
                    "source": "llm_write_scope_intent",
                }
            ],
        },
        "task_intent": {
            "allowed_modify_paths": ["relative/calc.py"],
            "scope_contract": {},
            "semantic_write_scope": {
                "allowed_modify_paths": ["relative/calc.py"],
                "consistency_issues": [],
            },
        },
    }

    result = ground_scope_contract_to_repo(state)

    assert result["changed"] is True
    assert state["scope_contract"]["allowed_modify_paths"] == []
    assert state["task_intent"]["allowed_modify_paths"] == []
    assert state["task_intent"]["semantic_write_scope"]["allowed_modify_paths"] == []
    assert state["scope_contract"]["unresolved_modify_targets"][0]["path"] == "relative/calc.py"
    assert state["scope_contract"]["path_operations"][0]["operation"] == "unresolved_modify_target"


def test_existing_llm_inferred_modify_path_is_kept_after_repo_scan(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "repo_map": {"files": ["src/app.py"], "py_files": ["src/app.py"]},
        "scope_contract": {
            "version": "scope_contract_v1",
            "allowed_modify_paths": ["src/app.py"],
            "path_operations": [
                {
                    "path": "src/app.py",
                    "operation": "allow_modify",
                    "source": "llm_write_scope_intent",
                }
            ],
        },
    }

    result = ground_scope_contract_to_repo(state)

    assert result["changed"] is False
    assert state["scope_grounding"]["allowed_modify_paths"] == ["src/app.py"]


def test_write_policy_block_routes_to_strategy_reflection_not_final_report():
    state = {
        "max_rounds": 20,
        "round_idx": 2,
        "last_tool_result": {
            "tool": "write_file",
            "ok": False,
            "message": "new file has no allowed write_intent",
            "data": {
                "blocked_by_policy": True,
                "path": "relative/calc.py",
                "approval_required": False,
            },
        },
    }

    route = route_after_tool(state)

    assert route == "strategy_reflection"
    assert state["failure"]["failure_type"] == "write_policy_blocked"
    assert state.get("stopped_reason") is None


def test_hard_policy_block_still_routes_to_report():
    state = {
        "max_rounds": 20,
        "round_idx": 2,
        "last_tool_result": {
            "tool": "write_file",
            "ok": False,
            "message": "global read-only/write-locked policy blocks write action",
            "data": {
                "blocked_by_policy": True,
                "read_only_violation": True,
                "path": "src/app.py",
            },
        },
    }

    route = route_after_tool(state)

    assert route == "report"
    assert state["stopped_reason"] == "blocked_by_policy"


def test_repair_action_budget_block_routes_back_to_repair_controller():
    state = {
        "max_rounds": 20,
        "round_idx": 6,
        "failure": {"failure_type": "pytest_failed", "signature": "sig"},
        "force_repair_action": {
            "blocked_read_attempts": 1,
            "allowed_tools": ["write_file", "edit_file", "run_tests", "finish"],
        },
        "last_tool_result": {
            "tool": "read_file",
            "ok": False,
            "message": "cached read already exists",
            "data": {
                "blocked_by_repair_action_budget": True,
                "path": ".coding_agent_test/t02/scripts/analyze_model_comparison.py",
            },
        },
    }

    route = route_after_tool(state)

    assert route == "repair"
    assert state.get("stopped_reason") is None


def test_pending_verification_after_write_runs_even_at_round_limit():
    state = {
        "max_rounds": 20,
        "round_idx": 20,
        "needs_verification": True,
        "last_tool_result": {
            "tool": "write_file",
            "ok": True,
            "message": "written",
            "data": {
                "path": "scripts/tool.py",
                "changed": True,
            },
        },
    }

    route = route_after_tool(state)

    assert route == "repo_scan"
    assert state.get("stopped_reason") is None


def test_failed_noop_edit_does_not_bypass_round_limit_with_stale_verification_flag():
    state = {
        "max_rounds": 20,
        "round_idx": 20,
        "needs_verification": True,
        "last_tool_result": {
            "tool": "edit_file",
            "ok": False,
            "message": "old_text not found",
            "data": {
                "path": ".coding_agent_test/t/tests/test_tool.py",
                "changed": False,
            },
        },
    }

    route = route_after_tool(state)

    assert route == "report"
    assert state.get("stopped_reason") == "max_rounds"


def test_declared_sandbox_fixture_is_not_treated_as_project_test_input(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "Run the command with INPUT.json and exit successfully.\n",
        encoding="utf-8",
    )
    state = {
        "workspace": str(tmp_path),
        "task": "Follow README.md.",
        "task_contract": {"requirement_atoms": [{
            "id": "requirement:cli",
            "description": "The documented CLI works.",
        }]},
    }
    step = {
        "name": "cli_fixture",
        "command": ["python", "-m", "sample.cli", "tests/fixtures/input.json"],
        "verifies": ["requirement:cli"],
        "basis": [{
            "source": "README.md",
            "quote": "Run the command with INPUT.json and exit successfully.",
        }],
        "expected": "The command exits successfully.",
        "sandbox": {
            "files": [{"path": "tests/fixtures/input.json", "content": "[]"}],
        },
    }

    grounding = validate_step_grounding(state, step)

    assert grounding["status"] == "accepted"
    assert grounding["unsupported_test_inputs"] == []


def test_grounding_accepts_markdown_stripped_ordered_contract_sentences(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "Every item must be an `int`. An unrelated note. Invalid items raise `TypeError`.\n",
        encoding="utf-8",
    )
    state = {
        "workspace": str(tmp_path),
        "task": "Follow README.md.",
        "task_contract": {"requirement_atoms": [{
            "id": "requirement:contract",
            "description": "README.md behavior works.",
        }]},
    }
    step = {
        "name": "direct_probe",
        "command": ["python", "probe.py"],
        "verifies": ["requirement:contract"],
        "basis": [{
            "source": "README.md",
            "quote": "Every item must be an int. Invalid items raise TypeError.",
        }],
        "expected": "Invalid items raise TypeError.",
        "sandbox": {"files": [{"path": "probe.py", "content": "raise TypeError\n"}]},
    }

    grounding = validate_step_grounding(state, step)

    assert grounding["status"] == "accepted"
    assert grounding["invalid_citations"] == []


def test_llm_verification_hint_is_not_an_authoritative_grounding_source(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "task": "Make the documented behavior work.",
        "task_contract": {"requirement_atoms": [{
            "id": "requirement:documented_behavior",
            "description": "The documented behavior works.",
            "evidence": ["documented behavior"],
            "verify_hint": "Passing pytest proves every documented behavior.",
            "source": "llm_task_requirement",
        }]},
    }

    sources = build_grounding_sources(state)

    assert "documented behavior" in sources["requirement:documented_behavior"]
    assert "The documented behavior works." not in sources["requirement:documented_behavior"]
    assert "Passing pytest proves" not in sources["requirement:documented_behavior"]


def test_modified_implementation_cannot_define_a_new_required_edge_case(tmp_path: Path):
    (tmp_path / "tool.py").write_text(
        "def total(values):\n    return sum(values)\n",
        encoding="utf-8",
    )
    state = {
        "workspace": str(tmp_path),
        "task": "Implement total(values) and run the existing tests.",
        "changed_files": ["tool.py"],
        "generated_files": [{"path": "tool.py", "kind": "code"}],
        "task_contract": {"requirement_atoms": [{
            "id": "requirement:total",
            "description": "total(values) returns the sum.",
        }]},
    }
    step = {
        "name": "invented_wrong_type",
        "command": ["python", "probe.py"],
        "verifies": ["requirement:total"],
        "basis": [{
            "source": "tool.py",
            "quote": "def total(values):\n    return sum(values)",
        }],
        "expected": "Passing an empty object raises TypeError.",
        "sandbox": {"files": [{"path": "probe.py", "content": "raise TypeError\n"}]},
    }

    grounding = validate_step_grounding(state, step)

    assert grounding["status"] == "rejected"
    assert grounding["implementation_only_citations"] == step["basis"]
    assert any("cannot create new required behavior" in reason for reason in grounding["reasons"])
