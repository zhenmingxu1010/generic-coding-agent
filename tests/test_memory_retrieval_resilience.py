from __future__ import annotations

import json
from pathlib import Path

from coding_agent.memory.project_memory import (
    load_project_memory,
    memory_paths,
    update_project_memory_from_repo,
)
from coding_agent.memory.reflexion_store import append_reflexion, load_recent_reflexions
from coding_agent.memory.retrieval_store import retrieve_by_task_terms


def test_corrupt_project_memory_recovers_and_can_be_rewritten(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGENT_RUNS_DIR", str(tmp_path / "runs"))
    profile_path = memory_paths(workspace)["profile_json"]
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text('{"broken":', encoding="utf-8")

    recovered = load_project_memory(workspace)

    assert recovered["version"] == "v1.13"
    assert recovered["file_count"] == 0

    update_project_memory_from_repo(
        {"workspace": str(workspace), "repo_map": {"files": [], "py_files": []}}
    )
    rewritten = json.loads(profile_path.read_text(encoding="utf-8"))
    assert rewritten["version"] == "v1.13"


def test_reflexion_reader_skips_corrupt_lines(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AGENT_RUNS_DIR", str(tmp_path / "runs"))
    path = append_reflexion(workspace, {"lesson": "inspect evidence first"})
    with path.open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")
    append_reflexion(workspace, {"lesson": "verify after repair"})

    rows = load_recent_reflexions(workspace, limit=8)

    assert [row["lesson"] for row in rows] == [
        "inspect evidence first",
        "verify after repair",
    ]


def test_retrieval_prioritizes_code_identifiers_in_long_prompts(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "loader.py").write_text(
        "def parse_config(value):\n    return value\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("configuration guide\n", encoding="utf-8")
    task = (
        "Please carefully investigate the existing behavior because several users "
        "consistently report that parse_config returns the wrong value."
    )

    result = retrieve_by_task_terms(str(tmp_path), task, max_files=2)

    assert result["matched_files"][0] == "src/loader.py"
