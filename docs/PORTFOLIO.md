# Portfolio Notes

## Résumé bullets

- Built a general-purpose coding agent with LangGraph, structured tools,
  requirement decomposition, bounded repair loops, project memory, and
  resumable clarification.
- Designed an evidence-driven final gate that combines command execution,
  pytest/JUnit parsing, interface checks, artifact checks, and write-scope
  audits to prevent unsupported success claims.
- Added path/command guardrails, protected-file enforcement, internal test
  isolation, secret/path-redacted audit export, and a provider-neutral model
  configuration layer.
- Created an offline regression suite and an eleven-scenario real-model matrix
  covering analysis, generation, modification, negative verification,
  short-prompt defaults, clarification, memory, and source-pollution checks.
- Built an evaluator-owned real-repository harness that separates hidden
  acceptance from Agent success claims and reports final-gate precision,
  protected mutations, modification scope, latency, calls, and token usage.

Current public evidence: 536 offline tests, 11/11 model-backed scenario
validators, and 4/4 externally accepted historical repairs, including one
three-file change. The Agent claimed success in 3/4 cases and conservatively
rejected the accepted multi-file repair because direct verification evidence
remained incomplete. This pilot is too small to present as broad benchmark
performance.

## Five-minute interview structure

1. Problem: coding agents often confuse tool success with task success.
2. Design: LangGraph state machine; LLM semantics plus deterministic authority.
3. Core mechanism: dual contracts, requirement atoms, grounded verification,
   final gate.
4. Reliability: bounded diagnosis/repair, ownership, read cache, stale-state
   clearing, negative tests.
5. Evidence: offline suite, eleven live scenarios, sanitized audits.
6. Limits and roadmap: Python-first verification, in-memory LangGraph
   checkpoint, no OS sandbox, language adapters next.

## STAR example

Situation: model-generated tests and semantic reviewers sometimes created
false failures even after observable behavior passed.

Task: improve reliability without adding fixture-specific compatibility code.

Action: separated user and implementation contracts, grounded every scenario
to exact evidence, rejected requirements derived only from generated code,
validated self-negating reviewer output, bounded repair reads, and added
machine-readable detailed matrix checks.

Result: the canonical eleven-case matrix passed, including zero-test rejection,
protected-file checks, short-prompt clarification, and repair scenarios, while
the core remained domain- and provider-neutral.

## Honest positioning

Call this a release-candidate alpha and a substantial systems project, not a
production replacement for hosted coding agents. The strongest interview signal
is the reasoning about contracts, evidence, safety, evaluation, and failure
recovery—not the number of source files.
