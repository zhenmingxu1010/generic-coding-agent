# Real-world repair evaluation

The reproducible pilot uses four historical Python defects sourced from public
Git repositories and BugsInPy metadata. Every case pins a buggy commit and a
historical fixed commit.

## Evaluation protocol

The agent receives a clean checkout of the buggy commit and a short issue-style
task. It cannot see the historical patch, fixed source, or hidden tests.

The evaluator independently requires:

- the hidden regression to fail on the buggy commit;
- the same regression to pass on the historical fixed commit;
- protected test source to remain unchanged after the agent run; and
- hidden acceptance to pass only after the agent has stopped.

The `tqdm-tenumerate-start` case also injects an evaluator-owned compatibility
test for an existing NumPy special branch. This caught an early over-broad fix
that passed the historical regression but changed an unrelated return shape.
After the generic minimal-diff policy was strengthened, the agent produced the
same one-line implementation change as the historical repair.

## Current pilot result

| Case | External result | Agent final gate | Changed project files | Tokens | Protected tests changed |
|---|---:|---:|---:|---:|---:|
| PySnooper file output | resolved | `verified_ok` | 1 | 64,074 | 0 |
| tqdm `tenumerate(start)` | resolved | `verified_ok` | 1 | 77,711 | 0 |
| tqdm unknown total + scale | resolved | `verified_ok` | 1 | 69,837 | 0 |
| Cookiecutter failed-hook cleanup | resolved | conservative false negative | 3 | 219,429 | 0 |

The sanitized machine-readable result is
[`evaluations/real_world/pilot-summary.json`](../evaluations/real_world/pilot-summary.json).
Raw reports remain local because they contain machine-specific workspace paths
and model audit locations.

The v2 summary treats the hidden post-run result as evaluator-owned truth and
compares it with the Agent's final claim. On this small pilot:

- external acceptance: 4/4;
- true-positive success claims: 3;
- false-positive claims: 0;
- false-negative claims: 1;
- success-claim precision: 100%;
- success-claim recall: 75%;
- protected mutation cases: 0;
- model calls: 47;
- reported tokens: 431,051; and
- multi-file change cases: 1.

Precision and recall are descriptive only at this sample size. The multi-file
case demonstrates one accepted three-file repair, not broad repository-level
performance. Its false-negative Agent claim also records the current limit:
the implementation was correct, but autonomous verification did not produce
sufficient trustworthy direct evidence.

## Generic improvements found by the pilot

- Agent and target-project Python interpreters can be different.
- Virtual-environment launcher symlinks are preserved instead of being resolved
  to a base interpreter that loses installed packages.
- Isolated behavior probes can import project code without copying or mutating
  the real workspace inputs.
- Static interface checks recognize imported and re-exported Python symbols.
- Existing failing tests are captured before source modification. They are
  downgraded only when the final run collects at least as many tests and every
  remaining failure has the same test, exception type, and normalized message.
- Repair prompts require the smallest evidence-backed behavioral diff and treat
  unrelated branches as compatibility constraints.
- Initial multi-file changes can complete a bounded implementation batch before
  verification, while post-failure repairs remain edit-then-verify.
- Verification probes reject definite local-call arity errors before execution,
  and catch/cleanup scenarios are instructed to inject the lower-level failure
  rather than rely on fragile full-stack fixtures.

## Limits

Four defects are a useful smoke test, not a statistically meaningful benchmark.
The one multi-file case is evidence of a specific error-propagation/cleanup
repair, not complex issue-resolution breadth. The next expansion should include
more multi-file changes, ambiguous issue text, dependency/API migrations, and
repositories with larger test suites. SWE-bench or an equivalent containerized
benchmark should be reported separately when the required storage and
reproducible runtime are available.

## Reproduction

See [`evaluations/real_world/README.md`](../evaluations/real_world/README.md) for
the command and schema. The runner performs no network installation. Callers
provide a full local source clone and a target test interpreter so dependency
or interpreter failures are reported as environment failures rather than agent
failures.
