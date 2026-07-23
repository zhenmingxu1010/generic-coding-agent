from pathlib import Path

from coding_agent.ux.task_defaults import (
    extract_existing_workspace_path,
    extract_new_workspace_path,
    prepare_task_for_agent,
    should_auto_route_chat_to_code,
)
from coding_agent.contracts.analysis_contract import build_analysis_contract


def test_extract_existing_workspace_path_handles_absolute_path_with_chinese_suffix(tmp_path: Path):
    project = tmp_path / "001" / "demo_project"
    project.mkdir(parents=True)
    task = f"帮我分析{project}这个文件夹下面的情况"

    out = extract_existing_workspace_path(task, tmp_path)

    assert out == project.resolve()


def test_extract_existing_workspace_path_handles_posix_path_after_chinese_prefix(tmp_path: Path):
    project = tmp_path / "001" / "demo_project"
    project.mkdir(parents=True)
    task = f"帮我分析{project}这个文件夹，CLI 参数里可能还有 project/date"

    out = extract_existing_workspace_path(task, tmp_path)

    assert out == project.resolve()


def test_prepare_project_understanding_task_switches_workspace_and_adds_defaults(tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    task = f"查看一下 {project}"

    prepared = prepare_task_for_agent(task, base_workspace=tmp_path, mode="code")

    assert prepared.workspace == project.resolve()
    assert prepared.workspace_changed is True
    assert "project_understanding" in prepared.defaults_applied
    assert "Terminal UI default project-understanding instructions" not in prepared.task
    assert "Terminal UI default project-understanding instructions" in prepared.runtime_instructions
    assert str(project.resolve()) in prepared.runtime_instructions


def test_project_understanding_defaults_do_not_change_analysis_contract_type(tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    task = f"帮我分析{project}这个文件夹下面的情况"

    prepared = prepare_task_for_agent(task, base_workspace=tmp_path, mode="code")
    contract = build_analysis_contract(prepared.task)

    assert "project_understanding" in prepared.defaults_applied
    assert "summary" not in prepared.task.lower()
    assert contract["report_type"] == "repository_overview"


def test_prepare_modify_task_switches_workspace_without_project_readonly_defaults(tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()
    task = f"修复 {project} 里面的除零 bug"

    prepared = prepare_task_for_agent(task, base_workspace=tmp_path, mode="code")

    assert prepared.workspace == project.resolve()
    assert "mentioned_workspace" in prepared.defaults_applied
    assert "project_understanding" not in prepared.defaults_applied
    assert "Terminal UI default project-understanding instructions" not in prepared.task


def test_extract_new_workspace_path_keeps_nonexistent_leaf_for_create_task(tmp_path: Path):
    root = tmp_path / "agent_regression_workspaces"
    root.mkdir()
    project = root / "t10_greenfield_duration_cli"
    task = f"请在 {project} 生成一个新的 Python CLI 项目"

    out = extract_new_workspace_path(task, tmp_path)

    assert out == project.resolve()


def test_prepare_create_task_switches_to_nonexistent_project_path_not_parent(tmp_path: Path):
    root = tmp_path / "agent_regression_workspaces"
    root.mkdir()
    project = root / "t10_greenfield_duration_cli"
    task = f"请在{project}这个文件夹下面从0生成一个新的 Python CLI 项目"

    prepared = prepare_task_for_agent(task, base_workspace=tmp_path, mode="code")

    assert prepared.workspace == project.resolve()
    assert prepared.workspace != root.resolve()
    assert prepared.workspace_changed is True
    assert "mentioned_workspace" in prepared.defaults_applied
    assert "new or existing directory" in prepared.runtime_instructions


def test_prepare_create_task_does_not_treat_cli_value_project_date_as_workspace(tmp_path: Path):
    project = tmp_path / "agent_regression_workspaces" / "t10_greenfield_duration_cli"
    task = (
        "这是一个全新的空工作区。请从 0 创建一个完整 Python CLI 项目。"
        "CLI 支持 --input、--output-json、--group-by project/date、--round 参数。"
    )

    prepared = prepare_task_for_agent(task, base_workspace=project, mode="code")

    assert prepared.workspace == project.resolve()
    assert prepared.workspace_changed is False
    assert prepared.mentioned_workspace is None


def test_chat_auto_routes_only_when_existing_workspace_is_mentioned(tmp_path: Path):
    project = tmp_path / "repo"
    project.mkdir()

    assert should_auto_route_chat_to_code(f"分析 {project}", tmp_path) is True
    assert should_auto_route_chat_to_code("普通聊天，不涉及路径", tmp_path) is False


def test_chat_auto_routes_create_task_with_new_workspace_path(tmp_path: Path):
    root = tmp_path / "agent_regression_workspaces"
    root.mkdir()
    project = root / "t10_greenfield_duration_cli"

    assert should_auto_route_chat_to_code(f"请在 {project} 生成一个新的 Python CLI 项目", tmp_path) is True
