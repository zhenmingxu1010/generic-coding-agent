from pathlib import Path

import pytest

from coding_agent.workspace.repo_map import build_repository_map, coverage_check, heuristic_select_evidence, add_targeted_retrieval


def test_repository_map_detects_roles_without_fixed_names(tmp_path: Path):
    # Deliberately avoid scripts/, src/, train.py naming conventions.
    (tmp_path / "docs").mkdir()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "outputs").mkdir()
    (tmp_path / "docs" / "overview.md").write_text("This project trains a cloud mask model and evaluates IoU.", encoding="utf-8")
    (tmp_path / "bin" / "launch_experiment.py").write_text(
        """
import argparse
from pkg.dataflow import CloudDataset
from pkg.netdef import Net

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=1)
    args = parser.parse_args()
    for epoch in range(args.epochs):
        pass

if __name__ == '__main__':
    main()
""",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "dataflow.py").write_text("class CloudDataset: pass\ndef make_loader(): pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "netdef.py").write_text("import torch\nclass Net(torch.nn.Module): pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "objective.py").write_text("class BoundaryLoss: pass\ndef compute_loss(): pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "scoreboard.py").write_text("def iou(): pass\ndef thickness_mae(): pass\n", encoding="utf-8")
    (tmp_path / "outputs" / "run_summary.json").write_text('{"iou": 0.5, "mae": 1.0}', encoding="utf-8")

    repo_map = build_repository_map(str(tmp_path))
    by_role = repo_map["candidates_by_role"]
    assert by_role["entrypoint"], by_role
    assert by_role["data_pipeline"], by_role
    assert by_role["model_definition"], by_role
    assert by_role["loss_definition"], by_role
    assert by_role["metric_evaluation"], by_role
    assert by_role["results_or_outputs"], by_role


def test_targeted_retrieval_improves_missing_roles(tmp_path: Path):
    (tmp_path / "a.py").write_text("import argparse\nif __name__ == '__main__': pass", encoding="utf-8")
    (tmp_path / "b.py").write_text("class MyDataset: pass", encoding="utf-8")
    (tmp_path / "c.py").write_text("class MyLoss: pass", encoding="utf-8")
    (tmp_path / "d.json").write_text('{"metric": 1}', encoding="utf-8")
    repo_map = build_repository_map(str(tmp_path))
    selection = {"selected_files": ["a.py"], "role_assignments": {"entrypoint": ["a.py"]}}
    before = coverage_check(selection, repo_map)
    after_sel = add_targeted_retrieval(selection, repo_map, max_files=8)
    after = coverage_check(after_sel, repo_map)
    assert after["coverage_ratio"] >= before["coverage_ratio"]
    assert len(after_sel["selected_files"]) >= 1


def test_repository_map_skips_file_symlink_outside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.py"
    workspace.mkdir()
    outside.write_text("EXTERNAL_SECRET = 'do-not-read'\n", encoding="utf-8")
    link = workspace / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    repo_map = build_repository_map(str(workspace))

    assert "linked.py" not in repo_map["files"]
    assert all(record["path"] != "linked.py" for record in repo_map["records"])
