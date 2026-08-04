from __future__ import annotations

from coding_agent.core.implementation_batch import update_implementation_batch
from coding_agent.graph import route_after_context, route_after_tool, route_after_verify


def test_route_after_context_stops_before_repair_when_round_budget_exhausted():
    state = {
        "mode": "write",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 20,
        "failure": {"failure_type": "contract_error", "signature": "sig"},
        "verification": {"ok": False},
    }

    route = route_after_context(state)

    assert route == "report"
    assert state["stopped_reason"] == "max_rounds"


def test_route_after_verify_replans_evidence_without_repairing_implementation():
    state = {
        "mode": "write",
        "read_only": False,
        "verification": {"ok": False, "results": [{"name": "behavior", "returncode": 0}]},
        "contract_ok": False,
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 1,
        },
        "verification_plan_attempts": 0,
        "requirement_atom_check": {
            "atoms": [{
                "id": "requirement:behavior",
                "status": "unverified",
                "type": "behavior",
                "data": {"evidence_mode": "execution"},
            }]
        },
    }

    assert route_after_verify(state) == "verify"
    assert state["needs_verification"] is True
    assert state.get("failure") is None


def test_route_after_verify_does_not_loop_on_artifact_evidence_gap(monkeypatch):
    state = {
        "mode": "write",
        "read_only": False,
        "verification": {"ok": False, "results": [{"name": "pytest", "returncode": 0}]},
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 1,
        },
        "requirement_atom_check": {
            "atoms": [{
                "id": "requirement:package_metadata",
                "status": "unverified",
                "type": "constraint",
                "data": {"evidence_mode": "artifact"},
            }]
        },
        "verification_plan_attempts": 0,
    }
    monkeypatch.setattr("coding_agent.graph.deliverable_review_needed", lambda _state: False)

    assert route_after_verify(state) == "report"
    assert state["stopped_reason"] == "verification_evidence_incomplete"


def test_route_after_verify_stops_after_bounded_evidence_replanning():
    state = {
        "mode": "write",
        "read_only": False,
        "verification": {"ok": False, "results": [{"name": "behavior", "returncode": 0}]},
        "contract_ok": False,
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 1,
        },
        "verification_plan_attempts": 3,
        "verification_claims": {"requirement:behavior": {"status": "unverified"}},
    }

    assert route_after_verify(state) == "report"
    assert state["stopped_reason"] == "verification_evidence_incomplete"
    assert state["failure"]["failure_type"] == "verification_evidence_incomplete"


def test_exhausted_document_evidence_routes_to_deliverable_audit(tmp_path):
    (tmp_path / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "mode": "modify",
        "read_only": False,
        "verification": {"ok": False, "results": [{"name": "pytest", "returncode": 0}]},
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 1,
        },
        "verification_plan_attempts": 3,
        "verification_claims": {"requirement:documented": {"status": "unverified"}},
        "scope_contract": {"allowed_modify_paths": ["tool.py"]},
        "changed_files": [],
    }

    assert route_after_verify(state) == "deliverable_review"
    assert state["needs_verification"] is False


def test_route_after_verify_diagnoses_real_command_failure_instead_of_replanning_evidence():
    state = {
        "mode": "write",
        "read_only": False,
        "verification": {"ok": False, "results": [{"name": "behavior", "returncode": 1, "timed_out": False}]},
        "contract_ok": False,
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 1,
        },
        "verification_plan_attempts": 0,
    }

    assert route_after_verify(state) == "diagnose"


def test_route_after_tool_stops_blocked_policy_reflection_when_budget_exhausted():
    state = {
        "mode": "write",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 20,
        "last_tool_result": {
            "tool": "edit_file",
            "ok": False,
            "message": "path is protected by explicit task scope",
            "data": {"blocked_by_policy": True, "path": "scripts/generated.py"},
        },
    }

    route = route_after_tool(state)

    assert route == "report"
    assert state["stopped_reason"] == "max_rounds"


def test_route_after_tool_allows_final_verification_after_last_successful_write():
    state = {
        "mode": "write",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 20,
        "needs_verification": True,
        "last_tool_result": {
            "tool": "write_file",
            "ok": True,
            "data": {"changed": True, "path": "scripts/generated.py"},
        },
    }

    route = route_after_tool(state)

    assert route == "repo_scan"
    assert "stopped_reason" not in state


