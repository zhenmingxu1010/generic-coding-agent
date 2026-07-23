from __future__ import annotations

from pathlib import Path

from coding_agent.contracts.requirement_atoms import evaluate_requirement_atoms, extract_requirement_atoms
from coding_agent.verification.test_path_policy import normalize_generated_test_write_path
from coding_agent.scope.task_intent import classify_task_intent


def test_llm_semantic_source_modify_allows_project_change_without_literal_permission():
    task = "Make the existing calculator handle division and keep the public API stable."
    intent = classify_task_intent(
        task,
        {
            "task_type": "modify_code",
            "write_scope_intent": {
                "task_mode": "source_modify",
                "source_modification": {
                    "allowed": True,
                    "confidence": 0.91,
                    "reason": "The requested result changes existing project behavior.",
                },
                "existing_file_modification": {"allowed": True, "confidence": 0.91},
                "allowed_operations": [
                    {
                        "path": "src/calculator.py",
                        "operation": "modify_existing",
                        "confidence": 0.88,
                        "reason": "Likely implementation target.",
                    }
                ],
                "confidence": 0.91,
            },
        },
    )

    assert intent["intent_source"] == "llm_semantic"
    assert intent["mode"] == "modify"
    assert intent["operation_mode"] == "scoped_modify"
    assert intent["source_modify_intent"] is True
    assert intent["agent_read_only"] is False
    assert "src/calculator.py" in intent["allowed_modify_paths"]


def test_llm_semantic_safe_create_blocks_source_modify():
    task = "Produce a separate report script for this repository without changing existing project files."
    intent = classify_task_intent(
        task,
        {
            "task_type": "write_script",
            "write_scope_intent": {
                "task_mode": "safe_create",
                "source_modification": {"allowed": False, "confidence": 0.94},
                "existing_file_modification": {"allowed": False, "confidence": 0.94},
                "allowed_operations": [
                    {"path": "scripts/report.py", "operation": "create_new", "confidence": 0.9}
                ],
                "protected_paths": [{"path": "**", "reason": "Existing project files are protected."}],
                "confidence": 0.94,
            },
        },
    )

    assert intent["mode"] == "write"
    assert intent["operation_mode"] == "safe_create"
    assert intent["source_modify_intent"] is False
    assert intent["agent_read_only"] is False
    assert intent["create_paths"] == ["scripts/report.py"]
    assert "**" in intent["scope_contract"]["protected_existing_globs"]


def test_low_confidence_source_modify_is_safely_downgraded():
    intent = classify_task_intent(
        "Improve the repository based on the issue description.",
        {
            "write_scope_intent": {
                "task_mode": "source_modify",
                "source_modification": {"allowed": True, "confidence": 0.4},
                "existing_file_modification": {"allowed": True, "confidence": 0.4},
                "ambiguities": ["The prompt does not clearly authorize source edits."],
                "confidence": 0.4,
            }
        },
    )

    assert intent["intent_source"] == "llm_semantic"
    assert intent["operation_mode"] == "read_only_analysis"
    assert intent["source_modify_intent"] is False
    assert intent["semantic_write_scope"]["safety_downgraded"] is True
    assert intent["agent_read_only"] is True


def test_contradictory_source_modify_is_safely_downgraded():
    intent = classify_task_intent(
        "Prepare the changes but do not touch existing files.",
        {
            "write_scope_intent": {
                "task_mode": "source_modify",
                "source_modification": {"allowed": True, "confidence": 0.92},
                "existing_file_modification": {"allowed": False, "confidence": 0.92},
                "confidence": 0.92,
            }
        },
    )

    assert intent["operation_mode"] == "read_only_analysis"
    assert intent["source_modify_intent"] is False
    assert "source_modification_allowed_but_existing_file_modification_forbidden" in intent["semantic_write_scope"]["consistency_issues"]


def test_concrete_modify_paths_override_spurious_isolation_flag():
    intent = classify_task_intent(
        "Add low stock reporting. Modify inventory/stock.py and inventory/cli.py, and add a new test file.",
        {
            "write_scope_intent": {
                "task_mode": "source_modify",
                "source_modification": {"allowed": True, "confidence": 0.9},
                "existing_file_modification": {"allowed": True, "confidence": 0.9},
                "allowed_operations": [
                    {"path": "inventory/stock.py", "operation": "modify_existing", "confidence": 0.9},
                    {"path": "inventory/cli.py", "operation": "modify_existing", "confidence": 0.9},
                    {"path": "tests/test_new_cli.py", "operation": "create_new", "confidence": 0.8},
                ],
                "confidence": 0.9,
            }
        },
    )

    assert intent["operation_mode"] == "scoped_modify"
    assert intent["mode"] == "modify"
    assert intent["source_modify_intent"] is True
    assert intent["semantic_write_scope"]["source_modification_allowed"] is True
    assert "inventory/stock.py" in intent["allowed_modify_paths"]
    assert "inventory/cli.py" in intent["allowed_modify_paths"]


