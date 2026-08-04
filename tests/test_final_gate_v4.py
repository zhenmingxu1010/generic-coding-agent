from coding_agent.nodes.final_gate import compute_final_gate


def _base_write_state():
    return {
        "mode": "write",
        "read_only": False,
        "verification": {"ok": True},
        "contract_ok": True,
        "needs_verification": False,
        "changed_files": ["script.py"],
        "generated_files": [{"path": "script.py"}],
        "repair_history": [],
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 0,
        },
    }


def test_final_gate_accepts_clean_verified_write_state():
    gate = compute_final_gate(_base_write_state())

    assert gate["ok"] is True
    assert gate["failures"] == []


def test_final_gate_replaces_stale_failure_reason_after_successful_resume():
    state = _base_write_state()
    state["stopped_reason"] = "max_rounds_with_unresolved_failure"

    gate = compute_final_gate(state)

    assert gate["ok"] is True
    assert gate["stopped_reason"] == "verified_ok"


def test_final_gate_prefers_completed_contract_atom_check_over_stale_pending_summary():
    state = _base_write_state()
    state["requirement_atom_summary"] = {
        "required_total": 4,
        "required_failed": 0,
        "required_unverified": 4,
    }
    state["contract_check"] = {
        "ok": True,
        "semantic_contract_check": {
            "requirement_atom_check": {
                "ok": True,
                "summary": {
                    "required_total": 4,
                    "required_failed": 0,
                    "required_unverified": 0,
                },
                "atoms": [],
            }
        },
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is True
    assert gate["stopped_reason"] == "verified_ok"


def test_final_gate_does_not_report_max_rounds_before_budget_is_exhausted():
    state = _base_write_state()
    state.update({
        "verification": {"ok": False},
        "contract_ok": False,
        "needs_verification": True,
        "stopped_reason": "done",
        "round_idx": 3,
        "max_rounds": 10,
    })

    gate = compute_final_gate(state)

    assert gate["stopped_reason"] == "unresolved_verification_failure"


def test_final_gate_reports_max_rounds_only_when_budget_is_exhausted():
    state = _base_write_state()
    state.update({
        "verification": {"ok": False},
        "contract_ok": False,
        "needs_verification": True,
        "stopped_reason": "done",
        "round_idx": 10,
        "max_rounds": 10,
    })

    gate = compute_final_gate(state)

    assert gate["stopped_reason"] == "max_rounds_with_unresolved_failure"


def test_final_gate_rejects_source_modify_with_only_generated_tests():
    state = _base_write_state()
    state.update(
        {
            "mode": "modify",
            "task_intent": {"source_modify_intent": True, "operation_mode": "scoped_modify"},
            "changed_files": ["tests/test_divide.py"],
            "generated_files": [{"path": "tests/test_divide.py", "kind": "test"}],
        }
    )

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "source_modify_without_code_change" in gate["failures"]


def test_final_gate_rejects_scope_allowed_modify_with_only_generated_tests():
    state = _base_write_state()
    state.update(
        {
            "mode": "write",
            "task_intent": {
                "operation_mode": "safe_create",
                "scope_contract": {"allowed_modify_paths": ["inventory/stock.py"]},
            },
            "scope_contract": {"allowed_modify_paths": ["inventory/stock.py"]},
            "changed_files": ["tests/test_new_cli.py"],
            "generated_files": [{"path": "tests/test_new_cli.py", "kind": "test"}],
        }
    )

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "source_modify_without_code_change" in gate["failures"]


def test_final_gate_accepts_source_modify_with_code_change():
    state = _base_write_state()
    state.update(
        {
            "mode": "modify",
            "task_intent": {"source_modify_intent": True, "operation_mode": "scoped_modify"},
            "changed_files": ["calculator.py", "tests/test_divide.py"],
            "generated_files": [{"path": "tests/test_divide.py", "kind": "test"}],
        }
    )

    gate = compute_final_gate(state)

    assert "source_modify_without_code_change" not in gate["failures"]


def test_final_gate_accepts_verified_modify_task_when_behavior_already_satisfies_requirements():
    state = _base_write_state()
    state.update(
        {
            "mode": "modify",
            "task_intent": {"source_modify_intent": True, "operation_mode": "scoped_modify"},
            "changed_files": [],
            "generated_files": [],
            "executed_verification_steps": [
                {
                    "name": "documented_behavior",
                    "command": ["python", "tool.py", "sample.txt"],
                    "verifies": ["requirement:documented_behavior"],
                    "returncode": 0,
                    "timed_out": False,
                    "executed": True,
                }
            ],
            "verification_infrastructure_step_names": ["py_compile"],
        }
    )

    gate = compute_final_gate(state)

    assert gate["ok"] is True
    assert "source_modify_without_code_change" not in gate["failures"]
    assert "requirements_verified_without_implementation_change" in gate["warnings"]


def test_final_gate_accepts_noop_repair_proved_by_named_project_tests():
    state = _base_write_state()
    atom = {
        "id": "requirement:documented_behavior",
        "type": "behavior",
        "source": "llm_task_requirement",
        "status": "passed",
        "data": {"evidence_mode": "execution"},
        "details": {
            "verification_claim": {
                "status": "passed",
                "cited_steps": ["pytest"],
                "evidence": ["passing project test names match the requirement"],
            }
        },
    }
    state.update({
        "mode": "modify",
        "task_intent": {"source_modify_intent": True, "operation_mode": "scoped_modify"},
        "changed_files": [],
        "generated_files": [],
        "executed_verification_steps": [{
            "name": "pytest",
            "command": ["python", "-m", "pytest"],
            "verifies": [],
            "returncode": 0,
            "timed_out": False,
            "executed": True,
        }],
        "verification": {
            "ok": True,
            "results": [{"name": "pytest", "returncode": 0, "executed": True}],
        },
        "requirement_atom_check": {
            "ok": True,
            "atoms": [atom],
            "summary": {"required_total": 1, "required_failed": 0, "required_unverified": 0},
        },
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 0,
        },
    })

    gate = compute_final_gate(state)

    assert gate["ok"] is True
    assert "requirements_verified_without_implementation_change" in gate["warnings"]


