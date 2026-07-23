from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_check.py"
SPEC = importlib.util.spec_from_file_location("release_check", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_validate_distributions_reports_missing_required_sdist_member(tmp_path: Path):
    (tmp_path / "package.whl").write_bytes(b"placeholder")
    root = tmp_path / "package-0.1.0"
    root.mkdir()
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    with tarfile.open(tmp_path / "package.tar.gz", "w:gz") as archive:
        archive.add(root, arcname=root.name)

    result = MODULE.validate_distributions(tmp_path)

    assert result["ok"] is False
    assert any("sdist missing LICENSE" in failure for failure in result["failures"])


def test_required_distribution_manifest_covers_release_evidence():
    assert "docs/VALIDATION.md" in MODULE.REQUIRED_SDIST_MEMBERS
    assert "docs/assets/validated-demo.gif" in MODULE.REQUIRED_SDIST_MEMBERS
    assert "docs/assets/validated-demo.txt" in MODULE.REQUIRED_SDIST_MEMBERS
    assert "regression_matrix/matrix.json" in MODULE.REQUIRED_SDIST_MEMBERS
    assert "scripts/collect_regression_audits.py" in MODULE.REQUIRED_SDIST_MEMBERS


def test_wheel_smoke_does_not_put_source_checkout_on_pythonpath():
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'smoke_env.pop("PYTHONPATH", None)' in source
    assert "p.is_relative_to" in source
    assert "cwd=temp_root" in source
