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


@pytest.mark.parametrize(
    "args",
    [
        ["find", ".", "-delete"],
        ["find", ".", "-exec", "sh", "-c", "echo unsafe", ";"],
        ["find", ".", "-fprint", "result.txt"],
        ["sed", "-i", "s/a/b/", "file.txt"],
        ["sed", "--in-place=.bak", "s/a/b/", "file.txt"],
        ["git", "diff", "--output=result.diff"],
    ],
)
def test_command_guard_blocks_indirect_write_or_execution(args, tmp_path):
    with pytest.raises(ValueError):
        CommandGuard(workspace=tmp_path).check(args)


@pytest.mark.parametrize(
    "args",
    [
        ["python", "check.py"],
        ["pytest", "-q"],
        ["sh", "check.sh"],
        ["find", "."],
        ["sed", "-n", "1p", "x.txt"],
    ],
)
def test_read_only_command_guard_blocks_project_execution_and_ambiguous_tools(args, tmp_path):
    with pytest.raises(ValueError, match="not allowed"):
        CommandGuard(read_only=True, workspace=tmp_path).check(args)


def test_command_guard_blocks_workspace_script_symlink_to_external_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_script = tmp_path / "external.sh"
    external_script.write_text("#!/bin/sh\necho external\n", encoding="utf-8")
    link = workspace / "check.sh"
    try:
        link.symlink_to(external_script)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="escapes workspace"):
        CommandGuard(workspace=workspace).check(["sh", "check.sh"])
