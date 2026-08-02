from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "real_world_case_v2"
LEGACY_SCHEMA_VERSION = "real_world_case_v1"
RESULT_VERSION = "real_world_result_v2"
LEGACY_RESULT_VERSION = "real_world_result_v1"
EXPECTED_CHANGE_SHAPES = {"localized", "multi_file", "either"}
ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
PROTECTED_ENVIRONMENT_NAMES = {
    "HOME",
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
}
INTERNAL_CHANGE_PREFIXES = (
    ".coding_agent/",
    ".coding_agent_test/",
    ".pytest_cache/",
)


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    project: str
    repository_url: str
    buggy_commit: str
    fixed_commit: str
    task: str
    hidden_test_paths: tuple[str, ...]
    hidden_files: tuple[tuple[str, str], ...]
    test_command: tuple[str, ...]
    protected_globs: tuple[str, ...] = ("tests/**",)
    timeout_seconds: int = 120
    categories: tuple[str, ...] = ("repair",)
    expected_change_shape: str = "either"
    test_environment: tuple[tuple[str, str], ...] = ()
    protected_ignore_globs: tuple[str, ...] = ()


def _required_text(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _string_tuple(data: dict[str, Any], name: str, *, required: bool = True) -> tuple[str, ...]:
    value = data.get(name)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be a non-empty list of strings")
    return tuple(value)


def load_case(path: str | Path) -> CaseSpec:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version") not in {SCHEMA_VERSION, LEGACY_SCHEMA_VERSION}:
        raise ValueError(f"unsupported case version: {data.get('version')!r}")
    timeout = data.get("timeout_seconds", 120)
    if not isinstance(timeout, int) or timeout < 1:
        raise ValueError("timeout_seconds must be a positive integer")
    hidden_files_raw = data.get("hidden_files") or []
    if not isinstance(hidden_files_raw, list):
        raise ValueError("hidden_files must be a list")
    hidden_files: list[tuple[str, str]] = []
    for item in hidden_files_raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not item.get("path")
            or not isinstance(item.get("content"), str)
        ):
            raise ValueError("each hidden_files item requires string path and content")
        hidden_files.append((item["path"], item["content"]))
    categories = _string_tuple(data, "categories", required=False) or ("repair",)
    expected_change_shape = data.get("expected_change_shape", "either")
    if expected_change_shape not in EXPECTED_CHANGE_SHAPES:
        allowed = ", ".join(sorted(EXPECTED_CHANGE_SHAPES))
        raise ValueError(f"expected_change_shape must be one of: {allowed}")
    test_environment_raw = data.get("test_environment") or {}
    if not isinstance(test_environment_raw, dict):
        raise ValueError("test_environment must be an object")
    test_environment: list[tuple[str, str]] = []
    for name, value in test_environment_raw.items():
        if (
            not isinstance(name, str)
            or not ENVIRONMENT_NAME.fullmatch(name)
            or name in PROTECTED_ENVIRONMENT_NAMES
            or name.startswith("AGENT_")
        ):
            raise ValueError(f"test_environment contains protected name: {name!r}")
        if not isinstance(value, str):
            raise ValueError(f"test_environment value must be a string: {name}")
        test_environment.append((name, value))
    return CaseSpec(
        case_id=_required_text(data, "case_id"),
        project=_required_text(data, "project"),
        repository_url=_required_text(data, "repository_url"),
        buggy_commit=_required_text(data, "buggy_commit"),
        fixed_commit=_required_text(data, "fixed_commit"),
        task=_required_text(data, "task"),
        hidden_test_paths=_string_tuple(data, "hidden_test_paths"),
        hidden_files=tuple(hidden_files),
        test_command=_string_tuple(data, "test_command"),
        protected_globs=_string_tuple(data, "protected_globs", required=False) or ("tests/**",),
        timeout_seconds=timeout,
        categories=categories,
        expected_change_shape=expected_change_shape,
        test_environment=tuple(sorted(test_environment)),
        protected_ignore_globs=_string_tuple(
            data, "protected_ignore_globs", required=False
        ),
    )


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "command": command,
            "returncode": proc.returncode,
            "timed_out": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": proc.stdout.decode("utf-8", errors="replace")[-12000:],
            "stderr": proc.stderr.decode("utf-8", errors="replace")[-12000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": None,
            "timed_out": True,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": (exc.stdout or b"").decode("utf-8", errors="replace")[-12000:],
            "stderr": (exc.stderr or b"").decode("utf-8", errors="replace")[-12000:],
        }


