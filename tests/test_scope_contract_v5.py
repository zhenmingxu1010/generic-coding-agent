from __future__ import annotations

from pathlib import Path

from coding_agent.memory.workspace_baseline import ensure_workspace_baseline
from coding_agent.nodes.file_plan import _required_create_targets
from coding_agent.nodes.final_gate import compute_final_gate
from coding_agent.contracts.requirement_atoms import extract_requirement_atoms
from coding_agent.scope.scope_contract import build_scope_contract, path_is_protected
from coding_agent.scope.task_intent import classify_task_intent
from coding_agent.scope.write_intent import build_write_intents, can_execute_write_intent
from coding_agent.scope.write_scope_audit import build_write_scope_audit


def zh(escaped: str) -> str:
    return escaped.encode("ascii").decode("unicode_escape")


DEBUG_TASK = zh(
    r"\u4fee\u590d\u8fd9\u4e2a\u5df2\u6709 Python \u9879\u76ee\uff0c"
    r"\u4f7f pytest \u901a\u8fc7\u3002\u5141\u8bb8\u4fee\u6539 calculator.py\uff0c"
    r"\u4f46\u7981\u6b62\u4fee\u6539 tests/test_calculator.py "
    r"\u548c\u4efb\u4f55\u5df2\u6709\u6d4b\u8bd5\u6587\u4ef6\u3002"
    r"\u4e0d\u8981\u5220\u9664\u6d4b\u8bd5\uff0c\u4e0d\u8981\u6539\u5f31\u6d4b\u8bd5\u3002"
    r"\u4fee\u590d\u540e\u8fd0\u884c pytest \u9a8c\u8bc1\u3002"
)

ISOLATED_CREATE_TASK = zh(
    r"\u8fd9\u662f\u4e00\u4e2a\u5df2\u6709\u9879\u76ee\u3002"
    r"\u672c\u8f6e\u53ea\u5141\u8bb8\u521b\u5efa\u65b0\u6587\u4ef6\uff0c"
    r"\u7981\u6b62\u4fee\u6539\u4efb\u4f55\u5df2\u6709\u9879\u76ee\u6587\u4ef6\uff0c"
    r"\u5c24\u5176\u7981\u6b62\u4fee\u6539 src/original.py\u3002"
    r"\u8bf7\u521b\u5efa scripts/report_value.py\uff0c"
    r"\u8fd0\u884c\u540e\u6253\u5370\u56fa\u5b9a\u6587\u672c report ok\u3002"
    r"\u53ef\u4ee5\u521b\u5efa\u6700\u5c0f pytest \u9a8c\u8bc1\u811a\u672c\u8f93\u51fa\u3002"
    r"\u6700\u540e\u8fd0\u884c\u9a8c\u8bc1\u3002"
)


def test_scope_contract_associates_each_path_with_nearest_operation_marker():
    task = zh(
        r"\u7981\u6b62\u4fee\u6539 README.md \u548c tests/ \u4e0b\u7684\u4efb\u4f55\u6587\u4ef6\uff0c"
        r"\u7981\u6b62\u65b0\u589e\u4f9d\u8d56\uff0c\u53ea\u5141\u8bb8\u4fee\u6539 task_stats.py\u3002"
    )

    scope = build_scope_contract(task)

    assert scope["allowed_modify_paths"] == ["task_stats.py"]
    assert "task_stats.py" not in scope["forbidden_modify_paths"]
    assert "task_stats.py" not in scope["protected_existing_paths"]
    assert "README.md" in scope["forbidden_modify_paths"]


def test_scope_contract_parses_allowed_and_forbidden_paths():
    scope = build_scope_contract(DEBUG_TASK)

    assert scope["allowed_modify_paths"] == ["calculator.py"]
    assert scope["protected_existing_paths"] == ["tests/test_calculator.py"]
    assert "tests/**" in scope["protected_existing_globs"]


