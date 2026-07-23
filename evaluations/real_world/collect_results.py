from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from .runner import RESULT_VERSION


SUMMARY_VERSION = "real_world_summary_v1"


def collect_results(paths: Iterable[Path]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != RESULT_VERSION:
            raise ValueError(f"unsupported result version in {path}")
        case = data.get("case") or {}
        agent = data.get("agent") or {}
        final = agent.get("final") or {}
        hidden_test = (data.get("acceptance") or {}).get("hidden_test") or {}
        changed = [
            item
            for item in agent.get("changed_paths_before_hidden_tests") or []
            if not str(item).startswith((".coding_agent/", ".coding_agent_test/"))
        ]
        cases.append(
            {
                "case_id": case.get("case_id"),
                "project": case.get("project"),
                "status": data.get("status"),
                "environment_reachable": bool((data.get("preflight") or {}).get("reachable")),
                "agent_reported_ok": final.get("ok") is True,
                "agent_stopped_reason": final.get("stopped_reason"),
                "hidden_tests_passed": hidden_test.get("returncode") == 0,
                "protected_mutations": list(agent.get("protected_mutations") or []),
                "changed_project_paths": changed,
                "agent_duration_seconds": (agent.get("process") or {}).get("duration_seconds"),
            }
        )
    durations = [
        float(item["agent_duration_seconds"])
        for item in cases
        if isinstance(item.get("agent_duration_seconds"), (int, float))
    ]
    return {
        "version": SUMMARY_VERSION,
        "total": len(cases),
        "resolved": sum(item["status"] == "resolved" for item in cases),
        "environment_unreachable": sum(
            item["status"] == "environment_unreachable" for item in cases
        ),
        "agent_reported_ok": sum(bool(item["agent_reported_ok"]) for item in cases),
        "hidden_tests_passed": sum(bool(item["hidden_tests_passed"]) for item in cases),
        "protected_mutation_cases": sum(bool(item["protected_mutations"]) for item in cases),
        "average_agent_duration_seconds": (
            round(sum(durations) / len(durations), 3) if durations else None
        ),
        "cases": cases,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect sanitized real-world evaluation results.")
    parser.add_argument("results", nargs="+", help="Result JSON files.")
    parser.add_argument("--output", required=True, help="Sanitized summary JSON destination.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = collect_results(Path(item) for item in args.results)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"total": summary["total"], "resolved": summary["resolved"], "output": str(output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
