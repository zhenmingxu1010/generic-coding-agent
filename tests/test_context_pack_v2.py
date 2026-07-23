from __future__ import annotations

import json
from pathlib import Path

from coding_agent.memory.context_pack import build_context_pack, render_context_pack_markdown
from coding_agent.nodes.context_compress import context_compress_node
from coding_agent.workspace.repo_map import build_repository_map


def _write_demo_repo(root: Path) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "README.md").write_text("# Demo\n\nA tiny project.\n", encoding="utf-8")
    (root / "src" / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n\n"
        "def divide(a, b):\n"
        "    return a / b\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_calc.py").write_text(
        "from src.calc import add, divide\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )


def test_context_pack_selects_evidence_with_line_ranges_and_reasons(tmp_path: Path):
    _write_demo_repo(tmp_path)
    repo_map = build_repository_map(str(tmp_path))
    state = {
        "workspace": str(tmp_path),
        "task": "Fix divide behavior and keep tests passing.",
        "mode": "debug",
        "read_only": False,
        "round_idx": 2,
        "repo_map": repo_map,
        "relevant_context": {"matched_files": ["src/calc.py"]},
        "failure": {"target_file": "src/calc.py", "message": "ZeroDivisionError"},
        "traceback_issues": [{"file": "tests/test_calc.py", "message": "failure"}],
    }

    pack = build_context_pack(state, max_chars=12000)

    assert pack["version"].startswith("context_pack_v2")
    assert pack["budget"]["max_chars"] == 12000
    paths = [item["path"] for item in pack["selected_files"]]
    assert "src/calc.py" in paths
    assert "tests/test_calc.py" in paths
    calc_block = next(block for block in pack["evidence_blocks"] if block["path"] == "src/calc.py")
    assert calc_block["start_line"] == 1
    assert calc_block["end_line"] >= 4
    assert "active_failure_target" in calc_block["reason"]
    assert "4: def divide" in calc_block["content"]


def test_context_pack_markdown_is_budgeted(tmp_path: Path):
    _write_demo_repo(tmp_path)
    long_text = "\n".join(f"def f_{i}():\n    return {i}" for i in range(500))
    (tmp_path / "src" / "large.py").write_text(long_text, encoding="utf-8")
    repo_map = build_repository_map(str(tmp_path))
    state = {
        "workspace": str(tmp_path),
        "task": "Summarize large project.",
        "mode": "analyze",
        "read_only": True,
        "repo_map": repo_map,
        "relevant_context": {"matched_files": ["src/large.py", "src/calc.py"]},
    }

    pack = build_context_pack(state, max_chars=9000)
    rendered = render_context_pack_markdown(pack, max_chars=5000)

    assert len(rendered) <= 5000
    assert "# Coding Agent Context Pack v2" in rendered
    assert "## Evidence Blocks" in rendered


def test_context_compress_writes_context_pack_and_summary(tmp_path: Path):
    _write_demo_repo(tmp_path)
    run_dir = tmp_path / ".coding_agent" / "t"
    repo_map = build_repository_map(str(tmp_path))
    state = {
        "workspace": str(tmp_path),
        "thread_id": "t",
        "run_dir": str(run_dir),
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state_snapshot.json"),
        "context_pack_path": str(run_dir / "context_pack.json"),
        "context_summary_path": str(run_dir / "context_summary.md"),
        "short_term_memory_path": str(run_dir / "short_term_memory.md"),
        "task": "Fix divide behavior.",
        "mode": "debug",
        "read_only": False,
        "repo_map": repo_map,
        "relevant_context": {"matched_files": ["src/calc.py"]},
        "failure": {"target_file": "src/calc.py", "message": "division bug"},
    }

    out = context_compress_node(state)

    pack_path = Path(out["context_pack_path"])
    summary_path = Path(out["context_summary_path"])
    assert pack_path.exists()
    assert summary_path.exists()
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    assert pack["version"].startswith("context_pack_v2")
    assert pack["evidence_blocks"]
    assert "Coding Agent Context Pack v2" in summary_path.read_text(encoding="utf-8")


def test_context_pack_evidence_uses_role_diversity_not_only_repeated_results(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "experiments").mkdir()
    (tmp_path / "README.md").write_text("# Demo ML project\n\nTraining and evaluation pipeline.\n", encoding="utf-8")
    (tmp_path / "src" / "models.py").write_text(
        "import torch\n\n"
        "class TinyModel(torch.nn.Module):\n"
        "    def forward(self, x):\n"
        "        return x\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "data.py").write_text(
        "class DemoDataset:\n"
        "    def __len__(self):\n"
        "        return 1\n\n"
        "def build_dataloader():\n"
        "    return DemoDataset()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "losses.py").write_text(
        "def focal_loss(pred, target):\n"
        "    loss = pred - target\n"
        "    return loss\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "metrics.py").write_text(
        "def iou_score(pred, target):\n"
        "    return 1.0\n\n"
        "def accuracy(pred, target):\n"
        "    return 1.0\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "train.py").write_text(
        "import argparse\n\n"
        "def train():\n"
        "    optimizer = None\n"
        "    epoch = 1\n"
        "    return optimizer, epoch\n\n"
        "if __name__ == '__main__':\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--config')\n"
        "    train()\n",
        encoding="utf-8",
    )
    metrics_paths = []
    for idx in range(12):
        run_dir = tmp_path / "experiments" / f"run{idx}"
        run_dir.mkdir(parents=True)
        rel = f"experiments/run{idx}/metrics_test.json"
        metrics_paths.append(rel)
        (tmp_path / rel).write_text(
            json.dumps({
                "model": f"m{idx}",
                "loss": "focal",
                "accuracy": 0.8,
                "iou": 0.7,
                "mae": 1.2,
            }),
            encoding="utf-8",
        )
    repo_map = build_repository_map(str(tmp_path))
    state = {
        "workspace": str(tmp_path),
        "task": "Read the project deeply and compare experiment metrics.",
        "mode": "analyze",
        "read_only": True,
        "repo_map": repo_map,
        "relevant_context": {
            "matched_files": metrics_paths + [
                "src/models.py",
                "src/data.py",
                "src/losses.py",
                "src/metrics.py",
                "scripts/train.py",
                "README.md",
            ],
            "memory_matched_files": metrics_paths,
        },
    }

    pack = build_context_pack(state, max_chars=16000)

    evidence_paths = [block["path"] for block in pack["evidence_blocks"]]
    assert "src/models.py" in evidence_paths
    assert "src/data.py" in evidence_paths
    assert "src/losses.py" in evidence_paths
    assert "src/metrics.py" in evidence_paths
    repeated_metric_blocks = [path for path in evidence_paths if path.endswith("/metrics_test.json")]
    assert len(repeated_metric_blocks) <= 2
    diversity = pack["evidence_diversity"]
    assert "model_definition" in diversity["covered_roles"]
    assert "loss_definition" in diversity["covered_roles"]
    assert "data_pipeline" in diversity["covered_roles"]
    assert "metric_evaluation" in diversity["covered_roles"]


def test_context_pack_uses_repo_analysis_role_assignments_when_memory_results_dominate(tmp_path: Path):
    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "experiments").mkdir()
    (tmp_path / "README.md").write_text("# Demo ML project\n", encoding="utf-8")
    (tmp_path / "src" / "demo" / "train.py").write_text(
        "import argparse\n"
        "from .data import DemoDataset\n"
        "from .models import TinyModel\n"
        "from .losses import focal_loss\n"
        "from .metrics import iou_score\n\n"
        "def train():\n"
        "    optimizer = None\n"
        "    epoch = 1\n"
        "    return optimizer, epoch\n\n"
        "if __name__ == '__main__':\n"
        "    argparse.ArgumentParser()\n"
        "    train()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "demo" / "data.py").write_text(
        "class DemoDataset:\n"
        "    def __iter__(self):\n"
        "        yield {'image': 1, 'mask': 1}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "demo" / "models.py").write_text(
        "import torch\n\n"
        "class TinyModel(torch.nn.Module):\n"
        "    def forward(self, x):\n"
        "        return x\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "demo" / "losses.py").write_text(
        "def focal_loss(pred, target):\n"
        "    loss = pred - target\n"
        "    return loss\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "demo" / "metrics.py").write_text(
        "def iou_score(pred, target):\n"
        "    return 1.0\n",
        encoding="utf-8",
    )
    metrics_paths = []
    for idx in range(16):
        run_dir = tmp_path / "experiments" / f"run{idx}"
        run_dir.mkdir(parents=True)
        rel = f"experiments/run{idx}/metrics_test.json"
        metrics_paths.append(rel)
        (tmp_path / rel).write_text(
            json.dumps({"model": f"m{idx}", "loss": "focal", "iou": 0.7, "accuracy": 0.8}),
            encoding="utf-8",
        )
    repo_map = build_repository_map(str(tmp_path))
    state = {
        "workspace": str(tmp_path),
        "task": "Deeply read this ML project and explain data, models, losses, metrics, workflows, and results.",
        "mode": "analyze",
        "read_only": True,
        "repo_map": repo_map,
        "relevant_context": {
            "matched_files": [],
            "memory_matched_files": metrics_paths,
        },
        "repo_analysis_context": {
            "selection": {
                "selected_files": metrics_paths[:10],
                "role_assignments": {
                    "project_overview": ["README.md"],
                    "entrypoint": ["src/demo/train.py"],
                    "data_pipeline": ["src/demo/data.py"],
                    "model_definition": ["src/demo/models.py"],
                    "loss_definition": ["src/demo/losses.py"],
                    "metric_evaluation": ["src/demo/metrics.py"],
                    "results_or_outputs": metrics_paths[:8],
                },
            }
        },
    }

    pack = build_context_pack(state, max_chars=18000)

    selected_paths = [item["path"] for item in pack["selected_files"]]
    evidence_paths = [block["path"] for block in pack["evidence_blocks"]]
    for path in [
        "src/demo/data.py",
        "src/demo/models.py",
        "src/demo/losses.py",
        "src/demo/metrics.py",
    ]:
        assert path in selected_paths
        assert path in evidence_paths
    repeated_metric_blocks = [path for path in evidence_paths if path.endswith("/metrics_test.json")]
    assert len(repeated_metric_blocks) <= 2


