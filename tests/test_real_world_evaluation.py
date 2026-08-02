from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from evaluations.real_world.collect_results import (
    SUMMARY_VERSION,
    collect_results,
    load_case_metadata,
)
from evaluations.real_world.runner import (
    LEGACY_SCHEMA_VERSION,
    RESULT_VERSION,
    SCHEMA_VERSION,
    _external_acceptance_passed,
    _hidden_patch,
    _render_command,
    _target_environment,
    load_case,
    snapshot_paths,
)

ROOT = Path(__file__).resolve().parents[1]


def _write_case(path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "case_id": "sample-case",
        "project": "sample",
        "repository_url": "https://example.invalid/sample.git",
        "buggy_commit": "a" * 40,
        "fixed_commit": "b" * 40,
        "task": "Fix the reported behavior without changing tests.",
        "hidden_test_paths": ["tests/test_bug.py"],
        "hidden_files": [],
        "test_command": ["{python}", "-m", "pytest", "-q"],
        "protected_globs": ["tests/**"],
        "timeout_seconds": 30,
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_case_validates_schema(tmp_path: Path) -> None:
    case = load_case(_write_case(tmp_path / "case.json"))
    assert case.case_id == "sample-case"
    assert case.hidden_test_paths == ("tests/test_bug.py",)
    assert case.timeout_seconds == 30
    assert case.categories == ("repair",)
    assert case.expected_change_shape == "either"
    assert case.test_environment == ()
    assert case.protected_ignore_globs == ()

    legacy = load_case(
        _write_case(tmp_path / "legacy.json", version=LEGACY_SCHEMA_VERSION)
    )
    assert legacy.categories == ("repair",)

    invalid = _write_case(tmp_path / "invalid.json", version="future")
    with pytest.raises(ValueError, match="unsupported case version"):
        load_case(invalid)

    invalid_shape = _write_case(
        tmp_path / "invalid-shape.json", expected_change_shape="repository_wide"
    )
    with pytest.raises(ValueError, match="expected_change_shape"):
        load_case(invalid_shape)

    protected_environment = _write_case(
        tmp_path / "protected-environment.json",
        test_environment={"AGENT_LLM_API_KEY": "not-allowed"},
    )
    with pytest.raises(ValueError, match="protected name"):
        load_case(protected_environment)


def test_checked_in_real_world_cases_use_valid_current_schema() -> None:
    case_paths = sorted((ROOT / "evaluations" / "real_world" / "cases").glob("*.json"))

    assert len(case_paths) >= 4
    cases = [load_case(path) for path in case_paths]
    assert all(case.categories for case in cases)
    assert any(case.expected_change_shape == "multi_file" for case in cases)


def test_render_command_replaces_only_declared_runtime_values(tmp_path: Path) -> None:
    command = _render_command(
        ("{python}", "check.py", "--root", "{workspace}"),
        python=Path("/runtime/python"),
        workspace=tmp_path,
    )
    assert command == ["/runtime/python", "check.py", "--root", str(tmp_path)]


def test_target_environment_activates_test_interpreter_and_isolates_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    spec = load_case(
        _write_case(
            tmp_path / "case.json",
            test_environment={"DISABLE_NETWORK_TESTS": "1"},
        )
    )
    python = tmp_path / "venv" / "bin" / "python"
    home = tmp_path / "home"

    env = _target_environment(spec, test_python=python, isolated_home=home)

    assert env["HOME"] == str(home)
    assert env["PATH"].split(os.pathsep)[0] == str(python.parent)
    assert env["VIRTUAL_ENV"] == str(python.parent.parent)
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["DISABLE_NETWORK_TESTS"] == "1"
    assert home.is_dir()


def test_snapshot_paths_detects_protected_test_mutation(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    target = tests / "test_behavior.py"
    target.write_text("assert True\n", encoding="utf-8")
    before = snapshot_paths(tmp_path, ("tests/**",))
    target.write_text("assert False\n", encoding="utf-8")
    after = snapshot_paths(tmp_path, ("tests/**",))
    assert before.keys() == after.keys()
    assert before["tests/test_behavior.py"] != after["tests/test_behavior.py"]


def test_snapshot_paths_ignores_generated_python_caches(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    cache = tests / "__pycache__"
    cache.mkdir(parents=True)
    (tests / "test_behavior.py").write_text("assert True\n", encoding="utf-8")
    (cache / "test_behavior.cpython-312.pyc").write_bytes(b"generated")
    snapshot = snapshot_paths(tmp_path, ("tests/**",))
    assert set(snapshot) == {"tests/test_behavior.py"}


def test_snapshot_paths_ignores_declared_runtime_artifacts_only(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    replay = tests / "test-replay"
    replay.mkdir(parents=True)
    (tests / "test_behavior.py").write_text("assert True\n", encoding="utf-8")
    (replay / "generated.json").write_text("{}\n", encoding="utf-8")

    snapshot = snapshot_paths(
        tmp_path,
        ("tests/**",),
        ("tests/test-replay/*.json",),
    )

    assert set(snapshot) == {"tests/test_behavior.py"}


def test_hidden_patch_contains_only_configured_test_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "src.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_bug.py").write_text("assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Eval", "-c", "user.email=eval@example.invalid", "commit", "-qm", "buggy"],
        cwd=repo,
        check=True,
    )
    buggy = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "src.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tests / "test_bug.py").write_text("assert VALUE == 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Eval", "-c", "user.email=eval@example.invalid", "commit", "-qm", "fixed"],
        cwd=repo,
        check=True,
    )
    fixed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    spec = load_case(
        _write_case(tmp_path / "case.json", buggy_commit=buggy, fixed_commit=fixed)
    )
    patch = _hidden_patch(spec, repo).decode("utf-8")
    assert "tests/test_bug.py" in patch
    assert "src.py" not in patch


def test_external_acceptance_is_independent_of_agent_success_claim() -> None:
    common = {
        "protected_mutations": [],
        "hidden_patch": {"returncode": 0},
        "hidden_test": {"returncode": 0},
    }
    assert _external_acceptance_passed(
        agent_process={"returncode": 1, "timed_out": False}, **common
    )
    assert not _external_acceptance_passed(
        agent_process={"returncode": None, "timed_out": True}, **common
    )
    assert not _external_acceptance_passed(
        agent_process={"returncode": 0, "timed_out": False},
        protected_mutations=["tests/test_bug.py"],
        hidden_patch={"returncode": 0},
        hidden_test={"returncode": 0},
    )


def _write_result(
    path: Path,
    *,
    case_id: str,
    claimed: bool,
    accepted: bool,
    reachable: bool = True,
    changed: list[str] | None = None,
) -> Path:
    data = {
        "version": RESULT_VERSION,
        "case": {
            "case_id": case_id,
            "project": "sample",
            "categories": ["repair", "multi-file"],
            "expected_change_shape": "multi_file",
        },
        "status": (
            "environment_unreachable"
            if not reachable
            else ("resolved" if accepted else "unresolved")
        ),
        "preflight": {"reachable": reachable},
        "agent": {
            "process": {
                "returncode": 0 if claimed else 1,
                "timed_out": False,
                "duration_seconds": 10.0,
            },
            "final": {
                "ok": claimed,
                "stopped_reason": "verified_ok" if claimed else "verification_failed",
                "token_usage": {
                    "totals": {
                        "calls": 2,
                        "prompt_tokens": 80,
                        "completion_tokens": 20,
                        "total_tokens": 100,
                    }
                },
            },
            "changed_paths_before_hidden_tests": changed or ["src/a.py", "src/b.py"],
            "protected_mutations": [],
        },
        "acceptance": {
            "passed": accepted,
            "hidden_patch": {"returncode": 0},
            "hidden_test": {"returncode": 0 if accepted else 1},
        },
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_collect_results_reports_final_gate_alignment_and_efficiency(
    tmp_path: Path,
) -> None:
    paths = [
        _write_result(tmp_path / "tp.json", case_id="tp", claimed=True, accepted=True),
        _write_result(tmp_path / "fp.json", case_id="fp", claimed=True, accepted=False),
        _write_result(tmp_path / "fn.json", case_id="fn", claimed=False, accepted=True),
        _write_result(
            tmp_path / "skip.json",
            case_id="skip",
            claimed=False,
            accepted=False,
            reachable=False,
        ),
    ]

    summary = collect_results(paths)

    assert summary["version"] == SUMMARY_VERSION
    assert summary["total"] == 4
    assert summary["eligible"] == 3
    assert summary["external_acceptance_passed"] == 2
    assert summary["resolution_rate"] == pytest.approx(2 / 3, abs=0.0001)
    assert summary["final_gate"] == {
        "true_positive": 1,
        "false_positive": 1,
        "false_negative": 1,
        "true_negative": 0,
        "success_claim_precision": 0.5,
        "success_claim_recall": 0.5,
        "false_positive_claim_rate": 0.5,
    }
    assert summary["total_token_usage"]["total_tokens"] == 300
    assert summary["total_token_usage"]["calls"] == 6
    assert summary["average_total_tokens"] == 100
    assert summary["average_changed_project_files"] == 2
    assert summary["multi_file_change_cases"] == 3
    assert summary["categories"]["multi-file"]["resolution_rate"] == pytest.approx(
        2 / 3, abs=0.0001
    )
    assert summary["cases"][3]["final_gate_alignment"] == "not_evaluated"


def test_collect_results_excludes_agent_internal_changes(tmp_path: Path) -> None:
    result = _write_result(
        tmp_path / "result.json",
        case_id="internal-filter",
        claimed=True,
        accepted=True,
        changed=[
            ".coding_agent_test/thread/test_generated.py",
            ".pytest_cache/v/cache/nodeids",
            "src/real_change.py",
        ],
    )

    summary = collect_results([result])

    case = summary["cases"][0]
    assert case["changed_project_paths"] == ["src/real_change.py"]
    assert case["changed_project_file_count"] == 1
    assert case["multi_file_change"] is False


def test_collect_results_excludes_declared_project_runtime_artifacts(
    tmp_path: Path,
) -> None:
    result = _write_result(
        tmp_path / "result.json",
        case_id="runtime-filter",
        claimed=True,
        accepted=True,
        changed=[
            "tests/runtime/generated.json",
            "src/real_change.py",
        ],
    )
    data = json.loads(result.read_text(encoding="utf-8"))
    data["case"]["protected_ignore_globs"] = ["tests/runtime/*.json"]
    result.write_text(json.dumps(data), encoding="utf-8")

    summary = collect_results([result])

    assert summary["cases"][0]["changed_project_paths"] == ["src/real_change.py"]


def test_case_metadata_enriches_legacy_results(tmp_path: Path) -> None:
    result = _write_result(
        tmp_path / "result.json",
        case_id="legacy-case",
        claimed=True,
        accepted=True,
    )
    result_data = json.loads(result.read_text(encoding="utf-8"))
    result_data["case"].pop("categories")
    result_data["case"].pop("expected_change_shape")
    result.write_text(json.dumps(result_data), encoding="utf-8")
    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    (case_dir / "legacy-case.json").write_text(
        json.dumps(
            {
                "case_id": "legacy-case",
                "categories": ["repair", "api-migration"],
                "expected_change_shape": "multi_file",
            }
        ),
        encoding="utf-8",
    )

    summary = collect_results(
        [result], case_metadata=load_case_metadata(case_dir)
    )

    case = summary["cases"][0]
    assert case["categories"] == ["api-migration", "repair"]
    assert case["expected_change_shape"] == "multi_file"
