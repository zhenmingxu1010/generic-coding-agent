from pathlib import Path
import pytest

from coding_agent.safety.path_guard import PathGuard, is_within_workspace


def test_path_guard_blocks_escape(tmp_path):
    guard = PathGuard(tmp_path)
    with pytest.raises(ValueError):
        guard.resolve("../outside.txt")


def test_path_guard_allows_relative(tmp_path):
    guard = PathGuard(tmp_path)
    p = guard.resolve("a/b.txt")
    assert p == (tmp_path / "a" / "b.txt").resolve()


def test_workspace_boundary_does_not_confuse_sibling_prefix(tmp_path):
    workspace = tmp_path / "repo"
    sibling = tmp_path / "repo-copy" / "secret.txt"
    workspace.mkdir()
    sibling.parent.mkdir()
    sibling.write_text("secret", encoding="utf-8")

    assert is_within_workspace(workspace, workspace / "inside.txt") is True
    assert is_within_workspace(workspace, sibling) is False
