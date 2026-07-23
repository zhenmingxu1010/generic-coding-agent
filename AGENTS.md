# Coding Agent Repository Instructions

## Project Identity

This repository implements a general-purpose coding and chat agent. It must be
able to understand repositories, modify existing code, create new projects,
verify behavior, repair failures, preserve context, expose a terminal UI, and
produce audit evidence.

The current source of truth is this `coding_agent_clean` repository. Older
version directories are historical evidence only.

## Permanent Rule

Never add project-specific, benchmark-specific, current-test-specific, data-
format-specific, file-name-specific, or error-message-specific logic to the
core runtime merely to pass a regression case.

Do not restore old compatibility branches or legacy output layouts unless a
currently supported public contract actually requires them.

When a test fails, determine whether it exposes:

1. a general runtime defect;
2. an invalid or outdated test assumption;
3. an LLM planning/generation failure;
4. missing execution evidence;
5. an environment problem.

Fix the correct layer. Do not make the implementation imitate one fixture.

## LLM And Runtime Responsibilities

- LLM: understand intent, inspect project evidence, plan files, generate code,
  interpret ambiguous failures, propose task-specific verification.
- Runtime: enforce path and command safety, validate tool schemas, record state,
  cap rounds and repair calls, execute verification, detect no progress, and
  decide final success from evidence.
- Generic safety may be deterministic. Project semantics must come from the
  user task, repository evidence, or the current LLM-generated contract.

## Current Layout

- User deliverables go to the selected workspace when the interpreted task
  permits source writes.
- Agent-owned tests are disabled by default. When explicitly enabled, they go
  under `.coding_agent_test/<thread-id>`.
- Run records, project memory, chat sessions, traces, messages, context packs,
  patches, and final reports go under `AGENT_RUNS_DIR` or the repository
  `.agent_runs` directory.
- New runs must not write to legacy `.coding_agent/<thread-id>/work`.
- Do not use `/tmp` as the default work or audit location.

## Engineering Workflow

- Read the current implementation before proposing structural changes.
- Prefer removing conflicting or obsolete mechanisms over adding another
  compatibility layer.
- Keep changes scoped and add tests for generic invariants.
- Do not generate user-visible tests unless the user requests them.
- Do not weaken existing project tests to hide an implementation bug.
- Stop boundedly when no progress is possible; do not consume repeated LLM
  calls reading the same unchanged file.
- Preserve executable evidence, traceback details, file ownership, and write
  scope in audits.

## Communication Preferences

- Respond in Chinese when the user writes Chinese.
- Commands given to the user must be complete and directly copyable.
- Pair each agent test command with its audit export command.
- Store audit zips in the configured audit directory.
- Explain root causes, not only surface symptoms.
- Be explicit about token use and avoid unnecessary LLM reruns.

## Verification

- Compilation or an LLM claim alone does not prove behavior.
- Required behavior must be grounded in an executed command and observable
  output.
- `pytest` with zero collected tests is not success.
- A generated scenario unsupported by the user task or repository evidence
  must not force public implementation changes.
- Final reports should summarize the result, changed files, verification
  commands/output, artifact paths, and token usage for humans.
