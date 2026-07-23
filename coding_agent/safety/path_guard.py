from __future__ import annotations

from pathlib import Path


class PathGuard:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str | Path) -> Path:
        p = Path(relative_path)
        if p.is_absolute():
            raise ValueError(f"Absolute path is not allowed: {relative_path}")
        target = (self.workspace / p).resolve()
        if self.workspace != target and self.workspace not in target.parents:
            raise ValueError(f"Path escapes workspace: {relative_path}")
        return target

    def rel(self, path: str | Path) -> str:
        return str(Path(path).resolve().relative_to(self.workspace))
