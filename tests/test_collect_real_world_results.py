from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluations.real_world.collect_results import collect_results
from evaluations.real_world.runner import RESULT_VERSION


def _result(path: Path, *, status: str = "resolved", final_ok: bool = True) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": RESULT_VERSION,
                "case": {"case_id": path.stem, "project": "sample"},
                "status": status,
                "preflight": {"reachable": True},
                "agent": {
                    "final": {"ok": final_ok, "stopped_reason": "verified_ok"},
                    "process": {"duration_seconds": 2.5},
                    "changed_paths_before_hidden_tests": [
                        ".coding_agent_test/thread/probe.py",
                        "src/fix.py",
                    ],
                    "protected_mutations": [],
                },
                "acceptance": {"hidden_test": {"returncode": 0}},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_collect_results_is_sanitized_and_counts_outcomes(tmp_path: Path) -> None:
    summary = collect_results(
        [_result(tmp_path / "one.json"), _result(tmp_path / "two.json")]
    )
    assert summary["total"] == 2
    assert summary["resolved"] == 2
    assert summary["agent_reported_ok"] == 2
    assert summary["hidden_tests_passed"] == 2
    assert summary["protected_mutation_cases"] == 0
    assert summary["average_agent_duration_seconds"] == 2.5
    assert summary["cases"][0]["changed_project_paths"] == ["src/fix.py"]
    assert "source_repo" not in summary["cases"][0]


def test_collect_results_rejects_unknown_version(tmp_path: Path) -> None:
    path = _result(tmp_path / "bad.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = "unknown"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported result version"):
        collect_results([path])