def test_task_intent_routes_explicit_fix_to_scoped_modify():
    intent = classify_task_intent(DEBUG_TASK, {"read_only": False})

    assert intent["mode"] == "debug"
    assert intent["operation_mode"] == "scoped_modify"
    assert intent["create_requested"] is False
    assert intent["allowed_modify_paths"] == ["calculator.py"]
    assert intent["protected_existing_paths"] == ["tests/test_calculator.py"]
    assert "tests/test_calculator.py" not in intent["read_reference_paths"]


def test_protected_external_test_does_not_become_input_source_atom():
    intent = classify_task_intent(DEBUG_TASK, {"read_only": False})

    atoms = extract_requirement_atoms(DEBUG_TASK, {}, {"task_intent": intent})

    assert all((atom.get("data") or {}).get("path") != "tests/test_calculator.py" for atom in atoms)


def test_write_intent_allows_only_explicitly_allowed_existing_source(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "tests" / "test_calculator.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    intent = classify_task_intent(DEBUG_TASK, {"read_only": False})
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "task": DEBUG_TASK,
        "mode": intent["mode"],
        "read_only": False,
        "task_intent": intent,
        "scope_contract": intent["scope_contract"],
    }

    intents = build_write_intents(state, {"files": []})
    state["write_intents"] = intents

    assert intents["allowed_write_paths"] == ["calculator.py"]
    assert "tests/test_calculator.py" in intents["blocked_write_paths"]
    assert can_execute_write_intent(state, "calculator.py", exists=True)[0] is True
    assert can_execute_write_intent(state, "tests/test_calculator.py", exists=True)[0] is False


def test_allowed_modify_path_overrides_same_path_stale_protection(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "calculator.py").write_text("def divide(a, b):\n    return a * b\n", encoding="utf-8")
    (tmp_path / "tests" / "test_calculator.py").write_text("from calculator import divide\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    scope = {
        "allowed_modify_paths": ["calculator.py"],
        "forbidden_modify_paths": ["calculator.py", "tests/test_calculator.py"],
        "protected_existing_paths": ["calculator.py", "tests/test_calculator.py"],
        "protected_existing_globs": [],
    }
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "task": "Fix calculator.py but do not modify tests/test_calculator.py.",
        "mode": "modify",
        "read_only": False,
        "task_intent": {"operation_mode": "scoped_modify", "scope_contract": scope},
        "scope_contract": scope,
    }

    intents = build_write_intents(state, {"files": []})
    state["write_intents"] = intents

    assert path_is_protected(scope, "calculator.py") is False
    assert path_is_protected(scope, "tests/test_calculator.py") is True
    assert "calculator.py" in intents["allowed_write_paths"]
    assert "tests/test_calculator.py" not in intents["allowed_write_paths"]
    assert can_execute_write_intent(state, "calculator.py", exists=True)[0] is True
    assert can_execute_write_intent(state, "tests/test_calculator.py", exists=True)[0] is False


def test_llm_scope_can_expand_to_successfully_read_neighboring_source(tmp_path: Path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "hooks.py").write_text("def run():\n    return 0\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    scope = {
        "allowed_modify_paths": ["package/core.py"],
        "semantic_write_scope_source": "llm",
    }
    state = {
        "workspace": str(tmp_path),
        "task": "Fix the package behavior.",
        "mode": "debug",
        "read_only": False,
        "scope_contract": scope,
        "action_history": [{
            "tool": "read_file",
            "args": {"path": "package/hooks.py"},
            "ok": True,
        }],
    }

    ok, reason, detail = can_execute_write_intent(
        state, "package/hooks.py", exists=True
    )

    assert ok is True
    assert "scope expansion" in reason
    assert detail["scope_expansion"] is True
    assert scope["expanded_modify_paths"] == ["package/hooks.py"]
    assert state["scope_expansions"][0]["source"] == "runtime_read_grounded_scope_expansion"


