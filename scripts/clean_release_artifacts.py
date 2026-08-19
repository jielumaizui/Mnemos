#!/usr/bin/env python3
"""Clean generated artifacts before publishing or auditing the source tree."""

from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


SKIP_DIRS = {
    ".git",
    ".venv",
    ".audit_venv",
    "venv",
    "env",
    "ENV",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
ROOT_ARTIFACT_DIRS = {"build", "dist"}
ROOT_ARTIFACT_FILES = {"EOF"}


@dataclass(frozen=True)
class Artifact:
    path: Path
    kind: str


def collect_artifacts(repo_root: Path) -> list[Artifact]:
    repo_root = repo_root.resolve()
    artifacts: list[Artifact] = []

    for name in ROOT_ARTIFACT_DIRS:
        path = repo_root / name
        if path.exists():
            artifacts.append(Artifact(path, "dir"))

    for name in ROOT_ARTIFACT_FILES:
        path = repo_root / name
        if path.exists():
            artifacts.append(Artifact(path, "file"))

    for current, dirs, files in os.walk(repo_root):
        current_path = Path(current)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in ROOT_ARTIFACT_DIRS]

        for dirname in list(dirs):
            if dirname == "__pycache__":
                path = current_path / dirname
                artifacts.append(Artifact(path, "dir"))
                dirs.remove(dirname)

        for filename in files:
            if filename.endswith(".pyc"):
                artifacts.append(Artifact(current_path / filename, "file"))

    return sorted(artifacts, key=lambda item: item.path.as_posix())


def remove_artifacts(artifacts: list[Artifact]) -> int:
    removed = 0
    for artifact in artifacts:
        if not artifact.path.exists():
            continue
        if artifact.kind == "dir":
            shutil.rmtree(artifact.path)
        else:
            artifact.path.unlink()
        removed += 1
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean Mnemos generated release artifacts")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root, defaults to this script's parent repo",
    )
    parser.add_argument("--apply", action="store_true", help="delete artifacts instead of listing")
    parser.add_argument("--check", action="store_true", help="exit non-zero if artifacts exist")
    args = parser.parse_args(argv)

    artifacts = collect_artifacts(args.repo_root)
    if not artifacts:
        print("No release artifacts found.")
        return 0

    for artifact in artifacts:
        print(f"{artifact.kind}: {artifact.path}")

    if args.apply:
        removed = remove_artifacts(artifacts)
        print(f"Removed {removed} artifact(s).")
        return 0

    if args.check:
        print(f"Found {len(artifacts)} release artifact(s). Run with --apply to remove them.")
        return 1

    print(f"Found {len(artifacts)} release artifact(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
