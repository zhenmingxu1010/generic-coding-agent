from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

from coding_agent.workspace.run_paths import agent_repo_root, run_dir_for


TEXT_SUFFIXES = {
    ".json", ".jsonl", ".md", ".txt", ".py", ".sh", ".bash", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv", ".diff",
}
TEXT_NAMES = {"Dockerfile", "Makefile", "Procfile"}
SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)"),
    re.compile(r"(?i)(authorization\s*[:=]\s*[\"']?bearer\s+)([^\s\"',}]+)"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{12,}"),
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export a coding-agent audit bundle")
    p.add_argument("--workspace", required=True)
    p.add_argument("--thread-id", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--include-workspace", action="store_true", help="Also include workspace source files, excluding agent state directories")
    return p.parse_args()


def _redact_text(text: str, replacements: list[tuple[str, str]]) -> str:
    for value, placeholder in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if value:
            text = text.replace(value, placeholder)
    text = SECRET_PATTERNS[0].sub(r"\1<REDACTED>", text)
    text = SECRET_PATTERNS[1].sub(r"\1<REDACTED>", text)
    text = SECRET_PATTERNS[2].sub("<REDACTED_TOKEN>", text)
    return text


def _audit_bytes(path: Path, replacements: list[tuple[str, str]]) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return _redact_text(text, replacements).encode("utf-8")


def add_if_exists(
    zf: zipfile.ZipFile,
    path: Path,
    arc_prefix: str = "agent",
    *,
    replacements: list[tuple[str, str]] | None = None,
) -> None:
    if path.exists() and path.is_file():
        zf.writestr(
            f"{arc_prefix}/{path.name}",
            _audit_bytes(path, replacements or []),
        )


def export_audit_bundle(
    workspace: str | Path,
    thread_id: str,
    out: str | Path,
    *,
    include_workspace: bool = False,
) -> Path:
    workspace = Path(workspace).resolve()
    run_dir = run_dir_for(workspace, thread_id)
    out = Path(out).resolve()
    if not run_dir.exists():
        raise FileNotFoundError(
            f"Cannot export audit: run directory does not exist: {run_dir}. "
            "Check that --workspace and --thread-id match the agent run."
        )
    final_json = run_dir / "final.json"
    if not final_json.is_file():
        raise FileNotFoundError(
            f"Cannot export audit: final.json does not exist: {final_json}. "
            "The agent run may have crashed before finalization, or --thread-id may be wrong."
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    replacements = [
        (str(run_dir), "<RUN_DIR>"),
        (str(workspace), "<WORKSPACE>"),
        (str(agent_repo_root()), "<AGENT_REPO>"),
        (str(Path.home()), "<HOME>"),
    ]
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in [
            "final.json",
            "final_report.md",
            "final_report_human.md",
            "analysis_report.md",
            "trace.jsonl",
            "messages.jsonl",
            "context_pack.json",
            "context_summary.md",
            "state_snapshot.json",
            "repository_map.json",
            "short_term_memory.md",
            "restore_manifest.json",
            "analysis_contract.json",
            "analysis_contract_check.json",
        ]:
            add_if_exists(zf, run_dir / name, replacements=replacements)
        memory_dir = run_dir.parent / "project_memory"
        if memory_dir.exists():
            for p in memory_dir.rglob("*"):
                if p.is_file():
                    zf.writestr(
                        f"project_memory/{p.relative_to(memory_dir)}",
                        _audit_bytes(p, replacements),
                    )
        patches = run_dir / "patches"
        if patches.exists():
            for p in patches.rglob("*"):
                if p.is_file():
                    zf.writestr(f"agent/patches/{p.relative_to(patches)}", _audit_bytes(p, replacements))
        failed_writes = run_dir / "failed_writes"
        if failed_writes.exists():
            for p in failed_writes.rglob("*"):
                if p.is_file():
                    zf.writestr(f"agent/failed_writes/{p.relative_to(failed_writes)}", _audit_bytes(p, replacements))
        agent_tests = workspace / ".coding_agent_test" / thread_id
        if agent_tests.exists():
            for p in agent_tests.rglob("*"):
                if p.is_file():
                    if "__pycache__" in p.parts or p.suffix == ".pyc":
                        continue
                    zf.writestr(f"agent_tests/{p.relative_to(agent_tests)}", _audit_bytes(p, replacements))
        backups = run_dir / "backups"
        if backups.exists():
            for p in backups.rglob("*"):
                if p.is_file():
                    zf.writestr(f"agent/backups/{p.relative_to(backups)}", _audit_bytes(p, replacements))
        if include_workspace:
            for p in workspace.rglob("*"):
                if not p.is_file():
                    continue
                if ".agent_runs" in p.parts or ".coding_agent" in p.parts or ".coding_agent_test" in p.parts or "__pycache__" in p.parts or ".git" in p.parts:
                    continue
                zf.writestr(f"workspace/{p.relative_to(workspace)}", _audit_bytes(p, replacements))
        zf.writestr(
            "audit_manifest.json",
            json.dumps({
                "version": "audit_manifest_v1",
                "text_sanitization": True,
                "redacted": ["workspace path", "run path", "agent repository path", "home path", "common API key and bearer token patterns"],
                "warning": "Review workspace source and command output before public sharing; automated redaction is defense in depth, not a guarantee.",
            }, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        if not zf.namelist():
            raise RuntimeError(
                f"Cannot export audit: no files were added from run directory: {run_dir}. "
                "This indicates an invalid or incomplete agent run."
            )
    return out


def main() -> None:
    args = parse_args()
    out = export_audit_bundle(
        args.workspace,
        args.thread_id,
        args.out,
        include_workspace=args.include_workspace,
    )
    print({"audit_zip": str(out), "size_bytes": out.stat().st_size})


if __name__ == "__main__":
    main()
