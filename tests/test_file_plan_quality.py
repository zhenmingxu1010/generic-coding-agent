from pathlib import Path

from coding_agent.nodes.file_plan import _sanitize_verify_steps


def _state(tmp_path: Path, atom_id: str, description: str) -> dict:
    return {
        "workspace": str(tmp_path),
        "task": description,
        "task_contract": {
            "requirement_atoms": [{
                "id": atom_id,
                "description": description,
                "verify_hint": description,
            }]
        },
    }


def _grounding(atom_id: str, quote: str) -> dict:
    return {
        "basis": [{"source": atom_id, "quote": quote}],
        "expected": quote,
    }


def test_sandbox_module_command_infers_local_package_copy(tmp_path: Path):
    (tmp_path / "sample_pkg").mkdir()
    (tmp_path / "sample_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "sample_pkg" / "cli.py").write_text("print('ok')\n", encoding="utf-8")
    description = "Run python -m sample_pkg.cli input.json."
    state = _state(tmp_path, "requirement:cli", description)
    state["repo_map"] = {"files": ["sample_pkg/__init__.py", "sample_pkg/cli.py"]}

    steps = _sanitize_verify_steps(state, [{
        "name": "cli_fixture",
        "command": ["python", "-m", "sample_pkg.cli", "input.json"],
        "verifies": ["requirement:cli"],
        **_grounding("requirement:cli", description),
        "sandbox": {"files": [{"path": "input.json", "content": "[]"}]},
    }])

    assert steps[0]["sandbox"]["copy_paths"] == ["sample_pkg"]


def test_verification_step_preserves_documented_success_exit_codes(tmp_path: Path):
    description = "Invalid input exits with status 2."
    state = _state(tmp_path, "requirement:invalid", description)

    steps = _sanitize_verify_steps(state, [{
        "name": "invalid_input",
        "command": ["python", "tool.py", "bad.json"],
        "verifies": ["requirement:invalid"],
        **_grounding("requirement:invalid", description),
        "success_exit_codes": [2, 2, 999, "bad"],
    }])

    assert steps[0]["success_exit_codes"] == [2]


def test_file_plan_verify_steps_drop_external_absolute_output_paths(tmp_path: Path):
    run_dir = tmp_path / ".agent_runs" / "thread"
    run_dir.mkdir(parents=True)
    state = {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "task": "The public command supports --help.",
        "task_contract": {"requirement_atoms": [{
            "id": "requirement:output",
            "required": True,
            "description": "The public command supports --help.",
        }]},
    }

    steps = _sanitize_verify_steps(
        state,
        [
            {"name": "outside", "command": ["python", "scripts/report.py", "--output", "/tmp/report.out"], "verifies": ["requirement:output"]},
            {
                "name": "help",
                "command": ["python", "scripts/report.py", "--help"],
                "verifies": ["requirement:output"],
                **_grounding("task", "The public command supports --help."),
            },
        ],
    )

    assert [step["name"] for step in steps] == ["help"]
    assert state["skipped_file_plan_verify_steps"][0]["reason"] == "verification step uses absolute path outside workspace/run directory"


def test_file_plan_verify_steps_drop_shell_and_inline_python(tmp_path: Path):
    run_dir = tmp_path / ".agent_runs" / "thread"
    run_dir.mkdir(parents=True)
    state = {"workspace": str(tmp_path), "run_dir": str(run_dir), "task_contract": {"requirement_atoms": []}}

    steps = _sanitize_verify_steps(
        state,
        [
            {"name": "shell", "command": ["bash", "-lc", "python app.py"], "verifies": []},
            {"name": "inline", "command": ["python", "-c", "print('x')"], "verifies": []},
            {"name": "compile", "command": ["python", "-m", "compileall", "-q", "."], "verifies": []},
        ],
    )

    assert steps == []
    reasons = {item["reason"] for item in state["skipped_file_plan_verify_steps"]}
    assert "verification step may not invoke a shell interpreter" in reasons
    assert "verification step may not execute inline Python" in reasons
    assert "verification step is not bound to a known requirement" in reasons


def test_file_plan_accepts_direct_shell_script_with_sandbox_fixture(tmp_path: Path):
    description = "The script counts lines in a supplied text file."
    state = _state(tmp_path, "requirement:count", description)
    steps = _sanitize_verify_steps(state, [{
        "name": "count_lines",
        "command": ["sh", "count.sh", "input.txt"],
        "verifies": ["requirement:count"],
        **_grounding("requirement:count", description),
        "sandbox": {
            "copy_paths": ["count.sh"],
            "files": [{"path": "input.txt", "content": "one\ntwo\n"}],
        },
    }])

    assert steps[0]["command"] == ["sh", "count.sh", "input.txt"]


def test_file_plan_normalizes_declared_sandbox_fixture_reference(tmp_path: Path):
    description = "The tool accepts input.data and produces an observable result."
    state = _state(tmp_path, "requirement:behavior", description)

    steps = _sanitize_verify_steps(state, [{
        "name": "behavior",
        "command": ["python", "tool.py", "{verification_dir}/input.data"],
        "verifies": ["requirement:behavior"],
        **_grounding("requirement:behavior", description),
        "sandbox": {
            "copy_paths": ["tool.py"],
            "files": [{"path": "input.data", "content": "example"}],
        },
    }])

    assert steps[0]["command"] == ["python", "tool.py", "input.data"]
    assert state["normalized_file_plan_verify_steps"][0]["corrections"] == [
        {"from": "{verification_dir}/input.data", "to": "input.data"}
    ]


def test_file_plan_keeps_verification_output_placeholder(tmp_path: Path):
    description = "The tool supports --output and writes the requested report."
    state = _state(tmp_path, "requirement:output", description)

    steps = _sanitize_verify_steps(state, [{
        "name": "output",
        "command": ["python", "tool.py", "--output={verification_dir}/report.data"],
        "verifies": ["requirement:output"],
        **_grounding("task", description),
        "sandbox": {
            "copy_paths": ["tool.py"],
            "files": [{"path": "input.data", "content": "example"}],
        },
    }])

    assert steps[0]["command"][-1] == "--output={verification_dir}/report.data"
    assert "normalized_file_plan_verify_steps" not in state


def test_file_plan_normalizes_placeholder_in_fixture_and_command(tmp_path: Path):
    description = "The tool accepts input.data and produces an observable result."
    state = _state(tmp_path, "requirement:behavior", description)

    steps = _sanitize_verify_steps(state, [{
        "name": "behavior",
        "command": ["python", "tool.py", "{verification_dir}/input.data"],
        "verifies": ["requirement:behavior"],
        **_grounding("requirement:behavior", description),
        "sandbox": {
            "copy_paths": ["tool.py"],
            "files": [{"path": "{verification_dir}/input.data", "content": "example"}],
        },
    }])

    assert steps[0]["sandbox"]["files"][0]["path"] == "input.data"
    assert steps[0]["command"] == ["python", "tool.py", "input.data"]
    corrections = state["normalized_file_plan_verify_steps"][0]["corrections"]
    assert corrections == [
        {"from": "{verification_dir}/input.data", "to": "input.data"},
        {"from": "{verification_dir}/input.data", "to": "input.data"},
    ]


def test_file_plan_infers_sandbox_copy_for_planned_command_target(tmp_path: Path):
    description = "Run src/tool.py with input.data and observe the requested behavior."
    state = _state(tmp_path, "requirement:behavior", description)

    steps = _sanitize_verify_steps(
        state,
        [{
            "name": "behavior",
            "command": ["python", "src/tool.py", "input.data"],
            "verifies": ["requirement:behavior"],
            **_grounding("requirement:behavior", description),
            "sandbox": {"files": [{"path": "input.data", "content": "example"}]},
        }],
        planned_files=[{"path": "src/tool.py", "kind": "code"}],
    )

    assert steps[0]["sandbox"]["copy_paths"] == ["src/tool.py"]
    assert state["inferred_sandbox_copy_paths"] == [
        {"name": "behavior", "paths": ["src/tool.py"]}
    ]


def test_file_plan_rejects_uncited_verification_behavior(tmp_path: Path):
    state = _state(tmp_path, "requirement:behavior", "The tool prints the requested summary.")

    steps = _sanitize_verify_steps(state, [{
        "name": "invented_mode",
        "command": ["python", "tool.py", "--invented-mode"],
        "verifies": ["requirement:behavior"],
        "expected": "The invented mode succeeds.",
    }])

    assert steps == []
    assert state["skipped_file_plan_verify_steps"][-1]["grounding"]["status"] == "rejected"


def test_file_plan_rejects_public_option_absent_from_cited_evidence(tmp_path: Path):
    description = "The tool prints the requested summary."
    state = _state(tmp_path, "requirement:behavior", description)

    steps = _sanitize_verify_steps(state, [{
        "name": "invented_mode",
        "command": ["python", "tool.py", "--invented-mode"],
        "verifies": ["requirement:behavior"],
        **_grounding("requirement:behavior", description),
    }])

    assert steps == []
    grounding = state["skipped_file_plan_verify_steps"][-1]["grounding"]
    assert grounding["unsupported_options"] == ["--invented-mode"]


def test_llm_requirement_text_alone_cannot_authorize_new_public_option(tmp_path: Path):
    description = "The tool supports --invented-mode."
    state = _state(tmp_path, "requirement:behavior", description)
    state["task"] = "Fix the existing tool so it follows its documentation."

    steps = _sanitize_verify_steps(state, [{
        "name": "invented_mode",
        "command": ["python", "tool.py", "--invented-mode"],
        "verifies": ["requirement:behavior"],
        **_grounding("requirement:behavior", description),
    }])

    assert steps == []
    assert state["skipped_file_plan_verify_steps"][-1]["grounding"]["unsupported_options"] == ["--invented-mode"]


def test_file_plan_accepts_public_option_supported_by_repository_evidence(tmp_path: Path):
    (tmp_path / "README.md").write_text("Run the program with --mode compact.\n", encoding="utf-8")
    state = _state(tmp_path, "requirement:behavior", "The documented command must work.")

    steps = _sanitize_verify_steps(state, [{
        "name": "documented_mode",
        "command": ["python", "tool.py", "--mode", "compact"],
        "verifies": ["requirement:behavior"],
        "basis": [{"source": "README.md", "quote": "Run the program with --mode compact."}],
        "expected": "The documented compact mode completes successfully.",
    }])

    assert [step["name"] for step in steps] == ["documented_mode"]
    assert steps[0]["grounding"]["status"] == "accepted"


def test_file_plan_rejects_uncited_test_source_as_application_input(tmp_path: Path):
    description = "The command reads a documented data file and prints a summary."
    state = _state(tmp_path, "requirement:behavior", description)

    steps = _sanitize_verify_steps(state, [{
        "name": "invalid_input",
        "command": ["python", "tool.py", "tests/test_tool.py"],
        "verifies": ["requirement:behavior"],
        **_grounding("requirement:behavior", description),
    }])

    assert steps == []
    grounding = state["skipped_file_plan_verify_steps"][-1]["grounding"]
    assert grounding["unsupported_test_inputs"] == ["tests/test_tool.py"]


def test_document_delegated_requirement_requires_document_citation(tmp_path: Path):
    (tmp_path / "README.md").write_text("The command accepts a JSON array.\n", encoding="utf-8")
    state = {
        "workspace": str(tmp_path),
        "task": "Run pytest and satisfy README.md.",
        "task_contract": {"requirement_atoms": [{
            "id": "requirement:readme_contract",
            "type": "behavior",
            "description": "All behavior in README.md must work.",
            "evidence": ["satisfy README.md"],
            "source": "llm_task_requirement",
        }]},
    }

    steps = _sanitize_verify_steps(state, [{
        "name": "pytest_only",
        "command": ["pytest", "-q"],
        "verifies": ["requirement:readme_contract"],
        "basis": [{"source": "task", "quote": "Run pytest and satisfy README.md."}],
        "expected": "pytest exits successfully.",
    }])

    assert steps == []
    grounding = state["skipped_file_plan_verify_steps"][-1]["grounding"]
    assert grounding["missing_contract_citations"] == {
        "requirement:readme_contract": ["README.md"]
    }


def test_file_plan_preserves_bounded_standard_input(tmp_path: Path):
    description = "The program counts lines from standard input."
    state = _state(tmp_path, "requirement:count", description)
    steps = _sanitize_verify_steps(state, [{
        "name": "count_stdin",
        "command": ["python", "count.py"],
        "stdin": "one\ntwo\nthree\n",
        "verifies": ["requirement:count"],
        **_grounding("requirement:count", description),
    }])

    assert steps[0]["stdin"] == "one\ntwo\nthree\n"
