from pathlib import Path

from coding_agent.contracts.artifact_constraints import tests_creation_prohibited as prohibits_test_creation
from coding_agent.contracts.contract import extract_task_contract
from coding_agent.nodes.file_plan import _filter_prohibited_files, _required_create_targets
from coding_agent.nodes.final_gate import compute_final_gate
from coding_agent.scope.task_intent import classify_task_intent
from coding_agent.scope.write_intent import build_write_intents, can_execute_write_intent


TASK_NO_TESTS = "Create scripts/hello.py as a small Python CLI script. Do not modify existing project files. Do not create any test files."


def test_task_intent_does_not_infer_tests_when_tests_are_prohibited():
    intent = classify_task_intent(TASK_NO_TESTS)

    assert prohibits_test_creation(TASK_NO_TESTS) is True
    assert intent["mode"] == "write"
    assert intent["create_paths"] == ["scripts/hello.py"]
    assert intent["prohibited_artifacts"][0]["kind"] == "tests"
    assert not any("test" in p for p in intent["create_paths"])


def test_task_intent_filters_llm_suggested_test_paths_when_prohibited():
    intent = classify_task_intent(
        TASK_NO_TESTS,
        {
            "task_type": "write_script",
            "create_paths": ["scripts/hello.py", "tests/test_hello.py"],
            "read_only": False,
        },
    )

    assert intent["create_paths"] == ["scripts/hello.py"]
    assert any(m["path"] == "tests/test_hello.py" and m["intent"] == "prohibited_artifact" for m in intent["path_mentions"])


def test_contract_records_prohibited_tests_without_expect_tests():
    contract = extract_task_contract(TASK_NO_TESTS, {"task_type": "write_script"})

    assert "tests" not in contract["expected_artifacts"]
    assert "pytest_if_tests_exist" not in contract["verification_gates"]
    assert "create_tests" in contract["prohibited_actions"]
    assert contract["prohibited_artifacts"][0]["kind"] == "tests"


def test_contract_treats_tests_not_user_deliverable_as_prohibited():
    task = "Create a small Python project. You may verify it internally, but final deliverables are runnable code and README. Do not create user-facing pytest test files."

    contract = extract_task_contract(task, {"task_type": "generate_project"})

    assert prohibits_test_creation(task) is True
    assert "tests" not in contract["expected_artifacts"]
    assert "create_tests" in contract["prohibited_actions"]
    assert contract["prohibited_artifacts"][0]["kind"] == "tests"


def test_required_targets_and_file_plan_filter_drop_prohibited_tests():
    intent = classify_task_intent(
        TASK_NO_TESTS,
        {"task_type": "write_script", "create_paths": ["scripts/hello.py", "tests/test_hello.py"]},
    )
    contract = extract_task_contract(TASK_NO_TESTS, {"task_type": "write_script"})
    targets = _required_create_targets(TASK_NO_TESTS, contract, intent)
    paths = [x["path"] for x in targets]
    assert paths == ["scripts/hello.py"]

    kept, blocked = _filter_prohibited_files(
        [
            {"path": "scripts/hello.py", "kind": "code"},
            {"path": "tests/test_hello.py", "kind": "test"},
        ],
        contract["prohibited_artifacts"],
    )
    assert [x["path"] for x in kept] == ["scripts/hello.py"]
    assert [x["path"] for x in blocked] == ["tests/test_hello.py"]


def test_write_intent_denies_prohibited_test_artifacts(tmp_path: Path):
    contract = extract_task_contract(TASK_NO_TESTS, {"task_type": "write_script"})
    intent = classify_task_intent(TASK_NO_TESTS)
    state = {
        "workspace": str(tmp_path),
        "task": TASK_NO_TESTS,
        "mode": "write",
        "read_only": False,
        "task_contract": contract,
        "task_intent": intent,
    }
    plan = {"files": [
        {"path": "scripts/hello.py", "kind": "code"},
        {"path": "tests/test_hello.py", "kind": "test"},
    ]}
    intents = build_write_intents(state, plan)["by_path"]

    assert intents["scripts/hello.py"]["allowed"] is True
    assert intents["tests/test_hello.py"]["allowed"] is False
    assert intents["tests/test_hello.py"]["source"] == "prohibited_artifact_constraint"

    ok, reason, data = can_execute_write_intent(state, "tests/test_hello.py", exists=False)
    assert ok is False
    assert "prohibited" in reason
    assert data["prohibited_artifacts"][0]["kind"] == "tests"


def test_final_gate_fails_if_prohibited_test_artifact_was_created():
    contract = extract_task_contract(TASK_NO_TESTS, {"task_type": "write_script"})
    intent = classify_task_intent(TASK_NO_TESTS)
    state = {
        "mode": "write",
        "task": TASK_NO_TESTS,
        "task_contract": contract,
        "task_intent": intent,
        "verification": {"ok": True},
        "contract_ok": True,
        "needs_verification": False,
        "changed_files": [
            "scripts/hello.py",
            ".coding_agent_test/t/tests/test_hello.py",
        ],
        "generated_files": [
            {"path": "scripts/hello.py"},
            {"path": ".coding_agent_test/t/tests/test_hello.py"},
        ],
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 0,
        },
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert any(x.startswith("prohibited_artifact_created:") for x in gate["failures"])
