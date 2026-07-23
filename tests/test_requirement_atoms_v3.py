from __future__ import annotations

from pathlib import Path

from coding_agent.contracts.requirement_atoms import (
    evaluate_requirement_atoms,
    extract_requirement_atoms,
    summarize_requirement_atoms,
)


def _by_id(atoms):
    return {atom["id"]: atom for atom in atoms}


def test_structured_requirements_are_project_agnostic():
    task_spec = {
        "create_paths": ["src/report_tool.py"],
        "requirements": [
            {
                "id": "reads_requested_input",
                "kind": "behavior",
                "description": "The selected input changes the observable report.",
                "required": True,
                "verification_hint": "Execute with two inputs and compare outputs.",
            },
            {
                "id": "usable_documentation",
                "kind": "quality",
                "description": "Usage documentation is sufficient for a new user.",
                "required": True,
            },
        ],
    }
    supervisor = {
        "task_intent": {
            "create_paths": ["src/report_tool.py"],
            "operation_mode": "safe_create",
            "semantic_write_scope": {"existing_file_modification_allowed": False},
        }
    }

    atoms = _by_id(extract_requirement_atoms("build the requested tool", task_spec, supervisor))

    assert atoms["artifact:src/report_tool.py"]["type"] == "artifact_exists"
    assert atoms["write_scope:no_existing_project_modification"]["type"] == "write_scope"
    assert atoms["requirement:reads_requested_input"]["type"] == "behavior"
    assert atoms["requirement:usable_documentation"]["type"] == "quality"


def test_structured_requirement_preserves_exact_user_evidence():
    atoms = extract_requirement_atoms(
        "Make the documented command work.",
        {
            "requirements": [{
                "id": "documented_command",
                "kind": "behavior",
                "description": "README.md behavior works.",
                "user_evidence": ["documented command"],
                "verification_hint": "Assume every test covers the documentation.",
            }]
        },
        {},
    )

    assert atoms[0]["evidence"] == ["documented command"]


def test_symbolic_internal_test_location_is_not_an_artifact_atom():
    atoms = extract_requirement_atoms(
        "Create scripts/tool.py; internal tests belong under .coding_agent_test/<thread-id>.",
        {
            "create_paths": ["scripts/tool.py", ".coding_agent_test/<thread-id>"],
            "requirements": [{
                "id": "internal_test_location",
                "kind": "artifact",
                "path": ".coding_agent_test/<thread-id>",
                "description": "Internal tests use the runtime-owned location.",
            }],
        },
        {},
    )

    ids = {atom["id"] for atom in atoms}
    assert "artifact:scripts/tool.py" in ids
    assert all("<thread-id>" not in atom_id for atom_id in ids)


def test_symbolic_internal_test_location_constraint_is_runtime_policy():
    atoms = extract_requirement_atoms(
        "Create scripts/tool.py. Agent verification tests must be written under .coding_agent_test/<thread-id>.",
        {
            "create_paths": ["scripts/tool.py"],
            "requirements": [{
                "id": "agent_tests_path",
                "kind": "constraint",
                "description": "Agent verification tests are written under .coding_agent_test/<thread-id>.",
                "evidence_mode": "artifact",
                "user_evidence": ["tests must be written under .coding_agent_test/<thread-id>"],
            }],
        },
        {},
    )

    ids = {atom["id"] for atom in atoms}
    assert "artifact:scripts/tool.py" in ids
    assert "requirement:agent_tests_path" not in ids


def test_explicit_internal_test_creation_remains_a_requirement():
    atoms = extract_requirement_atoms(
        "Create verification tests and place them under .coding_agent_test/<thread-id>.",
        {
            "requirements": [{
                "id": "agent_tests_path",
                "kind": "constraint",
                "description": "Tests exist under .coding_agent_test/<thread-id>.",
                "evidence_mode": "artifact",
            }],
        },
        {},
    )

    assert "requirement:agent_tests_path" in {atom["id"] for atom in atoms}


def test_missing_semantic_scope_does_not_create_false_existing_file_ban():
    supervisor = {
        "task_intent": {
            "operation_mode": "scoped_modify",
            "source_modify_intent": True,
            "semantic_write_scope": {
                "available": False,
                "valid": False,
                "existing_file_modification_allowed": False,
            },
        }
    }

    atoms = extract_requirement_atoms(
        "Repair the existing implementation.",
        {"task_type": "modify_code"},
        supervisor,
    )

    assert "write_scope:no_existing_project_modification" not in _by_id(atoms)


