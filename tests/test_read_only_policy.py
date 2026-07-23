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
