from __future__ import annotations

import argparse
import json
import os
import re
import site
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / ".agent_runs" / "release-check.json"
REQUIRED_SDIST_MEMBERS = {
    "README.md",
    "LICENSE",
    "ROADMAP.md",
    "docs/ARCHITECTURE.md",
    "docs/VALIDATION.md",
    "docs/REAL_WORLD_EVALUATION.md",
    "docs/assets/validated-demo.svg",
    "docs/assets/validated-demo.txt",
    "regression_matrix/matrix.json",
    "evaluations/real_world/pilot-summary.json",
    "evaluations/real_world/cases/pysnooper-file-output.json",
    "scripts/collect_regression_audits.py",
}
SECRET_PATTERNS = {
    "openai_style_key": re.compile(r"\b" + "sk" + r"-[A-Za-z0-9_-]{16,}\b"),
    "bearer_token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.I),
    "private_macos_home": re.compile("/" + r"Users/[^/\s]+/"),
}


def _run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return {
        "command": command,
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "stdout": proc.stdout[-12000:],
        "stderr": proc.stderr[-12000:],
    }


def _source_files() -> list[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return [ROOT / rel for rel in proc.stdout.splitlines() if rel]


def scan_source_tree() -> dict[str, Any]:
    hits: list[dict[str, str]] = []
    tracked_local_configs: list[str] = []
    for path in _source_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.endswith(("model.local.yaml", ".env")):
            tracked_local_configs.append(rel)
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                hits.append({"path": rel, "pattern": name})
    return {
        "ok": not hits and not tracked_local_configs,
        "hits": hits,
        "tracked_local_configs": tracked_local_configs,
        "files_scanned": len(_source_files()),
    }


def validate_distributions(dist_dir: Path) -> dict[str, Any]:
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    failures: list[str] = []
    if len(wheels) != 1:
        failures.append(f"expected one wheel, found {len(wheels)}")
    if len(sdists) != 1:
        failures.append(f"expected one source distribution, found {len(sdists)}")
    members: set[str] = set()
    if sdists:
        with tarfile.open(sdists[0], "r:gz") as archive:
            for name in archive.getnames():
                parts = Path(name).parts
                if len(parts) > 1:
                    members.add(Path(*parts[1:]).as_posix())
        missing = sorted(REQUIRED_SDIST_MEMBERS - members)
        failures.extend(f"sdist missing {item}" for item in missing)
    return {
        "ok": not failures,
        "wheel": str(wheels[0]) if wheels else None,
        "sdist": str(sdists[0]) if sdists else None,
        "failures": failures,
    }


def wheel_smoke(wheel: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="generic-agent-wheel-") as temp:
        temp_root = Path(temp).resolve()
        env_dir = temp_root / "venv"
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(env_dir)
        python = env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        bin_dir = python.parent
        temp_site = _run(
            [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
            cwd=temp_root,
        )
        dependency_paths = [path for path in site.getsitepackages() if Path(path).resolve() != ROOT]
        Path(temp_site["stdout"].strip()).joinpath("shared-dependencies.pth").write_text(
            "\n".join(dependency_paths) + "\n",
            encoding="utf-8",
        )
        smoke_env = os.environ.copy()
        smoke_env.pop("PYTHONPATH", None)
        import_probe = (
            "import coding_agent, pathlib; "
            "p=pathlib.Path(coding_agent.__file__).resolve(); "
            f"assert p.is_relative_to(pathlib.Path({str(temp_root)!r})), p; "
            "print(coding_agent.__version__, p)"
        )
        checks = [
            temp_site,
            _run([str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)], cwd=temp_root),
            _run([str(python), "-c", import_probe], cwd=temp_root, env=smoke_env),
            _run([str(bin_dir / "coding-agent"), "--help"], cwd=temp_root, env=smoke_env),
            _run([str(bin_dir / "coding-agent-chat"), "--help"], cwd=temp_root, env=smoke_env),
            _run([str(bin_dir / "coding-agent-export-audit"), "--help"], cwd=temp_root, env=smoke_env),
        ]
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local v0.1 release-candidate gate.")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="JSON report path.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip compile and pytest checks.")
    parser.add_argument("--skip-build", action="store_true", help="Reuse the existing dist directory.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    checks: dict[str, Any] = {"source_scan": scan_source_tree()}
    if not args.skip_tests:
        checks["compile"] = _run(
            [sys.executable, "-m", "compileall", "-q", "coding_agent", "evaluations", "tests"]
        )
        checks["pytest"] = _run([sys.executable, "-m", "pytest", "-q"])
    dist_dir = ROOT / "dist"
    if not args.skip_build:
        checks["build"] = _run([sys.executable, "-m", "build", "--no-isolation"])
    checks["distributions"] = validate_distributions(dist_dir)
    wheel_value = checks["distributions"].get("wheel")
    if wheel_value:
        checks["wheel_smoke"] = wheel_smoke(Path(wheel_value))
    report = {
        "version": "release_check_v1",
        "ok": all(bool(item.get("ok")) for item in checks.values()),
        "python": sys.version,
        "checks": checks,
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, result in checks.items():
        print(f"{'PASS' if result.get('ok') else 'FAIL'} {name}")
    print(json.dumps({"ok": report["ok"], "report": str(out)}, ensure_ascii=False))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
