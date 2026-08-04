from __future__ import annotations

import pytest

from coding_agent.contracts.requirement_atoms import extract_requirement_atoms
from coding_agent.contracts.task_completeness import assess_task_completeness
from coding_agent.core.resume import prepare_resumed_state
from coding_agent.nodes.final_gate import compute_final_gate
from coding_agent.nodes.task_clarify import route_after_task_clarify, task_clarify_node
from coding_agent.scope.task_intent import classify_task_intent


REPO_WITH_TESTS = {
    "files": ["app.py", "tests/test_app.py"],
    "has_tests": True,
    "project_types": ["python"],
}
EMPTY_REPO = {"files": [], "has_tests": False, "project_types": []}


@pytest.mark.parametrize(
    ("task", "activity", "decision"),
    [
        ("写个脚本", "create", "clarify"),
        ("做个项目", "create", "clarify"),
        ("build something", "create", "clarify"),
        ("write a script", "create", "clarify"),
        ("写个脚本统计文本行数", "create", "proceed"),
        ("做一个 todo CLI", "create", "proceed"),
        ("build a todo CLI", "create", "proceed"),
        ("create a JSON formatter", "create", "proceed"),
        ("看看这个项目", "inspect", "inspect_then_proceed"),
        ("帮我了解一下这个仓库", "inspect", "inspect_then_proceed"),
        ("review this repository", "inspect", "inspect_then_proceed"),
        ("加个导出功能", "modify", "inspect_then_proceed"),
        ("add CSV export", "modify", "inspect_then_proceed"),
        ("把日志改成 JSON 格式", "modify", "inspect_then_proceed"),
        ("修一下这个项目", "repair", "inspect_then_proceed"),
        ("fix this project", "repair", "inspect_then_proceed"),
        ("修复运行时报出的 KeyError", "repair", "inspect_then_proceed"),
        ("fix the parser traceback", "repair", "inspect_then_proceed"),
    ],
)
def test_short_prompt_matrix(task: str, activity: str, decision: str):
    intent = classify_task_intent(task, {})
    result = assess_task_completeness(task, {}, intent, REPO_WITH_TESTS)
    assert result["activity"] == activity
    assert result["decision"] == decision


@pytest.mark.parametrize("task", ["修一下这个项目", "fix this project"])
def test_vague_repair_without_repository_or_tests_requires_symptom(task: str):
    result = assess_task_completeness(task, {}, classify_task_intent(task, {}), EMPTY_REPO)
    assert result["decision"] == "clarify"
    assert [item["id"] for item in result["questions"]] == ["failure_symptom"]


@pytest.mark.parametrize("task", ["修个问题", "fix this bug", "repair it"])
def test_generic_repair_placeholder_uses_tests_or_clarifies(task: str):
    intent = classify_task_intent(task, {})
    with_tests = assess_task_completeness(task, {}, intent, REPO_WITH_TESTS)
    empty = assess_task_completeness(task, {}, intent, EMPTY_REPO)
    assert with_tests["decision"] == "inspect_then_proceed"
    assert empty["decision"] == "clarify"


def test_short_and_detailed_prompt_share_same_execution_policy():
    short = "写个脚本统计文本行数"
    detailed = (
        "请创建一个 Python 脚本，读取用户提供的文本文件，统计其中的行数，"
        "在终端打印整数结果，并通过一次代表性示例运行验证。"
    )
    short_result = assess_task_completeness(short, {}, classify_task_intent(short, {}), EMPTY_REPO)
    detailed_result = assess_task_completeness(detailed, {}, classify_task_intent(detailed, {}), EMPTY_REPO)
    assert short_result["activity"] == detailed_result["activity"] == "create"
    assert short_result["decision"] == detailed_result["decision"] == "proceed"
    assert {item["id"] for item in short_result["implementation_requirements"]} == {
        item["id"] for item in detailed_result["implementation_requirements"]
    }


def test_empty_workspace_build_cli_is_not_confused_by_add_command_name():
    task = (
        "Build an installable Python CLI called worklog. "
        "Support add TITLE, list, done ID, and stats commands."
    )
    intent = classify_task_intent(task, {
        "task_type": "generate_project",
        "create_paths": ["pyproject.toml", "worklog/__main__.py"],
        "write_scope_intent": {
            "task_mode": "generate_project",
            "operation_mode": "safe_create",
            "source_modification": {"allowed": False},
            "existing_file_modification": {"allowed": False},
            "allowed_operations": [
                {"path": "pyproject.toml", "operation": "create_new"},
                {"path": "worklog/__main__.py", "operation": "create_new"},
            ],
            "confidence": 0.95,
        },
    })

    result = assess_task_completeness(task, {}, intent, EMPTY_REPO)

    assert result["activity"] == "create"
    assert result["decision"] == "proceed"
    assert result["target_clarity"] == "new_artifact"


