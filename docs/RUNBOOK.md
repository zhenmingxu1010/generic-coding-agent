# Runbook

## Create a development environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

## Configure a model

```bash
cp configs/model.example.yaml configs/model.local.yaml
```

Set `base_url`, `api_key`, and `model` in the local file. Environment variables
with the `AGENT_LLM_` prefix override file configuration.

```bash
python -m coding_agent.check_llm
```

## Run modes

Read-only analysis:

```bash
coding-agent --workspace ./project \
  --task "Analyze this repository without modifying it." \
  --thread-id analysis --clean-agent-state
```

Generate a new project in an empty workspace:

```bash
mkdir -p ./scratch-project
coding-agent --workspace ./scratch-project \
  --task "Create a tested Python CLI that converts JSON Lines to CSV." \
  --thread-id generate --clean-agent-state
```

Repair an existing project:

```bash
coding-agent --workspace ./project --repair-existing \
  --thread-id repair --clean-agent-state
```

Resume a stopped run:

```bash
coding-agent --workspace ./project --thread-id repair --resume
```

Answer a pending clarification and resume the same run:

```bash
coding-agent --workspace ./project --thread-id short-task --resume \
  --clarification-answer "Read a text-file path and print its line count."
```

For a longer answer, use a UTF-8 file:

```bash
coding-agent --workspace ./project --thread-id short-task --resume \
  --clarification-file ./clarification.txt
```

## Inspect a result

```bash
python -m json.tool ./project/.agent_runs/repair/final.json
tail -n 20 ./project/.agent_runs/repair/trace.jsonl
```

`final.json` is the authoritative summary. Check `final.ok`, `stopped_reason`,
`final_gate_status`, `requirement_atom_summary`, `verification`, and
`write_scope_audit`.

`clarification_required` is an expected controlled outcome, not a runtime
crash. Inspect `clarification_questions`, answer only the decision-changing
gap, and resume with the same workspace and thread ID.

## Export an audit

```bash
coding-agent-export-audit \
  --workspace ./project \
  --thread-id repair \
  --out ./repair-audit.zip
```

Add `--include-workspace` only when the source can safely be shared.
Text audit files have common token patterns and local absolute paths redacted;
binary files and unusual secret formats still require manual review. Check
`audit_manifest.json` before publishing a bundle.

## Local quality checks

```bash
python -m compileall -q coding_agent tests
python -m pytest -q
python -m build
```

## Troubleshooting

- Connection failure: verify the base URL ends at the API root, commonly `/v1`,
  and run `python -m coding_agent.check_llm`.
- Wrong model: set `AGENT_LLM_MODEL` explicitly or disable `auto_model`.
- Empty model content: disable provider thinking mode in local configuration if
  the provider separates reasoning from the final message.
- Unexpected policy block: inspect `trace.jsonl` and `write_scope_audit` before
  broadening permissions.
- Repeated verification failure: inspect the exact executed commands and
  failure fingerprint; do not increase budgets before understanding the loop.
- A direct shell-script verification may use `sh relative/script.sh ...`, but
  `sh -c`, `bash -lc`, redirection, and command chaining are intentionally
  rejected.
