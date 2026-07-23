from pathlib import Path

from coding_agent.scope.task_intent import classify_task_intent
from coding_agent.scope.mode_policy import classify_mode_heuristic, resolve_read_only
from coding_agent.nodes.file_plan import _required_create_targets
from coding_agent.scope.write_intent import build_write_intents
from coding_agent.memory.workspace_baseline import ensure_workspace_baseline


def test_readonly_analysis_script_is_write_task_not_global_readonly():
    task = (
        "这是一个全新的已有项目工作区，目前没有任何历史 Agent 记忆。请先只读扫描项目结构，"
        "然后新增一个只读分析脚本 scripts/summarize_metrics.py。脚本应读取 "
        "data/service_summary.json。写一个新的最小 pytest。"
    )
    intent = classify_task_intent(task)
    assert intent["mode"] == "write"
    assert intent["agent_read_only"] is False
    assert intent["script_read_only"] is True
    assert intent["scan_first"] is True
    assert "scripts/summarize_metrics.py" in intent["create_paths"]
    assert "tests/test_summarize_metrics.py" not in intent["create_paths"]
    assert "data/service_summary.json" in intent["read_reference_paths"]
    assert classify_mode_heuristic(task, "auto", {"task_type": "analyze"}) == "write"
    assert resolve_read_only(task, "write", supervisor_read_only=True) is False


def test_required_targets_come_from_intent_not_read_references():
    task = "创建新脚本 scripts/foo.py。脚本运行时只读取 data/input.json。写一个 pytest。"
    intent = classify_task_intent(task)
    targets = _required_create_targets(task, {"expected_artifacts": ["tests"]}, intent)
    paths = {x["path"] for x in targets}
    assert "scripts/foo.py" in paths
    assert "tests/test_foo.py" not in paths
    assert "data/input.json" not in paths


def test_internal_verification_tests_are_not_user_deliverables():
    task = (
        "创建新脚本 scripts/metrics_report.py。"
        "Agent 自己用于验证的测试不要作为用户交付文件写入项目 tests 或其他测试目录，"
        "必须放入内部测试目录。"
    )

    intent = classify_task_intent(task, {"read_only": False})

    assert "scripts/metrics_report.py" in intent["create_paths"]
    assert all(not path.startswith("tests/") for path in intent["create_paths"])
    assert all("inferred pytest target" not in str(m.get("context", "")) for m in intent["path_mentions"])


def test_symbolic_agent_test_location_is_not_a_create_target():
    task = (
        "允许新增 scripts/summarize_metrics.py。Agent 自己创建的验证测试必须位于 "
        ".coding_agent_test/<thread-id>。"
    )

    intent = classify_task_intent(
        task,
        {
            "task_type": "write_script",
            "create_paths": [
                "scripts/summarize_metrics.py",
                ".coding_agent_test/<thread-id>",
            ],
        },
    )

    assert intent["create_paths"] == ["scripts/summarize_metrics.py"]
    assert all("<thread-id>" not in str(item.get("path")) for item in intent["path_mentions"])


def test_task_intent_dedupes_equivalent_root_and_tests_targets():
    intent = classify_task_intent(
        "Create timecalc.py and pytest tests.",
        {
            "task_type": "generate_project",
            "create_paths": ["timecalc.py", "test_timecalc.py"],
        },
    )

    assert "timecalc.py" in intent["create_paths"]
    assert "tests/test_timecalc.py" in intent["create_paths"]
    assert "test_timecalc.py" not in intent["create_paths"]
    create_mentions = [m["path"] for m in intent["path_mentions"] if m.get("intent") == "create_target"]
    assert "test_timecalc.py" not in create_mentions


def test_task_intent_rejects_unmentioned_directory_create_hints_from_llm():
    intent = classify_task_intent(
        "Create scripts/report.py. Keep generated verification tests internal and do not deliver them as project files.",
        {
            "task_type": "write_script",
            "write_scope_intent": {
                "task_mode": "safe_create",
                "source_modification": {"allowed": False, "confidence": 0.95},
                "existing_file_modification": {"allowed": False, "confidence": 0.95},
                "allowed_operations": [
                    {"path": "scripts/report.py", "operation": "create_new", "confidence": 0.95},
                    {"path": "internal_tests/", "operation": "create_new", "confidence": 0.95},
                    {"path": "internal_tests", "operation": "create_new", "confidence": 0.95},
                ],
                "confidence": 0.95,
            },
        },
    )

    assert "scripts/report.py" in intent["create_paths"]
    assert "internal_tests" not in intent["create_paths"]
    assert "internal_tests/" not in intent["create_paths"]


def test_write_intents_allow_created_script_and_protect_reference(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "service_summary.json").write_text("{}\n", encoding="utf-8")
    ensure_workspace_baseline(tmp_path)
    task = "新增一个只读分析脚本 scripts/summarize_metrics.py。脚本应读取 data/service_summary.json。写一个 pytest。"
    state = {"workspace": str(tmp_path), "task": task, "mode": "write", "read_only": False, "thread_id": "t"}
    plan = {"files": [
        {"path": "scripts/summarize_metrics.py", "kind": "code", "purpose": "analysis script"},
        {"path": "tests/test_summarize_metrics.py", "kind": "test", "purpose": "generated test"},
        {"path": "data/service_summary.json", "kind": "data", "purpose": "input summary"},
    ]}
    intents = build_write_intents(state, plan)["by_path"]
    assert intents["scripts/summarize_metrics.py"]["allowed"] is True
    assert intents["tests/test_summarize_metrics.py"]["allowed"] is True
    assert intents["data/service_summary.json"]["allowed"] is False
    assert intents["data/service_summary.json"]["operation"] == "read_reference"
