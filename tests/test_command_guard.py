import pytest

from coding_agent.safety.command_guard import CommandGuard


def test_command_guard_allows_python():
    assert CommandGuard().check(["python", "-V"])[0] == "python"


@pytest.mark.parametrize("executable", ["python3", "python3.10", "python.exe"])
def test_command_guard_accepts_python_interpreter_aliases(executable):
    assert CommandGuard().check([executable, "-V"])[0] == executable


def test_command_guard_blocks_unknown():
    with pytest.raises(ValueError):
        CommandGuard().check(["sudo", "rm", "-rf", "/"])


def test_command_guard_blocks_dangerous_pattern():
    with pytest.raises(ValueError):
        CommandGuard().check(["rm", "-rf", "/"])


def test_command_guard_allows_direct_workspace_shell_script(tmp_path):
    script = tmp_path / "check.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    assert CommandGuard(workspace=tmp_path).check(["sh", "check.sh"])[0] == "sh"


@pytest.mark.parametrize("args", [["sh", "-c", "echo unsafe"], ["bash", "-lc", "echo unsafe"]])
def test_command_guard_rejects_inline_shell(args, tmp_path):
    with pytest.raises(ValueError, match="only run a workspace script directly"):
        CommandGuard(workspace=tmp_path).check(args)
