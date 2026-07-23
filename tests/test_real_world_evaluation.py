from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evaluations.real_world.runner import (
    SCHEMA_VERSION,
    _hidden_patch,
    _render_command,
    load_case,
    snapshot_paths,
)


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

    invalid = _write_case(tmp_path / "invalid.json", version="future")
    with pytest.raises(ValueError, match="unsupported case version"):
        load_case(invalid)


def test_render_command_replaces_only_declared_runtime_values(tmp_path: Path) -> None:
    command = _render_command(
        ("{python}", "check.py", "--root", "{workspace}"),
        python=Path("/runtime/python"),
        workspace=tmp_path,
    )
    assert command == ["/runtime/python", "check.py", "--root", str(tmp_path)]


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
