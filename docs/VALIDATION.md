# Validation Record

This page records reproducible checks completed for the current alpha source.
It is evidence for the listed scenarios, not a claim that every repository or
model will behave identically.

## Environment

- Date: 2026-07-23
- Platform: macOS arm64
- Python: 3.12
- Model protocol: OpenAI-compatible chat completions
- Model used for live checks: `deepseek-v4-flash`
- API connectivity check: passed
- Offline suite: 536 passed
- Distribution build: wheel and source distribution passed
- Built-wheel smoke check: package imports and CLI help passed
- Local release gate: source scan, compile, pytest, build, distribution
  contents, wheel install, and all console entry points passed

No endpoint credential is stored in this repository. Provider selection is
reported only to make the result reproducible; core behavior remains provider
neutral.

## Real-repository repair pilot

The current pilot resolved 4/4 pinned historical defects with hidden post-run
acceptance and no protected test source changes. Three localized cases match
Agent final-gate success. The added multi-file Cookiecutter case is externally
accepted but remains a conservative final-gate false negative because the
Agent did not construct sufficient direct execution evidence. One localized
case includes an additional hidden compatibility check that caught and
eliminated an initially over-broad repair. See
[REAL_WORLD_EVALUATION.md](REAL_WORLD_EVALUATION.md) for the protocol,
per-case results, generic fixes, and explicit limits of this small sample.

The v2 sanitized summary separates external acceptance from the Agent's success
claim. This pilot contains three true-positive claims, zero false-positive
claims, one false-negative claim, 47 model calls, and 431,051 reported tokens.
One accepted repair changed three implementation files, providing a first
multi-file data point rather than broad multi-file benchmark evidence.

## Live short-prompt checks

| Scenario | Prompt / transition | Result | Calls | Tokens | Key evidence |
| --- | --- | --- | ---: | ---: | --- |
| Short functional task | `写个脚本统计文本行数` | `verified_ok` | 6 | 15,615 | Generated one script; representative three-line execution printed `3`; all 3 required atoms passed |
| Missing core behavior | `写个脚本` | `clarification_required` | 1 | 1,606 | Controlled outcome; one focused question; zero source changes |
| Clarification resume | Same thread plus “统计指定文本文件的行数，并把整数结果打印到终端。” | `verified_ok` | 6 total | 10,860 | Original prompt and clarification history preserved; direct shell-script fixture execution printed `3`; all 4 atoms passed |
| Colloquial inspection | `看看这个项目` | `analysis_complete` | 3 | 12,502 | Forced read-only analysis; zero source changes; evidence report passed quality checks; project memory updated |

## Audit review

Four corresponding audit bundles were exported with workspace fixtures. Each
contains `audit_manifest.json`. A scan compared the configured real API key and
common `sk-*` patterns against every archive member and checked for the local
home path:

- credential hits: 0;
- common token-pattern hits: 0;
- private home-path hits: 0;
- clarification-required source changes: 0;
- read-only inspection source changes: 0.

The bundles remain local and ignored by Git. Automated redaction is not a
substitute for reviewing a bundle before publication.

## Eleven-case capability matrix

The first uninterrupted matrix run exposed three failures in verification
aggregation, repair-state clearing, and generated-test handling. Those defects
were fixed as general runtime invariants and covered by offline tests. Failed
cases were rerun in fresh workspaces; a detailed collector then found and
removed one stale T06 audit that had passed its top-level result while changing
an undeclared symbolic path.

The canonical bundle contains one final valid audit for each scenario. Strict
collection validates detailed conditions, not only `final.ok`. The final T02
audit also demonstrates a detected runtime `TypeError`, deterministic failure
ownership, one bounded implementation repair, and successful re-verification.

| Case | Capability | Expected outcome | Final result | Rounds | Calls | Tokens |
| --- | --- | --- | --- | ---: | ---: | ---: |
| T01 | Read-only project understanding | success | `analysis_complete` | 0 | 3 | 14,789 |
| T02 | Isolated report generation | success | `verified_ok` | 1 | 9 | 39,152 |
| T03 | CLI argument behavior | success | `verified_ok` | 0 | 7 | 31,638 |
| T04 | Interface repair | success | `verified_ok` | 2 | 10 | 50,526 |
| T05 | Zero tests negative case | controlled failure | `pytest_zero_tests_collected` | 0 | 2 | 7,046 |
| T06 | No source pollution | success | `verified_ok` | 0 | 8 | 23,903 |
| T07 | Existing bug repair | success | `verified_ok` | 3 | 9 | 42,814 |
| T08 | Greenfield CLI project | success | `verified_ok` | 5 | 17 | 112,726 |
| T09 | Short prompt defaults | success | `verified_ok` | 0 | 6 | 15,769 |
| T10 | Ambiguous prompt | controlled clarification | `clarification_required` | 0 | 1 | 1,700 |
| T11 | Colloquial inspection | success | `analysis_complete` | 0 | 4 | 18,646 |

Total: 11/11 detailed validators passed, 76 model calls, and 358,709 reported
tokens across the selected audits. The relatively expensive T08 repair path is
kept in the record rather than replaced with a more favorable rerun.

The canonical audit bundle is local and ignored because it contains prompts,
source excerpts, and generated workspaces. Recreate it with the renderer and
validate it with:

```bash
python scripts/collect_regression_audits.py \
  --audit-dir .agent_runs/regression-audits \
  --strict
```

## Distribution evidence

Run `python scripts/release_check.py` to regenerate the local release report,
then record release-asset hashes outside the source archive with
`shasum -a 256 dist/*`. A source distribution cannot contain its own stable
hash because adding that hash changes the archive.

## Public-release evidence

Commit `9dbcf9545f6f3d8a227852f211f1c213803cf116` was published as
[`v0.1.0-alpha`](https://github.com/zhenmingxu1010/generic-coding-agent/releases/tag/v0.1.0-alpha).
The `main` push and release-tag push both passed the public Python 3.10–3.13 CI
matrix. The release contains the checked wheel and source distribution with
GitHub-recorded SHA-256 digests.
