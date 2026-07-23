from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_regression_audits.py"
SPEC = importlib.util.spec_from_file_location("collect_regression_audits", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_detailed_t06_validation_rejects_symbolic_placeholder_change():
    final = {
        "ok": True,
        "write_scope_audit": {
            "existing_project_modified_files": [],
            "source_changed_files": [
                "scripts/inspect_event_schema.py",
                "docs/event_schema.md",
                ".coding_agent_test/<thread-id>",
            ],
        },
    }
    names = {
        "workspace/scripts/inspect_event_schema.py",
        "workspace/docs/event_schema.md",
    }

    failures = MODULE._validate_case("T06", final, names)

    assert "source changes are not exactly the two declared deliverables" in failures


def test_detailed_t06_validation_accepts_exact_deliverables():
    final = {
        "ok": True,
        "write_scope_audit": {
            "existing_project_modified_files": [],
            "source_changed_files": [
                "scripts/inspect_event_schema.py",
                "docs/event_schema.md",
            ],
        },
    }
    names = {
        "workspace/scripts/inspect_event_schema.py",
        "workspace/docs/event_schema.md",
    }

    assert MODULE._validate_case("T06", final, names) == []


def test_detailed_t10_validation_requires_controlled_zero_write_stop():
    final = {
        "ok": False,
        "outcome": "clarification_required",
        "controlled_failure": True,
        "clarification_questions": [{"question": "What should it do?"}],
        "write_scope_audit": {"source_changed_files": []},
    }

    assert MODULE._validate_case("T10", final, set()) == []
