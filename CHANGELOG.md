# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Post-scan task-completeness assessment for short and colloquial prompts.
- Explicit implementation assumptions and `implementation:*` verification
  atoms, kept separate from exact-evidence user requirements.
- Controlled `clarification_required` outcomes with CLI and interactive Chat
  resume support.
- Bounded standard-input verification and direct workspace shell-script
  execution without enabling inline shell commands.
- Audit export manifest plus text redaction for common credentials and local
  absolute paths.
- Detailed eleven-case audit validation and an offline local release gate.
- Project deep dive, Agent interview guide, demo guide, completion criteria,
  and portfolio notes.
- Accessible terminal-demo transcript and animated validation summary.

### Changed

- Colloquial Chinese create, repair, modify, and inspection intent coverage.
- The provider-neutral capability matrix now includes eleven scenarios,
  including short prompts, ambiguity handling, and conversational inspection.
- Successful verification explicitly clears stale graph failure state; symbolic
  internal-test paths cannot become deliverable artifacts; repair reads may use
  a bounded whitelist of current generated support files.
- Agent-default execution evidence is aggregated from already-grounded user
  behavior instead of forcing duplicate or expanded verification scenarios.
- Failed scenarios with accepted task grounding remain implementation evidence
  even when an oracle reviewer notices that the current code cannot pass them.
- Traceback paths inside the workspace are normalized before repair target
  locking, and streamed model responses now obey a total wall-clock deadline.
- The release gate proves wheel imports originate from an isolated temporary
  installation instead of being shadowed by the source checkout.

### Planned

- Add verification adapters beyond Python and pytest.
- Publish sanitized results under the final public release commit.
- Add persistent checkpointer options for long-running sessions.

## [0.1.0] - 2026-07-21

### Added

- LangGraph-based workflow for repository analysis, planning, implementation,
  verification, diagnosis, bounded repair, and final reporting.
- Explicit task intent, scope contracts, write guards, and workspace baselines.
- Requirement atoms and execution-backed final-gate decisions.
- Structured tool registry for file, shell, Python, and pytest operations.
- Project memory, context compression, traces, and exportable audit bundles.
- Read-only analysis, existing-project modification, and greenfield generation
  modes.
- A generic eight-case regression matrix and an offline unit-test suite.
