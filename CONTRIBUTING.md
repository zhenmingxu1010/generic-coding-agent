# Contributing

Thank you for helping improve Generic Coding Agent.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
python -m pytest -q
```

## Pull requests

1. Open an issue first for large behavior or architecture changes.
2. Keep changes general-purpose. Do not add branches, prompts, fixtures, or
   fallback behavior for one private repository or benchmark case.
3. Add or update tests for behavior changes.
4. Run the full offline test suite before submitting.
5. Update documentation and `CHANGELOG.md` when user-facing behavior changes.

## Design expectations

- Safety and scope decisions should be deterministic where possible.
- A successful final result must be backed by executed evidence.
- Model-specific behavior belongs in configuration or adapters, not task logic.
- New tools must use the structured tool interface and declare their effects.
- Compatibility fallbacks must address a documented class of failures and have
  regression tests.

By contributing, you agree that your contributions are licensed under the
Apache License 2.0.