def test_llm_scope_does_not_expand_to_unread_or_unrelated_source(tmp_path: Path):
    (tmp_path / "package").mkdir()
    (tmp_path / "other").mkdir()
    for rel in ("package/core.py", "package/blind.py", "other/read.py"):
        (tmp_path / rel).write_text("VALUE = 1\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    scope = {
        "allowed_modify_paths": ["package/core.py"],
        "semantic_write_scope_source": "llm",
    }
    state = {
        "workspace": str(tmp_path),
        "task": "Fix the package behavior.",
        "mode": "debug",
        "read_only": False,
        "scope_contract": scope,
        "action_history": [{
            "tool": "read_file",
            "args": {"path": "other/read.py"},
            "ok": True,
        }],
    }

    assert can_execute_write_intent(
        state, "package/blind.py", exists=True
    )[0] is False
    assert can_execute_write_intent(
        state, "other/read.py", exists=True
    )[0] is False


def test_repository_discoverable_repair_can_expand_beyond_llm_guessed_area(tmp_path: Path):
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "storage.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "metadata.toml").write_text("[project]\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    scope = {
        "allowed_modify_paths": ["metadata.toml"],
        "semantic_write_scope_source": "llm",
    }
    state = {
        "workspace": str(tmp_path),
        "task": "Inspect and repair the existing project implementation.",
        "mode": "debug",
        "read_only": False,
        "task_intent": {"source_modify_intent": True},
        "task_completeness": {"target_clarity": "repository_discoverable"},
        "scope_contract": scope,
        "action_history": [{
            "tool": "read_file",
            "args": {"path": "package/storage.py"},
            "ok": True,
        }],
    }

    ok, _reason, detail = can_execute_write_intent(
        state, "package/storage.py", exists=True
    )

    assert ok is True
    assert detail["scope_expansion"] is True
    assert scope["expanded_modify_paths"] == ["package/storage.py"]


