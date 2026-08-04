# Security Model

## Protected assets

- Source files and data outside the authorized workspace.
- Existing workspace files that the task does not authorize changing.
- API credentials and model-provider secrets.
- Integrity of project tests and verification evidence.
- Confidential source or prompt content stored in run artifacts.

## Trust boundaries

The user task, target repository, model output, generated code, and shell output
may all be untrusted. Deterministic runtime guards should therefore own
authorization and evidence decisions instead of delegating them to model text.

## Current controls

- Path normalization and workspace-boundary checks.
- Symlink target validation across repository discovery, text search,
  interface checks, verification discovery, and workspace baselines.
- Read-only and semantic write-scope policies.
- Project execution disabled during read-only analysis, with a separate
  explicit verify-only execution path that does not enable write tools.
- Workspace baselines and post-action change audits.
- Command policy checks before shell execution.
- Rejection of indirect command writes/execs through `find`, in-place `sed`,
  and Git output options.
- Direct shell-script and standard-input verification without enabling inline
  shell commands, redirection, or interpreter options.
- Structured tool arguments and results.
- Isolation of agent-generated verification tests.
- Bounded rounds, repair calls, repeated actions, and repeated failures.
- Final-gate rejection of failed or missing required evidence.
- Git ignores for local credentials and run artifacts.
- Defense-in-depth text redaction and a manifest on exported audit bundles.

## Out of scope

The project does not currently provide:

- kernel-, container-, or virtual-machine isolation;
- complete protection from malicious dependency installation or test code;
- a formal proof that command filtering blocks every shell escape;
- automatic redaction of every secret or private source fragment in audits;
- safe multi-tenant execution.

## Recommended deployment

Run each untrusted task in an ephemeral container or virtual machine with a
non-privileged user, a mounted workspace, restricted network access, resource
limits, and short-lived provider credentials. Keep human review between agent
output and deployment.
