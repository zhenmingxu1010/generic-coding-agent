from __future__ import annotations

import json
from pathlib import Path

from coding_agent.graph import route_after_retrieve
from coding_agent.nodes.file_plan import _planning_evidence
from coding_agent.nodes.verify import _dedupe, verify_node
from coding_agent.verification import behavior_review


class _Client:
    response = "{}"

    def __init__(self, *_args, **_kwargs):
        pass

    def chat(self, *_args, **_kwargs):
        return self.response


def _behavior_state(tmp_path: Path) -> dict:
    atom = {
        "id": "requirement:observable_behavior",
        "type": "behavior",
        "description": "The public command produces an observable result.",
        "required": True,
        "status": "pending",
    }
    return {
        "workspace": str(tmp_path),
        "run_dir": str(tmp_path / ".agent_runs" / "t"),
        "thread_id": "t",
        "task": "Create a tool with the requested observable behavior.",
        "task_contract": {"requirement_atoms": [atom]},
        "file_plan": {
            "files": [{"path": "tool.py", "kind": "code"}],
            "verify_steps": [
                {
                    "name": "public_behavior",
                    "command": ["python", "tool.py", "{verification_dir}/result.txt"],
                    "verifies": [atom["id"]],
                    "basis": [{
                        "source": "task",
                        "quote": "Create a tool with the requested observable behavior.",
                    }],
                    "expected": "The requested public behavior is observable.",
                    "grounding": {"status": "accepted"},
                    "timeout_sec": 30,
                }
            ],
        },
        "mode": "write",
        "read_only": False,
        "changed_files": ["tool.py"],
        "generated_files": [{"path": "tool.py", "kind": "code", "ok": True}],
        "trace_path": str(tmp_path / ".agent_runs" / "t" / "trace.jsonl"),
        "state_snapshot_path": str(tmp_path / ".agent_runs" / "t" / "state.json"),
    }


def test_write_route_builds_context_before_planning():
    assert route_after_retrieve({"mode": "write", "read_only": False}) == "context_compress"


def test_planning_evidence_prioritizes_explicit_read_reference():
    state = {
        "task_intent": {"read_reference_paths": ["data/schema.any"]},
        "context_pack": {
            "evidence_blocks": [
                {"path": "src/large.py", "priority": 100, "content": "broad source"},
                {"path": "data/schema.any", "priority": 1, "content": "real nested structure"},
            ]
        },
    }
    text = _planning_evidence(state, max_chars=1000)
    assert text.index("data/schema.any") < text.index("src/large.py")
    assert "real nested structure" in text


