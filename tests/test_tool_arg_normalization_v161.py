from pathlib import Path

from coding_agent.tools.registry import execute_tool


def test_edit_file_alias_returns_schema_feedback_not_crash(tmp_path: Path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n", encoding="utf-8")
    res = execute_tool(
        str(tmp_path),
        "edit_file",
        {"file_path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
    )
    assert not res.ok
    assert res.data["tool_schema_error"] is True
    assert res.data["normalized_args"]["path"] == "a.py"
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_write_file_alias_returns_schema_feedback_not_crash(tmp_path: Path):
    res = execute_tool(str(tmp_path), "write_file", {"filename": "b.py", "text": "print(1)\n"})
    assert not res.ok
    assert res.data["tool_schema_error"] is True
    assert res.data["normalized_args"] == {"path": "b.py", "content": "print(1)\n"}
    assert not (tmp_path / "b.py").exists()


def test_missing_tool_args_are_structured_failure(tmp_path: Path):
    res = execute_tool(str(tmp_path), "edit_file", {"path": "x.py"})
    assert not res.ok
    assert "missing required tool args" in res.message
    assert res.data["missing_args"] == ["old_text", "new_text"]
