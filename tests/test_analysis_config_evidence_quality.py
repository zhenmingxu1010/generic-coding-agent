from coding_agent.nodes.analyze_report import _context_pack_evidence_quality


def _context_pack():
    return {
        "version": "context_pack_v2.1",
        "evidence_blocks": [
            {
                "path": "src/pkg/train.py",
                "ok": True,
                "evidence_selection": ["role_representative:entrypoint"],
                "symbols": {},
                "content": "1: def main(): pass",
            },
            {
                "path": "experiments/run42/config.json",
                "ok": True,
                "evidence_selection": ["role_representative:config_or_arguments"],
                "symbols": {},
                "content": (
                    '1: {"data_root": "/data", "stats_path": "stats.json", '
                    '"model": "resnet", "loss": "focal", "batch_size": 8, "seed": 1}'
                ),
            },
        ],
    }


def test_config_evidence_accepts_glob_path_and_structured_fields():
    report = """
src/pkg/train.py is the training entrypoint.
Configuration files are stored under experiments/*/config.json and include
data_root, stats_path, model, loss, batch_size, and seed.
"""

    quality = _context_pack_evidence_quality(report, _context_pack())

    assert quality["ok"] is True
    config_item = next(item for item in quality["path_required"] if item["role"] == "config_or_arguments")
    assert config_item["satisfied_by_path_pattern"] is True
    assert config_item["path_pattern_hits"] == ["experiments/*/config.json"]


def test_config_evidence_accepts_structured_fields_without_exact_path():
    report = """
src/pkg/train.py is the training entrypoint.
The configuration schema includes data_root, stats_path, model, loss,
batch_size, and seed.
"""

    quality = _context_pack_evidence_quality(report, _context_pack())

    assert quality["ok"] is True
    config_item = next(item for item in quality["path_required"] if item["role"] == "config_or_arguments")
    assert config_item["satisfied_by_structured_fields"] is True
    assert config_item["structured_field_hits"][:3] == ["data_root", "stats_path", "model"]