def test_final_gate_rejects_no_change_when_only_infrastructure_check_passed():
    state = _base_write_state()
    state.update(
        {
            "mode": "modify",
            "task_intent": {"source_modify_intent": True, "operation_mode": "scoped_modify"},
            "changed_files": [],
            "generated_files": [],
            "executed_verification_steps": [
                {
                    "name": "py_compile",
                    "command": ["python", "-m", "compileall", "-q", "."],
                    "verifies": [],
                    "returncode": 0,
                    "timed_out": False,
                    "executed": True,
                }
            ],
            "verification_infrastructure_step_names": ["py_compile"],
        }
    )

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "source_modify_without_code_change" in gate["failures"]


def test_final_gate_requires_contract_ok_true_for_write_modes():
    state = _base_write_state()
    state["contract_ok"] = None

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "contract_check_missing_or_failed" in gate["failures"]


def test_final_gate_rejects_active_failure_even_after_verification():
    state = _base_write_state()
    state["failure"] = {
        "failure_type": "runtime_error",
        "signature": "sig",
        "message": "still unresolved",
    }
    state["failure_issues"] = [{"type": "runtime_error", "message": "still unresolved"}]

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "active_failure_present" in gate["failures"]


def test_final_gate_ignores_stale_failure_after_clean_verification():
    state = _base_write_state()
    state["failure"] = {
        "failure_type": "runtime_error",
        "signature": "sig",
        "message": "resolved by repair",
    }
    state["failure_issues"] = []

    gate = compute_final_gate(state)

    assert gate["ok"] is True
    assert "active_failure_present" not in gate["failures"]


def test_final_gate_treats_failed_agent_generated_tests_as_advisory_when_contract_passed():
    state = _base_write_state()
    state.update(
        {
            "workspace": "/tmp/project",
            "verification": {
                "ok": False,
                "test_results": {
                    "ok": False,
                    "total": 2,
                    "passed": 1,
                    "failed": 1,
                    "errors": 0,
                    "runs": [{"name": "pytest", "total": 2, "passed": 1, "failed": 1, "errors": 0}],
                    "failures": [
                        {
                            "file": ".coding_agent_test/t/tests/test_tool.py",
                            "message": "generated assertion failed",
                        }
                    ],
                },
            },
            "semantic_contract_check": {"ok": True},
            "failure": {
                "failure_type": "test_assertion_error",
                "target_file": ".coding_agent_test/t/tests/test_tool.py",
            },
            "generated_files": [
                {"path": "tool.py", "kind": "code"},
                {"path": ".coding_agent_test/t/tests/test_tool.py", "kind": "test"},
            ],
            "changed_files": [
                "tool.py",
                ".coding_agent_test/t/tests/test_tool.py",
            ],
            "artifact_registry": {"agent_generated_tests": [".coding_agent_test/t/tests/test_tool.py"]},
            "stopped_reason": "repair_protocol_blocked",
        }
    )

    gate = compute_final_gate(state)

    assert gate["ok"] is True
    assert gate["failures"] == []
    assert "agent_generated_tests_failed_but_contract_passed" in gate["warnings"]
    assert gate["stopped_reason"] == "verified_with_generated_test_warnings"


