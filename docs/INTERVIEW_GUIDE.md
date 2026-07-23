# Coding Agent Interview Guide

## A 90-second explanation

I built a general coding agent on LangGraph. Instead of allowing the model to
declare success, I decompose tasks into requirement atoms and require grounded
execution evidence. The LLM handles semantic work such as intent, planning,
code generation, and failure interpretation; deterministic runtime code owns
write scope, command safety, retry budgets, pytest parsing, and the final gate.
The system supports read-only analysis, project generation, existing-code
repair, short-prompt defaults, clarification/resume, project memory, and
sanitized audit export. I validate it with an offline test suite and an
eleven-scenario real-model matrix, including negative cases such as zero tests
and ambiguous prompts.

## Common Agent questions

### What is an Agent?

An Agent is an LLM-centered system that observes state, chooses actions, uses
tools, receives feedback, and iterates toward a goal. A workflow has mostly
predefined transitions; an autonomous agent chooses more of the path. This
project is a controlled hybrid: the graph fixes safety-critical phases while
the model chooses task-specific actions inside them.

### ReAct, planning, and reflection

ReAct interleaves reasoning summaries and actions. Planning separates a larger
goal into steps before acting. Reflection reviews a failure or lack of progress
and changes strategy. In production, all three need budgets and observable
state; unlimited self-reflection usually increases cost rather than reliability.

### Why LangGraph?

LangGraph makes state, branches, cycles, and checkpoints explicit. That fits a
coding agent with analyze/write/verify/repair paths and bounded loops. A simple
chain is easier for linear prompts; a hand-written loop can be smaller, but is
harder to inspect once recovery paths multiply.

### Tools and function calling

A tool should have a narrow schema, validated arguments, a structured result,
timeouts, and explicit side effects. Tool success is not task success. For
example, writing a file successfully creates a verification obligation; a
pytest process returning without tests is still failure.

### State, checkpoint, and idempotency

State is the explicit information required for the next decision. A checkpoint
persists that state so work can resume. Idempotent actions can be safely
repeated; file edits usually are not, so the runtime records hashes, patches,
and repeated actions. This project uses in-memory LangGraph checkpoints plus
project-owned snapshots for cross-process resume.

### Short-term and long-term memory

Short-term memory is current task context and action history. Long-term memory
stores reusable project facts or prior outcomes. Memory needs relevance,
retention, migration, deletion, and poisoning controls. More memory is not
automatically better; stale context can reduce accuracy.

### Context engineering and RAG

RAG retrieves external evidence before generation. Coding agents need
repository-aware retrieval: file paths, symbols, tests, errors, and changed
artifacts. Context engineering also includes compression, ordering, provenance,
and token budgets. This project uses bounded file evidence and structured
compression rather than a vector database.

### Guardrails and sandboxing

Guardrails validate model requests and outputs. Sandboxing isolates process and
filesystem capabilities at the OS/container layer. Path and command checks are
useful defense in depth but are not a sandbox. A strong answer distinguishes
prompt rules, runtime policy, and infrastructure isolation.

### Evaluation

Evaluate both outcomes and process invariants:

- task success and required behavior;
- unauthorized writes and unsafe commands;
- test collection and execution evidence;
- recovery rate, rounds, calls, latency, and tokens;
- false-success and false-failure rates;
- reproducibility across models and repositories.

Offline deterministic tests and model-backed scenario tests serve different
purposes. Negative cases are essential because a system can look strong while
silently treating “0 tests” or “I wrote the file” as success.

### Hallucination and grounding

Grounding connects a claim to authoritative task or repository evidence.
Generated code cannot create a new user requirement. Verification commands need
citations and observable expectations, and reviewer output needs deterministic
validation because reviewer models can also hallucinate.

### Multi-agent systems

Multiple agents help when roles can operate independently with clear handoff
contracts. They also multiply coordination errors, context cost, and duplicated
work. This project uses specialized nodes in one state graph; that is easier to
audit than multiple autonomous agents for the current scope.

### MCP

Model Context Protocol standardizes how models discover and call external tools
and resources. It can replace custom integration plumbing, but does not solve
authorization, output validation, retries, or task-level correctness by itself.

## Project-specific follow-ups

- Why not pure LangChain AgentExecutor? Explicit branches and final gates are
  central here; a generic executor would hide too much control flow.
- Why use an LLM evidence reviewer? Some behavior is semantic, but its claims
  are accepted only when cited steps and runtime sources are valid.
- Biggest bug found by evaluation? Successful verification once retained stale
  failure state during graph merging; the final gate correctly rejected it.
- How is overfitting prevented? Core rules must be provider-, repository-, and
  benchmark-neutral; case-specific expectations stay in the regression harness.
- What would you build next? Persistent checkpoint storage, language-neutral
  verification adapters, container isolation, and cross-model evaluation.
