from __future__ import annotations

import os
from pathlib import Path

from coding_agent.nodes.verify import _default_commands, _verification_extra_env
from coding_agent.workspace.run_paths import agent_repo_root
from coding_agent.verification.console_entry import (
    adapt_console_command,
    invoke_entry_point,
    load_pep621_console_scripts,
)


def _project(tmp_path: Path) -> None:
    (tmp_path / "sample_cli.py").write_text(
        "import sys\n"
        "def main():\n"
        "    print('|'.join(sys.argv))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'sample-project'\n"
        "version = '0.1.0'\n"
        "[project.scripts]\n"
        "sample-tool = 'sample_cli:main'\n",
        encoding="utf-8",
    )


def test_load_and_adapt_declared_pep621_console_script(tmp_path: Path):
    _project(tmp_path)

    scripts = load_pep621_console_scripts(tmp_path)
    command, adapter = adapt_console_command(tmp_path, ["sample-tool", "run", "input.txt"])

    assert scripts == {"sample-tool": "sample_cli:main"}
    assert command[:3] == ["python", "-m", "coding_agent.verification.console_entry"]
    assert command[3:] == ["sample-tool", "sample_cli:main", "--", "run", "input.txt"]
    assert adapter["public_command"] == ["sample-tool", "run", "input.txt"]


def test_unknown_console_command_is_not_adapted(tmp_path: Path):
    _project(tmp_path)

    command, adapter = adapt_console_command(tmp_path, ["undeclared-tool", "run"])

    assert command == ["undeclared-tool", "run"]
    assert adapter is None


def test_invoke_entry_point_preserves_public_argv(tmp_path: Path, monkeypatch, capsys):
    _project(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    result = invoke_entry_point("sample-tool", "sample_cli:main", ["run", "input.txt"])

    assert result == 0
    assert capsys.readouterr().out.strip() == "sample-tool|run|input.txt"


def test_default_verification_commands_adapt_console_entry(tmp_path: Path):
    _project(tmp_path)
    state = {
        "workspace": str(tmp_path),
        "thread_id": "console-test",
        "mode": "write",
        "read_only": False,
        "file_plan": {
            "verify_steps": [
                {
                    "name": "public_cli",
                    "command": ["sample-tool", "run", "input.txt"],
                    "verifies": ["requirement:cli"],
                }
            ]
        },
    }

    commands = dict(_default_commands(state))

    assert commands["public_cli"][:3] == ["python", "-m", "coding_agent.verification.console_entry"]
    assert state["verification_command_adapters"]["public_cli"]["script_name"] == "sample-tool"


def test_console_adapter_environment_adds_agent_runtime_only_for_adapted_step(tmp_path: Path):
    state = {
        "workspace": str(tmp_path),
        "verification_command_adapters": {"public_cli": {"kind": "pep621_console_script"}},
    }

    adapted = _verification_extra_env(state, "public_cli", include_project_workspace=True)
    ordinary = _verification_extra_env(state, "ordinary")

    assert str(tmp_path.resolve()) in adapted["PYTHONPATH"].split(os.pathsep)
    assert str(agent_repo_root().resolve()) in adapted["PYTHONPATH"].split(os.pathsep)
    assert ordinary is None
