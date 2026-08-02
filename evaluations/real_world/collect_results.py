from __future__ import annotations

import argparse
import fnmatch
import json
import statistics
from pathlib import Path
from typing import Any, Iterable

from .runner import LEGACY_RESULT_VERSION, RESULT_VERSION


SUMMARY_VERSION = "real_world_summary_v2"
INTERNAL_CHANGE_PREFIXES = (
    ".coding_agent/",
    ".coding_agent_test/",
    ".pytest_cache/",
)
TOKEN_KEYS = (
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "total_tokens",
)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _project_changes(
    agent: dict[str, Any],
    ignore_globs: Iterable[str] = (),
) -> list[str]:
    return sorted(
        {
            str(item)
            for item in agent.get("changed_paths_before_hidden_tests") or []
            if not str(item).startswith(INTERNAL_CHANGE_PREFIXES)
            and not any(
                fnmatch.fnmatchcase(str(item), pattern)
                for pattern in ignore_globs
            )
        }
    )


def _token_totals(final: dict[str, Any]) -> dict[str, int] | None:
    token_usage = final.get("token_usage") or {}
    totals = token_usage.get("totals") or {}
    if not isinstance(totals, dict) or not totals:
        return None
    return {key: int(totals.get(key, 0) or 0) for key in TOKEN_KEYS}


def _external_acceptance(data: dict[str, Any], agent: dict[str, Any]) -> bool:
    acceptance = data.get("acceptance") or {}
    explicit = acceptance.get("passed")
    if isinstance(explicit, bool):
        return explicit
    hidden_patch = acceptance.get("hidden_patch") or {}
    hidden_test = acceptance.get("hidden_test") or {}
    process = agent.get("process") or {}
    return (
        not bool(process.get("timed_out"))
        and not list(agent.get("protected_mutations") or [])
        and hidden_patch.get("returncode") == 0
        and hidden_test.get("returncode") == 0
    )


def _alignment(*, reachable: bool, claimed: bool, accepted: bool) -> str:
    if not reachable:
        return "not_evaluated"
    if claimed and accepted:
        return "true_positive"
    if claimed and not accepted:
        return "false_positive"
    if not claimed and accepted:
        return "false_negative"
    return "true_negative"


