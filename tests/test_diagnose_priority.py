from coding_agent.nodes.diagnose import diagnose_node


def test_name_error_impl_beats_test_file(tmp_path):
    trace = tmp_path / "trace.jsonl"
    snap = tmp_path / "state.json"
    state = {
        "trace_path": str(trace),
        "state_snapshot_path": str(snap),
        "verification": {
            "results": [
                {
                    "name": "pytest",
                    "command": ["python", "-m", "pytest", "-q"],
                    "returncode": 1,
                    "stdout": "tests/test_cli.py::test_x FAILED\ncore.py:8: NameError: name 'Student' is not defined\n",
                    "stderr": "",
                }
            ]
        },
    }
    out = diagnose_node(state)
    assert out["failure"]["failure_type"] == "name_error_impl"
    assert out["failure"]["target_file"] == "core.py"
