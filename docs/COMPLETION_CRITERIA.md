# Completion Criteria

“100%” in this repository means every check in a declared release scope has
reproducible evidence. It does not mean that an LLM system can never fail on an
unknown repository.

## Core Agent v0.1 scope

Core capability is complete for v0.1 when:

- all offline tests pass;
- all eleven model-backed regression cases satisfy their detailed conditions;
- analyze, create, modify, verify-only, negative-test, clarification, resume,
  short-prompt, scope-isolation, and memory paths are represented;
- required behavior is connected to executed evidence;
- read-only and protected-file tasks report zero unauthorized source changes;
- repair loops, repeated reads, and model action retries are bounded;
- no benchmark-, fixture-, provider-, or business-domain rule is present in
  the core runtime.

The supported v0.1 boundary is Python/pytest plus basic direct POSIX shell
execution. JavaScript, Go, Rust, persistent LangGraph storage, and OS-level
sandboxing are roadmap items rather than hidden claims.

## Local release-candidate scope

A local `v0.1.0-alpha` candidate is complete when:

- source scan, compile, pytest, wheel/sdist build, package-content checks,
  clean wheel smoke tests, and every console `--help` check pass through
  `python scripts/release_check.py`;
- the canonical eleven-case bundle passes
  `python scripts/collect_regression_audits.py --strict`;
- local model configs, credentials, run state, and audit archives are ignored;
- release, security, contribution, architecture, validation, demo, and
  limitation documents are present.

Public release completion additionally required choosing the repository URL,
running public CI on the exact commit, scanning the resulting Git history,
creating the tag, and publishing the release. Those account-owned actions were
completed for `v0.1.0-alpha` on 2026-07-23.

## v0.2 real-world evidence scope

The next evidence milestone is complete when:

- at least ten eligible real-repository cases are published with reproducible
  provenance and hidden external acceptance;
- the set includes multi-file repair, ambiguous issue text, dependency/API
  migration, and a larger preserved test suite;
- external resolution is reported separately from the Agent's final claim;
- success-claim precision, false positives, false negatives, changed-file
  scope, duration, model calls, and tokens are reported;
- every runtime change prompted by an evaluation failure represents a general
  failure class and has an offline regression test.

## Project readiness

The project is release-ready when a reviewer can:

- understand the problem and architecture from the README in under five
  minutes;
- reproduce the offline release gate and model-backed matrix;
- inspect honest metrics, failures, fixes, and limitations;
- follow a short demo without private configuration;
- read the architecture, security, and validation guides;
- reuse concise résumé bullets and a spoken project explanation.
