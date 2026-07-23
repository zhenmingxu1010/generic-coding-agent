from pathlib import Path

from coding_agent.contracts.contract import extract_task_contract, contract_quality_check
from coding_agent.tools.file_tools import write_file
from coding_agent.nodes.diagnose import diagnose_node


def test_contract_uses_structured_domain_requirements_without_keyword_inference():
    task_spec = {
        "read_only": False,
        "requirements": [
            {
                "id": "requested_domain_behavior",
                "kind": "behavior",
                "description": "The program produces the user-requested domain result.",
                "evidence_mode": "execution",
            }
        ],
    }

    contract = extract_task_contract("Implement the requested behavior.", task_spec, {"mode": "write"})
    atom_ids = {atom["id"] for atom in contract["requirement_atoms"]}

    assert atom_ids == {"requirement:requested_domain_behavior"}
    assert contract["required_behaviors"] == []
    assert "execution_based_verification" in contract["verification_gates"]


def test_contract_does_not_treat_protected_tests_directory_as_requested_tests():
    task = (
        "Create files scripts/inspect_experiment_schema.py and "
        "docs/agent_schema_notes.md. Do not write to real scripts, src, tests, "
        "experiments, summary, metrics, or config directories."
    )
    supervisor = {
        "mode": "write",
        "operation_mode": "safe_create",
        "task_intent": {
            "operation_mode": "safe_create",
            "create_paths": ["scripts/inspect_experiment_schema.py", "docs/agent_schema_notes.md"],
            "semantic_write_scope": {
                "operation_mode": "safe_create",
            },
        },
    }

    contract = extract_task_contract(task, {"read_only": False}, supervisor)

    assert "tests" not in contract["expected_artifacts"]
    assert "README" not in contract["expected_artifacts"]
    assert "entrypoint" not in contract["expected_artifacts"]
    assert "core_logic" not in contract["expected_artifacts"]
    assert "pytest_if_tests_exist" not in contract["verification_gates"]
    atom_ids = {atom["id"] for atom in contract["requirement_atoms"]}
    assert atom_ids == {
        "artifact:scripts/inspect_experiment_schema.py",
        "artifact:docs/agent_schema_notes.md",
        "write_scope:no_existing_project_modification",
    }


def test_syntax_aware_write_file_reports_indentation_error(tmp_path):
    r = write_file(str(tmp_path), "bad.py", "if True:\nprint('x')\n")
    assert r.data["changed"] is False
    assert r.data["rejected_write"] is True
    assert r.data["syntax_check"]["checked"] is True
    assert r.data["syntax_check"]["ok"] is False
    assert r.ok is False
    assert not (tmp_path / "bad.py").exists()


def test_diagnose_detects_indentation_error(tmp_path):
    state = {
        "trace_path": str(tmp_path / "trace.jsonl"),
        "state_snapshot_path": str(tmp_path / "state.json"),
        "verification": {"results": [{"name": "py_compile", "command": ["python", "-m", "compileall", "-q", "."], "returncode": 1, "stdout": "*** Error compiling './duration_calculator.py'...\n  File \"./duration_calculator.py\", line 8\n    total += x\n    ^\nIndentationError: expected an indented block after 'if' statement on line 7\n", "stderr": ""}]},
    }
    out = diagnose_node(state)
    assert out["failure"]["failure_type"] == "syntax_level_error"
    assert out["failure"]["target_file"] == "duration_calculator.py"


def test_contract_quality_does_not_treat_conftest_as_weak_test(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text("import sys\n", encoding="utf-8")
    contract = {"expected_artifacts": [], "required_behaviors": [], "verification_gates": []}

    q = contract_quality_check(str(tmp_path), contract)

    assert q["ok"] is True
    assert not any("weak tests detected" in warning for warning in q["warnings"])
    assert q["workspace_contract_scan"]["test_files"] == []


def test_contract_quality_rejects_unexpected_mixed_script_noise_in_generated_code(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "report.py").write_text(
        "def main():\n"
        "    print('best бк model=demo')\n",
        encoding="utf-8",
    )
    state = {
        "task": "Create scripts/report.py that prints an English technical report.",
        "generated_files": [{"path": "scripts/report.py", "kind": "code"}],
    }

    q = contract_quality_check(str(tmp_path), {"expected_artifacts": ["requested_code_files"]}, state=state)

    assert q["ok"] is False
    assert any("unexpected CYRILLIC" in failure for failure in q["failures"])
    assert q["generated_text_hygiene"]["ok"] is False


def test_contract_quality_allows_script_requested_by_task_language(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "report.py").write_text(
        "def main():\n"
        "    print('总计')\n",
        encoding="utf-8",
    )
    state = {
        "task": "请创建 scripts/report.py，输出中文摘要。",
        "generated_files": [{"path": "scripts/report.py", "kind": "code"}],
    }

    q = contract_quality_check(str(tmp_path), {"expected_artifacts": ["requested_code_files"]}, state=state)

    assert q["ok"] is True
    assert q["generated_text_hygiene"]["ok"] is True
