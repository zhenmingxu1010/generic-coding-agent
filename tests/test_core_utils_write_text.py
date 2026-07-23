from pathlib import Path

from coding_agent.core.utils import write_text_file


def test_write_text_file_creates_missing_parent_directories(tmp_path: Path):
    target = tmp_path / "reports" / "nested" / "result.md"

    write_text_file(target, "# result\n")

    assert target.read_text(encoding="utf-8") == "# result\n"
