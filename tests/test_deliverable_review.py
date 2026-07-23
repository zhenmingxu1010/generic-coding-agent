from __future__ import annotations

import json
from pathlib import Path

from coding_agent.graph import route_after_verify
import coding_agent.nodes.deliverable_review as review_module
from coding_agent.nodes.deliverable_review import (
    deliverable_review_needed,
    deliverable_review_node,
    route_after_deliverable_review,
)


class _Client:
    response = "{}"
    last_messages = []
    calls = []

    def __init__(self, *_args, **_kwargs):
        pass

    def chat(self, messages, **_kwargs):
        self.__class__.last_messages = messages
        self.__class__.calls.append(messages)
        return self.response


class _SequencedClient(_Client):
    responses = []
    calls = []

    def chat(self, messages, **kwargs):
        self.__class__.calls.append((messages, kwargs))
        return self.__class__.responses[len(self.__class__.calls) - 1]


def _state(tmp_path: Path) -> dict:
    run_dir = tmp_path / ".agent_runs" / "t"
    run_dir.mkdir(parents=True)
    (tmp_path / "tool.py").write_text("print(3)\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Running the tool prints 4.\n", encoding="utf-8")
    return {
        "workspace": str(tmp_path),
        "run_dir": str(run_dir),
        "thread_id": "t",
        "trace_path": str(run_dir / "trace.jsonl"),
        "state_snapshot_path": str(run_dir / "state.json"),
        "messages_path": str(run_dir / "messages.jsonl"),
        "task": "Create tool.py and document its observable output.",
        "task_contract": {"required_behaviors": ["documented output matches execution"]},
        "mode": "write",
        "read_only": False,
        "changed_files": ["tool.py", "README.md"],
        "verification": {"ok": True, "results": [{"name": "run", "returncode": 0, "stdout": "3\n"}]},
        "max_rounds": 6,
        "round_idx": 0,
    }


def test_verified_write_routes_to_one_final_deliverable_review(tmp_path: Path):
    state = _state(tmp_path)

    assert deliverable_review_needed(state) is True
    assert route_after_verify(state) == "deliverable_review"


def test_noop_modify_still_reviews_allowed_existing_targets(tmp_path: Path):
    state = _state(tmp_path)
    state["changed_files"] = []
    state["mode"] = "modify"
    state["scope_contract"] = {"allowed_modify_paths": ["tool.py"]}

    assert deliverable_review_needed(state) is True


