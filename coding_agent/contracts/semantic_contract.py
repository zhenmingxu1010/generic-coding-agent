from __future__ import annotations

from typing import Any

from coding_agent.contracts.requirement_atoms import evaluate_requirement_atoms


def run_semantic_contract_checks(
    workspace: str,
    contract: dict[str, Any],
    *,
    task: str = "",
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate generic contract atoms without task-specific data probes.

    A general coding agent should not contain built-in business probes for JSON,
    CSV, text, metric files, or any other domain. Concrete behavior verification
    should come from project tests, user commands, or tests the LLM decides to
    write for the current task. The runtime keeps a structured audit of
    requirement atoms, but it does not invent format-specific sample inputs.
    """
    requirement_atom_check = evaluate_requirement_atoms(
        workspace,
        list(contract.get("requirement_atoms") or []),
        state=state,
    )
    failures = list(requirement_atom_check.get("failures", []))
    warnings = list(requirement_atom_check.get("warnings", []))
    summary = requirement_atom_check.get("summary") or {}
    if int(summary.get("required_unverified", 0) or 0):
        failures.append(
            f"required requirements lack execution evidence: {summary.get('required_unverified')}"
        )
    return {
        "ok": bool(requirement_atom_check.get("ok")) and not failures,
        "failures": failures,
        "warnings": warnings,
        "sample_data_review": {
            "ok": True,
            "skipped": True,
            "reason": "generic_core_has_no_format_specific_data_probe",
            "reviews": [],
            "failures": [],
            "warnings": [],
        },
        "requirement_atom_check": requirement_atom_check,
        "semantic_checks": [],
    }