def _must_run(command: list[str], *, cwd: Path, timeout: int = 120) -> dict[str, Any]:
    result = _run(command, cwd=cwd, timeout=timeout)
    if result["returncode"] != 0:
        raise RuntimeError(
            f"command failed: {command!r}\n{result['stdout']}\n{result['stderr']}"
        )
    return result


def _prepare_checkout(source_repo: Path, destination: Path, commit: str) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _must_run(
        ["git", "clone", "--local", "--no-hardlinks", "--quiet", str(source_repo), str(destination)],
        cwd=destination.parent,
    )
    _must_run(["git", "checkout", "--quiet", "--detach", commit], cwd=destination)


def _hidden_patch(spec: CaseSpec, source_repo: Path) -> bytes:
    command = [
        "git",
        "diff",
        "--binary",
        spec.buggy_commit,
        spec.fixed_commit,
        "--",
        *spec.hidden_test_paths,
    ]
    proc = subprocess.run(command, cwd=str(source_repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
    if not proc.stdout.strip():
        raise ValueError("the selected commits do not contain a hidden-test change")
    return proc.stdout


def _apply_patch(workspace: Path, patch: bytes, timeout: int) -> dict[str, Any]:
    return _run(["git", "apply", "--whitespace=nowarn", "-"], cwd=workspace, timeout=timeout, stdin=patch)


def _install_hidden_files(workspace: Path, files: Iterable[tuple[str, str]]) -> list[str]:
    installed: list[str] = []
    root = workspace.resolve()
    for relative, content in files:
        target = (root / relative).resolve()
        target.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        installed.append(target.relative_to(root).as_posix())
    return installed


def _render_command(command: Iterable[str], *, python: Path, workspace: Path) -> list[str]:
    values = {"python": str(python), "workspace": str(workspace)}
    return [part.format_map(values) for part in command]


def _target_environment(
    spec: CaseSpec,
    *,
    test_python: Path,
    isolated_home: Path,
) -> dict[str, str]:
    isolated_home.mkdir(parents=True, exist_ok=True)
    python = test_python.expanduser().absolute()
    env = os.environ.copy()
    env["HOME"] = str(isolated_home)
    env["PATH"] = os.pathsep.join(
        [str(python.parent), env.get("PATH", "")]
    ).rstrip(os.pathsep)
    env["VIRTUAL_ENV"] = str(python.parent.parent)
    env["PYTHONNOUSERSITE"] = "1"
    env.update(dict(spec.test_environment))
    return env


def snapshot_paths(
    root: Path,
    globs: Iterable[str],
    ignore_globs: Iterable[str] = (),
) -> dict[str, str]:
    matches: set[Path] = set()
    for pattern in globs:
        patterns = (pattern, f"{pattern}/*") if pattern.endswith("/**") else (pattern,)
        for candidate in patterns:
            matches.update(path for path in root.glob(candidate) if path.is_file())
    result: dict[str, str] = {}
    for path in sorted(matches):
        relative_parts = path.relative_to(root).parts
        if (
            "__pycache__" in relative_parts
            or ".pytest_cache" in relative_parts
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        relative = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in ignore_globs):
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _changed_paths(workspace: Path) -> list[str]:
    result = _must_run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=workspace,
    )
    paths: list[str] = []
    for line in result["stdout"].splitlines():
        if len(line) >= 4:
            paths.append(line[3:].split(" -> ")[-1])
    return sorted(set(paths))


def _project_changed_paths(
    paths: Iterable[str],
    ignore_globs: Iterable[str] = (),
) -> list[str]:
    return sorted(
        {
            path
            for path in paths
            if not path.startswith(INTERNAL_CHANGE_PREFIXES)
            and not any(
                fnmatch.fnmatchcase(path, pattern)
                for pattern in ignore_globs
            )
        }
    )


def _read_agent_final(agent_project: Path, thread_id: str) -> dict[str, Any] | None:
    runs_root = Path(os.getenv("AGENT_RUNS_DIR") or (agent_project / ".agent_runs"))
    candidates = list(runs_root.glob(f"*/{thread_id}/final.json"))
    if not candidates:
        return None
    path = max(candidates, key=lambda item: item.stat().st_mtime_ns)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _change_shape_matches(expected: str, changed_paths: Iterable[str]) -> bool:
    count = len(tuple(changed_paths))
    if expected == "localized":
        return count <= 1
    if expected == "multi_file":
        return count >= 2
    return True


def _external_acceptance_passed(
    *,
    agent_process: dict[str, Any],
    protected_mutations: Iterable[str],
    hidden_patch: dict[str, Any],
    hidden_test: dict[str, Any] | None,
) -> bool:
    """Return the evaluator-owned outcome independently of the Agent's claim."""
    return (
        not bool(agent_process.get("timed_out"))
        and not tuple(protected_mutations)
        and hidden_patch.get("returncode") == 0
        and hidden_test is not None
        and hidden_test.get("returncode") == 0
    )


def run_evaluation(
    spec: CaseSpec,
    *,
    source_repo: Path,
    work_root: Path,
    test_python: Path,
    agent_python: Path,
    agent_project: Path,
    max_rounds: int = 12,
    max_repair_calls: int = 6,
) -> dict[str, Any]:
    source_repo = source_repo.resolve()
    if not (source_repo / ".git").exists():
        raise ValueError(f"source_repo is not a Git repository: {source_repo}")

    case_root = work_root.resolve() / spec.case_id
    baseline_workspace = case_root / "baseline"
    control_workspace = case_root / "fixed-control"
    agent_workspace = case_root / "agent-workspace"
    patch = _hidden_patch(spec, source_repo)
    target_env = _target_environment(
        spec,
        test_python=test_python,
        isolated_home=case_root / "home",
    )

    _prepare_checkout(source_repo, baseline_workspace, spec.buggy_commit)
    baseline_patch = _apply_patch(baseline_workspace, patch, spec.timeout_seconds)
    baseline_test = None
    if baseline_patch["returncode"] == 0:
        _install_hidden_files(baseline_workspace, spec.hidden_files)
        baseline_test = _run(
            _render_command(spec.test_command, python=test_python, workspace=baseline_workspace),
            cwd=baseline_workspace,
            timeout=spec.timeout_seconds,
            env=target_env,
        )

    _prepare_checkout(source_repo, control_workspace, spec.fixed_commit)
    _install_hidden_files(control_workspace, spec.hidden_files)
    control_test = _run(
        _render_command(spec.test_command, python=test_python, workspace=control_workspace),
        cwd=control_workspace,
        timeout=spec.timeout_seconds,
        env=target_env,
    )

    reachable = (
        baseline_patch["returncode"] == 0
        and baseline_test is not None
        and baseline_test["returncode"] not in (None, 0)
        and control_test["returncode"] == 0
    )
    report: dict[str, Any] = {
        "version": RESULT_VERSION,
        "case": asdict(spec),
        "source_repo": str(source_repo),
        "workspaces": {
            "baseline": str(baseline_workspace),
            "fixed_control": str(control_workspace),
            "agent": str(agent_workspace),
        },
        "preflight": {
            "reachable": reachable,
            "baseline_patch": baseline_patch,
            "baseline_test": baseline_test,
            "fixed_control_test": control_test,
        },
    }
    if not reachable:
        report.update({"status": "environment_unreachable", "resolved": False})
        return report

    _prepare_checkout(source_repo, agent_workspace, spec.buggy_commit)
    protected_before = snapshot_paths(
        agent_workspace,
        spec.protected_globs,
        spec.protected_ignore_globs,
    )
    thread_id = f"real-world-{spec.case_id}"
    agent_command = [
        str(agent_python),
        "-m",
        "coding_agent.main",
        "--workspace",
        str(agent_workspace),
        "--task",
        spec.task,
        "--thread-id",
        thread_id,
        "--clean-agent-state",
        "--max-rounds",
        str(max_rounds),
        "--max-repair-calls",
        str(max_repair_calls),
    ]
    agent_env = target_env.copy()
    agent_env.setdefault("AGENT_LLM_TIMEOUT", "60")
    agent_env.setdefault("AGENT_LLM_TIMEOUT_RETRIES", "0")
    agent_env["AGENT_TARGET_PYTHON"] = str(test_python.expanduser().absolute())
    agent_result = _run(
        agent_command,
        cwd=agent_project.resolve(),
        timeout=max(spec.timeout_seconds, 900),
        env=agent_env,
    )
    protected_after = snapshot_paths(
        agent_workspace,
        spec.protected_globs,
        spec.protected_ignore_globs,
    )
    protected_mutations = sorted(
        path
        for path in set(protected_before) | set(protected_after)
        if protected_before.get(path) != protected_after.get(path)
    )
    changed_paths = _changed_paths(agent_workspace)
    changed_project_paths = _project_changed_paths(
        changed_paths,
        spec.protected_ignore_globs,
    )

    evaluation_patch = _apply_patch(agent_workspace, patch, spec.timeout_seconds)
    hidden_test = None
    if evaluation_patch["returncode"] == 0:
        installed_hidden_files = _install_hidden_files(agent_workspace, spec.hidden_files)
        hidden_test = _run(
            _render_command(spec.test_command, python=test_python, workspace=agent_workspace),
            cwd=agent_workspace,
            timeout=spec.timeout_seconds,
            env=target_env,
        )
    external_acceptance_passed = _external_acceptance_passed(
        agent_process=agent_result,
        protected_mutations=protected_mutations,
        hidden_patch=evaluation_patch,
        hidden_test=hidden_test,
    )
    agent_final = _read_agent_final(agent_project.resolve(), thread_id)
    agent_reported_ok = bool(agent_final and agent_final.get("ok") is True)
    report.update(
        {
            "status": "resolved" if external_acceptance_passed else "unresolved",
            "resolved": external_acceptance_passed,
            "agent": {
                "process": agent_result,
                "final": agent_final,
                "changed_paths_before_hidden_tests": changed_paths,
                "protected_mutations": protected_mutations,
            },
            "acceptance": {
                "hidden_patch": evaluation_patch,
                "installed_hidden_files": installed_hidden_files if evaluation_patch["returncode"] == 0 else [],
                "hidden_test": hidden_test,
                "passed": external_acceptance_passed,
            },
            "measurements": {
                "agent_reported_ok": agent_reported_ok,
                "changed_project_file_count": len(changed_project_paths),
                "changed_top_level_areas": sorted(
                    {path.split("/", 1)[0] for path in changed_project_paths}
                ),
                "multi_file_change": len(changed_project_paths) >= 2,
                "expected_change_shape": spec.expected_change_shape,
                "change_shape_matches": _change_shape_matches(
                    spec.expected_change_shape, changed_project_paths
                ),
            },
        }
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one isolated, hidden-test real-world repair evaluation."
    )
    parser.add_argument(
        "--case",
        required=True,
        help="Path to a real_world_case_v2 JSON file (v1 remains readable).",
    )
    parser.add_argument("--source-repo", required=True, help="Local full clone containing both commits.")
    parser.add_argument("--work-root", required=True, help="Disposable directory for evaluation workspaces.")
    parser.add_argument("--test-python", required=True, help="Python executable for target-project tests.")
    parser.add_argument("--agent-python", default=sys.executable, help="Python executable containing coding_agent.")
    parser.add_argument("--agent-project", default=".", help="Agent project root (and model config cwd).")
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--max-repair-calls", type=int, default=6)
    parser.add_argument("--result", required=True, help="JSON result destination.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run_evaluation(
        load_case(args.case),
        source_repo=Path(args.source_repo),
        work_root=Path(args.work_root),
        test_python=Path(args.test_python),
        agent_python=Path(args.agent_python),
        agent_project=Path(args.agent_project),
        max_rounds=args.max_rounds,
        max_repair_calls=args.max_repair_calls,
    )
    destination = Path(args.result)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"case_id": report["case"]["case_id"], "status": report["status"], "result": str(destination)}))
    if report["status"] == "environment_unreachable":
        raise SystemExit(2)
    if not report["resolved"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