def test_deliverable_review_turns_grounded_cross_file_contradiction_into_repair(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    _Client.response = json.dumps({
        "blocking_issues": [{
            "path": "README.md",
            "message": "documented output contradicts the implementation",
            "evidence": "README says 4 while tool.py prints 3",
        }],
        "warnings": [],
        "summary": "one concrete inconsistency",
    })
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    out = deliverable_review_node(state)

    assert out["deliverable_review"]["ok"] is False
    assert out["failure"]["failure_type"] == "deliverable_consistency_error"
    assert out["failure"]["target_file"] == "README.md"
    assert route_after_deliverable_review(out) == "repair"
    assert deliverable_review_needed(out) is False


def test_deliverable_review_drops_issues_for_paths_not_supplied(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    _Client.response = json.dumps({
        "blocking_issues": [{
            "path": "unrelated.py",
            "message": "invented issue",
            "evidence": "not in supplied files",
        }],
        "warnings": [],
        "summary": "invalid path",
    })
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    out = deliverable_review_node(state)

    assert out["deliverable_review"]["ok"] is True
    assert out["deliverable_review"]["blocking_issues"] == []
    assert route_after_deliverable_review(out) == "report"


def test_deliverable_review_drops_self_negating_blocking_issue(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    _Client.response = json.dumps({
        "blocking_issues": [{
            "path": "tool.py",
            "message": "The implementation is correct; no blocking issue.",
            "evidence": "No contradiction found.",
        }],
        "warnings": [],
        "summary": "No blocking issues found.",
    })
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    out = deliverable_review_node(state)

    assert out["deliverable_review"]["ok"] is True
    assert out["deliverable_review"]["blocking_issues"] == []


def test_deliverable_review_drops_self_negating_auxiliary_verb(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    _Client.response = json.dumps({
        "blocking_issues": [{
            "path": "tool.py",
            "message": "The output matches exactly, so no blocking issue is found.",
            "evidence": "The implementation correctly handles the required fallback case.",
        }],
        "warnings": [],
        "summary": "The implementation is correct.",
    })
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    out = deliverable_review_node(state)

    assert out["deliverable_review"]["ok"] is True
    assert out["deliverable_review"]["blocking_issues"] == []
    assert out.get("failure") is None


def test_deliverable_review_drops_long_self_negating_evidence(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    _Client.response = json.dumps({
        "blocking_issues": [{
            "path": "tool.py",
            "message": "Iteration might mutate the input.",
            "evidence": "The code only reads each item and never writes to the list. No blocking issue.",
        }],
        "warnings": [],
        "summary": "No blocking issues found.",
    })
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    out = deliverable_review_node(state)

    assert out["deliverable_review"]["ok"] is True


def test_deliverable_review_drops_self_negating_message(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    _Client.response = json.dumps({
        "blocking_issues": [{
            "path": "tool.py",
            "message": "The implementation correctly rejects the value. No contradiction found.",
            "evidence": "The guard raises the required exception.",
        }],
        "warnings": [],
        "summary": "No blocking issues found.",
    })
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    out = deliverable_review_node(state)

    assert out["deliverable_review"]["ok"] is True


def test_deliverable_review_drops_evidence_that_calls_itself_false_positive(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    _Client.response = json.dumps({
        "blocking_issues": [{
            "path": "tool.py",
            "message": "The required guard is missing.",
            "evidence": "The guard is present and correct; this is a false positive. No blocking issue found here.",
        }],
        "warnings": [],
        "summary": "No blocking issues found.",
    })
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    out = deliverable_review_node(state)

    assert out["deliverable_review"]["ok"] is True


def test_deliverable_review_receives_read_only_reference_contract(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    state["changed_files"] = ["tool.py"]
    state["scope_contract"] = {"read_reference_paths": ["README.md"]}
    _Client.response = json.dumps({"blocking_issues": [], "warnings": [], "summary": "ok"})
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    out = deliverable_review_node(state)
    prompt = _Client.last_messages[-1]["content"]

    assert "Referenced contract files" in prompt
    assert "Running the tool prints 4." in prompt
    assert out["deliverable_review"]["reviewed_reference_files"] == ["README.md"]


def test_reference_contract_review_omits_green_test_bias_and_verification_hints(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    state["changed_files"] = ["tool.py"]
    state["scope_contract"] = {"read_reference_paths": ["README.md"]}
    state["verification"] = {"ok": True, "results": [{"stdout": "999 tests passed"}]}
    state["task_contract"] = {"requirement_atoms": [{
        "id": "requirement:docs",
        "description": "README.md behavior works.",
        "evidence": ["documented behavior"],
        "verify_hint": "Green pytest proves the complete contract.",
    }]}
    _Client.response = json.dumps({"blocking_issues": [], "warnings": [], "summary": "ok"})
    _Client.calls = []
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    deliverable_review_node(state)
    prompts = "\n".join(call[-1]["content"] for call in _Client.calls)

    assert "Mandatory contract audit" in prompts
    assert "999 tests passed" not in prompts
    assert "Green pytest proves" not in prompts


def test_reference_review_receives_actual_custom_success_exit_code(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    state["changed_files"] = ["tool.py"]
    state["scope_contract"] = {"read_reference_paths": ["README.md"]}
    state["file_plan"] = {"verify_steps": [{
        "name": "invalid_input",
        "basis": [{"source": "README.md", "quote": "invalid input exits 2"}],
        "expected": "exit code 2",
        "success_exit_codes": [2],
    }]}
    state["verification"] = {"results": [{
        "name": "invalid_input",
        "returncode": 0,
        "actual_returncode": 2,
        "success_exit_codes": [2],
        "stderr": "error: invalid input",
    }]}
    _Client.response = json.dumps({"blocking_issues": [], "warnings": [], "summary": "ok"})
    _Client.calls = []
    monkeypatch.setattr(review_module, "OpenAICompatClient", _Client)

    deliverable_review_node(state)
    prompts = "\n".join(call[-1]["content"] for call in _Client.calls)

    assert '"actual_returncode": 2' in prompts
    assert '"success_exit_codes": [2]' in prompts


def test_clean_primary_contract_review_gets_independent_counterexample_pass(monkeypatch, tmp_path: Path):
    state = _state(tmp_path)
    state["mode"] = "modify"
    state["changed_files"] = []
    state["scope_contract"] = {
        "allowed_modify_paths": ["tool.py"],
        "read_reference_paths": ["README.md"],
    }
    (tmp_path / "README.md").write_text(
        "The input must be a JSON array. Invalid input exits with status 2.\n",
        encoding="utf-8",
    )
    (tmp_path / "tool.py").write_text(
        "def run(value):\n    return [item for item in value]\n",
        encoding="utf-8",
    )
    _SequencedClient.calls = []
    _SequencedClient.responses = [
        json.dumps({"blocking_issues": [], "warnings": [], "summary": "looks good"}),
        json.dumps({
            "blocking_issues": [{
                "path": "tool.py",
                "message": "the required outer JSON array type is not enforced",
                "evidence": "run iterates any value, so an empty object is accepted",
            }],
            "warnings": [],
            "summary": "counterexample found",
        }),
    ]
    monkeypatch.setattr(review_module, "OpenAICompatClient", _SequencedClient)

    out = deliverable_review_node(state)

    assert len(_SequencedClient.calls) == 2
    assert _SequencedClient.calls[1][1]["purpose"] == "deliverable_contract_counterexample"
    assert out["deliverable_review"]["ok"] is False
    assert out["failure"]["target_file"] == "tool.py"
    assert route_after_deliverable_review(out) == "repair"
