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

Public release completion additionally requires account-owned actions that
cannot be proved locally: choose the repository URL, run public CI on the exact
commit, scan the resulting Git history, create the tag, and publish the release.

## Portfolio scope

The project is portfolio-complete when a reviewer can:

- understand the problem and architecture from the README in under five
  minutes;
- reproduce the offline release gate and model-backed matrix;
- inspect honest metrics, failures, fixes, and limitations;
- follow a short demo without private configuration;
- read a technical deep dive and interview guide;
- reuse concise résumé bullets and a spoken project explanation.
