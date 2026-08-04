from __future__ import annotations

import json
from pathlib import Path

from coding_agent.nodes.failure_owner import failure_owner_node
from coding_agent.nodes.repair import _repair_target_contents_prompt, repair_node
from coding_agent.nodes.report import report_node
from coding_agent.nodes.tool_exec import _force_repair_policy_result
from coding_agent.repair.failure_analysis import decompose_failure_issues
from coding_agent.repair.repair_controller import (
    build_repair_controller,
    finalize_repair_controller,
    force_action_from_controller,
)
from coding_agent.core.state import AgentState


def _base_state(tmp_path: Path) -> dict:
    run_dir = tmp_path / ".coding_agent" / "t"
    run_dir.mkdir(parents=True)
    return {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "final_path": str(run_dir / "final.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "mode": "write",
        "read_only": False,
        "task": "Create scripts/tool.py and verify its requested behavior.",
        "file_plan": {
            "files": [
                {"path": "scripts/tool.py", "kind": "code"},
                {"path": ".coding_agent_test/t/tests/test_tool.py", "kind": "test"},
            ]
        },
        "generated_files": [
            {"path": "scripts/tool.py", "kind": "code"},
            {"path": ".coding_agent_test/t/tests/test_tool.py", "kind": "test"},
        ],
        "changed_files": [
            "scripts/tool.py",
            ".coding_agent_test/t/tests/test_tool.py",
        ],
        "workspace_baseline": {"files": []},
    }


def test_failed_requirement_needs_owner_decision_before_writes(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {"failure_type": "contract_error", "signature": "sig"}
    state["requirement_atom_check"] = {
        "atoms": [
            {
                "id": "requirement:observable_behavior",
                "type": "behavior",
                "status": "failed",
                "required": True,
                "description": "The public command must produce the requested result.",
                "details": {
                    "verification_claim": {
                        "reason": "the executed command returned an unexpected value"
                    }
                },
            }
        ]
    }

    controller = build_repair_controller(state)

    assert controller["route"] == "inspect_more"
    assert controller["failure_owner"] == "unknown"
    assert controller["target_files"] == []
    issue = controller["issues"][0]
    assert issue["owner"] == "requirement"
    assert issue["atom_id"] == "requirement:observable_behavior"
    assert "unexpected value" in issue["repair_hint"]


def test_llm_owner_decision_finalizes_implementation_target(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {"failure_type": "contract_error", "signature": "sig"}
    controller = finalize_repair_controller(
        state,
        {
            "failure_owner": "implementation",
            "strategy": "fix_implementation",
            "target_files": ["scripts/tool.py"],
            "reason": "runtime evidence identifies the implementation",
        },
    )
    force = force_action_from_controller(controller)

    assert controller["route"] == "fix_implementation"
    assert controller["finalized"] is True
    assert controller["target_files"] == ["scripts/tool.py"]
    assert force is not None
    assert force["allowed_target_files"] == ["scripts/tool.py"]

    state["repair_controller"] = controller
    state["force_repair_action"] = force
    blocked = _force_repair_policy_result(
        state,
        "write_file",
        {
            "path": ".coding_agent_test/t/tests/test_tool.py",
            "content": "def test_tool():\n    assert True\n",
        },
    )
    allowed = _force_repair_policy_result(
        state,
        "write_file",
        {"path": "scripts/tool.py", "content": "def main():\n    return 0\n"},
    )

    assert blocked is not None
    assert blocked.data["blocked_by_force_repair_path"] is True
    assert allowed is None


def test_behavior_failure_keeps_authorized_scope_and_drops_invented_target(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {"failure_type": "contract_error", "signature": "behavior-sig"}
    state["scope_contract"] = {
        "allowed_modify_paths": [
            "package/hooks.py",
            "package/generate.py",
            "package/exceptions.py",
        ]
    }
    state["failure_issues"] = [{
        "owner": "implementation",
        "type": "verification_command_failed",
        "message": "the requested failure behavior was not observed",
        "target_file": None,
    }]

    controller = finalize_repair_controller(
        state,
        {
            "failure_owner": "implementation",
            "strategy": "fix_implementation",
            "target_files": ["package/hooks.py", "package/main.py"],
            "reason": "repair the failed observable behavior",
        },
    )

    assert controller["target_files"] == [
        "package/hooks.py",
        "package/generate.py",
        "package/exceptions.py",
    ]


def test_repository_discoverable_repair_keeps_traceback_target_over_llm_guess(tmp_path: Path):
    state = _base_state(tmp_path)
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "storage.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "metadata.toml").write_text("[project]\n", encoding="utf-8")
    state.update({
        "mode": "debug",
        "task": "Inspect and repair the existing project implementation.",
        "task_intent": {"source_modify_intent": True},
        "task_completeness": {"target_clarity": "repository_discoverable"},
        "scope_contract": {
            "allowed_modify_paths": ["metadata.toml"],
            "semantic_write_scope_source": "llm",
        },
        "failure": {"failure_type": "runtime_error", "signature": "runtime-target"},
        "failure_issues": [{
            "owner": "implementation",
            "type": "attributeerror",
            "file": "package/storage.py",
            "target_file": "package/storage.py",
            "message": "runtime traceback points to the implementation",
        }],
    })

    controller = build_repair_controller(state)

    assert controller["target_files"] == ["package/storage.py"]


def test_safe_create_unlocalized_failure_keeps_generated_code_batch(tmp_path: Path):
    state = _base_state(tmp_path)
    state["file_plan"]["files"].extend([
        {"path": "package/__main__.py", "kind": "code"},
        {"path": "package/cli.py", "kind": "code"},
    ])
    state["generated_files"].extend([
        {"path": "package/__main__.py", "kind": "code"},
        {"path": "package/cli.py", "kind": "code"},
    ])
    state["failure"] = {"failure_type": "contract_error", "signature": "safe-create-runtime"}
    state["failure_issues"] = [{
        "owner": "implementation",
        "type": "contract_failure",
        "message": "a public subprocess reported the wrong exit status",
    }]

    controller = build_repair_controller(state)

    assert controller["target_files"] == [
        "scripts/tool.py",
        "package/__main__.py",
        "package/cli.py",
    ]


def test_safe_create_localized_failure_allows_adjacent_generated_owner(tmp_path: Path):
    state = _base_state(tmp_path)
    state["task_intent"] = {"operation_mode": "safe_create"}
    state["file_plan"]["files"].extend([
        {"path": "package/storage.py", "kind": "code"},
        {"path": "package/models.py", "kind": "code"},
    ])
    state["generated_files"].extend([
        {"path": "package/storage.py", "kind": "code"},
        {"path": "package/models.py", "kind": "code"},
    ])
    state["failure"] = {"failure_type": "runtime_error", "signature": "safe-create-localized"}
    state["failure_issues"] = [{
        "owner": "implementation",
        "type": "attributeerror",
        "file": "package/storage.py",
        "target_file": "package/storage.py",
        "message": "a generated dependency does not expose the called attribute",
    }]

    controller = build_repair_controller(state)
    force = force_action_from_controller(controller)

    assert controller["target_files"] == [
        "package/storage.py",
        "scripts/tool.py",
        "package/models.py",
    ]
    assert force is not None
    assert "package/models.py" in force["allowed_target_files"]


def test_generated_test_decision_cannot_target_project_code(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {"failure_type": "test_failure", "signature": "sig"}

    controller = finalize_repair_controller(
        state,
        {
            "failure_owner": "generated_test",
            "strategy": "fix_generated_test",
            "target_files": [
                "scripts/tool.py",
                ".coding_agent_test/t/tests/test_tool.py",
            ],
            "reason": "the generated oracle does not match the requested API",
        },
    )

    assert controller["route"] == "fix_generated_test"
    assert controller["target_files"] == [".coding_agent_test/t/tests/test_tool.py"]


def test_runtime_failure_with_non_test_target_is_deterministic(monkeypatch, tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {
        "failure_type": "runtime_error",
        "target_file": "scripts/tool.py",
        "signature": "runtime-sig",
    }

    class _UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("a structured implementation failure should not need another LLM call")

    monkeypatch.setattr("coding_agent.nodes.failure_owner.OpenAICompatClient", _UnexpectedClient)

    out = failure_owner_node(state)

    assert out["failure_owner_mode"] == "deterministic"
    assert out["failure_owner"] == "implementation"
    assert out["strategy_decision"]["strategy"] == "fix_implementation"
    assert out["repair_controller"]["target_files"] == ["scripts/tool.py"]


def test_absolute_traceback_path_inside_workspace_becomes_relative_repair_target(tmp_path: Path):
    state = _base_state(tmp_path)
    absolute_target = tmp_path / "scripts" / "tool.py"
    state["failure"] = {"failure_type": "runtime_error", "signature": "runtime-sig"}
    state["failure_issues"] = [{
        "owner": "implementation",
        "type": "typeerror",
        "file": str(absolute_target),
        "target_file": str(absolute_target),
        "message": "TypeError from generated implementation",
    }]

    controller = build_repair_controller(state)
    force = force_action_from_controller(controller)

    assert controller["target_files"] == ["scripts/tool.py"]
    assert controller["primary_issue"]["file"] == "scripts/tool.py"
    assert force is not None
    assert force["required_path"] == "scripts/tool.py"


def test_finalized_owner_decision_normalizes_workspace_absolute_target(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {"failure_type": "runtime_error", "signature": "runtime-sig"}

    controller = finalize_repair_controller(
        state,
        {
            "failure_owner": "implementation",
            "strategy": "fix_implementation",
            "target_files": [str(tmp_path / "scripts" / "tool.py")],
            "reason": "traceback identifies generated implementation",
        },
    )

    assert controller["target_files"] == ["scripts/tool.py"]
    assert force_action_from_controller(controller)["required_path"] == "scripts/tool.py"


def test_protected_external_test_failure_uses_deterministic_implementation_owner(monkeypatch, tmp_path: Path):
    state = _base_state(tmp_path)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_tool.py").write_text("def test_value():\n    assert False\n", encoding="utf-8")
    state["failure"] = {
        "failure_type": "test_assertion_error",
        "target_file": "tests/test_tool.py",
        "signature": "assertion-sig",
    }
    state["scope_contract"] = {
        "allowed_modify_paths": ["scripts/tool.py"],
        "protected_existing_globs": ["tests/**"],
    }

    class _UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("protected external-test failures have a deterministic owner")

    monkeypatch.setattr("coding_agent.nodes.failure_owner.OpenAICompatClient", _UnexpectedClient)

    out = failure_owner_node(state)

    assert out["failure_owner_mode"] == "deterministic"
    assert out["failure_owner"] == "implementation"
    assert out["repair_controller"]["target_files"] == ["scripts/tool.py"]


def test_repair_prompt_includes_exact_controller_target(tmp_path: Path):
    state = _base_state(tmp_path)
    target = tmp_path / "scripts" / "tool.py"
    target.parent.mkdir(parents=True)
    target.write_text("def value():\n    return 1\n", encoding="utf-8")

    text = _repair_target_contents_prompt(state, {"target_files": ["scripts/tool.py"]})

    assert "===== scripts/tool.py =====" in text
    assert "return 1" in text


def test_repair_prompt_keeps_head_and_tail_of_long_target(tmp_path: Path):
    state = _base_state(tmp_path)
    target = tmp_path / "scripts" / "long_tool.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "HEAD_ANCHOR = 1\n"
        + ("middle_value = 0\n" * 1200)
        + "TAIL_ANCHOR = 2\n",
        encoding="utf-8",
    )

    text = _repair_target_contents_prompt(
        state,
        {"target_files": ["scripts/long_tool.py"]},
    )

    assert "HEAD_ANCHOR = 1" in text
    assert "TAIL_ANCHOR = 2" in text
    assert "middle omitted" in text


def test_repair_node_stops_before_llm_when_call_budget_is_exhausted(monkeypatch, tmp_path: Path):
    state = _base_state(tmp_path)
    state.update(
        {
            "max_repair_llm_calls": 2,
            "repair_llm_call_count": 2,
            "failure": {"failure_type": "runtime_error", "signature": "runtime-sig"},
        }
    )

    class _UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("the repair budget must stop before creating an LLM client")

    monkeypatch.setattr("coding_agent.nodes.repair.OpenAICompatClient", _UnexpectedClient)

    out = repair_node(state)

    assert out["decision"]["action"]["tool"] == "finish"
    assert out["stopped_reason"] == "repair_llm_call_budget_exhausted"
    assert out["repair_llm_call_budget"] == {"limit": 2, "used": 2, "remaining": 0}


def test_unverified_requirements_collect_evidence_without_writing(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {"failure_type": "contract_error", "signature": "evidence-gap"}
    state["verification"] = {
        "ok": False,
        "results": [{"name": "behavior_probe", "returncode": 0, "timed_out": False}],
    }
    state["requirement_atom_check"] = {
        "atoms": [
            {
                "id": "requirement:alternate_case",
                "type": "behavior",
                "status": "unverified",
                "required": True,
            }
        ]
    }

    controller = build_repair_controller(state)
    force = force_action_from_controller(controller)

    assert controller["route"] == "complete_verification"
    assert controller["failure_owner"] == "verification_evidence"
    assert "write_file" in controller["blocked_tools"]
    assert force is not None
    assert "write_file" in force["blocked_tools"]


def test_concrete_implementation_issue_overrides_unverified_evidence_gap(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {
        "failure_type": "deliverable_consistency_error",
        "signature": "contract-defect",
    }
    state["failure_issues"] = [{
        "owner": "implementation",
        "type": "deliverable_consistency_error",
        "file": "scripts/tool.py",
        "target_file": "scripts/tool.py",
        "message": "the documented input type is not enforced",
        "source": "deliverable_review",
    }]
    state["verification"] = {
        "ok": False,
        "results": [{"name": "pytest", "returncode": 0, "timed_out": False}],
    }
    state["requirement_atom_check"] = {
        "atoms": [{
            "id": "requirement:reference_contract",
            "type": "behavior",
            "status": "unverified",
            "required": True,
        }]
    }

    controller = build_repair_controller(state)

    assert controller["route"] == "fix_implementation"
    assert controller["failure_owner"] == "implementation"
    assert controller["target_files"] == ["scripts/tool.py"]
    assert "edit_file" not in controller["blocked_tools"]


def test_deliverable_issue_keeps_authorized_multifile_repair_scope(
    tmp_path: Path,
):
    state = _base_state(tmp_path)
    state["failure"] = {
        "failure_type": "deliverable_consistency_error",
        "signature": "partial-multifile-fix",
    }
    state["scope_contract"] = {
        "allowed_modify_paths": [
            "package/hooks.py",
            "package/generate.py",
            "package/exceptions.py",
        ],
    }
    state["failure_issues"] = [{
        "owner": "implementation",
        "type": "deliverable_consistency_error",
        "file": "package/exceptions.py",
        "target_file": "package/exceptions.py",
        "message": "the exception exists but the required behavior is not implemented",
        "source": "deliverable_review",
    }]

    controller = build_repair_controller(state)
    controller["allowed_read_files"] = list(controller["target_files"])
    force = force_action_from_controller(controller)

    assert controller["target_files"] == [
        "package/hooks.py",
        "package/generate.py",
        "package/exceptions.py",
    ]
    assert force is not None
    assert force["allowed_target_files"] == controller["target_files"]
    assert "read_file" in force["allowed_tools"]


def test_failure_issues_are_part_of_persistent_agent_state():
    assert "failure_issues" in AgentState.__annotations__


def test_zero_collected_routes_to_registered_tests(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {"failure_type": "test_failure", "signature": "zero"}
    state["verification"] = {
        "test_results": {
            "runs": [
                {
                    "total": 0,
                    "stdout": "collected 0 items",
                    "type": "pytest_zero_collected",
                }
            ]
        }
    }

    controller = build_repair_controller(state)

    assert controller["route"] == "fix_test_registry"
    assert controller["failure_owner"] == "generated_test"
    assert controller["target_files"] == [".coding_agent_test/t/tests/test_tool.py"]
    assert "read_file" in controller["blocked_tools"]


def test_dynamic_zero_collection_does_not_override_structured_project_run(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {"failure_type": "verification_failed", "signature": "dynamic-zero"}
    state["verification"] = {
        "results": [{
            "name": "guessed_selector",
            "returncode": 4,
            "stdout": "collected 0 items\nno tests ran",
            "stderr": "ERROR: not found",
        }],
        "test_results": {
            "runs": [{
                "name": "pytest",
                "total": 12,
                "passed": 12,
                "failed": 0,
                "errors": 0,
                "stdout": "12 passed",
            }]
        },
    }
    state["failure_issues"] = [{
        "owner": "implementation",
        "type": "verification_command_failed",
        "message": "the requested scenario did not execute",
    }]

    controller = build_repair_controller(state)

    assert controller["route"] != "fix_test_registry"
    assert not any(
        issue.get("type") == "pytest_zero_collected"
        for issue in controller["issues"]
    )


def test_repair_issues_exclude_accepted_baseline_and_rejected_oracle(tmp_path: Path):
    state = _base_state(tmp_path)
    state["verification"] = {
        "results": [
            {
                "name": "pytest",
                "returncode": 0,
                "stdout": "AssertionError: accepted baseline mismatch",
                "stderr": "",
            },
            {
                "name": "bad_fixture",
                "returncode": 1,
                "stdout": "FileNotFoundError: invalid generated fixture",
                "stderr": "",
            },
            {
                "name": "requested_behavior",
                "returncode": 1,
                "stdout": "ERROR: required exception was not raised",
                "stderr": "",
            },
        ],
        "test_results": {
            "accepted_preexisting_failures": True,
            "runs": [{
                "failures": [{
                    "test": "tests.test_old::test_old",
                    "type": "AssertionError",
                    "message": "accepted baseline mismatch",
                }]
            }],
        },
    }
    state["verification_oracle_review"] = {
        "rejected_step_names": ["bad_fixture"],
    }

    issues = decompose_failure_issues(state)
    text = json.dumps(issues)

    assert "requested_behavior" in text
    assert "accepted baseline mismatch" not in text
    assert "invalid generated fixture" not in text


def test_missing_symbol_in_generated_test_routes_to_interface_repair(tmp_path: Path):
    state = _base_state(tmp_path)
    state["failure"] = {"failure_type": "test_failure", "signature": "import"}
    state["failure_issues"] = [
        {
            "owner": "generated_test",
            "type": "missing_imported_symbol",
            "file": ".coding_agent_test/t/tests/test_tool.py",
            "target_file": "scripts/tool.py",
            "message": "the generated test imports an unavailable symbol",
        }
    ]

    controller = build_repair_controller(state)

    assert controller["route"] == "resolve_interface_mismatch"
    assert controller["failure_owner"] == "generated_test"
    assert controller["strategy"] == "fix_generated_test"


def test_rejected_generated_syntax_requires_full_rewrite(tmp_path: Path):
    state = _base_state(tmp_path)
    target = ".coding_agent_test/t/tests/test_tool.py"
    state["failure"] = {
        "failure_type": "syntax_level_error",
        "signature": "syntax",
        "target_file": target,
        "syntax_check": {"checked": True, "ok": False, "error": "SyntaxError"},
        "message": "generated content is invalid Python",
    }

    controller = build_repair_controller(state)
    force = force_action_from_controller(controller)

    assert controller["route"] == "rewrite_rejected_generated_file"
    assert controller["required_tool"] == "write_file"
    assert controller["target_files"] == [target]
    assert force is not None
    assert force["required_tool"] == "write_file"
    assert "read_file" in force["blocked_tools"]


def test_generic_requirement_issue_has_no_format_or_project_assumption(tmp_path: Path):
    state = _base_state(tmp_path)
    state["requirement_atom_check"] = {
        "atoms": [
            {
                "id": "requirement:domain_result",
                "type": "quality",
                "status": "failed",
                "required": True,
                "description": "The result must satisfy the requested domain rule.",
                "details": {
                    "verification_claim": {"reason": "observed output violates the stated rule"}
                },
            }
        ]
    }

    issues = decompose_failure_issues(state)
    issue = next(item for item in issues if item.get("atom_id") == "requirement:domain_result")

    assert issue["owner"] == "requirement"
    assert issue["target_file"] is None
    assert "observed output violates" in issue["repair_hint"]


def test_empty_sequence_exception_is_not_reclassified_as_a_data_format(tmp_path: Path):
    state = _base_state(tmp_path)
    state["verification"] = {
        "results": [
            {
                "name": "behavior_probe",
                "returncode": 1,
                "stdout": "",
                "stderr": "ValueError: max() arg is an empty sequence",
            }
        ]
    }

    issues = decompose_failure_issues(state)

    assert issues
    assert all(issue.get("type") != "schema_parse_empty" for issue in issues)
    assert any(issue.get("type") == "verification_command_failed" for issue in issues)


def test_report_hides_resolved_failure_fields_after_verified_ok(tmp_path: Path):
    state = _base_state(tmp_path)
    script_path = tmp_path / "scripts" / "tool.py"
    test_path = tmp_path / ".coding_agent_test" / "t" / "tests" / "test_tool.py"
    script_path.parent.mkdir(parents=True)
    test_path.parent.mkdir(parents=True)
    script_path.write_text("def main():\n    return 0\n", encoding="utf-8")
    test_path.write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    state.update(
        {
            "verification": {
                "ok": True,
                "results": [
                    {
                        "name": "pytest",
                        "command": ["python", "-m", "pytest"],
                        "returncode": 0,
                        "stdout": "1 passed\n",
                        "stderr": "",
                    }
                ],
                "test_results": {
                    "ok": True,
                    "total": 1,
                    "passed": 1,
                    "failed": 0,
                    "errors": 0,
                    "runs": [{"total": 1, "passed": 1, "failed": 0, "errors": 0}],
                },
            },
            "contract_ok": True,
            "contract_check": {"ok": True, "failures": [], "warnings": []},
            "requirement_atom_check": {
                "atoms": [],
                "summary": {"required_failed": 0, "required_unverified": 0},
            },
            "requirement_atom_summary": {
                "required_total": 0,
                "required_failed": 0,
                "required_unverified": 0,
            },
            "semantic_contract_check": {
                "ok": True,
                "requirement_atom_check": {
                    "atoms": [],
                    "summary": {"required_failed": 0, "required_unverified": 0},
                },
            },
            "failure_owner": "generated_test",
            "strategy_decision": {"strategy": "fix_generated_test"},
            "failure_issues": [{"type": "missing_imported_symbol"}],
            "repair_controller": {"route": "resolve_interface_mismatch"},
            "verification_reason": "old write changed a file",
            "needs_verification": False,
            "stopped_reason": "verified_ok",
        }
    )

    out = report_node(state)

    final = Path(out["final_path"]).read_text(encoding="utf-8")
    assert '"failure_owner": null' in final
    assert '"strategy_decision": null' in final
    assert '"verification_reason": ""' in final
    final_obj = json.loads(final)
    assert final_obj["resolved_repair"]["status"] == "resolved"
    assert "failure_issues" not in final_obj["resolved_repair"]
