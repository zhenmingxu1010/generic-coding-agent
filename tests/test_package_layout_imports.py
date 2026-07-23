import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module_or_package_exists(module_name: str) -> bool:
    parts = module_name.split(".")
    path = ROOT.joinpath(*parts)
    return path.with_suffix(".py").exists() or (path / "__init__.py").exists()


def _python_files():
    for base in ("coding_agent", "tests", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            if "__pycache__" not in path.parts:
                yield path


def test_coding_agent_imports_resolve_after_package_layout_cleanup():
    missing = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("coding_agent") and not _module_or_package_exists(alias.name):
                        missing.append((path.relative_to(ROOT).as_posix(), alias.name))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "coding_agent":
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        candidate = f"coding_agent.{alias.name}"
                        if not _module_or_package_exists(candidate):
                            missing.append((path.relative_to(ROOT).as_posix(), candidate))
                elif node.module.startswith("coding_agent") and not _module_or_package_exists(node.module):
                    missing.append((path.relative_to(ROOT).as_posix(), node.module))

    assert missing == []


def test_source_distribution_manifest_covers_public_project_assets():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for required in [
        "ROADMAP.md",
        "configs",
        "docs",
        "examples",
        "regression_matrix",
        "scripts",
    ]:
        assert required in manifest
