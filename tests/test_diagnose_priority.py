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


def test_diagnose_ignores_raw_output_from_normalized_baseline_pass(tmp_path):
    state = {
        "workspace": str(tmp_path),
        "trace_path": str(tmp_path / "trace.jsonl"),
        "state_snapshot_path": str(tmp_path / "state.json"),
        "verification": {
            "results": [
                {
                    "name": "pytest",
                    "command": ["python", "-m", "pytest"],
                    "returncode": 0,
                    "stdout": "tests/test_old.py FAILED\nAssertionError: baseline mismatch\n",
                    "stderr": "",
                },
                {
                    "name": "requested_behavior",
                    "command": ["python", "probe.py"],
                    "returncode": 1,
                    "stdout": "ERROR: required exception was not raised\n",
                    "stderr": "",
                },
            ]
        },
        "contract_check": {
            "ok": False,
            "failures": ["required behavior failed"],
        },
    }

    out = diagnose_node(state)

    assert out["failure"]["failure_type"] == "contract_error"
    assert "baseline mismatch" not in out["failure"]["raw_excerpt"]
