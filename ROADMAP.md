# Roadmap

Generic Coding Agent is intentionally alpha software. Roadmap items are framed
as independently reviewable issues so external contributors can participate.

## v0.1 alpha hardening

- Persist LangGraph checkpoints across processes instead of relying only on
  JSON state snapshots.
- Publish the complete eleven-case model-backed regression summary and selected
  sanitized audits under the exact release commit.
- Add deterministic tests for multiple clarification rounds in the interactive
  terminal UI.

Completed for the public alpha: Python 3.10–3.13 CI, wheel/sdist release assets,
495 offline tests, the eleven-case validation record, and a three-case
real-repository pilot.

## v0.2 real-world evidence

- Expand the public repair set from three localized cases to at least ten
  eligible cases.
- Include multi-file repair, ambiguous issue text, dependency/API migration,
  and a repository with a larger preserved test suite.
- Report external resolution separately from Agent success claims, including
  final-gate precision, false positives, false negatives, changed-file scope,
  duration, model calls, and tokens.
- Turn every observed failure into a general runtime invariant and offline
  regression test; never add case- or benchmark-specific policy to the Agent.

## Verification adapters

- Define a language-neutral adapter interface for discovery, syntax checks,
  tests, and representative execution.
- Add first-class JavaScript/TypeScript support without changing the generic
  requirement model.
- Add Go and Rust adapters with the same evidence and timeout semantics.
- Improve command-result normalization for non-pytest test runners.

## Retrieval and memory

- Add optional symbol/embedding indexes behind a stable retrieval interface.
- Define retention and deletion controls for project memory and audits.
- Add migration tests for memory and state schema versions.

## Security and operations

- Provide a reference container profile with restricted network and resource
  limits.
- Add a pluggable secret scanner for audit exports.
- Fuzz command/path guards and malformed model JSON handling.
- Document threat-model differences between trusted local projects and
  untrusted third-party repositories.

## Portfolio and community

- Record a short terminal demo covering clarify, resume, verification, and
  audit export.
- Add architecture decision records for LangGraph orchestration, direct
  OpenAI-compatible model access, and dual task contracts.
- Publish contribution labels for good-first-issue, verification-adapter, and
  security work.
