from __future__ import annotations

from typing import Any


def requirement_atom_trace_status(state: dict[str, Any]) -> dict[str, Any]:
    check = state.get("requirement_atom_check") or ((state.get("semantic_contract_check") or {}).get("requirement_atom_check") or {})
    atoms = check.get("atoms") or state.get("requirement_atoms") or (state.get("task_contract") or {}).get("requirement_atoms") or []
    summary = check.get("summary") or state.get("requirement_atom_summary") or (state.get("task_contract") or {}).get("requirement_atom_summary") or {}
    compact_atoms = []
    for atom in atoms:
        if not isinstance(atom, dict):
            continue
        compact_atoms.append({
            "id": atom.get("id"),
            "type": atom.get("type"),
            "required": atom.get("required", True),
            "status": atom.get("status", "unverified"),
            "description": atom.get("description", ""),
        })
    return {
        "summary": summary,
        "atoms": compact_atoms,
        "required_failed": int(summary.get("required_failed", 0) or 0),
        "required_unverified": int(summary.get("required_unverified", 0) or 0),
    }
