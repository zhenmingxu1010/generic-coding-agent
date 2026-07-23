from pathlib import Path

from coding_agent.repair.import_error_context import build_import_error_context
from coding_agent.repair.repair_controller import build_repair_controller
from coding_agent.repair.traceback_parser import parse_traceback_issues


def test_traceback_parser_extracts_missing_module_name():
    text = """
E   ModuleNotFoundError: No module named 'invoice_analyzer'
"""

    issues = parse_traceback_issues(text)

    issue = next(item for item in issues if item["exception_type"] == "ModuleNotFoundError")
    assert issue["missing_module"] == "invoice_analyzer"
    assert issue["module"] == "invoice_analyzer"


def test_import_error_context_reports_layout_facts_without_fixed_recipe(tmp_path: Path):
    (tmp_path / "src" / "invoice_analyzer").mkdir(parents=True)
    (tmp_path / "src" / "invoice_analyzer" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "invoice_analyzer" / "cli.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n\n[project.scripts]\ninvoice-analyzer = 'invoice_analyzer.cli:main'\n",
        encoding="utf-8",
    )
    state = {
        "workspace": str(tmp_path),
        "traceback_issues": [
            {
                "type": "modulenotfounderror",
                "exception_type": "ModuleNotFoundError",
                "missing_module": "invoice_analyzer",
                "module": "invoice_analyzer",
                "message": "ModuleNotFoundError: No module named 'invoice_analyzer'",
            }
        ],
        "verification": {
            "results": [
                {
                    "name": "pytest",
                    "command": ["python", "-m", "pytest", "-q"],
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "ModuleNotFoundError: No module named 'invoice_analyzer'",
                }
            ]
        },
    }

    context = build_import_error_context(state)

    assert context["present"] is True
    assert context["project_config"]["pyproject"]["has_project_scripts"] is True
    module_context = context["missing_modules"][0]
    assert module_context["module"] == "invoice_analyzer"
    assert "src/invoice_analyzer" in module_context["candidate_package_dirs"]
    assert {"path": "src/invoice_analyzer/__init__.py", "kind": "package_init", "exists": True} in module_context["module_path_candidates"]
    assert any(
        item["path"] == "src/invoice_analyzer/cli.py" and item["kind"] == "package_python_file"
        for item in module_context["module_path_candidates"]
    )
    assert "PYTHONPATH=src" not in str(context)


def test_import_error_context_handles_missing_external_dependency_generically(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "verification": {
            "results": [
                {
                    "name": "custom",
                    "command": ["python", "main.py"],
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "ModuleNotFoundError: No module named 'some_external_package'",
                }
            ]
        },
    }

    context = build_import_error_context(state)

    module_context = context["missing_modules"][0]
    assert module_context["module"] == "some_external_package"
    assert module_context["candidate_package_dirs"] == []
    assert any("No package directory" in fact for fact in module_context["facts"])


def test_repair_controller_uses_import_context_as_target_candidates(tmp_path: Path):
    (tmp_path / "src" / "demo_pkg").mkdir(parents=True)
    (tmp_path / "src" / "demo_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "demo_pkg" / "cli.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    issue = {
        "owner": "implementation",
        "type": "modulenotfounderror",
        "exception_type": "ModuleNotFoundError",
        "missing_module": "demo_pkg",
        "module": "demo_pkg",
        "message": "ModuleNotFoundError: No module named 'demo_pkg'",
    }
    state = {
        "workspace": str(tmp_path),
        "failure": {"signature": "sig"},
        "traceback_issues": [issue],
        "failure_issues": [issue],
    }
    state["import_error_context"] = build_import_error_context(state)

    controller = build_repair_controller(state)

    assert controller["route"] == "fix_implementation_import_api"
    assert "src/demo_pkg/cli.py" in controller["target_files"]
    assert "src/demo_pkg/__init__.py" in controller["target_files"]
    assert "pyproject.toml" in controller["target_files"]