def test_evidence_reviewer_requires_successful_bound_step(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    _Client.response = json.dumps({
        "claims": [
            {
                "atom_id": "requirement:observable_behavior",
                "status": "passed",
                "cited_steps": ["unrelated"],
                "evidence": ["claimed success"],
                "reason": "looks correct",
            }
        ]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    claims = behavior_review.review_behavior_evidence(
        state,
        [{"name": "public_behavior", "returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False}],
        [],
    )

    assert claims["requirement:observable_behavior"]["status"] == "unverified"


def test_agent_default_execution_gate_reuses_grounded_task_behavior(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    state["task_contract"]["requirement_atoms"].append({
        "id": "implementation:requested_change_execution",
        "type": "behavior",
        "description": "The requested change has execution evidence.",
        "required": True,
        "source": "agent_implementation_default",
        "data": {"evidence_mode": "execution", "contract_source": "agent_defaults"},
    })
    _Client.response = json.dumps({
        "claims": [
            {
                "atom_id": "requirement:observable_behavior",
                "status": "passed",
                "cited_steps": ["public_behavior"],
                "evidence": ["observable result"],
                "reason": "the requested behavior executed",
            },
            {
                "atom_id": "implementation:requested_change_execution",
                "status": "unverified",
                "cited_steps": [],
                "evidence": ["the task behavior passed"],
                "reason": "no separately bound duplicate step",
            },
        ]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    claims = behavior_review.review_behavior_evidence(
        state,
        [{
            "name": "public_behavior",
            "returncode": 0,
            "stdout": "observable result",
            "stderr": "",
            "timed_out": False,
            "executed": True,
        }],
        [],
    )

    aggregate = claims["implementation:requested_change_execution"]
    assert aggregate["status"] == "passed"
    assert aggregate["cited_steps"] == ["public_behavior"]
    assert "requirement:observable_behavior" in aggregate["evidence"][0]


def test_agent_default_cli_gate_reuses_observed_usage_path(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    state["task_contract"]["requirement_atoms"].append({
        "id": "implementation:usable_cli_invocation",
        "type": "behavior",
        "description": "The CLI exposes usage guidance.",
        "required": True,
        "source": "agent_implementation_default",
        "data": {"evidence_mode": "execution", "contract_source": "agent_defaults"},
    })
    state["file_plan"]["verify_steps"][0].update({
        "command": ["python", "tool.py"],
        "expected": "stderr contains a Usage message.",
    })
    _Client.response = json.dumps({"claims": [
        {
            "atom_id": "requirement:observable_behavior",
            "status": "passed",
            "cited_steps": ["public_behavior"],
            "evidence": ["Usage: tool.py INPUT"],
            "reason": "the public path executed",
        },
        {
            "atom_id": "implementation:usable_cli_invocation",
            "status": "unverified",
            "cited_steps": [],
            "evidence": [],
            "reason": "not separately bound",
        },
    ]})
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    claims = behavior_review.review_behavior_evidence(
        state,
        [{
            "name": "public_behavior",
            "returncode": 0,
            "stdout": "",
            "stderr": "Usage: tool.py INPUT",
            "timed_out": False,
            "executed": True,
        }],
        [],
    )

    assert claims["implementation:usable_cli_invocation"]["status"] == "passed"
    assert claims["implementation:usable_cli_invocation"]["cited_steps"] == ["public_behavior"]


def test_passing_project_tests_are_reused_as_matching_requirement_evidence(monkeypatch, tmp_path: Path):
    atoms = [
        {
            "id": "requirement:all_tests_pass",
            "type": "behavior",
            "description": "All existing tests pass.",
            "verify_hint": "Run pytest and observe all tests pass.",
            "data": {"evidence_mode": "execution"},
        },
        {
            "id": "requirement:cli_unchanged",
            "type": "behavior",
            "description": "task_stats.py 的命令行接口保持正常工作。",
            "verify_hint": "运行 task_stats.py 的命令行入口。",
            "data": {"evidence_mode": "execution"},
        },
        {
            "id": "requirement:uncovered_behavior",
            "type": "behavior",
            "description": "A separate export behavior works.",
            "data": {"evidence_mode": "execution"},
        },
    ]
    state = {
        "workspace": str(tmp_path),
        "task": "Repair task_stats.py without changing its CLI.",
        "task_contract": {"requirement_atoms": atoms},
        "file_plan": {"verify_steps": []},
        "test_results": {
            "runs": [{
                "name": "pytest",
                "ok": True,
                "total": 2,
                "testcases": [
                    {"test": "tests.test_task_stats::test_cli_reads_json_and_prints_summary", "status": "passed"},
                    {"test": "tests.test_task_stats::test_empty_input", "status": "passed"},
                ],
            }]
        },
    }
    _Client.response = json.dumps({"claims": []})
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    claims = behavior_review.review_behavior_evidence(
        state,
        [{"name": "pytest", "returncode": 0, "stdout": "2 passed", "stderr": "", "timed_out": False}],
        [],
    )

    assert claims["requirement:all_tests_pass"]["status"] == "passed"
    assert claims["requirement:cli_unchanged"]["status"] == "unverified"
    assert claims["requirement:uncovered_behavior"]["status"] == "unverified"


def test_one_generic_test_name_token_does_not_prove_specific_behavior(
    monkeypatch,
    tmp_path: Path,
):
    atom = {
        "id": "requirement:cleanup",
        "type": "behavior",
        "description": "A failed hook removes the partial output directory.",
        "verify_hint": "Run a failed hook and confirm the directory is absent.",
        "data": {"evidence_mode": "execution"},
    }
    state = {
        "workspace": str(tmp_path),
        "task": "Clean partial output after a failed hook.",
        "task_contract": {"requirement_atoms": [atom]},
        "file_plan": {"verify_steps": []},
        "test_results": {"runs": [{
            "name": "pytest",
            "ok": True,
            "total": 1,
            "testcases": [{
                "test": "tests.test_hooks::test_run_hook",
                "status": "passed",
            }],
        }]},
    }
    _Client.response = json.dumps({"claims": []})
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    claims = behavior_review.review_behavior_evidence(
        state,
        [{"name": "pytest", "returncode": 0, "stdout": "1 passed"}],
        [],
    )

    assert claims[atom["id"]]["status"] == "unverified"


def test_document_contract_is_not_proven_by_a_partially_matching_test_name(monkeypatch, tmp_path: Path):
    atom = {
        "id": "requirement:readme_contract",
        "type": "behavior",
        "description": "All CLI behavior in README.md is satisfied.",
        "evidence": ["README.md contract"],
        "data": {"evidence_mode": "execution"},
    }
    state = {
        "workspace": str(tmp_path),
        "task": "Make the README.md contract work.",
        "task_contract": {"requirement_atoms": [atom]},
        "file_plan": {"verify_steps": []},
        "test_results": {"runs": [{
            "name": "pytest",
            "ok": True,
            "total": 1,
            "testcases": [{"test": "tests.test_cli::test_cli_output", "status": "passed"}],
        }]},
    }
    _Client.response = json.dumps({"claims": [{
        "atom_id": atom["id"],
        "status": "passed",
        "cited_steps": ["pytest"],
        "reason": "a CLI-named test passed",
    }]})
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    claims = behavior_review.review_behavior_evidence(
        state,
        [{"name": "pytest", "returncode": 0, "stdout": "1 passed", "stderr": "", "timed_out": False}],
        [],
    )

    assert claims[atom["id"]]["status"] == "unverified"


def test_document_contract_verify_hint_cannot_turn_it_into_all_tests_requirement(monkeypatch, tmp_path: Path):
    atom = {
        "id": "requirement:readme_contract",
        "type": "behavior",
        "description": "README.md 中全部命令行契约成立。",
        "evidence": ["README.md 中全部命令行契约"],
        "verify_hint": "pytest 测试全部通过即证明契约成立",
        "data": {"evidence_mode": "execution"},
    }
    state = {
        "workspace": str(tmp_path),
        "task": "Satisfy README.md.",
        "task_contract": {"requirement_atoms": [atom]},
        "file_plan": {"verify_steps": []},
        "test_results": {"runs": [{
            "name": "pytest",
            "ok": True,
            "total": 1,
            "testcases": [{"test": "tests.test_cli::test_cli_output", "status": "passed"}],
        }]},
    }
    _Client.response = json.dumps({"claims": []})
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    claims = behavior_review.review_behavior_evidence(
        state,
        [{"name": "pytest", "returncode": 0, "stdout": "1 passed", "stderr": "", "timed_out": False}],
        [],
    )

    assert claims[atom["id"]]["status"] == "unverified"


def test_evidence_reviewer_skips_llm_when_bound_execution_failed(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)

    monkeypatch.setattr(
        behavior_review,
        "review_failed_verification_oracles",
        lambda _state, _steps, _results: {
            "public_behavior": {"name": "public_behavior", "status": "grounded", "reason": "task-grounded"}
        },
    )

    class _UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("failed execution should not invoke the evidence-review LLM")

    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _UnexpectedClient)

    claims = behavior_review.review_behavior_evidence(
        state,
        [{
            "name": "public_behavior",
            "returncode": 1,
            "stdout": "expected 5, got 4",
            "stderr": "",
            "timed_out": False,
        }],
        [],
    )

    claim = claims["requirement:observable_behavior"]
    assert claim["status"] == "failed"
    assert claim["cited_steps"] == ["public_behavior"]
    assert state["verification_review_mode"] == "deterministic_failed_execution"


def test_unexecuted_verification_is_unverified_not_implementation_failure(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)

    class _UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("unexecuted verification should use deterministic claims")

    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _UnexpectedClient)
    claims = behavior_review.review_behavior_evidence(
        state,
        [{
            "name": "public_behavior",
            "returncode": 1,
            "stdout": "",
            "stderr": "command blocked by policy",
            "timed_out": False,
            "executed": False,
            "failure_kind": "command_policy",
        }],
        [],
    )

    claim = claims["requirement:observable_behavior"]
    assert claim["status"] == "unverified"
    assert "could not be executed" in claim["reason"]


def test_failed_execution_keeps_other_successful_bound_requirement_passed(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    successful_atom = {
        "id": "requirement:second_behavior",
        "type": "behavior",
        "description": "A second public behavior remains observable.",
        "required": True,
        "status": "pending",
        "data": {"evidence_mode": "execution"},
    }
    state["task_contract"]["requirement_atoms"].append(successful_atom)
    state["file_plan"]["verify_steps"].append({
        "name": "second_behavior",
        "command": ["python", "tool.py", "--second"],
        "verifies": ["requirement:second_behavior"],
        "basis": [{"source": "requirement:second_behavior", "quote": "A second public behavior remains observable."}],
        "expected": "The second public behavior is observable.",
        "grounding": {"status": "accepted"},
    })

    monkeypatch.setattr(
        behavior_review,
        "review_failed_verification_oracles",
        lambda _state, _steps, _results: {
            "public_behavior": {"name": "public_behavior", "status": "grounded", "reason": "task-grounded"}
        },
    )

    class _UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("mixed execution results must use deterministic claims")

    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _UnexpectedClient)
    claims = behavior_review.review_behavior_evidence(
        state,
        [
            {"name": "public_behavior", "returncode": 1, "stdout": "bad", "stderr": "", "timed_out": False},
            {"name": "second_behavior", "returncode": 0, "stdout": "observable", "stderr": "", "timed_out": False},
        ],
        [],
    )

    assert claims["requirement:observable_behavior"]["status"] == "failed"
    assert claims["requirement:second_behavior"]["status"] == "passed"
    assert claims["requirement:second_behavior"]["cited_steps"] == ["second_behavior"]


def test_unsupported_failed_scenario_does_not_override_grounded_success(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    state["file_plan"]["verify_steps"].append({
        "name": "unsupported_scenario",
        "command": ["python", "tool.py", "unexpected-mode"],
        "verifies": ["requirement:observable_behavior"],
        "basis": [{
            "source": "task",
            "quote": "Create a tool with the requested observable behavior.",
        }],
        "expected": "An extra behavior succeeds.",
        "grounding": {"status": "accepted"},
    })
    monkeypatch.setattr(
        behavior_review,
        "review_failed_verification_oracles",
        lambda _state, _steps, _results: {
            "unsupported_scenario": {
                "name": "unsupported_scenario",
                "status": "unsupported",
                "reason": "the scenario adds behavior not stated by the task",
            }
        },
    )

    claims = behavior_review.review_behavior_evidence(
        state,
        [
            {"name": "public_behavior", "returncode": 0, "stdout": "requested result", "stderr": "", "timed_out": False},
            {"name": "unsupported_scenario", "returncode": 2, "stdout": "", "stderr": "unsupported", "timed_out": False},
        ],
        [],
    )

    claim = claims["requirement:observable_behavior"]
    assert claim["status"] == "passed"
    assert claim["cited_steps"] == ["public_behavior"]
    assert state["verification_oracle_review"]["rejected_step_names"] == ["unsupported_scenario"]
    assert [step["name"] for step in state["file_plan"]["verify_steps"]] == ["public_behavior"]


def test_oracle_cannot_reject_grounded_scenario_because_implementation_failed(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)

    _Client.response = json.dumps({
        "step_reviews": [{
            "name": "public_behavior",
            "status": "unsupported",
            "reason": "The current implementation fails with a runtime error.",
        }]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    reviews = behavior_review.review_failed_verification_oracles(
        state,
        state["file_plan"]["verify_steps"],
        [{
            "name": "public_behavior",
            "returncode": 1,
            "stdout": "",
            "stderr": "TypeError: broken formatter",
            "timed_out": False,
            "executed": True,
        }],
    )

    assert reviews["public_behavior"]["status"] == "grounded"


def test_oracle_cannot_use_observed_missing_behavior_to_invalidate_scenario(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)

    _Client.response = json.dumps({
        "step_reviews": [{
            "name": "public_behavior",
            "status": "unsupported",
            "reason": (
                "The actual output shows the operation succeeded unexpectedly; "
                "the code did not raise the required exception."
            ),
        }]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    reviews = behavior_review.review_failed_verification_oracles(
        state,
        state["file_plan"]["verify_steps"],
        [{
            "name": "public_behavior",
            "returncode": 1,
            "stdout": "ERROR: operation succeeded unexpectedly",
            "stderr": "",
            "timed_out": False,
            "executed": True,
        }],
    )

    assert reviews["public_behavior"]["status"] == "grounded"


def test_oracle_can_still_reject_behavior_absent_from_contract(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)

    _Client.response = json.dumps({
        "step_reviews": [{
            "name": "public_behavior",
            "status": "unsupported",
            "reason": "The cited task never states this extra input mode.",
        }]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    reviews = behavior_review.review_failed_verification_oracles(
        state,
        state["file_plan"]["verify_steps"],
        [{
            "name": "public_behavior",
            "returncode": 2,
            "stdout": "",
            "stderr": "unsupported option",
            "timed_out": False,
            "executed": True,
        }],
    )

    assert reviews["public_behavior"]["status"] == "unsupported"


def test_oracle_preserves_rejection_for_probe_with_missing_required_argument(
    monkeypatch, tmp_path: Path,
):
    state = _behavior_state(tmp_path)
    _Client.response = json.dumps({
        "step_reviews": [{
            "name": "public_behavior",
            "status": "unsupported",
            "reason": (
                "The probe script calls run('x') without the required context "
                "argument, causing TypeError, so it does not exercise the actual signature."
            ),
        }]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    reviews = behavior_review.review_failed_verification_oracles(
        state,
        state["file_plan"]["verify_steps"],
        [{
            "name": "public_behavior",
            "returncode": 1,
            "stderr": "TypeError: missing required positional argument: context",
            "executed": True,
        }],
    )

    assert reviews["public_behavior"]["status"] == "unsupported"


def test_evidence_reviewer_accepts_authoritative_runtime_constraint(monkeypatch, tmp_path: Path):
    atom = {
        "id": "requirement:artifact_placement",
        "type": "constraint",
        "description": "Agent-owned checks remain outside user deliverables.",
        "required": True,
        "status": "pending",
        "data": {"evidence_mode": "runtime"},
    }
    state = {
        "workspace": str(tmp_path),
        "task": "Create one user deliverable and keep agent checks internal.",
        "task_contract": {"requirement_atoms": [atom]},
        "generated_files": [{"path": "tool.py", "kind": "code", "user_visible": True}],
        "changed_files": ["tool.py"],
        "output_layout": {"agent_test_root": ".coding_agent_test/t"},
    }
    _Client.response = json.dumps({
        "claims": [
            {
                "atom_id": atom["id"],
                "status": "passed",
                "cited_steps": [],
                "cited_runtime": ["runtime:generated_files", "runtime:output_layout"],
                "evidence": ["Only tool.py is user-visible; the internal root is separate."],
                "reason": "runtime artifact records prove placement",
            }
        ]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    claims = behavior_review.review_behavior_evidence(state, [], [])

    assert claims[atom["id"]]["status"] == "passed"
    assert claims[atom["id"]]["cited_runtime"] == [
        "runtime:generated_files",
        "runtime:output_layout",
    ]


def test_verification_dedupe_preserves_same_command_in_distinct_sandboxes():
    commands = [
        ("normal_scenario", ["python", "tool.py"]),
        ("fallback_scenario", ["python", "tool.py"]),
    ]
    state = {
        "verification_step_claims": {
            "normal_scenario": ["requirement:normal"],
            "fallback_scenario": ["requirement:fallback"],
        },
        "verification_step_sandboxes": {
            "fallback_scenario": {
                "copy_paths": ["tool.py"],
                "files": [{"path": "input.dat", "content": "alternate"}],
                "omit_paths": [],
            }
        },
    }

    assert _dedupe(commands, state=state) == commands


def test_evidence_reviewer_does_not_split_scalar_fields_into_characters(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    _Client.response = json.dumps({
        "claims": [
            {
                "atom_id": "requirement:observable_behavior",
                "status": "passed",
                "cited_steps": "public_behavior",
                "evidence": "observable result",
                "reason": "executed public behavior",
            }
        ]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    claims = behavior_review.review_behavior_evidence(
        state,
        [{"name": "public_behavior", "returncode": 0, "stdout": "observable result", "stderr": "", "timed_out": False}],
        [],
    )

    claim = claims["requirement:observable_behavior"]
    assert claim["status"] == "passed"
    assert claim["cited_steps"] == ["public_behavior"]
    assert claim["evidence"] == ["observable result"]


def test_verification_planner_replans_an_unverified_existing_step(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    state["verification_claims"] = {
        "requirement:observable_behavior": {
            "atom_id": "requirement:observable_behavior",
            "status": "unverified",
            "cited_steps": ["public_behavior"],
        }
    }
    _Client.response = json.dumps({
        "verify_steps": [
            {
                "name": "public_behavior_with_observable_fixture",
                "command": ["python", "tool.py", "{verification_dir}/alternate.txt"],
                "verifies": ["requirement:observable_behavior"],
                "basis": [{
                    "source": "task",
                    "quote": "Create a tool with the requested observable behavior.",
                }],
                "expected": "The requested public behavior is observable.",
                "timeout_sec": 30,
            }
        ]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    update = behavior_review.supplement_verification_steps(state)

    assert state["verification_plan_attempts"] == 1
    assert update["requested_requirement_ids"] == ["requirement:observable_behavior"]
    assert [step["name"] for step in update["added"]] == ["public_behavior_with_observable_fixture"]
    assert len(state["file_plan"]["verify_steps"]) == 2


def test_verification_replan_demands_direct_contract_boundary_scenarios(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    (tmp_path / "README.md").write_text(
        "The input file must contain a JSON array.\n",
        encoding="utf-8",
    )
    atom = state["task_contract"]["requirement_atoms"][0]
    atom["description"] = "All behavior in README.md must hold."
    atom["evidence"] = ["README.md defines the command contract"]
    state["scope_contract"] = {"read_reference_paths": ["README.md"]}
    state["verification_claims"] = {
        atom["id"]: {"atom_id": atom["id"], "status": "unverified"}
    }
    state["verification"] = {
        "test_results": {"runs": [{"testcases": [{"test": "tests.test_cli::test_valid_array"}]}]}
    }

    class _CapturingClient(_Client):
        messages = []

        def chat(self, messages, **_kwargs):
            self.__class__.messages = messages
            return json.dumps({"verify_steps": []})

    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _CapturingClient)

    behavior_review.supplement_verification_steps(state)
    system = _CapturingClient.messages[0]["content"]
    prompt = _CapturingClient.messages[1]["content"]

    assert "wrong top-level type" in system
    assert "minimal empty value" in system
    assert "sandbox.files" in system
    assert "never use" in system and "python -c" in system
    assert "do not resubmit the same pytest command" in prompt
    assert "no more than two steps" in prompt
    assert "minimal empty wrong-type boundary value" in prompt
    assert "tests.test_cli::test_valid_array" in prompt
    assert "README.md" in prompt


def test_verification_planner_uses_small_batches_with_probe_sized_output_budget(
    monkeypatch, tmp_path: Path,
):
    state = _behavior_state(tmp_path)
    state["file_plan"]["verify_steps"] = []

    class _CapturingClient(_Client):
        kwargs = {}

        def chat(self, messages, **kwargs):
            self.__class__.kwargs = kwargs
            self.__class__.messages = messages
            return json.dumps({"verify_steps": []})

    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _CapturingClient)

    behavior_review.supplement_verification_steps(state)

    assert "at most two minimal steps" in _CapturingClient.messages[0]["content"]
    assert "exact signature and execution preconditions" in _CapturingClient.messages[0]["content"]
    assert "inject or monkeypatch the lower-level" in _CapturingClient.messages[0]["content"]
    assert _CapturingClient.kwargs["max_tokens"] == 4000


def test_compact_review_result_preserves_actual_custom_exit_code():
    rows = behavior_review._compact_results_for_review([{
        "name": "invalid_input",
        "returncode": 0,
        "actual_returncode": 2,
        "success_exit_codes": [2],
    }])

    assert rows[0]["normalized_returncode"] == 0
    assert rows[0]["actual_returncode"] == 2
    assert rows[0]["success_exit_codes"] == [2]


def test_json_array_contract_requires_wrong_top_level_scenario(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "The input file must contain a JSON array.\n",
        encoding="utf-8",
    )
    atom = {
        "id": "requirement:contract",
        "description": "README.md command contract works.",
        "evidence": ["README.md contract"],
    }
    state = {"workspace": str(tmp_path)}
    valid_only = [{
        "name": "valid_array",
        "verifies": [atom["id"]],
        "sandbox": {"files": [{"path": "input.json", "content": "[]"}]},
    }]
    with_empty_object = valid_only + [{
        "name": "wrong_object",
        "verifies": [atom["id"]],
        "sandbox": {"files": [{"path": "input.json", "content": "{}"}]},
    }]

    assert behavior_review._has_wrong_json_top_level_scenario(
        state, atom, valid_only, {"valid_array"}
    ) is False
    assert behavior_review._has_wrong_json_top_level_scenario(
        state, atom, with_empty_object, {"valid_array", "wrong_object"}
    ) is True


def test_verification_planner_does_not_expand_plan_while_prior_execution_failed(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    state["verification"] = {
        "ok": False,
        "results": [{"name": "public_behavior", "returncode": 1, "timed_out": False}],
    }
    state["verification_claims"] = {
        "requirement:observable_behavior": {
            "atom_id": "requirement:observable_behavior",
            "status": "unverified",
        }
    }

    class _UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("repair must run before verification-plan expansion")

    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _UnexpectedClient)
    update = behavior_review.supplement_verification_steps(state)

    assert update["skipped"] is True
    assert update["failed_prior_steps"] == ["public_behavior"]
    assert state.get("verification_plan_attempts", 0) == 0


def test_verification_planner_can_replace_an_unexecuted_step(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    state["verification"] = {
        "ok": False,
        "results": [{
            "name": "public_behavior",
            "returncode": 1,
            "timed_out": False,
            "executed": False,
            "failure_kind": "launch_error",
        }],
    }
    state["verification_claims"] = {
        "requirement:observable_behavior": {
            "atom_id": "requirement:observable_behavior",
            "status": "unverified",
        }
    }
    _Client.response = json.dumps({
        "verify_steps": [
            {
                "name": "replacement_behavior",
                "command": ["python", "tool.py"],
                "verifies": ["requirement:observable_behavior"],
                "basis": [{
                    "source": "task",
                    "quote": "Create a tool with the requested observable behavior.",
                }],
                "expected": "The requested public behavior is observable.",
            }
        ]
    })
    monkeypatch.setattr(behavior_review, "OpenAICompatClient", _Client)

    update = behavior_review.supplement_verification_steps(state)

    assert [step["name"] for step in update["added"]] == ["replacement_behavior"]


def test_verify_executes_file_plan_steps_and_records_artifacts(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    state.update({
        "failure": {"failure_type": "old_failure"},
        "failure_issues": [{"type": "old_failure"}],
        "failure_owner": "generated_test",
        "strategy_decision": {"strategy": "repair"},
        "repair_controller": {"status": "active"},
        "force_repair_action": {"allowed_tools": ["edit_file"]},
    })
    run_dir = Path(state["run_dir"])
    run_dir.mkdir(parents=True)
    (tmp_path / "tool.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "path = Path(sys.argv[1])\n"
        "path.parent.mkdir(parents=True, exist_ok=True)\n"
        "path.write_text('observable result', encoding='utf-8')\n"
        "print('observable result')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("coding_agent.nodes.verify.supplement_verification_steps", lambda _state: {"added": [], "missing": []})
    monkeypatch.setattr(
        "coding_agent.nodes.verify.review_behavior_evidence",
        lambda _state, _results, _artifacts: {
            "requirement:observable_behavior": {
                "atom_id": "requirement:observable_behavior",
                "status": "passed",
                "cited_steps": ["public_behavior"],
                "evidence": ["observable result"],
                "reason": "executed public behavior",
            }
        },
    )

    out = verify_node(state)

    assert out["verification"]["ok"] is True
    assert any(result["name"] == "public_behavior" for result in out["verification"]["results"])
    assert out["verification_artifacts"][0]["path"] == "result.txt"
    assert "observable result" in out["verification_artifacts"][0]["preview"]
    assert out["requirement_atom_summary"]["required_unverified"] == 0
    assert out["failure"] is None
    assert out["failure_issues"] == []
    assert out["failure_owner"] is None
    assert out["strategy_decision"] is None
    assert out["repair_controller"] is None
    assert out["force_repair_action"] is None


def test_verify_runs_disposable_sandbox_without_mutating_workspace(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    run_dir = Path(state["run_dir"])
    run_dir.mkdir(parents=True)
    (tmp_path / "marker.txt").write_text("present in real workspace", encoding="utf-8")
    (tmp_path / "tool.py").write_text(
        "from pathlib import Path\n"
        "if Path('marker.txt').exists():\n"
        "    raise SystemExit('marker unexpectedly present')\n"
        "print('isolated fallback path')\n",
        encoding="utf-8",
    )
    state["file_plan"]["verify_steps"][0] = {
        "name": "isolated_behavior",
        "command": ["python", "tool.py"],
        "verifies": ["requirement:observable_behavior"],
        "timeout_sec": 30,
        "sandbox": {
            "copy_paths": ["tool.py", "marker.txt"],
            "omit_paths": ["marker.txt"],
            "files": [],
        },
    }
    monkeypatch.setattr("coding_agent.nodes.verify.supplement_verification_steps", lambda _state: {"added": [], "missing": []})
    monkeypatch.setattr(
        "coding_agent.nodes.verify.review_behavior_evidence",
        lambda _state, _results, _artifacts: {
            "requirement:observable_behavior": {
                "atom_id": "requirement:observable_behavior",
                "status": "passed",
                "cited_steps": ["isolated_behavior"],
                "cited_runtime": [],
                "evidence": ["isolated fallback path"],
                "reason": "the disposable scenario executed",
            }
        },
    )

    out = verify_node(state)

    result = next(item for item in out["verification"]["results"] if item["name"] == "isolated_behavior")
    assert result["returncode"] == 0
    assert "isolated fallback path" in result["stdout"]
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "present in real workspace"
    sandbox = Path(out["verification_step_workspaces"]["isolated_behavior"])
    assert sandbox.is_dir()
    assert not (sandbox / "marker.txt").exists()


def test_verify_sandbox_can_import_project_code_without_copying_it(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    Path(state["run_dir"]).mkdir(parents=True)
    (tmp_path / "local_api.py").write_text("VALUE = 'workspace-code'\n", encoding="utf-8")
    state["file_plan"]["verify_steps"][0] = {
        "name": "isolated_import",
        "command": ["python", "probe.py"],
        "verifies": ["requirement:observable_behavior"],
        "timeout_sec": 30,
        "sandbox": {
            "copy_paths": [],
            "omit_paths": [],
            "files": [{
                "path": "probe.py",
                "content": "from local_api import VALUE\nassert VALUE == 'workspace-code'\n",
            }],
        },
    }
    monkeypatch.setattr(
        "coding_agent.nodes.verify.supplement_verification_steps",
        lambda _state: {"added": [], "missing": []},
    )
    monkeypatch.setattr(
        "coding_agent.nodes.verify.review_behavior_evidence",
        lambda _state, _results, _artifacts: {
            "requirement:observable_behavior": {
                "atom_id": "requirement:observable_behavior",
                "status": "passed",
                "cited_steps": ["isolated_import"],
                "cited_runtime": [],
                "evidence": ["project module imported from the workspace"],
                "reason": "the isolated probe executed",
            }
        },
    )

    out = verify_node(state)

    result = next(item for item in out["verification"]["results"] if item["name"] == "isolated_import")
    assert result["returncode"] == 0
    sandbox = Path(out["verification_step_workspaces"]["isolated_import"])
    assert not (sandbox / "local_api.py").exists()


def test_sandbox_verification_dir_exists_and_only_outputs_are_collected(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    Path(state["run_dir"]).mkdir(parents=True)
    (tmp_path / "tool.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text('sandbox output', encoding='utf-8')\n",
        encoding="utf-8",
    )
    state["file_plan"]["verify_steps"][0]["sandbox"] = {
        "copy_paths": ["tool.py"],
        "files": [{"path": "fixture.txt", "content": "not an output"}],
        "omit_paths": [],
    }
    monkeypatch.setattr("coding_agent.nodes.verify.supplement_verification_steps", lambda _state: {"added": [], "missing": []})
    monkeypatch.setattr(
        "coding_agent.nodes.verify.review_behavior_evidence",
        lambda _state, _results, _artifacts: {
            "requirement:observable_behavior": {
                "atom_id": "requirement:observable_behavior",
                "status": "passed",
                "cited_steps": ["public_behavior"],
                "evidence": ["sandbox output"],
                "reason": "sandbox output was captured",
            }
        },
    )

    out = verify_node(state)

    result = next(item for item in out["verification"]["results"] if item["name"] == "public_behavior")
    assert result["returncode"] == 0
    paths = [item["path"] for item in out["verification_artifacts"]]
    assert "sandboxes/public_behavior/.verification/result.txt" in paths
    assert not any(path.endswith("fixture.txt") for path in paths)


def test_verify_executes_same_argv_for_normal_and_sandbox_scenarios(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    second_atom = {
        "id": "requirement:fallback_behavior",
        "type": "behavior",
        "description": "The alternate workspace state remains supported.",
        "required": True,
        "status": "pending",
    }
    state["task_contract"]["requirement_atoms"].append(second_atom)
    state["file_plan"]["verify_steps"] = [
        {
            "name": "normal_behavior",
            "command": ["python", "tool.py"],
            "verifies": ["requirement:observable_behavior"],
        },
        {
            "name": "fallback_behavior",
            "command": ["python", "tool.py"],
            "verifies": [second_atom["id"]],
            "sandbox": {"copy_paths": ["tool.py"], "files": [{"path": "mode.txt", "content": "fallback"}]},
        },
    ]
    Path(state["run_dir"]).mkdir(parents=True)
    (tmp_path / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr("coding_agent.nodes.verify.supplement_verification_steps", lambda _state: {"added": [], "missing": []})
    monkeypatch.setattr(
        "coding_agent.nodes.verify.review_behavior_evidence",
        lambda _state, _results, _artifacts: {
            "requirement:observable_behavior": {
                "atom_id": "requirement:observable_behavior",
                "status": "passed",
                "cited_steps": ["normal_behavior"],
                "cited_runtime": [],
                "evidence": ["ok"],
                "reason": "normal scenario executed",
            },
            second_atom["id"]: {
                "atom_id": second_atom["id"],
                "status": "passed",
                "cited_steps": ["fallback_behavior"],
                "cited_runtime": [],
                "evidence": ["ok"],
                "reason": "sandbox scenario executed",
            },
        },
    )

    out = verify_node(state)

    names = [result["name"] for result in out["verification"]["results"]]
    assert "normal_behavior" in names
    assert "fallback_behavior" in names
    assert out["verification_plan_update"]["unexecuted_planned_steps"] == []
    assert set(out["verification_plan_update"]["executed_requirement_ids"]) == {
        "requirement:observable_behavior",
        second_atom["id"],
    }


def test_verify_accepts_documented_nonzero_success_exit_code(monkeypatch, tmp_path: Path):
    state = _behavior_state(tmp_path)
    Path(state["run_dir"]).mkdir(parents=True)
    (tmp_path / "tool.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
    state["file_plan"]["verify_steps"] = [{
        "name": "invalid_input_contract",
        "command": ["python", "tool.py"],
        "verifies": ["requirement:observable_behavior"],
        "success_exit_codes": [2],
    }]
    monkeypatch.setattr("coding_agent.nodes.verify.supplement_verification_steps", lambda _state: {"added": [], "missing": []})
    monkeypatch.setattr(
        "coding_agent.nodes.verify.review_behavior_evidence",
        lambda _state, _results, _artifacts: {
            "requirement:observable_behavior": {
                "atom_id": "requirement:observable_behavior",
                "status": "passed",
                "cited_steps": ["invalid_input_contract"],
                "evidence": ["actual exit code 2"],
                "reason": "the documented status was observed",
            }
        },
    )

    out = verify_node(state)
    result = next(item for item in out["verification"]["results"] if item["name"] == "invalid_input_contract")

    assert result["returncode"] == 0
    assert result["actual_returncode"] == 2
    assert result["success_exit_codes"] == [2]
    assert out["verification"]["ok"] is True
