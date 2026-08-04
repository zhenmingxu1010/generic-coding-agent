from __future__ import annotations

from pathlib import Path


def is_within_workspace(workspace: str | Path, path: str | Path) -> bool:
    """Return whether *path* resolves to the workspace or one of its children.

    Resolve both sides so symlink aliases cannot bypass the boundary.  Path
    ancestry is used instead of string prefixes because sibling paths such as
    ``/work/repo-copy`` are not children of ``/work/repo``.
    """
    try:
        root = Path(workspace).resolve()
        Path(path).resolve().relative_to(root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


class PathGuard:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str | Path) -> Path:
        p = Path(relative_path)
        if p.is_absolute():
            raise ValueError(f"Absolute path is not allowed: {relative_path}")
        target = (self.workspace / p).resolve()
        if not is_within_workspace(self.workspace, target):
            raise ValueError(f"Path escapes workspace: {relative_path}")
        return target

    def rel(self, path: str | Path) -> str:
        return str(Path(path).resolve().relative_to(self.workspace))
