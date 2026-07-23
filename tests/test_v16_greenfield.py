from pathlib import Path

from coding_agent.graph import route_after_retrieve, route_after_context
from coding_agent.nodes.file_plan import _fallback_file_plan


def test_greenfield_write_builds_context_before_file_plan():
    state = {"mode": "write", "read_only": False, "repo_map": {"files": []}}
    assert route_after_retrieve(state) == "context_compress"


def test_generate_project_fallback_does_not_invent_project_structure():
    state = {
        "mode": "generate_project",
        "task_contract": {
            "expected_artifacts": ["README", "entrypoint", "core_logic", "tests"],
            "required_behaviors": ["demo_command"],
        },
    }
    plan = _fallback_file_plan(state, "test")
    assert plan["files"] == []
    assert plan["verify_steps"] == []
    assert plan["rationale"].startswith("llm_file_plan_failed:")


def test_context_routes_to_repair_when_failure_present_even_if_needs_verify():
    state = {"mode": "write", "read_only": False, "file_plan": {"files": []}, "needs_verification": True, "verification": {"ok": False}, "failure": {"failure_type": "syntax_level_error"}}
    assert route_after_context(state) == "repair"