def test_known_short_create_uses_explicit_defaults_not_user_requirements():
    task = "写个脚本统计文本行数"
    result = assess_task_completeness(task, {}, classify_task_intent(task, {}), EMPTY_REPO)
    assert result["decision"] == "proceed"
    assert {item["field"] for item in result["assumptions"]} >= {"language", "output_layout", "acceptance"}
    atoms = extract_requirement_atoms(task, {
        "implementation_requirements": result["implementation_requirements"],
    })
    implementation_atoms = [item for item in atoms if item["id"].startswith("implementation:")]
    assert implementation_atoms
    assert all(item["source"] == "agent_implementation_default" for item in implementation_atoms)
    assert all("not an explicit user requirement" in item["evidence"][0] for item in implementation_atoms)


def test_inferred_artifact_requirement_does_not_fill_missing_core_behavior():
    task = "写个脚本"
    llm_spec = {
        "requirements": [{
            "id": "script_artifact",
            "kind": "artifact",
            "description": "Create script.py",
            "path": "script.py",
        }],
        "create_paths": ["script.py"],
    }
    result = assess_task_completeness(
        task,
        llm_spec,
        classify_task_intent(task, llm_spec),
        EMPTY_REPO,
    )
    assert result["decision"] == "clarify"
    assert result["behavior_clarity"] == "missing"


def test_task_clarify_pauses_before_project_actions(monkeypatch, tmp_path):
    events = []

    class Trace:
        def event(self, *args, **kwargs):
            events.append((args, kwargs))

        def snapshot(self, state):
            events.append((("snapshot",), {}))

    monkeypatch.setattr("coding_agent.nodes.task_clarify.get_trace", lambda state: Trace())
    task = "写个脚本"
    state = {
        "task": task,
        "workspace": str(tmp_path),
        "task_spec": {"objective": task},
        "task_intent": classify_task_intent(task, {}),
        "repo_map": EMPTY_REPO,
        "supervisor": {"mode": "write", "task_intent": classify_task_intent(task, {})},
        "changed_files": [],
        "generated_files": [],
    }
    result = task_clarify_node(state)
    assert route_after_task_clarify(result) == "report"
    assert result["stopped_reason"] == "clarification_required"
    assert result["clarification_questions"]
    assert result["changed_files"] == []
    assert result["generated_files"] == []
    gate = compute_final_gate(result)
    assert gate["outcome"] == "clarification_required"
    assert gate["controlled_failure"] is True
    assert gate["failures"] == ["clarification_required"]


def test_inspection_short_prompt_is_locked_read_only(monkeypatch, tmp_path):
    class Trace:
        def event(self, *args, **kwargs):
            pass

        def snapshot(self, state):
            pass

    monkeypatch.setattr("coding_agent.nodes.task_clarify.get_trace", lambda state: Trace())
    task = "看看这个项目"
    state = {
        "task": task,
        "workspace": str(tmp_path),
        "task_spec": {"objective": task},
        "task_intent": classify_task_intent(task, {}),
        "repo_map": REPO_WITH_TESTS,
        "supervisor": {"mode": "analyze"},
    }
    result = task_clarify_node(state)
    assert route_after_task_clarify(result) == "context_retrieve"
    assert result["mode"] == "analyze"
    assert result["read_only"] is True
    assert result["supervisor"]["allowed_write"] is False


def test_clarification_answer_rebuilds_task_and_discards_stale_contract():
    state = {
        "task": "写个脚本",
        "original_task": "写个脚本",
        "stopped_reason": "clarification_required",
        "clarification_questions": [{"id": "core_behavior", "question": "做什么？"}],
        "task_spec": {"objective": "stale"},
        "task_contract": {"objective": "stale"},
        "failure": {"failure_type": "clarification_required"},
        "round_idx": 5,
    }
    resumed = prepare_resumed_state(
        state,
        max_rounds=12,
        max_repair_calls=6,
        clarification_answer="统计文本文件的行数并打印结果",
    )
    assert resumed["task"].startswith("写个脚本")
    assert "统计文本文件的行数" in resumed["task"]
    assert resumed["clarification_history"][0]["answer"] == "统计文本文件的行数并打印结果"
    assert "task_spec" not in resumed
    assert "task_contract" not in resumed
    assert "failure" not in resumed
    assert resumed["round_idx"] == 0


def test_clarification_answer_rejected_for_non_clarification_checkpoint():
    with pytest.raises(ValueError, match="clarification_required"):
        prepare_resumed_state(
            {"task": "x", "stopped_reason": "max_rounds"},
            max_rounds=12,
            max_repair_calls=6,
            clarification_answer="answer",
        )
