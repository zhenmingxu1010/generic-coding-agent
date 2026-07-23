from __future__ import annotations

from pathlib import Path

from coding_agent.contracts.requirement_atoms import extract_requirement_atoms


DOMAIN_SPECIFIC_TERMS = {
    "private_fixture_alpha",
    "private_fixture_beta",
    "benchmark_only_branch",
    "customer_specific_patch",
}


def test_core_agent_contains_no_private_fixture_rules():
    root = Path(__file__).resolve().parents[1] / "coding_agent"
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        hits = sorted(term for term in DOMAIN_SPECIFIC_TERMS if term in text)
        if hits:
            offenders.append((str(path.relative_to(root)), hits))

    assert offenders == []


def test_task_requirements_come_from_structured_intake_not_core_format_rules():
    atoms = extract_requirement_atoms("Create the requested tool.", {
        "requirements": [
            {
                "id": "requested_report_content",
                "kind": "behavior",
                "description": "Report contains the fields and ranking requested by the user.",
                "required": True,
                "data": {"user_requested_fields": ["customer_id", "total_amount", "tax_rate"]},
            }
        ]
    })
    by_id = {atom["id"]: atom for atom in atoms}

    assert set(by_id) == {"requirement:requested_report_content"}
    assert by_id["requirement:requested_report_content"]["type"] == "behavior"
    assert by_id["requirement:requested_report_content"]["data"]["user_requested_fields"] == [
        "customer_id", "total_amount", "tax_rate"
    ]