def test_llm_semantic_protected_paths_merge_into_scope_contract():
    intent = classify_task_intent(
        "Fix the package behavior while keeping existing tests unchanged.",
        {
            "write_scope_intent": {
                "task_mode": "debug",
                "source_modification": {"allowed": True, "confidence": 0.9},
                "existing_file_modification": {"allowed": True, "confidence": 0.9},
                "protected_paths": [{"path": "tests/**", "reason": "Existing tests are oracle files."}],
                "confidence": 0.9,
            }
        },
    )

    assert intent["mode"] == "debug"
    assert intent["operation_mode"] == "scoped_modify"
    assert "tests/**" in intent["scope_contract"]["protected_existing_globs"]


def test_no_new_files_wildcard_does_not_block_explicit_existing_edits():
    intent = classify_task_intent(
        "Fix src/core.py, but do not add new files.",
        {
            "task_type": "fix_tests",
            "write_scope_intent": {
                "task_mode": "source_modify",
                "source_modification": {"allowed": True, "confidence": 0.95},
                "existing_file_modification": {"allowed": True, "confidence": 0.95},
                "allowed_operations": [
                    {"path": "src/core.py", "operation": "modify_existing", "confidence": 1.0},
                ],
                "protected_paths": [{"path": "**", "reason": "禁止新增文件"}],
                "confidence": 0.95,
            },
        },
    )

    assert intent["mode"] == "modify"
    assert intent["operation_mode"] == "scoped_modify"
    assert intent["source_modify_intent"] is True
    assert intent["allowed_modify_paths"] == ["src/core.py"]
    assert "**" not in intent["scope_contract"]["protected_existing_globs"]


def test_bare_write_scope_object_from_intake_is_accepted():
    intent = classify_task_intent(
        "Make the existing package expose a new helper.",
        {
            "task_mode": "source_modify",
            "source_modification": {"allowed": True, "confidence": 0.9},
            "existing_file_modification": {"allowed": True, "confidence": 0.9},
            "confidence": 0.9,
        },
    )

    assert intent["intent_source"] == "llm_semantic"
    assert intent["operation_mode"] == "scoped_modify"
    assert intent["source_modify_intent"] is True


def test_source_modify_llm_guessed_test_path_is_not_hard_create_contract():
    intent = classify_task_intent(
        "Add divide(a,b) to the existing calculator and verify with pytest.",
        {
            "write_scope_intent": {
                "task_mode": "source_modify",
                "source_modification": {"allowed": True, "confidence": 0.9},
                "existing_file_modification": {"allowed": True, "confidence": 0.9},
                "allowed_operations": [
                    {"path": "calculator.py", "operation": "modify_existing", "confidence": 0.9},
                    {"path": "test_calculator.py", "operation": "create_new", "confidence": 0.8},
                ],
                "confidence": 0.9,
            }
        },
    )

    assert intent["operation_mode"] == "scoped_modify"
    assert intent["allowed_modify_paths"] == ["calculator.py"]
    assert intent["create_paths"] == []


def test_generated_root_test_path_normalizes_to_project_tests_dir(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calculator.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "task": "Add divide(a,b) and verify with pytest.",
        "task_contract": {"expected_artifacts": ["tests"]},
        "repo_map": {"files": ["calculator.py", "tests/test_calculator.py"]},
        "test_policy": {"generate_internal_tests": True},
    }

    path, info = normalize_generated_test_write_path(state, "test_calculator.py")

    assert path == ".coding_agent_test/default/test_calculator.py"
    assert info["before"] == "test_calculator.py"


def test_generated_test_support_directory_normalizes_to_internal_agent_tests(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "task": "Create the requested project and verify it internally.",
        "task_contract": {"expected_artifacts": []},
        "repo_map": {"files": []},
        "test_policy": {"generate_internal_tests": True},
    }

    init_path, init_info = normalize_generated_test_write_path(state, "agent_tests/__init__.py")
    fixture_path, fixture_info = normalize_generated_test_write_path(state, "integration_tests/fixtures/sample.txt")

    assert init_path == ".coding_agent_test/t/agent_tests/__init__.py"
    assert init_info["before"] == "agent_tests/__init__.py"
    assert fixture_path == ".coding_agent_test/t/integration_tests/fixtures/sample.txt"
    assert fixture_info["before"] == "integration_tests/fixtures/sample.txt"


def test_behavior_requirements_use_execution_claims_not_symbol_name_scanning(tmp_path: Path):
    atoms = extract_requirement_atoms(
        "Implement the requested behavior.",
        {
            "requirements": [
                {
                    "id": "division_behavior",
                    "kind": "behavior",
                    "description": "Division returns the expected observable result.",
                    "required": True,
                }
            ]
        },
        {},
    )

    unverified = evaluate_requirement_atoms(str(tmp_path), atoms, state={"mode": "write"})
    assert unverified["summary"]["required_unverified"] == 1

    verified = evaluate_requirement_atoms(
        str(tmp_path),
        atoms,
        state={
            "mode": "write",
            "verification_claims": {
                "requirement:division_behavior": {
                    "status": "passed",
                    "cited_steps": ["public_division"],
                    "evidence": ["4 / 2 produced 2"],
                }
            },
        },
    )
    assert verified["ok"] is True
