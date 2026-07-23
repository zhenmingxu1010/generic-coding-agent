# Project Deep Dive

## What this project is

Generic Coding Agent is a stateful coding workflow. It understands a task and
repository, chooses a read/write/verify path, invokes structured tools, checks
observable behavior, repairs bounded failures, stores project memory, and emits
an auditable final decision.

The central design choice is that the model proposes and interprets, while the
runtime owns authority. The model may propose a file plan or verification
scenario; deterministic code decides whether a path is writable, a command is
allowed, evidence is grounded, retries remain in budget, and success is valid.

## Is it LangChain?

No LangChain chain or agent API is used directly. The workflow uses LangGraph's
`StateGraph` and in-process `MemorySaver`. `langchain-core` may appear in the
installed environment because LangGraph depends on it transitively. Model calls
go through this project's small OpenAI-compatible HTTP client, keeping provider
selection in configuration.

## Execution model

One typed `AgentState` moves through graph nodes. Important state categories
are:

- task understanding: task spec, mode, completeness decision, assumptions;
- authority: read-only policy, scope contract, write intents, protected paths;
- context: repository map, evidence blocks, compressed context, project memory;
- action: file plan, decision, tool result, changed files, patch history;
- evidence: verification results, pytest report, requirement claims, interface
  checks, deliverable review;
- recovery: failure type, owner, read cache, repair controller, round budgets;
- outcome: final gate, stopped reason, controlled failure, audit paths.

The graph is deliberately hybrid. LLM nodes solve semantic tasks that are hard
to encode generically. Runtime nodes and routers enforce invariants that should
not depend on model obedience.

## Dual contracts

The user contract contains only requirements grounded in exact task evidence.
The implementation contract contains safe defaults introduced by the agent,
such as a representative execution check for an underspecified short prompt.
Keeping them separate prevents an implementation preference from being
misreported as a user demand.

Requirements become atoms:

- `artifact_exists` — a requested file or directory exists;
- `behavior` — an observable command or test result proves behavior;
- `constraint` — a runtime fact such as write scope holds;
- `quality` — a grounded quality condition passes review.

Required failed or unverified atoms block unconditional success.

## Verification and repair

Verification plans cite the task or repository evidence that authorizes each
scenario. The runtime rejects invented options and scenarios grounded only in
newly generated implementation code. It executes commands, parses pytest JUnit
results, captures artifacts, and asks an evidence reviewer to bind observations
to exact atom IDs.

Failures are decomposed and assigned to implementation, generated test,
verification evidence, policy, or unknown ownership. The repair controller
locks write targets, supplies exact target contents, permits bounded relevant
reads, caches read ranges, and stops repeated no-progress behavior. A successful
post-repair verification explicitly clears stale failure state before the final
gate.

## Memory

LangGraph's `MemorySaver` supports a running process. Project-owned JSON
snapshots and memory below `.agent_runs` support CLI resume across processes.
The memory system stores repository profile, task summaries, evidence context,
reflections, traces, and artifact provenance. Clarification resume preserves
the original prompt and answer history, then rebuilds derived contracts.

This is memory for task continuity, not autonomous long-term identity. There
are no hidden cloud databases or vector services.

## Safety model

The Agent validates workspace-relative paths, write intent, protected files,
shell argv, test locations, and source-change audits. It blocks shell chaining,
redirection, inline shell interpreters, and paths outside the workspace. These
are application-level guardrails, not an operating-system sandbox. Untrusted
repositories should still run in a disposable container with minimal secrets
and permissions.

## Evaluation method

The offline suite validates deterministic invariants without an API. The
eleven-case live matrix covers read-only understanding, isolated generation,
CLI semantics, interface repair, zero-test rejection, source pollution,
existing bug repair, greenfield generation, short prompts, clarification, and
colloquial inspection. Audit bundles preserve final state, traces, messages,
patches, and optional workspace evidence after path/secret redaction.

## Main trade-offs

- A graph is more explicit and testable than an open-ended ReAct loop, but
  introduces more state and routing code.
- Direct model access reduces framework coupling, but the project owns retry,
  parsing, configuration, and token accounting.
- Evidence gates reduce false success, but add calls and can expose reviewer
  output errors that must themselves be validated.
- Python-first verification gives depth today; language adapters are required
  for equally strong cross-language support.
