"""Repository-scoped lock paths shared by a Git repository and its worktrees."""

from __future__ import annotations

from pathlib import Path
import subprocess


def git_common_lock_path(root: Path, lock_name: str) -> Path:
    """Resolve a lock below Git's common directory for all linked worktrees."""
    name = str(lock_name or "").strip()
    if not name or Path(name).name != name:
        raise ValueError("git_common_lock_name_invalid")
    repository = Path(root).resolve()
    completed = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repository,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    common_dir = completed.stdout.strip()
    if completed.returncode != 0 or not common_dir:
        raise RuntimeError("git_common_dir_unavailable")
    resolved = Path(common_dir)
    if not resolved.is_absolute():
        resolved = repository / resolved
    resolved = resolved.resolve()
    if not resolved.is_dir():
        raise RuntimeError("git_common_dir_invalid")
    return resolved / name


__all__ = ["git_common_lock_path"]
