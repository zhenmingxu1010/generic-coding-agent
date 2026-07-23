from pathlib import Path
import pytest

from coding_agent.safety.path_guard import PathGuard


def test_path_guard_blocks_escape(tmp_path):
    guard = PathGuard(tmp_path)
    with pytest.raises(ValueError):
        guard.resolve("../outside.txt")


def test_path_guard_allows_relative(tmp_path):
    guard = PathGuard(tmp_path)
    p = guard.resolve("a/b.txt")
    assert p == (tmp_path / "a" / "b.txt").resolve()
