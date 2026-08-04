from pathlib import Path

import pytest

from coding_agent.tools.file_tools import filter_files, read_many_files, search_text, write_file


def test_filter_files_matches_paths(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "scripts" / "train.sh").write_text("echo train", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "train.py").write_text("print('x')", encoding="utf-8")
    res = filter_files(str(tmp_path), regex=r"^(scripts|src)/")
    assert res.ok
    assert "scripts/train.sh" in res.data["matches"]
    assert "src/pkg/train.py" in res.data["matches"]


def test_read_many_files_limits(tmp_path: Path):
    (tmp_path / "a.py").write_text("a" * 100, encoding="utf-8")
    (tmp_path / "b.py").write_text("b" * 100, encoding="utf-8")
    res = read_many_files(str(tmp_path), ["a.py", "b.py"], per_file_chars=10, max_total_chars=200)
    assert res.ok
    assert len(res.data["files"]) == 2
    assert res.data["total_chars"] <= 120


def test_write_file_creates_missing_parent_directories(tmp_path: Path):
    res = write_file(str(tmp_path), "reports/nested/out.md", "# ok\n")

    assert res.ok
    assert (tmp_path / "reports" / "nested" / "out.md").read_text(encoding="utf-8") == "# ok\n"


def test_search_and_filter_skip_file_symlink_that_escapes_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    workspace.mkdir()
    outside.write_text("EXTERNAL_SECRET_TOKEN", encoding="utf-8")
    link = workspace / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    search = search_text(str(workspace), "EXTERNAL_SECRET_TOKEN")
    filtered = filter_files(str(workspace), glob="*.txt")

    assert search.ok
    assert search.data["matches"] == []
    assert "linked.txt" not in filtered.data["matches"]
