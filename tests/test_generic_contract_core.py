from __future__ import annotations

from coding_agent.contracts.semantic_contract import run_semantic_contract_checks
from coding_agent.nodes.final_gate import compute_final_gate


def test_generic_contract_check_does_not_create_format_specific_probes(tmp_path):
    contract = {
        "objective": "Create a CLI that reads some input file and prints a summary.",
        "requirement_atoms": [
            {
                "id": "requirement:input_changes_output",
                "type": "behavior",
                "required": True,
                "status": "pending",
            }
        ],
    }

    out = run_semantic_contract_checks(str(tmp_path), contract, task=contract["objective"])

    assert out["ok"] is False
    assert out["semantic_checks"] == []
    assert out["sample_data_review"]["skipped"] is True
    atom = out["requirement_atom_check"]["atoms"][0]
    assert atom["status"] == "unverified"


def test_final_gate_rejects_unverified_required_atoms():
    gate = compute_final_gate(
        {
            "mode": "write",
            "read_only": False,
            "verification": {"ok": True},
            "contract_ok": True,
            "needs_verification": False,
            "changed_files": ["tool.py"],
            "requirement_atom_summary": {
                "required_total": 1,
                "required_failed": 0,
                "required_unverified": 1,
            },
        }
    )

    assert gate["ok"] is False
    assert "required_requirement_atoms_unverified:1" in gate["failures"]
