# Capability Regression Matrix

The matrix defines eleven provider-neutral end-to-end scenarios for Generic
Coding Agent. It complements the offline unit suite by exercising complete LLM
workflows against disposable fixture projects.

| ID | Capability | Main assertion |
| --- | --- | --- |
| T01 | Read-only understanding | Evidence-based report with no workspace writes |
| T02 | Isolated generation | New deliverable without modifying existing files |
| T03 | CLI behavior | Arguments change real input and output behavior |
| T04 | Interface repair | Existing tests drive a bounded implementation repair |
| T05 | Zero-test rejection | No collected tests cannot produce success |
| T06 | Pollution prevention | Internal tests stay outside user deliverables |
| T07 | Existing-project fix | Source changes pass preserved project tests |
| T08 | Greenfield generation | Runnable CLI, documentation, and tests from scratch |
| T09 | Short prompt defaults | One-line task proceeds with explicit assumptions |
| T10 | Ambiguity stop | Missing core behavior pauses before source writes |
| T11 | Colloquial inspection | Conversational request routes to read-only analysis |

## Usage

Configure an OpenAI-compatible model first, then render one case:

```bash
python scripts/render_regression_matrix.py --case T01
```

Render every case:

```bash
python scripts/render_regression_matrix.py --all
```

All default workspaces and audit bundles are created below `.agent_runs/`, which
is ignored by Git. Override the locations when needed:

```bash
python scripts/render_regression_matrix.py \
  --all \
  --agent-repo . \
  --audit-dir /tmp/coding-agent-audits \
  --work-root /tmp/coding-agent-regression
```

Each case contains human-readable `expected_conditions`. T05 is intentionally
negative: a controlled failure is the expected result.