def _category_summary(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    categories = sorted(
        {
            str(category)
            for item in cases
            for category in item.get("categories") or []
        }
    )
    result: dict[str, dict[str, Any]] = {}
    for category in categories:
        selected = [
            item for item in cases if category in (item.get("categories") or [])
        ]
        reachable = [item for item in selected if item["environment_reachable"]]
        accepted = sum(bool(item["external_acceptance_passed"]) for item in reachable)
        false_positives = sum(
            item["final_gate_alignment"] == "false_positive" for item in reachable
        )
        result[category] = {
            "total": len(selected),
            "eligible": len(reachable),
            "externally_accepted": accepted,
            "resolution_rate": _ratio(accepted, len(reachable)),
            "false_positive_claims": false_positives,
        }
    return result


def load_case_metadata(case_dir: Path) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for path in sorted(case_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        case_id = data.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"case metadata is missing case_id: {path}")
        if case_id in metadata:
            raise ValueError(f"duplicate case_id in metadata: {case_id}")
        metadata[case_id] = data
    return metadata


def collect_results(
    paths: Iterable[Path],
    *,
    run_date: str | None = None,
    scope_note: str | None = None,
    case_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") not in {RESULT_VERSION, LEGACY_RESULT_VERSION}:
            raise ValueError(f"unsupported result version in {path}")
        case = data.get("case") or {}
        agent = data.get("agent") or {}
        final = agent.get("final") or {}
        hidden_test = (data.get("acceptance") or {}).get("hidden_test") or {}
        reachable = bool((data.get("preflight") or {}).get("reachable"))
        claimed = final.get("ok") is True
        accepted = _external_acceptance(data, agent) if reachable else False
        published_case = (case_metadata or {}).get(str(case.get("case_id"))) or {}
        ignore_globs = (
            case.get("protected_ignore_globs")
            or published_case.get("protected_ignore_globs")
            or []
        )
        changed = _project_changes(agent, ignore_globs)
        categories = case.get("categories") or published_case.get("categories") or ["repair"]
        if not isinstance(categories, list):
            categories = ["repair"]
        token_totals = _token_totals(final)
        cases.append(
            {
                "case_id": case.get("case_id"),
                "project": case.get("project"),
                "categories": sorted({str(item) for item in categories if item}),
                "expected_change_shape": case.get(
                    "expected_change_shape",
                    published_case.get("expected_change_shape", "unspecified"),
                ),
                "status": data.get("status"),
                "environment_reachable": reachable,
                "agent_reported_ok": claimed,
                "agent_stopped_reason": final.get("stopped_reason"),
                "external_acceptance_passed": accepted,
                "final_gate_alignment": _alignment(
                    reachable=reachable, claimed=claimed, accepted=accepted
                ),
                "hidden_tests_passed": hidden_test.get("returncode") == 0,
                "protected_mutations": list(agent.get("protected_mutations") or []),
                "changed_project_paths": changed,
                "changed_project_file_count": len(changed),
                "changed_top_level_areas": sorted(
                    {path.split("/", 1)[0] for path in changed}
                ),
                "multi_file_change": len(changed) >= 2,
                "agent_duration_seconds": (agent.get("process") or {}).get("duration_seconds"),
                "token_usage": token_totals,
            }
        )
    eligible = [item for item in cases if item["environment_reachable"]]
    durations = [
        float(item["agent_duration_seconds"])
        for item in eligible
        if isinstance(item.get("agent_duration_seconds"), (int, float))
    ]
    token_rows = [
        item["token_usage"] for item in eligible if item.get("token_usage") is not None
    ]
    token_totals = {
        key: sum(int(row.get(key, 0) or 0) for row in token_rows)
        for key in TOKEN_KEYS
    }
    true_positives = sum(
        item["final_gate_alignment"] == "true_positive" for item in eligible
    )
    false_positives = sum(
        item["final_gate_alignment"] == "false_positive" for item in eligible
    )
    false_negatives = sum(
        item["final_gate_alignment"] == "false_negative" for item in eligible
    )
    true_negatives = sum(
        item["final_gate_alignment"] == "true_negative" for item in eligible
    )
    accepted = sum(bool(item["external_acceptance_passed"]) for item in eligible)
    claims = true_positives + false_positives
    changed_counts = [int(item["changed_project_file_count"]) for item in eligible]
    summary = {
        "version": SUMMARY_VERSION,
        "total": len(cases),
        "eligible": len(eligible),
        "resolved": sum(item["status"] == "resolved" for item in cases),
        "environment_unreachable": sum(
            item["status"] == "environment_unreachable" for item in cases
        ),
        "agent_reported_ok": sum(bool(item["agent_reported_ok"]) for item in cases),
        "hidden_tests_passed": sum(bool(item["hidden_tests_passed"]) for item in cases),
        "external_acceptance_passed": accepted,
        "protected_mutation_cases": sum(bool(item["protected_mutations"]) for item in cases),
        "final_gate": {
            "true_positive": true_positives,
            "false_positive": false_positives,
            "false_negative": false_negatives,
            "true_negative": true_negatives,
            "success_claim_precision": _ratio(true_positives, claims),
            "success_claim_recall": _ratio(true_positives, accepted),
            "false_positive_claim_rate": _ratio(false_positives, claims),
        },
        "resolution_rate": _ratio(accepted, len(eligible)),
        "average_agent_duration_seconds": (
            round(sum(durations) / len(durations), 3) if durations else None
        ),
        "median_agent_duration_seconds": (
            round(statistics.median(durations), 3) if durations else None
        ),
        "total_token_usage": token_totals if token_rows else None,
        "average_total_tokens": (
            round(token_totals["total_tokens"] / len(token_rows), 3)
            if token_rows
            else None
        ),
        "average_changed_project_files": (
            round(sum(changed_counts) / len(changed_counts), 3)
            if changed_counts
            else None
        ),
        "multi_file_change_cases": sum(
            bool(item["multi_file_change"]) for item in eligible
        ),
        "categories": _category_summary(cases),
        "cases": cases,
    }
    if run_date:
        summary["run_date"] = run_date
    if scope_note:
        summary["scope_note"] = scope_note
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect sanitized real-world evaluation results.")
    parser.add_argument("results", nargs="+", help="Result JSON files.")
    parser.add_argument("--output", required=True, help="Sanitized summary JSON destination.")
    parser.add_argument("--run-date", help="Optional YYYY-MM-DD date for a published summary.")
    parser.add_argument(
        "--case-dir",
        help="Optional directory of case definitions used to enrich legacy results.",
    )
    parser.add_argument(
        "--scope-note",
        help="Optional honest-scope statement included in a published summary.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = collect_results(
        (Path(item) for item in args.results),
        run_date=args.run_date,
        scope_note=args.scope_note,
        case_metadata=load_case_metadata(Path(args.case_dir)) if args.case_dir else None,
    )
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
