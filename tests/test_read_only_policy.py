from pathlib import Path

from coding_agent.tools.registry import execute_tool
from coding_agent.safety.command_guard import CommandGuard
import pytest


def test_read_only_blocks_write_file(tmp_path: Path):
    res = execute_tool(str(tmp_path), "write_file", {"path": "x.txt", "content": "x"}, read_only=True)
    assert not res.ok
    assert res.data["blocked_by_policy"] is True
    assert not (tmp_path / "x.txt").exists()


def test_read_only_command_guard_blocks_mutation():
    with pytest.raises(ValueError):
        CommandGuard(read_only=True).check(["mkdir", "abc"])


def test_read_only_registry_blocks_shell_even_when_command_is_allowlisted(tmp_path: Path):
    res = execute_tool(str(tmp_path), "run_shell", {"command": ["ls"]}, read_only=True)

    assert not res.ok
    assert res.data["blocked_by_policy"] is True
    assert res.data["read_only_execution_blocked"] is True


def test_read_only_registry_blocks_tests_before_project_code_runs(tmp_path: Path):
    test_file = tmp_path / "test_side_effect.py"
    marker = tmp_path / "marker.txt"
    test_file.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('changed', encoding='utf-8')\n"
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )

    res = execute_tool(
        str(tmp_path),
        "run_tests",
        {"targets": ["test_side_effect.py"]},
        read_only=True,
    )

    assert not res.ok
    assert res.data["read_only_execution_blocked"] is True
    assert not marker.exists()


def test_explicit_verify_only_override_can_execute_without_enabling_write_tools(tmp_path: Path):
    shell = execute_tool(
        str(tmp_path),
        "run_shell",
        {"command": ["ls"]},
        read_only=True,
        allow_read_only_execution=True,
    )
    write = execute_tool(
        str(tmp_path),
        "write_file",
        {"path": "blocked.txt", "content": "x"},
        read_only=True,
        allow_read_only_execution=True,
    )

    assert shell.ok
    assert not write.ok
    assert write.data["blocked_by_policy"] is True
    assert not (tmp_path / "blocked.txt").exists()
