# Open-source Release Checklist

## Required before public visibility

- [ ] Revoke any credential that has appeared in local files, logs, or chats.
- [x] Exclude local model configuration and runtime artifacts from Git.
- [x] Remove private absolute paths and domain-specific regression fixtures.
- [x] Add an OSI-approved license.
- [x] Add security, contribution, and conduct policies.
- [x] Add an offline CI workflow for supported Python versions.
- [x] Replace repository URL placeholders after the public repository is named.
- [x] Scan the complete Git history for credentials. The initial public history
  contained one root commit (`9dbcf95`); the credential/private-path scan found
  zero matches on 2026-07-23.

## Required for `v0.1.0-alpha`

- [x] Install successfully in a new virtual environment.
- [x] Pass the complete offline test suite locally (536 tests on 2026-07-23).
- [x] Pass the complete offline test suite in
  [public CI](https://github.com/zhenmingxu1010/generic-coding-agent/actions).
- [x] Build both wheel and source distribution.
- [x] Verify the source distribution includes documentation, example configs,
  regression definitions, scripts, and the Roadmap.
- [x] Verify every console entry point with `--help` or a smoke test.
- [x] Run short-prompt generation, clarification/resume, and read-only analysis
  end to end with non-private fixture workspaces.
- [x] Review four sanitized live audit bundles for credentials, private paths,
  expected outcomes, and source-write scope.
- [x] Pass the eleven-case canonical matrix with detailed machine validators.
- [x] Pass the local release-candidate script, including clean wheel smoke.

## Required for release-quality `v0.1.0`

- [x] Run the eleven-case capability matrix with recorded model configuration.
- [x] Document reproducible local results, including failures and limitations.
- [x] Add a short terminal recording or GIF with an accessible text transcript.
- [x] Add a concise architecture diagram and design rationale.
- [x] Add a roadmap with issues suitable for external contributors.
- [x] Add a reproducible three-minute demo path and bilingual technical documentation.
- [x] Confirm package install/import/CLI startup in a temporary wheel environment.

## Current `main` hardening

- [x] Pass 599 offline tests with 78% full-package line coverage after
  workspace-boundary, read-only execution, PEP 621 console-entry, transport,
  timeout, memory-recovery, retrieval, and terminal-routing hardening on
  2026-08-05.
- [x] Enforce a 75% full-package coverage floor in public CI.
- [x] Reject external-target symlinks across search, repository mapping,
  interface checks, verification discovery, and workspace baselines.
- [x] Keep explicit verify-only execution separate from read-only analysis and
  from source-write authorization.
