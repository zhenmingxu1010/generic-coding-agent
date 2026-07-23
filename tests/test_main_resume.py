from coding_agent.main import prepare_resumed_state


def test_prepare_resumed_state_clears_terminal_report_fields_and_updates_budgets():
    state = {
        "task": "repair the project",
        "stopped_reason": "max_rounds_with_unresolved_failure",
        "final_ok": False,
        "final_gate_status": {"ok": False},
        "runtime_ok": False,
        "failure": {"failure_type": "test_assertion_error"},
        "round_idx": 6,
        "max_rounds": 6,
        "max_repair_llm_calls": 1,
    }

    resumed = prepare_resumed_state(state, max_rounds=12, max_repair_calls=3)

    assert resumed["resumed_from_stopped_reason"] == "max_rounds_with_unresolved_failure"
    assert resumed["max_rounds"] == 12
    assert resumed["max_repair_llm_calls"] == 3
    assert resumed["resumed_from_checkpoint"] is True
    assert resumed["round_idx"] == 6
    assert resumed["failure"] == {"failure_type": "test_assertion_error"}
    assert "stopped_reason" not in resumed
    assert "final_ok" not in resumed
    assert "final_gate_status" not in resumed
    assert "runtime_ok" not in resumed
