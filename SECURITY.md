# Security Policy

## Supported versions

Security fixes are applied to the latest release and the default branch.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting feature for this repository. Include the
affected version, a minimal reproduction, the expected impact, and any known
mitigation.

## Security model

This project lets an LLM propose file and shell operations. The runtime applies
path, command, write-scope, and read-only guards, but these controls do not make
arbitrary untrusted code safe to execute.

Read-only analysis does not execute project shell commands or tests. A user may
explicitly request verify-only execution; project tests can contain arbitrary
code, so that mode still requires an isolated or trusted workspace. Workspace
file discovery ignores symbolic links that resolve outside the authorized
root, and command policy blocks known indirect write/exec forms, but these are
defense-in-depth controls rather than an operating-system sandbox.

- Run the agent with the least filesystem and network access it needs.
- Use a disposable workspace or container for untrusted repositories.
- Review generated changes before merging or deploying them.
- Keep API keys in `configs/model.local.yaml` or environment variables; never
  commit credentials.
- Treat audit bundles as sensitive because they may contain source excerpts,
  prompts, paths, and command output. Export applies common text redactions,
  but every bundle still requires manual review.

See `docs/SECURITY_MODEL.md` for trust boundaries and known limitations.