def test_initial_multifile_modify_batches_remaining_source_edits_before_verification():
    state = {
        "mode": "modify",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 4,
        "needs_verification": True,
        "scope_contract": {
            "allowed_modify_paths": ["errors.py", "runner.py", "service.py"],
        },
        "changed_files": ["errors.py"],
        "last_tool_result": {
            "tool": "edit_file",
            "ok": True,
            "data": {"changed": True, "path": "errors.py"},
        },
    }

    update_implementation_batch(state)
    assert route_after_tool(state) == "context_compress"
    assert state["implementation_batch_open"] is True
    assert state["implementation_batch_remaining"] == ["runner.py", "service.py"]
    assert route_after_context(state) == "act"


def test_initial_multifile_batch_verifies_after_all_scoped_targets_are_changed():
    state = {
        "mode": "modify",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 6,
        "needs_verification": True,
        "implementation_batch_open": True,
        "implementation_batch_started_round": 4,
        "scope_contract": {
            "allowed_modify_paths": ["errors.py", "runner.py", "service.py"],
        },
        "changed_files": ["errors.py", "runner.py", "service.py"],
        "last_tool_result": {
            "tool": "edit_file",
            "ok": True,
            "data": {"changed": True, "path": "service.py"},
        },
    }

    update_implementation_batch(state)
    assert route_after_tool(state) == "repo_scan"
    assert state["implementation_batch_open"] is False
    assert state["implementation_batch_remaining"] == []


def test_single_file_modify_still_verifies_immediately_after_edit():
    state = {
        "mode": "modify",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 2,
        "needs_verification": True,
        "scope_contract": {"allowed_modify_paths": ["tool.py"]},
        "changed_files": ["tool.py"],
        "last_tool_result": {
            "tool": "edit_file",
            "ok": True,
            "data": {"changed": True, "path": "tool.py"},
        },
    }

    update_implementation_batch(state)
    assert route_after_tool(state) == "repo_scan"


def test_route_after_tool_returns_command_policy_rejection_to_action_loop():
    state = {
        "mode": "modify",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 3,
        "last_tool_result": {
            "tool": "run_shell",
            "ok": False,
            "message": "candidate command rejected",
            "data": {
                "executed": False,
                "failure_kind": "command_policy",
                "returncode": 1,
            },
        },
    }

    route = route_after_tool(state)

    assert route == "context_compress"
    assert state.get("failure") is None


def test_route_after_tool_diagnoses_executed_command_failure():
    state = {
        "mode": "modify",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 3,
        "last_tool_result": {
            "tool": "run_shell",
            "ok": False,
            "message": "command finished",
            "data": {
                "executed": True,
                "failure_kind": "process_exit",
                "returncode": 1,
            },
        },
    }

    assert route_after_tool(state) == "diagnose"


def test_finish_after_direct_green_tests_still_verifies_pending_requirements():
    state = {
        "mode": "modify",
        "read_only": False,
        "max_rounds": 20,
        "round_idx": 5,
        "verification": {"ok": True, "results": [{"name": "run_tests", "returncode": 0}]},
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 1,
        },
        "requirement_atoms": [{
            "id": "requirement:documented_behavior",
            "required": True,
            "status": "pending",
        }],
        "last_tool_result": {
            "tool": "finish",
            "ok": True,
            "data": {"message": "tests passed"},
        },
    }

    route = route_after_tool(state)

    assert route == "verify"
    assert state["needs_verification"] is True
    assert state["verification_reason"] == "finish requested before authoritative requirement verification"


def test_route_after_verify_reports_when_only_internal_generated_tests_fail_after_contract_passes():
    state = {
        "mode": "write",
        "read_only": False,
        "needs_verification": True,
        "contract_ok": True,
        "semantic_contract_check": {"ok": True},
        "requirement_atom_summary": {"required_failed": 0, "required_unverified": 0},
        "verification": {
            "ok": False,
            "test_results": {
                "ok": False,
                "total": 2,
                "passed": 1,
                "failed": 1,
                "errors": 0,
                "runs": [{"name": "pytest", "total": 2, "passed": 1, "failed": 1, "errors": 0}],
                "failures": [{"file": ".coding_agent_test/t/tests/test_tool.py", "message": "internal assertion failed"}],
            },
        },
        "test_results": {
            "ok": False,
            "total": 2,
            "passed": 1,
            "failed": 1,
            "errors": 0,
            "runs": [{"name": "pytest", "total": 2, "passed": 1, "failed": 1, "errors": 0}],
            "failures": [{"file": ".coding_agent_test/t/tests/test_tool.py", "message": "internal assertion failed"}],
        },
        "generated_files": [
            {"path": "scripts/tool.py", "kind": "code"},
            {"path": ".coding_agent_test/t/tests/test_tool.py", "kind": "test"},
        ],
        "changed_files": ["scripts/tool.py", ".coding_agent_test/t/tests/test_tool.py"],
        "artifact_registry": {"agent_generated_tests": [".coding_agent_test/t/tests/test_tool.py"]},
    }

    route = route_after_verify(state)

    assert route == "report"
    assert state["needs_verification"] is False
    assert state["stopped_reason"] == "verified_with_generated_test_warnings"


