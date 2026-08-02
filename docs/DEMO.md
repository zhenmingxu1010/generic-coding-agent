# Demo Guide

The README terminal summary is a compact rendering of
[`docs/assets/validated-demo.txt`](assets/validated-demo.txt). It contains no
private model configuration and is a visual summary, not a substitute for
rerunning the commands below.

This demo shows behavior rather than a prewritten success transcript. Use a
disposable workspace and a private model configuration.

## Three-minute path

1. Run `python -m coding_agent.check_llm`.
2. Start an empty workspace and ask `写个脚本`.
3. Show the controlled `clarification_required` result and zero source changes.
4. Resume the same thread with `统计指定文本文件的行数，并把整数结果打印到终端。`.
5. Show the generated script, direct execution evidence, required atoms, and
   `verified_ok`.
6. Export the audit and inspect `audit_manifest.json`.

Copyable commands:

```bash
mkdir -p /tmp/generic-agent-demo

coding-agent \
  --workspace /tmp/generic-agent-demo \
  --task "写个脚本" \
  --thread-id portfolio-demo \
  --clean-agent-state

coding-agent \
  --workspace /tmp/generic-agent-demo \
  --thread-id portfolio-demo \
  --resume \
  --clarification-answer "统计指定文本文件的行数，并把整数结果打印到终端。"

coding-agent-export-audit \
  --workspace /tmp/generic-agent-demo \
  --thread-id portfolio-demo \
  --out /tmp/portfolio-demo-audit.zip \
  --include-workspace
```

## Full capability demo

Render one or all model-backed scenarios:

```bash
python scripts/render_regression_matrix.py --case T09
python scripts/render_regression_matrix.py --all
python scripts/collect_regression_audits.py \
  --audit-dir .agent_runs/regression-audits \
  --strict
```

The canonical 2026-07-23 record passed all eleven detailed scenario validators.
See `docs/VALIDATION.md` for results and limitations. Before recording a public
GIF, use a clean terminal profile and verify that no endpoint, key, username,
or private workspace appears on screen.
