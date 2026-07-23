from coding_agent.scope.mode_policy import explicit_read_only_requested, classify_mode_heuristic, resolve_read_only
from coding_agent.workspace.artifacts import build_artifact_registry


def test_do_not_delete_tests_does_not_make_debug_task_read_only():
    task = "修复这个项目，使 pytest 通过。不要删除测试。"
    mode = classify_mode_heuristic(task, "auto", {})
    assert mode == "debug"
    assert explicit_read_only_requested(task) is False
    assert resolve_read_only(task, mode, supervisor_read_only=True) is False


def test_explicit_read_only_still_locks_analyze_or_debug():
    task = "只读分析这个项目，不修改源码。"
    mode = classify_mode_heuristic(task, "auto", {"task_type": "analyze"})
    assert mode == "analyze"
    assert resolve_read_only(task, mode, supervisor_read_only=False) is True


def test_debug_mode_external_source_modifiable_but_external_test_protected():
    state = {
        "mode": "debug",
        "read_only": False,
        "repo_map": {"files": ["math_utils.py", "test_math_utils.py"]},
        "file_plan": {},
        "generated_files": [],
        "changed_files": [],
        "repair_history": [],
    }
    reg = build_artifact_registry(state)
    assert reg["by_path"]["math_utils.py"]["modifiable_by_agent"] is True
    assert reg["by_path"]["test_math_utils.py"]["modifiable_by_agent"] is False


def test_analyze_mode_nothing_modifiable():
    state = {
        "mode": "analyze",
        "read_only": True,
        "repo_map": {"files": ["src/a.py", "tests/test_a.py"]},
        "file_plan": {},
        "generated_files": [],
        "changed_files": [],
        "repair_history": [],
    }
    reg = build_artifact_registry(state)
    assert all(not item["modifiable_by_agent"] for item in reg["entries"])
