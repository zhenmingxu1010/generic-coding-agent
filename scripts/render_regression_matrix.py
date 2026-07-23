from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from string import Template
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "regression_matrix" / "matrix.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _load_matrix() -> dict[str, Any]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _q(value: str) -> str:
    return shlex.quote(str(value))


def _template(text: str, values: dict[str, str]) -> str:
    return Template(text).safe_substitute(values)


def _case_by_id(matrix: dict[str, Any], case_id: str) -> dict[str, Any]:
    wanted = case_id.upper()
    for case in matrix.get("cases", []):
        if str(case.get("id", "")).upper() == wanted:
            return case
    known = ", ".join(case.get("id", "") for case in matrix.get("cases", []))
    raise SystemExit(f"Unknown case {case_id!r}. Known cases: {known}")


def _render_case(case: dict[str, Any], values: dict[str, str]) -> str:
    thread = str(case["thread_id"])
    workspace = _template(str((case.get("workspace") or {}).get("template") or ""), values)
    audit_name = f"{case['id'].lower()}_{thread}_audit.zip"
    audit_path = f"$AUDIT_DIR/{audit_name}"
    include_workspace = " --include-workspace" if case.get("include_workspace_in_audit") else ""
    repair_existing = " \\\n  --repair-existing" if case.get("repair_existing") else ""
    task = str(case.get("task") or "")

    lines: list[str] = []
    lines.append("")
    lines.append(f"# ===== {case['id']}: {case['title_zh']} =====")
    lines.append(f"# {case['purpose_zh']}")
    lines.append(f"THREAD={_q(thread)}")
    lines.append(f"WORKSPACE={_q(workspace)}")
    lines.append("mkdir -p \"$AUDIT_DIR\"")
    lines.append("mkdir -p \"$(dirname \"$WORKSPACE\")\"")
    for command in case.get("setup_commands") or []:
        lines.append(_template(str(command), values))
    if task:
        lines.append("TASK=$(cat <<'EOF_TASK'")
        lines.append(task)
        lines.append("EOF_TASK")
        lines.append(")")
    run_parts = [
        "python -m coding_agent.main \\",
        "  --workspace \"$WORKSPACE\" \\",
    ]
    if task:
        run_parts.append("  --task \"$TASK\" \\")
    run_parts.append(f"  --max-rounds {int(case.get('max_rounds', 20))} \\")
    run_parts.append("  --thread-id \"$THREAD\" \\")
    run_parts.append("  --clean-agent-state")
    if repair_existing:
        run_parts.insert(2, "  --repair-existing \\")
    lines.extend(run_parts)
    lines.append("")
    lines.append(
        "python -m coding_agent.export_audit \\"
        "\n  --workspace \"$WORKSPACE\" \\"
        "\n  --thread-id \"$THREAD\" \\"
        f"\n  --out \"{audit_path}\"{include_workspace}"
    )
    lines.append("")
    lines.append("echo \"Audit: " + audit_path + "\"")
    lines.append("# Expected conditions:")
    for item in case.get("expected_conditions") or []:
        lines.append(f"# - {item}")
    return "\n".join(lines)


def render(matrix: dict[str, Any], cases: list[dict[str, Any]], args: argparse.Namespace) -> str:
    defaults = matrix.get("defaults") or {}
    llm_env = defaults.get("llm_env") or {}
    values = {
        "AGENT_REPO": args.agent_repo or defaults.get("agent_repo", ""),
        "AUDIT_DIR": args.audit_dir or defaults.get("audit_dir", ""),
        "REGRESSION_WORK_ROOT": args.work_root or defaults.get("regression_work_root", ""),
    }

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"AGENT_REPO={_q(values['AGENT_REPO'])}",
        f"AUDIT_DIR={_q(values['AUDIT_DIR'])}",
        f"REGRESSION_WORK_ROOT={_q(values['REGRESSION_WORK_ROOT'])}",
        "",
        "cd \"$AGENT_REPO\"",
        "mkdir -p \"$AUDIT_DIR\" \"$REGRESSION_WORK_ROOT\"",
        "",
    ]
    if args.include_llm_env:
        lines.append("# Optional LLM environment defaults. Omit this block to use configs/model.local.yaml.")
        for key, default in llm_env.items():
            lines.append(f"export {key}=\"${{{key}:-{default}}}\"")
        lines.append("")
    else:
        lines.append("# LLM config is loaded from configs/model.yaml and configs/model.local.yaml.")
        lines.append("# Existing AGENT_LLM_* environment variables still override config if you set them before running.")
    lines.append("")
    for case in cases:
        lines.append(_render_case(case, values))
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render copy-paste commands for the coding-agent regression matrix.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--case", help="Render one case, for example T01.")
    group.add_argument("--all", action="store_true", help="Render all matrix cases.")
    parser.add_argument("--agent-repo", help="Agent repository path.")
    parser.add_argument("--audit-dir", help="Directory for exported audit zip files.")
    parser.add_argument("--work-root", help="Root directory for temporary regression workspaces.")
    parser.add_argument(
        "--include-llm-env",
        action="store_true",
        help="Render legacy AGENT_LLM_* default exports. By default the generated script uses model config files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    matrix = _load_matrix()
    cases = matrix.get("cases") or []
    selected = cases if args.all else [_case_by_id(matrix, args.case)]
    print(render(matrix, selected, args), end="")


if __name__ == "__main__":
    main()