def test_context_pack_deprioritizes_prior_agent_artifacts_for_source_roles(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "metrics.py").write_text(
        "def iou_score(pred, target):\n"
        "    return 1.0\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "generated_metrics_probe.py").write_text(
        "def iou_score(pred, target):\n"
        "    return 0.0\n",
        encoding="utf-8",
    )
    repo_map = build_repository_map(str(tmp_path))
    state = {
        "workspace": str(tmp_path),
        "task": "Analyze project metric implementation.",
        "mode": "analyze",
        "read_only": True,
        "repo_map": repo_map,
        "artifact_provenance": {
            "artifacts": {
                "scripts/generated_metrics_probe.py": {
                    "origin": "agent_generated",
                    "safe_to_modify_by_future_agent": True,
                }
            }
        },
        "relevant_context": {
            "matched_files": [],
            "memory_matched_files": ["scripts/generated_metrics_probe.py"],
        },
        "repo_analysis_context": {
            "selection": {
                "role_assignments": {
                    "metric_evaluation": ["src/metrics.py", "scripts/generated_metrics_probe.py"]
                }
            }
        },
    }

    pack = build_context_pack(state, max_chars=10000)

    metric_block = next(
        block
        for block in pack["evidence_blocks"]
        if "role_representative:metric_evaluation" in block.get("evidence_selection", [])
    )
    assert metric_block["path"] == "src/metrics.py"


