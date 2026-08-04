from __future__ import annotations

import sys
from pathlib import Path

from coding_agent.tools.shell_tools import run_shell


def test_run_shell_python_uses_current_interpreter(tmp_path: Path):
    (tmp_path / "show_python.py").write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
    res = run_shell(str(tmp_path), ["python", "show_python.py"])

    assert res.ok
    assert res.data["command"][0] == sys.executable
    assert sys.executable in res.data["stdout"]


def test_run_shell_can_use_explicit_target_python(tmp_path: Path, monkeypatch):
    (tmp_path / "show_python.py").write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_TARGET_PYTHON", sys.executable)
    res = run_shell(str(tmp_path), ["python", "show_python.py"])

    assert res.ok
    assert res.data["command"][0] == sys.executable


def test_run_shell_ignores_missing_target_python(tmp_path: Path, monkeypatch):
    (tmp_path / "show_python.py").write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_TARGET_PYTHON", str(tmp_path / "missing-python"))
    res = run_shell(str(tmp_path), ["python", "show_python.py"])

    assert res.ok
    assert res.data["command"][0] == sys.executable


def test_run_shell_preserves_virtualenv_symlink(tmp_path: Path, monkeypatch):
    launcher = tmp_path / "venv-python"
    launcher.symlink_to(sys.executable)
    (tmp_path / "show_python.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_TARGET_PYTHON", str(launcher))
    res = run_shell(str(tmp_path), ["python", "show_python.py"])

    assert res.ok
    assert res.data["command"][0] == str(launcher)


def test_run_shell_python3_uses_current_interpreter(tmp_path: Path):
    (tmp_path / "show_python.py").write_text("import sys\nprint(sys.executable)\n", encoding="utf-8")
    res = run_shell(str(tmp_path), ["python3", "show_python.py"])

    assert res.ok
    assert res.data["command"][0] == sys.executable
    assert res.data["executed"] is True


def test_run_shell_rejects_inline_python(tmp_path: Path):
    res = run_shell(str(tmp_path), ["python", "-c", "print('unsafe')"])
    assert res.ok is False
    assert "Inline Python" in res.message
    assert res.data["executed"] is False
    assert res.data["failure_kind"] == "command_policy"


def test_run_shell_pytest_uses_current_interpreter_module(tmp_path: Path):
    res = run_shell(str(tmp_path), ["pytest", "--version"])

    assert res.data["command"][:3] == [sys.executable, "-m", "pytest"]


def test_run_shell_can_supply_standard_input_without_a_shell(tmp_path: Path):
    (tmp_path / "count.py").write_text(
        "import sys\nprint(len(sys.stdin.readlines()))\n",
        encoding="utf-8",
    )
    res = run_shell(
        str(tmp_path),
        ["python", "count.py"],
        input_text="one\ntwo\nthree\n",
    )

    assert res.ok is True
    assert res.data["stdout"] == "3\n"


def test_run_shell_merges_explicit_environment(tmp_path: Path):
    (tmp_path / "show_env.py").write_text(
        "import os\nprint(os.environ['EVALUATION_MARKER'])\n",
        encoding="utf-8",
    )
    res = run_shell(
        str(tmp_path),
        ["python", "show_env.py"],
        extra_env={"EVALUATION_MARKER": "available"},
    )

    assert res.ok is True
    assert res.data["stdout"] == "available\n"


def test_run_shell_timeout_preserves_partial_output_as_text(tmp_path: Path):
    (tmp_path / "slow.py").write_text(
        "import time\nprint('x' * 200, flush=True)\ntime.sleep(2)\n",
        encoding="utf-8",
    )

    res = run_shell(
        str(tmp_path),
        ["python", "slow.py"],
        timeout_sec=0.05,
        max_output_chars=20,
    )

    assert res.ok is False
    assert res.data["timed_out"] is True
    assert res.data["failure_kind"] == "timeout"
    assert isinstance(res.data["stdout"], str)
    assert res.data["stdout"].startswith("x" * 20)
