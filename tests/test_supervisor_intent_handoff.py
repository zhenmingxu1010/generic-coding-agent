from coding_agent.nodes import supervisor as supervisor_module
from coding_agent.scope.task_intent import classify_task_intent


def test_supervisor_preserves_valid_intake_scope_without_second_llm(monkeypatch, tmp_path):
    task = "Repair the existing implementation. Modifying existing project files is allowed."
    task_spec = {
        "task_type": "modify_code",
        "objective": task,
        "constraints": ["Existing project files may be modified."],
        "success_criteria": ["The documented behavior passes."],
        "requirements": [],
        "read_only": False,
        "write_scope_intent": {
            "task_mode": "debug",
            "source_modification": {
                "allowed": True,
                "confidence": 0.95,
                "reason": "The task requests an existing implementation repair.",
            },
            "existing_file_modification": {
                "allowed": True,
                "confidence": 0.95,
                "reason": "Repair requires editing the existing implementation.",
            },
            "allowed_operations": [],
            "protected_paths": [],
            "ambiguities": [],
            "confidence": 0.95,
            "reason": "Existing implementation repair.",
        },
    }
    intake_intent = classify_task_intent(task, task_spec)

    class UnexpectedSupervisorLLM:
        def __init__(self, *args, **kwargs):
            raise AssertionError("valid intake intent should not be classified again")

    monkeypatch.setattr(supervisor_module, "OpenAICompatClient", UnexpectedSupervisorLLM)
    state = {
        "task": task,
        "mode": "auto",
        "task_spec": task_spec,
        "task_intent": intake_intent,
        "trace_path": str(tmp_path / "trace.jsonl"),
        "state_snapshot_path": str(tmp_path / "state_snapshot.json"),
        "messages_path": str(tmp_path / "messages.jsonl"),
        "invariants": [],
    }

    out = supervisor_module.supervisor_node(state)

    assert out["mode"] == "debug"
    assert out["task_intent"]["semantic_write_scope"]["source_modification_allowed"] is True
    assert out["task_intent"]["semantic_write_scope"]["existing_file_modification_allowed"] is True
    atom_ids = {atom["id"] for atom in out["task_contract"]["requirement_atoms"]}
    assert "write_scope:no_existing_project_modification" not in atom_ids
