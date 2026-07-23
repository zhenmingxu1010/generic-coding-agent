# Architecture

## Overview

Generic Coding Agent is a stateful workflow built directly on LangGraph. A
single `AgentState` travels through deterministic routing functions and node
functions. LLM calls propose intent, plans, actions, reviews, and repairs;
runtime code owns scope enforcement, tool execution, evidence collection,
budgets, and the final decision.

The project does not directly import or declare LangChain. LangGraph may install
`langchain-core` transitively. `coding_agent/graph.py` imports `StateGraph`,
`START`, `END`, and `MemorySaver` from LangGraph, while
`coding_agent/core/llm_client.py` implements OpenAI-compatible HTTP requests.

## Main workflow

1. `intake` parses the task, operation mode, invariants, and scope.
2. `supervisor` selects the high-level execution path.
3. `repo_scan` builds a bounded repository map and workspace baseline.
4. `task_clarify` separates repository-discoverable details, safe defaults,
   and missing core behavior. It either records assumptions and continues or
   emits a resumable `clarification_required` result before source writes.
5. `context_retrieve` and `context_compress` select evidence for the next step.
6. Read-only tasks use `analyze_repo` and `analyze_report`.
7. Write tasks use `plan`, `file_plan`, `generate_files`, `act`, and
   `tool_exec`.
8. `verify` executes grounded verification steps and binds results to required
   behaviors.
9. Failed verification flows through `diagnose`, `failure_owner`, and `repair`.
10. `deliverable_review` checks artifacts that require semantic review.
11. `report` computes the final gate and persists the human and machine-readable
    outcome.

Routing code prevents unrestricted loops with action, round, repair-call,
repeated-failure, and context budgets.

## Package responsibilities

| Package | Responsibility |
| --- | --- |
| `contracts` | User contracts, implementation defaults, completeness, requirement atoms |
| `scope` | Intent, read-only policy, write scope, scope audit |
| `workspace` | Repository map, baseline, artifact registry, interfaces |
| `tools` | Structured file, shell, Python, and pytest operations |
| `verification` | Verification plans, claims, test policy, evidence review |
| `repair` | Failure parsing, ownership, read cache, repair controller |
| `memory` | Project profile, retrieval, context packs, traces, snapshots |
| `safety` | Path and command guards |
| `nodes` | LangGraph node implementations |
| `ux` | Terminal UI, reports, language, token accounting |

## Contracts and requirement atoms

The task is decomposed into required and optional atoms. Each atom has a type
such as artifact, behavior, constraint, or quality. Verification records claims
and execution evidence against these atoms. A required atom that is failed or
unverified prevents an unconditional success result.

This separation is important: the model can propose what should be checked,
but runtime evidence determines whether it was checked successfully.

User requirements and agent implementation defaults are separate contracts.
Hard user requirements require exact prompt evidence. Low-risk missing details
such as a conventional language or output layout are recorded as assumptions,
and their generic runnability checks use `implementation:*` atoms. Missing
information that changes the core behavior is never silently defaulted.

## Tool execution

Tools implement a shared structured result interface. The registry normalizes
arguments and returns machine-readable success, failure, policy, and change
metadata. File-changing tools are followed by a new repository scan and
verification obligation.

Shell access is filtered by command policy and remains inside the workspace
contract. Verification supports bounded standard input and direct
`sh relative/script.sh ...`/`bash relative/script.sh ...` execution, while
inline shell commands, interpreter options, redirection, and command chaining
remain blocked. It is still not equivalent to an operating-system sandbox.

## Memory and persistence

`MemorySaver` provides an in-process LangGraph checkpointer. Separately, the
agent writes project memory, context packs, traces, and state snapshots below
`.agent_runs/<workspace-key>/`. The CLI can resume from the agent-owned
snapshot after a process restart. Clarification answers rebuild derived task
contracts while preserving the original prompt and clarification history.

## Verification and final gate

Verification combines command outcomes, pytest result parsing, artifact
existence, interface checks, write-scope evidence, and LLM semantic review where
deterministic checks are insufficient. The final gate distinguishes:

- runtime execution success;
- contract completeness;
- required behavior evidence;
- write-scope compliance;
- controlled failures and warnings.

The goal is not to guarantee correctness. The goal is to avoid claiming
success when the available evidence does not support it.
