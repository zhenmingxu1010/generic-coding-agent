from pathlib import Path

from coding_agent.nodes.generate_files import _generated_sibling_context


def test_generated_sibling_context_uses_successfully_written_files(tmp_path: Path):
    (tmp_path / "tool.py").write_text(
        "def public_value():\n    return 7\n",
        encoding="utf-8",
    )
    (tmp_path / "broken.py").write_text("raise RuntimeError\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "generated_files": [
            {"path": "tool.py", "kind": "code", "ok": True},
            {"path": "broken.py", "kind": "code", "ok": False},
        ],
    }

    context = _generated_sibling_context(state, "tests/test_tool.py")

    assert "===== tool.py (kind=code) =====" in context
    assert "def public_value" in context
    assert "broken.py" not in context


def test_generated_sibling_context_respects_total_budget(tmp_path: Path):
    (tmp_path / "large.txt").write_text("x" * 1000, encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "generated_files": [{"path": "large.txt", "kind": "document", "ok": True}],
    }

    context = _generated_sibling_context(state, "next.txt", max_chars=120)

    assert len(context) <= 120