def test_final_gate_still_rejects_failed_external_tests():
    state = _base_write_state()
    state.update(
        {
            "verification": {
                "ok": False,
                "test_results": {
                    "ok": False,
                    "total": 1,
                    "passed": 0,
                    "failed": 1,
                    "errors": 0,
                    "runs": [{"name": "pytest", "total": 1, "passed": 0, "failed": 1, "errors": 0}],
                    "failures": [{"file": "tests/test_user_contract.py", "message": "external test failed"}],
                },
            },
            "semantic_contract_check": {"ok": True},
            "generated_files": [{"path": "script.py", "kind": "code"}],
            "artifact_registry": {"agent_generated_tests": []},
        }
    )

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "verification_failed" in gate["failures"]


def test_final_gate_rejects_failed_delivered_agent_generated_tests():
    state = _base_write_state()
    state.update(
        {
            "workspace": "/tmp/project",
            "verification": {
                "ok": False,
                "test_results": {
                    "ok": False,
                    "total": 1,
                    "passed": 0,
                    "failed": 1,
                    "errors": 0,
                    "runs": [{"name": "pytest", "total": 1, "passed": 0, "failed": 1, "errors": 0}],
                    "failures": [{"file": "tests/test_tool.py", "message": "delivered test failed"}],
                },
            },
            "semantic_contract_check": {"ok": True},
            "generated_files": [
                {"path": "tool.py", "kind": "code"},
                {"path": "tests/test_tool.py", "kind": "test", "user_visible": True},
            ],
            "artifact_registry": {"agent_generated_tests": ["tests/test_tool.py"]},
        }
    )

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "verification_failed" in gate["failures"]
    assert "agent_generated_tests_failed_but_contract_passed" not in gate["warnings"]


def test_final_gate_rejects_zero_collected_structured_tests():
    state = _base_write_state()
    state["verification"] = {
        "ok": True,
        "test_results": {
            "version": "run_tests_v1",
            "ok": True,
            "runs": [{"name": "pytest", "ok": True, "total": 0, "passed": 0, "failed": 0, "errors": 0}],
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
        },
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "pytest_zero_tests_collected" in gate["failures"]


def test_final_gate_rejects_zero_collected_shell_output():
    state = _base_write_state()
    state["verification"] = {
        "ok": True,
        "results": [
            {
                "name": "pytest",
                "command": ["python", "-m", "pytest", "-q"],
                "returncode": 0,
                "stdout": "collected 0 items\n",
                "stderr": "",
                "timed_out": False,
            }
        ],
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "pytest_zero_tests_collected" in gate["failures"]


def test_final_gate_requires_requirement_atom_check_when_required_atoms_exist():
    state = _base_write_state()
    state.pop("requirement_atom_summary")
    state["task_contract"] = {
        "requirement_atoms": [
            {"id": "artifact:x", "required": True, "status": "pending"},
        ]
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "missing_requirement_atom_check" in gate["failures"]


def test_final_gate_rejects_safe_create_existing_source_violation():
    state = _base_write_state()
    state["task_intent"] = {"operation_mode": "safe_create"}
    state["changed_files"] = ["script.py", "src/leak.py"]
    state["generated_files"] = [{"path": "script.py", "kind": "code"}, {"path": "src/leak.py", "kind": "code"}]

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "source_files_changed_in_safe_create" in gate["failures"]


def test_final_gate_marks_unfulfillable_scope_as_controlled_failure():
    state = _base_write_state()
    state.update(
        {
            "verification": {"ok": False},
            "contract_ok": False,
            "needs_verification": False,
            "stopped_reason": "task_unfulfillable_within_scope",
            "failure": {
                "failure_type": "task_unfulfillable_within_scope",
                "message": "missing API and source writes are forbidden",
            },
            "changed_files": [".coding_agent_test/t/test_divide_import.py"],
            "generated_files": [{"path": ".coding_agent_test/t/test_divide_import.py", "kind": "test"}],
            "requirement_atom_summary": {
                "required_total": 1,
                "required_failed": 1,
                "required_unverified": 0,
            },
        }
    )

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert gate["controlled_failure"] is True
    assert gate["outcome"] == "unfulfillable_within_scope"
    assert gate["stopped_reason"] == "task_unfulfillable_within_scope"


def test_final_gate_does_not_controlled_mark_unfulfillable_scope_with_write_violation():
    state = _base_write_state()
    state.update(
        {
            "verification": {"ok": False},
            "contract_ok": False,
            "needs_verification": False,
            "stopped_reason": "task_unfulfillable_within_scope",
            "failure": {
                "failure_type": "task_unfulfillable_within_scope",
                "message": "missing API and source writes are forbidden",
            },
            "task_intent": {"operation_mode": "safe_create"},
            "changed_files": ["src/leak.py"],
            "generated_files": [{"path": "src/leak.py", "kind": "code"}],
            "requirement_atom_summary": {
                "required_total": 1,
                "required_failed": 1,
                "required_unverified": 0,
            },
        }
    )

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert gate["controlled_failure"] is False
    assert gate["outcome"] == "failed"
    assert "source_files_changed_in_safe_create" in gate["failures"]