def test_behavior_requires_grounded_verification_claim(tmp_path: Path):
    atom = {
        "id": "requirement:observable_result",
        "type": "behavior",
        "description": "The public command produces the requested result.",
        "required": True,
        "status": "pending",
    }

    missing = evaluate_requirement_atoms(str(tmp_path), [atom], state={"mode": "write"})
    assert missing["ok"] is False
    assert missing["summary"]["required_unverified"] == 1

    passed = evaluate_requirement_atoms(
        str(tmp_path),
        [atom],
        state={
            "mode": "write",
            "verification_claims": {
                "requirement:observable_result": {
                    "status": "passed",
                    "cited_steps": ["public_behavior"],
                    "evidence": ["command returned the requested result"],
                }
            },
        },
    )
    assert passed["ok"] is True
    assert passed["summary"]["required_unverified"] == 0


def test_requested_artifact_and_safe_create_scope_are_static(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
    atoms = [
        {
            "id": "artifact:src/tool.py",
            "type": "artifact_exists",
            "description": "tool exists",
            "required": True,
            "data": {"path": "src/tool.py"},
        },
        {
            "id": "write_scope:no_existing_project_modification",
            "type": "write_scope",
            "description": "existing files unchanged",
            "required": True,
        },
    ]
    state = {
        "workspace": str(tmp_path),
        "mode": "write",
        "changed_files": ["src/tool.py"],
        "write_scope_audit": {
            "workspace_baseline_known": True,
            "existing_project_modified_files": [],
            "new_project_files": ["src/tool.py"],
            "no_existing_project_modification": True,
        },
    }

    out = evaluate_requirement_atoms(str(tmp_path), atoms, state=state)
    assert out["ok"] is True
    assert {atom["status"] for atom in out["atoms"]} == {"passed"}


def test_analysis_behaviors_are_delegated_to_analysis_contract(tmp_path: Path):
    atoms = [
        {
            "id": "requirement:explain_architecture",
            "type": "behavior",
            "description": "Explain architecture with evidence.",
            "required": True,
        }
    ]
    out = evaluate_requirement_atoms(str(tmp_path), atoms, state={"mode": "analyze"})
    assert out["ok"] is True
    assert out["atoms"][0]["status"] == "not_applicable"


def test_summary_counts_required_failures_and_unverified():
    summary = summarize_requirement_atoms([
        {"required": True, "status": "passed"},
        {"required": True, "status": "failed"},
        {"required": True, "status": "unverified"},
        {"required": False, "status": "unverified"},
    ])
    assert summary["required_total"] == 3
    assert summary["required_failed"] == 1
    assert summary["required_unverified"] == 1


def test_structured_artifact_uses_unique_concrete_create_target():
    task_spec = {
        "create_paths": ["timecalc.py", "tests/test_timecalc.py"],
        "requirements": [
            {
                "id": "test_artifact",
                "kind": "artifact",
                "path": "test_timecalc.py",
                "description": "Create the requested test artifact.",
            }
        ],
    }

    atoms = extract_requirement_atoms("create the files", task_spec, {})
    artifact_ids = [atom["id"] for atom in atoms if atom["type"] == "artifact_exists"]

    assert artifact_ids == ["artifact:timecalc.py", "artifact:tests/test_timecalc.py"]


def test_ambiguous_artifact_basename_is_not_guessed():
    task_spec = {
        "create_paths": ["unit/test_report.py", "integration/test_report.py"],
        "requirements": [
            {"kind": "artifact", "path": "test_report.py", "description": "Create another artifact."}
        ],
    }

    atoms = extract_requirement_atoms("create the files", task_spec, {})

    assert any(atom["id"] == "artifact:test_report.py" for atom in atoms)


def test_write_contract_excludes_process_and_pathless_artifact_requirements():
    task_spec = {
        "requirements": [
            {
                "id": "read_sources",
                "kind": "artifact",
                "description": "Read relevant source files before editing.",
                "evidence_mode": "artifact",
            },
            {
                "id": "explain_reasoning",
                "kind": "quality",
                "description": "Explain the diagnosis in the final response.",
                "evidence_mode": "analysis",
            },
            {
                "id": "observable_result",
                "kind": "behavior",
                "description": "The changed program produces the requested result.",
                "evidence_mode": "execution",
            },
        ],
        "task_intent": {"agent_read_only": False},
    }

    atoms = extract_requirement_atoms("Fix the existing program.", task_spec, {})

    assert [atom["id"] for atom in atoms] == ["requirement:observable_result"]


def test_workflow_and_response_scopes_do_not_become_deliverable_atoms():
    task_spec = {
        "requirements": [
            {
                "id": "observable_result",
                "kind": "behavior",
                "scope": "deliverable",
                "description": "The changed program produces the requested result.",
                "evidence_mode": "execution",
            },
            {
                "id": "inspect_first",
                "kind": "behavior",
                "scope": "workflow",
                "description": "Read the project before editing.",
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
    }

    atoms = extract_requirement_atoms("Fix the existing program.", task_spec, {})

    assert [atom["id"] for atom in atoms] == ["requirement:observable_result"]