def test_explicit_user_scope_never_expands_from_read_history(tmp_path: Path):
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "package" / "hooks.py").write_text("VALUE = 2\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    scope = {"allowed_modify_paths": ["package/core.py"]}
    state = {
        "workspace": str(tmp_path),
        "task": "Only modify package/core.py.",
        "mode": "debug",
        "read_only": False,
        "scope_contract": scope,
        "action_history": [{
            "tool": "read_file",
            "args": {"path": "package/hooks.py"},
            "ok": True,
        }],
    }

    ok, reason, detail = can_execute_write_intent(
        state, "package/hooks.py", exists=True
    )

    assert ok is False
    assert "outside the allowed" in reason
    assert detail["outside_allowed_modify_scope"] is True


def test_protected_path_is_not_create_target_or_inferred_test():
    intent = classify_task_intent(ISOLATED_CREATE_TASK, {"read_only": False})
    targets = _required_create_targets(ISOLATED_CREATE_TASK, {"expected_artifacts": ["tests"]}, intent)
    paths = {item["path"] for item in targets}

    assert "src/original.py" not in intent["create_paths"]
    assert "tests/test_original.py" not in intent["create_paths"]
    assert "src/original.py" in intent["protected_existing_paths"]
    assert "scripts/report_value.py" in paths
    assert "tests/test_report_value.py" not in paths


def test_safe_create_cannot_write_protected_original_path(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "original.py").write_text("VALUE = 1\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    intent = classify_task_intent(ISOLATED_CREATE_TASK, {"read_only": False})
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "task": ISOLATED_CREATE_TASK,
        "mode": "write",
        "read_only": False,
        "task_intent": intent,
        "scope_contract": intent["scope_contract"],
    }
    intents = build_write_intents(
        state,
        {"files": [{"path": "src/original.py", "kind": "code"}]},
    )

    assert "src/original.py" in intents["blocked_write_paths"]
    assert intents["by_path"]["src/original.py"]["allowed"] is False

    state["write_intents"] = intents
    ok, reason, detail = can_execute_write_intent(state, "src/original.py", exists=True)
    assert ok is False
    assert "protected" in reason
    assert detail["protected_by_scope"] is True


def test_final_gate_accepts_greenfield_direct_new_files_with_empty_baseline(tmp_path: Path):
    ensure_workspace_baseline(tmp_path)
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "mode": "generate_project",
        "read_only": False,
        "task_intent": {"operation_mode": "safe_create"},
        "verification": {"ok": True},
        "contract_ok": True,
        "needs_verification": False,
        "changed_files": ["timecalc.py", "README.md", ".coding_agent_test/t/tests/test_timecalc.py"],
        "generated_files": [
            {"path": "timecalc.py"},
            {"path": "README.md"},
            {"path": ".coding_agent_test/t/tests/test_timecalc.py", "kind": "test", "agent_internal": True},
        ],
        "repair_history": [],
        "requirement_atom_summary": {"required_total": 1, "required_failed": 0, "required_unverified": 0},
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is True
    assert gate["write_scope_audit"]["existing_project_modified_files"] == []
    assert gate["write_scope_audit"]["new_project_files"] == ["timecalc.py", "README.md"]
    assert gate["write_scope_audit"]["agent_test_changed_files"] == [".coding_agent_test/t/tests/test_timecalc.py"]


def test_final_gate_rejects_protected_original_path_write():
    intent = classify_task_intent(ISOLATED_CREATE_TASK, {"read_only": False})
    state = {
        "thread_id": "t",
        "mode": "write",
        "read_only": False,
        "task_intent": intent,
        "scope_contract": intent["scope_contract"],
        "verification": {"ok": True},
        "contract_ok": True,
        "needs_verification": False,
        "changed_files": ["src/original.py"],
        "generated_files": [{"path": "src/original.py"}],
        "repair_history": [],
        "requirement_atom_summary": {"required_total": 1, "required_failed": 0, "required_unverified": 0},
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "protected_path_written:src/original.py" in gate["failures"]


def test_final_gate_rejects_existing_change_outside_declared_and_expanded_scope(
    tmp_path: Path,
):
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "package" / "other.py").write_text("VALUE = 2\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "mode": "modify",
        "read_only": False,
        "scope_contract": {
            "allowed_modify_paths": ["package/core.py"],
            "expanded_modify_paths": [],
        },
        "verification": {"ok": True},
        "contract_ok": True,
        "needs_verification": False,
        "changed_files": ["package/other.py"],
        "generated_files": [],
        "repair_history": [],
        "requirement_atom_summary": {
            "required_total": 1,
            "required_failed": 0,
            "required_unverified": 0,
        },
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert (
        "out_of_scope_existing_path_written:package/other.py"
        in gate["failures"]
    )


def test_write_scope_audit_splits_source_files_and_internal_tests():
    state = {
        "thread_id": "t",
        "changed_files": ["scripts/report_value.py", ".coding_agent_test/t/tests/test_report_value.py"],
        "generated_files": [{"path": ".coding_agent_test/t/tests/test_report_value.py", "kind": "test"}],
        "repair_history": [
            {
                "changed": True,
                "files_changed": ["scripts/report_value.py"],
            }
        ],
    }
    audit = build_write_scope_audit(state)

    assert audit["source_changed_files"] == ["scripts/report_value.py"]
    assert audit["agent_test_changed_files"] == [".coding_agent_test/t/tests/test_report_value.py"]


def test_analyze_final_gate_rejects_unresolved_required_atoms():
    state = {
        "mode": "analyze",
        "read_only": True,
        "write_locked": True,
        "verification": {"ok": True, "analysis_ok": True},
        "analysis_quality": {"ok": True},
        "changed_files": [],
        "generated_files": [],
        "repair_history": [],
        "requirement_atom_summary": {"required_total": 2, "required_failed": 0, "required_unverified": 2},
    }

    gate = compute_final_gate(state)

    assert gate["ok"] is False
    assert "required_requirement_atoms_unverified:2" in gate["failures"]