def test_context_pack_prefers_role_specific_source_over_broad_or_importing_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "experiments" / "run0").mkdir(parents=True)
    (tmp_path / "src" / "train.py").write_text(
        "import argparse\n"
        "from models import TinyModel\n"
        "from losses import FocalLoss\n"
        "from metrics import MetricAccumulator\n\n"
        "def evaluate():\n"
        "    metric = MetricAccumulator()\n"
        "    return metric\n\n"
        "def train():\n"
        "    optimizer = None\n"
        "    epoch = 1\n"
        "    return optimizer, epoch\n\n"
        "if __name__ == '__main__':\n"
        "    argparse.ArgumentParser()\n"
        "    train()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "models.py").write_text(
        "import torch\n\n"
        "class TinyModel(torch.nn.Module):\n"
        "    def forward(self, x):\n"
        "        return x\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "losses.py").write_text(
        "class FocalLoss:\n"
        "    def forward(self, pred, target):\n"
        "        return pred - target\n\n"
        "def build_loss(name):\n"
        "    return FocalLoss()\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "metrics.py").write_text(
        "from losses import build_loss\n\n"
        "class MetricAccumulator:\n"
        "    def update(self, pred, target):\n"
        "        return self.compute(pred, target)\n\n"
        "def iou_score(pred, target):\n"
        "    return 1.0\n\n"
        "def compute(pred, target):\n"
        "    return iou_score(pred, target)\n",
        encoding="utf-8",
    )
    (tmp_path / "experiments" / "run0" / "metrics_test.json").write_text(
        json.dumps({"iou": 0.7, "loss": 0.2, "accuracy": 0.8}),
        encoding="utf-8",
    )
    repo_map = build_repository_map(str(tmp_path))
    state = {
        "workspace": str(tmp_path),
        "task": "Analyze model, loss, metric, and result files.",
        "mode": "analyze",
        "read_only": True,
        "repo_map": repo_map,
        "relevant_context": {
            "matched_files": [],
            "memory_matched_files": ["experiments/run0/metrics_test.json", "src/train.py"],
        },
        "repo_analysis_context": {
            "selection": {
                "selected_files": ["experiments/run0/metrics_test.json"],
                "role_assignments": {
                    "entrypoint": ["src/train.py"],
                    "model_definition": ["src/models.py", "src/train.py"],
                    "loss_definition": ["src/losses.py", "src/metrics.py", "src/train.py"],
                    "metric_evaluation": ["src/metrics.py", "experiments/run0/metrics_test.json"],
                    "results_or_outputs": ["experiments/run0/metrics_test.json"],
                },
            }
        },
    }

    pack = build_context_pack(state, max_chars=14000)

    by_selection = {
        selection: block["path"]
        for block in pack["evidence_blocks"]
        for selection in block.get("evidence_selection", [])
        if selection.startswith("role_representative:")
    }
    assert by_selection["role_representative:model_definition"] == "src/models.py"
    assert by_selection["role_representative:loss_definition"] == "src/losses.py"
    assert by_selection["role_representative:metric_evaluation"] == "src/metrics.py"
