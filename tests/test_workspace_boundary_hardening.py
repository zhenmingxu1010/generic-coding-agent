from pathlib import Path

import pytest

from coding_agent.contracts.contract import scan_workspace_contract
from coding_agent.memory.workspace_baseline import ensure_workspace_baseline
from coding_agent.workspace.interface_check import run_interface_consistency_check


def _external_python_symlink(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("EXTERNAL_SECRET = 'do-not-read'\n", encoding="utf-8")
    link = workspace / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    return workspace, link


def test_contract_and_interface_scans_skip_external_file_symlink(tmp_path: Path):
    workspace, link = _external_python_symlink(tmp_path)

    contract_scan = scan_workspace_contract(str(workspace))
    interface_scan = run_interface_consistency_check(str(workspace), {})

    assert link.name not in contract_scan["py_files"]
    assert link.name not in interface_scan["checked_modules"]
    assert link.name not in interface_scan["checked_tests"]


def test_workspace_baseline_skips_external_file_symlink(tmp_path: Path, monkeypatch):
    workspace, link = _external_python_symlink(tmp_path)
    monkeypatch.setenv("AGENT_RUNS_DIR", str(tmp_path / "agent-runs"))

    baseline = ensure_workspace_baseline(workspace)

    assert link.name not in baseline["files"]
