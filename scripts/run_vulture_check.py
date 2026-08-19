#!/usr/bin/env python3
"""Run Mnemos dead-code checks through vulture with a whitelist budget."""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WHITELIST = PROJECT_ROOT / "vulture_whitelist.py"
DEFAULT_BUDGET = 310


def count_whitelist_entries(path: Path = DEFAULT_WHITELIST) -> int:
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            count += 1
    return count


def vulture_available() -> bool:
    return importlib.util.find_spec("vulture") is not None


def build_vulture_command(
    repo_root: Path = PROJECT_ROOT,
    whitelist: Path = DEFAULT_WHITELIST,
    min_confidence: int = 60,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vulture",
        str(repo_root),
        str(whitelist),
        "--min-confidence",
        str(min_confidence),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run vulture dead-code check for Mnemos.")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--whitelist", type=Path, default=DEFAULT_WHITELIST)
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument("--min-confidence", type=int, default=60)
    parser.add_argument(
        "--budget-only",
        action="store_true",
        help="only validate the whitelist budget; do not invoke vulture",
    )
    args = parser.parse_args(argv)

    remaining = count_whitelist_entries(args.whitelist)
    print(f"Vulture whitelist entries: {remaining}/{args.budget}")
    if remaining > args.budget:
        print(
            f"ERROR: whitelist budget exceeded ({remaining}>{args.budget}).",
            file=sys.stderr,
        )
        return 1

    if args.budget_only:
        return 0

    if not vulture_available():
        print(
            "ERROR: vulture is not installed. Install dev dependencies with: "
            "python3 -m pip install -e '.[dev]'",
            file=sys.stderr,
        )
        return 2

    command = build_vulture_command(args.repo_root, args.whitelist, args.min_confidence)
    return subprocess.call(command, cwd=args.repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
