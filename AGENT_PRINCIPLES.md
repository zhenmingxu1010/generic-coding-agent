# Agent Principles

## Primary Non-Negotiable Principle

This is a general-purpose coding agent. The core runtime must never add
project-specific, benchmark-specific, or current-test-specific patches just to
make one regression case pass.

Examples of forbidden behavior:

- Adding domain-, benchmark-, fixture-, or customer-specific logic to the core
  runtime.
- Adding hardcoded compatibility functions for one generated script or one test
  name.
- Adding fixed file layouts such as `main.py`, `core.py`, `scripts/`, `tests/`,
  or `experiments/` unless the user task, the existing project, or the LLM file
  plan requires them.
- Treating regression tests as the product objective. Tests are evidence; the
  objective is real general coding usefulness.

Allowed core logic:

- Generic filesystem safety: prevent path traversal, accidental writes to agent
  runtime directories, and unauthorized source modifications.
- Generic execution safety: detect failed commands, timeouts, pytest collection
  failures, and repeated no-progress actions.
- Generic structured interfaces: tool schemas, file plans, requirement atoms,
  trace records, audit export, token usage accounting, and final gates.
- Generic semantic checks that are derived from the user task or LLM-generated
  contract, not from a fixed project or benchmark.
- Task-specific verification steps generated from the current requirement
  contract, provided they execute through the generic command safety layer and
  store disposable outputs outside user deliverables.
- Runtime-observed constraints and command-executed behavior are separate
  evidence channels. VCS commands do not replace the runtime's own write-scope
  and artifact records.
- Disposable scenarios may copy selected files and create fixtures only under
  the internal test root; they never mutate project inputs.

Success is evidence-based: compilation, `--help`, source inspection, or an LLM
claim alone cannot prove functional behavior. Every required behavior must be
bound to an executed verification step and grounded in its observable result.
Task-specific verification must cite exact task or repository evidence. A
generated verification scenario is not a requirement: unsupported or ambiguous
scenarios may be replaced, but their failure must never authorize expanding the
user-visible implementation.
Repair read caching is scoped by failure, file version, and line coverage so an
unread section remains available while repeated reads are blocked.

When a test fails, the first question must be:

> Does this failure reveal a general agent capability problem, or is the test
> asking the core runtime to know something project-specific?

If it is project-specific, fix the test, prompt, fixture, or LLM planning path.
Do not encode that project knowledge into the core runtime.

## Current Runtime Layout

- User deliverables are written to the selected workspace only when the task
  allows source/project writes.
- Agent-generated verification tests for existing-project tasks are stored under
  `.coding_agent_test/<thread-id>`.
- Agent run records, traces, messages, context packs, patches, and audit state
  are stored under `.agent_runs` in the agent repository or `AGENT_RUNS_DIR`.
- The agent must not use legacy `.coding_agent/<thread-id>/work` as a write
  target for new runs.