def test_route_after_verify_still_diagnoses_external_test_failure():
    state = {
        "mode": "write",
        "read_only": False,
        "needs_verification": True,
        "contract_ok": True,
        "semantic_contract_check": {"ok": True},
        "requirement_atom_summary": {"required_failed": 0, "required_unverified": 0},
        "verification": {
            "ok": False,
            "test_results": {
                "ok": False,
                "total": 1,
                "passed": 0,
                "failed": 1,
                "errors": 0,
                "runs": [{"name": "pytest", "total": 1, "passed": 0, "failed": 1, "errors": 0}],
                "failures": [{"file": "tests/test_user_contract.py", "message": "real test failed"}],
            },
        },
        "test_results": {
            "ok": False,
            "total": 1,
            "passed": 0,
            "failed": 1,
            "errors": 0,
            "runs": [{"name": "pytest", "total": 1, "passed": 0, "failed": 1, "errors": 0}],
            "failures": [{"file": "tests/test_user_contract.py", "message": "real test failed"}],
        },
        "generated_files": [
            {"path": "scripts/tool.py", "kind": "code"},
        ],
        "changed_files": ["scripts/tool.py"],
        "artifact_registry": {"agent_generated_tests": []},
    }

    route = route_after_verify(state)

    assert route == "diagnose"


def test_route_after_verify_stops_repeated_unchanged_failure():
    state = {
        "mode": "write",
        "read_only": False,
        "verification": {"ok": False, "results": [{"name": "behavior", "returncode": 1}]},
        "contract_ok": False,
        "requirement_atom_summary": {"required_failed": 1, "required_unverified": 0},
        "verification_stalled": True,
        "verification_failure_repeat_count": 3,
        "verification_failure_fingerprint": "same-evidence",
    }

    assert route_after_verify(state) == "report"
    assert state["stopped_reason"] == "repeated_verification_failure"
    assert state["failure_owner"] == "verification_controller"
    assert state["needs_verification"] is False


def test_repeated_evidence_gap_reaches_deliverable_review(tmp_path):
    (tmp_path / "tool.py").write_text("def run():\n    pass\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "mode": "modify",
        "read_only": False,
        "verification": {"ok": False, "results": [{"name": "compile", "returncode": 0}]},
        "requirement_atom_summary": {"required_failed": 0, "required_unverified": 1},
        "verification_plan_attempts": 3,
        "verification_stalled": True,
        "verification_failure_repeat_count": 3,
        "scope_contract": {"allowed_modify_paths": ["tool.py"]},
        "changed_files": ["tool.py"],
    }

    assert route_after_verify(state) == "deliverable_review"
    assert state["needs_verification"] is False


def test_verification_failure_fingerprint_ignores_run_specific_noise():
    from coding_agent.nodes.verify import _update_verification_progress_guard

    state = {}
    contract = {"failures": ["object at 0xABCDEF failed in 0.10s"]}
    first = [{
        "name": "check",
        "command": ["python", "check.py"],
        "returncode": 1,
        "stdout": "/tmp/pytest-of-user/pytest-1/test_case0/object at 0xABCDEF failed in 0.10s",
        "stderr": "",
        "timed_out": False,
    }]
    _update_verification_progress_guard(state, first, contract)
    first_fingerprint = state["verification_failure_fingerprint"]

    contract = {"failures": ["object at 0x123456 failed in 8.75s"]}
    second = [{
        "name": "check",
        "command": ["python", "check.py"],
        "returncode": 1,
        "stdout": "/tmp/pytest-of-user/pytest-99/test_case0/object at 0x123456 failed in 8.75s",
        "stderr": "",
        "timed_out": False,
    }]
    _update_verification_progress_guard(state, second, contract)

    assert state["verification_failure_fingerprint"] == first_fingerprint
    assert state["verification_failure_repeat_count"] == 2
