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

## Case schema

`real_world_case_v2` adds evaluator-only metadata without exposing historical
source fixes to the Agent:

- `categories` supports per-class reporting such as `multi-file`,
  `api-migration`, or `ambiguous-issue`;
- `expected_change_shape` is `localized`, `multi_file`, or `either`; and
- `hidden_test_paths` may contain multiple historical test paths; and
- `test_environment` may declare non-sensitive test controls such as disabling
  historical network tests.

The runner remains compatible with `real_world_case_v1`. Change-shape metadata
is descriptive and does not force the Agent to reproduce the historical source
patch; hidden behavior remains the acceptance authority.

The runner prepends the target interpreter's virtual-environment directory to
`PATH`, supplies an isolated `HOME`, disables the user site, and rejects case
attempts to override `HOME`, `PATH`, Python import paths, or `AGENT_*`
configuration.

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

## Sanitized metrics

Collect one or more raw results:

```bash
python -m evaluations.real_world.collect_results \
  .agent_runs/real-world/*.json \
  --case-dir evaluations/real_world/cases \
  --run-date YYYY-MM-DD \
  --scope-note "Describe exactly what this sample does and does not prove." \
  --output evaluations/real_world/summary.json
```

`real_world_summary_v2` reports:

- evaluator-owned external acceptance;
- Agent success claims and final-gate true/false positives/negatives;
- success-claim precision and recall;
- protected test mutations;
- changed project files, top-level areas, and multi-file changes;
- duration, model calls, and reported token usage; and
- category-level resolution.

Environment-unreachable cases remain visible but are excluded from resolution
and final-gate rate denominators.
