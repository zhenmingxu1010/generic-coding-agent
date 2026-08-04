from coding_agent.main import prepare_resumed_state
from coding_agent.graph import route_from_start
from coding_agent.core.state import AgentState


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


def test_resume_with_established_contract_skips_contract_reintake():
    state = prepare_resumed_state(
        {
            "task": "create the requested CLI",
            "stopped_reason": "unresolved_verification_failure",
            "task_spec": {"task_type": "generate_project"},
            "task_contract": {"requirement_atoms": [{"id": "requirement:original"}]},
            "verification": {"ok": False, "results": [{"name": "pytest", "returncode": 0}]},
        },
        max_rounds=12,
        max_repair_calls=6,
    )

    assert route_from_start(state) == "repo_scan"
    assert state["task_contract"]["requirement_atoms"] == [{"id": "requirement:original"}]
    assert state["needs_verification"] is True


def test_resume_without_established_contract_runs_intake():
    state = prepare_resumed_state(
        {"task": "create a CLI", "stopped_reason": "runtime_exception"},
        max_rounds=12,
        max_repair_calls=6,
    )

    assert route_from_start(state) == "intake"


def test_resume_routing_flags_are_persistent_agent_state():
    assert "resumed_from_checkpoint" in AgentState.__annotations__
    assert "resumed_from_stopped_reason" in AgentState.__annotations__
