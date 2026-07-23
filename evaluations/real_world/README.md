# Real-world repair evaluation

This directory is an evaluation layer, not agent policy. Case-specific repository
names, commits, and test paths live only in case data. Nothing under
`coding_agent/` imports this package.

Each case pins a public buggy commit and its historical fixed commit. The runner:

1. creates a buggy baseline and applies only the historical test change;
2. requires that hidden test to fail;
3. checks that the same test passes at the historical fixed commit;
4. gives the agent a fresh buggy checkout without the hidden test;
5. rejects edits to protected tests; and
6. applies the hidden test after the agent stops and records the external result.

This prevents a passing environment, an LLM success claim, or a weakened test from
being counted as a repair.

Cases may also declare evaluator-owned `hidden_files`. They are installed only
in the baseline, historical fixed control, and post-agent acceptance workspace.
They are never present while the agent is working. This supports compatibility
checks that were missing from the original historical regression.

Example:

```bash
python -m evaluations.real_world.runner \
  --case evaluations/real_world/cases/pysnooper-file-output.json \
  --source-repo /path/to/full/PySnooper-clone \
  --work-root /path/to/disposable/evaluation-workspaces \
  --test-python /path/to/target-test-python \
  --agent-python .venv/bin/python \
  --agent-project . \
  --result .agent_runs/real-world/pysnooper-file-output.json
```

The source clone and target test environment are deliberately supplied by the
caller. Network access and dependency installation are outside the benchmark
runner, making environment failures explicit and keeping runs reproducible.
