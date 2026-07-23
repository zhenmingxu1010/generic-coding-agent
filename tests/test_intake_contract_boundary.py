from __future__ import annotations

import json

from coding_agent.nodes.intake import intake_node


def test_internal_invariants_are_not_sent_as_user_task_contract(monkeypatch, tmp_path):
    captured = {}

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def chat(self, messages, **_kwargs):
            captured["messages"] = messages
            return json.dumps({
                "task_type": "analyze",
                "objective": "Explain the project.",
                "constraints": [],
                "success_criteria": [],
                "requirements": [],
                "read_only": True,
                "agent_read_only": True,
                "script_read_only": False,
                "scan_first": True,
                "create_paths": [],
                "read_reference_paths": [],
                "write_scope_intent": {},
            })

    monkeypatch.setattr("coding_agent.nodes.intake.OpenAICompatClient", _Client)
    state = {
        "workspace": str(tmp_path),
        "thread_id": "contract-boundary",
        "task": "Explain this project without changing it.",
        "mode": "auto",
        "invariants": ["INTERNAL_POLICY_MUST_NOT_BECOME_A_REQUIREMENT"],
    }

    out = intake_node(state)

    user_content = captured["messages"][-1]["content"]
    assert user_content == "Task:\nExplain this project without changing it."
    assert "INTERNAL_POLICY_MUST_NOT_BECOME_A_REQUIREMENT" not in json.dumps(out["task_spec"])


def test_intake_partitions_workflow_and_response_items_from_deliverables(monkeypatch, tmp_path):
    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def chat(self, _messages, **_kwargs):
            return json.dumps({
                "task_type": "modify_code",
                "objective": "Fix the documented behavior.",
                "constraints": [],
                "success_criteria": [],
                "requirements": [
                    {
                        "id": "documented_behavior",
                        "kind": "behavior",
                        "scope": "deliverable",
                        "description": "The program follows its documented behavior.",
                        "evidence_mode": "execution",
                        "user_evidence": ["follows README.md"],
                    },
                    {
                        "id": "inspect_readme",
                        "kind": "behavior",
                        "scope": "workflow",
                        "description": "Read the README before editing.",
                        "evidence_mode": "runtime",
                    },
                    {
                        "id": "explain_change",
                        "kind": "quality",
                        "scope": "response",
                        "description": "Explain the change in the final response.",
                        "evidence_mode": "analysis",
                    },
                ],
                "read_only": False,
                "agent_read_only": False,
                "script_read_only": False,
                "scan_first": True,
                "create_paths": [],
                "read_reference_paths": ["README.md"],
                "write_scope_intent": {
                    "source_modification": {"allowed": True, "confidence": 1.0, "reason": "bug fix"},
                    "existing_file_modification": {"allowed": True, "confidence": 1.0, "reason": "bug fix"},
                    "allowed_operations": [],
                    "protected_paths": [],
                    "ambiguities": [],
                    "confidence": 1.0,
                    "reason": "bug fix",
                },
            })

    monkeypatch.setattr("coding_agent.nodes.intake.OpenAICompatClient", _Client)
    out = intake_node({
        "workspace": str(tmp_path),
        "thread_id": "contract-scopes",
        "task": "Fix the existing program so it follows README.md.",
        "mode": "auto",
    })

    assert [item["id"] for item in out["task_spec"]["requirements"]] == ["documented_behavior"]
    assert [item["id"] for item in out["task_spec"]["workflow_steps"]] == ["inspect_readme"]
    assert [item["id"] for item in out["task_spec"]["response_requirements"]] == ["explain_change"]
    assert [atom["id"] for atom in out["task_contract"]["requirement_atoms"]] == ["requirement:documented_behavior"]


def test_intake_drops_ungrounded_inferred_hard_requirements(monkeypatch, tmp_path):
    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def chat(self, _messages, **_kwargs):
            return json.dumps({
                "task_type": "modify_code",
                "objective": "Repair documented behavior.",
                "constraints": [],
                "success_criteria": [
                    "The documented behavior works.",
                    "All related tests pass.",
                ],
                "requirements": [
                    {
                        "id": "documented_behavior",
                        "kind": "behavior",
                        "scope": "deliverable",
                        "description": "The program follows README.md.",
                        "evidence_mode": "execution",
                        "user_evidence": ["follows README.md"],
                    },
                    {
                        "id": "tests_pass",
                        "kind": "behavior",
                        "scope": "deliverable",
                        "description": "All related tests pass.",
                        "evidence_mode": "execution",
                        "user_evidence": [],
                    },
                ],
                "read_only": False,
                "write_scope_intent": {
                    "task_mode": "source_modify",
                    "source_modification": {"allowed": True, "confidence": 1.0},
                    "existing_file_modification": {"allowed": True, "confidence": 1.0},
                    "confidence": 1.0,
                },
            })

    monkeypatch.setattr("coding_agent.nodes.intake.OpenAICompatClient", _Client)
    out = intake_node({
        "workspace": str(tmp_path),
        "thread_id": "contract-grounding",
        "task": "Fix the existing program so it follows README.md.",
        "mode": "auto",
    })

    assert [item["id"] for item in out["task_spec"]["requirements"]] == ["documented_behavior"]
    assert out["task_spec"]["success_criteria"] == ["The program follows README.md."]
    assert out["task_spec"]["requirement_grounding"]["dropped"] == [
        {
            "id": "tests_pass",
            "description": "All related tests pass.",
            "reason": "no exact user-task evidence supports this hard requirement",
        }
    ]
    assert [atom["id"] for atom in out["task_contract"]["requirement_atoms"]] == [
        "requirement:documented_behavior"
    ]
