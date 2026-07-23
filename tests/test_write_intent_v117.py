from pathlib import Path

from coding_agent.memory.workspace_baseline import ensure_workspace_baseline, load_workspace_baseline
from coding_agent.memory.artifact_provenance import record_artifact_event
from coding_agent.scope.write_intent import build_write_intents, can_execute_write_intent


def base_state(tmp_path, task):
    ensure_workspace_baseline(tmp_path)
    return {
        "workspace": str(tmp_path),
        "task": task,
        "mode": "write",
        "read_only": False,
        "thread_id": "t1",
        "file_plan": {"files": []},
    }


def test_baseline_marks_existing_project_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "train.py").write_text("print('train')\n", encoding="utf-8")
    baseline = ensure_workspace_baseline(tmp_path)
    assert "src/train.py" in baseline["files"]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "new_tool.py").write_text("print('x')\n", encoding="utf-8")
    baseline2 = load_workspace_baseline(tmp_path)
    assert "scripts/new_tool.py" not in baseline2["files"]


def test_read_reference_summary_is_not_write_target(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "service_summary.json").write_text("{}\n", encoding="utf-8")
    task = "新增 scripts/summarize_metrics.py。脚本应读取 data/service_summary.json。"
    state = base_state(tmp_path, task)
    plan = {"files": [
        {"path": "scripts/summarize_metrics.py", "kind": "code", "purpose": "new script"},
        {"path": "data/service_summary.json", "kind": "data", "purpose": "input summary"},
    ]}
    intents = build_write_intents(state, plan)
    by = intents["by_path"]
    assert by["scripts/summarize_metrics.py"]["allowed"] is True
    assert by["scripts/summarize_metrics.py"]["operation"] == "create_new"
    assert by["data/service_summary.json"]["allowed"] is False
    assert by["data/service_summary.json"]["operation"] == "read_reference"


def test_output_data_path_can_be_created_when_local_wording_says_output(tmp_path):
    task = "Read invoices.csv and output summary.csv."
    state = base_state(tmp_path, task)

    intents = build_write_intents(state, {"files": []})
    by = intents["by_path"]

    assert by["invoices.csv"]["operation"] == "read_reference"
    assert by["summary.csv"]["operation"] == "create_new"
    assert by["summary.csv"]["allowed"] is True


def test_historical_agent_test_can_be_modified_even_under_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    test_file = tmp_path / "tests" / "test_summarize_metrics.py"
    test_file.write_text("def test_old():\n    assert False\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    record_artifact_event(
        tmp_path,
        path="tests/test_summarize_metrics.py",
        thread_id="old",
        task="old agent generated test",
        action="write_file",
        origin="agent_generated",
        kind="test",
    )
    task = "新增 scripts/summarize_metrics.py，并写一个最小 pytest。"
    state = base_state(tmp_path, task)
    plan = {"files": [{"path": "tests/test_summarize_metrics.py", "kind": "test", "purpose": "generated test"}]}
    state["write_intents"] = build_write_intents(state, plan)
    ok, reason, data = can_execute_write_intent(state, "tests/test_summarize_metrics.py", exists=True)
    assert ok is True, (reason, data)
    assert data["write_intent"]["operation"] == "modify_existing"


def test_project_existing_external_test_is_not_modified_by_name_alone(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_existing.py").write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    task = "修复项目，不要删除测试。"
    state = base_state(tmp_path, task)
    state["mode"] = "debug"
    state["write_intents"] = build_write_intents(state, {"files": [{"path": "tests/test_existing.py", "kind": "test", "purpose": "bad plan"}]})
    ok, reason, data = can_execute_write_intent(state, "tests/test_existing.py", exists=True)
    assert ok is False
    assert data["write_intent"]["operation"] == "approval_required"


def test_current_agent_generated_code_can_be_repaired_under_protected_scope(tmp_path):
    (tmp_path / "scripts").mkdir()
    target = tmp_path / "scripts" / "generated_tool.py"
    target.write_text("print('old')\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    state = base_state(tmp_path, "Create scripts/generated_tool.py without modifying existing project files.")
    state.update(
        {
            "thread_id": "t",
            "scope_contract": {"allowed_modify_paths": [], "protected_existing_globs": ["**", "**/*"]},
            "task_intent": {
                "operation_mode": "safe_create",
                "scope_contract": {"allowed_modify_paths": [], "protected_existing_globs": ["**", "**/*"]},
            },
            "generated_files": [{"path": "scripts/generated_tool.py", "kind": "code"}],
        }
    )
    state["write_intents"] = build_write_intents(
        state,
        {"files": [{"path": "scripts/generated_tool.py", "kind": "code"}]},
    )

    ok, reason, data = can_execute_write_intent(state, "scripts/generated_tool.py", exists=True)

    assert ok is True, (reason, data)
    assert data["current_agent_generated"] is True


def test_current_agent_generated_internal_test_can_be_repaired_under_protected_scope(tmp_path):
    target = tmp_path / ".coding_agent_test" / "t" / "tests" / "test_generated_tool.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_old():\n    assert False\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    state = base_state(tmp_path, "Create a tool and keep verification tests internal.")
    state.update(
        {
            "thread_id": "t",
            "scope_contract": {"allowed_modify_paths": [], "protected_existing_globs": ["**", "**/*"]},
            "task_intent": {
                "operation_mode": "safe_create",
                "scope_contract": {"allowed_modify_paths": [], "protected_existing_globs": ["**", "**/*"]},
            },
            "generated_files": [
                {"path": ".coding_agent_test/t/tests/test_generated_tool.py", "kind": "test", "agent_internal": True},
            ],
        }
    )
    state["write_intents"] = build_write_intents(
        state,
        {"files": [{"path": ".coding_agent_test/t/tests/test_generated_tool.py", "kind": "test"}]},
    )

    ok, reason, data = can_execute_write_intent(state, ".coding_agent_test/t/tests/test_generated_tool.py", exists=True)

    assert ok is True, (reason, data)
    assert data["current_agent_generated"] is True


def test_modify_mode_allows_llm_unresolved_support_file_creation(tmp_path):
    ensure_workspace_baseline(tmp_path)
    state = {
        "workspace": str(tmp_path),
        "task": "Fix this existing Python project so verification passes without weakening tests.",
        "mode": "modify",
        "read_only": False,
        "task_intent": {
            "source_modify_intent": True,
            "operation_mode": "scoped_modify",
            "scope_contract": {
                "protected_existing_globs": ["tests/**/*.py"],
                "unresolved_modify_targets": [
                    {"path": "conftest.py", "reason": "LLM inferred support file may be needed"},
                    {"path": "pyproject.toml", "reason": "LLM inferred project config may be needed"},
                ],
            },
        },
        "scope_contract": {
            "protected_existing_globs": ["tests/**/*.py"],
            "unresolved_modify_targets": [
                {"path": "conftest.py", "reason": "LLM inferred support file may be needed"},
                {"path": "pyproject.toml", "reason": "LLM inferred project config may be needed"},
            ],
        },
    }

    ok, reason, data = can_execute_write_intent(state, "conftest.py", exists=False)
    assert ok is True, (reason, data)
    assert data["implicit_support_create"] is True

    ok, reason, data = can_execute_write_intent(state, "pyproject.toml", exists=False)
    assert ok is True, (reason, data)
    assert data["implicit_support_create"] is True

    ok, reason, data = can_execute_write_intent(state, "random_support.py", exists=False)
    assert ok is False
    assert "no allowed write_intent" in reason
