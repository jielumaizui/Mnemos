#!/usr/bin/env python3
"""Source inventory helper for code audits.

Generated artifacts and third-party dependency directories are excluded by
default so audit counts describe Mnemos-owned files.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable

GENERATED_PATH_NAMES = {
    ".git",
    ".audit_venv",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "mnemos.egg-info",
    ".DS_Store",
}


def _is_generated(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    return any(part in GENERATED_PATH_NAMES for part in rel_parts)


def iter_project_files(root: Path | str) -> Iterable[Path]:
    root = Path(root)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_generated(path, root):
            continue
        yield path


def build_inventory(root: Path | str) -> dict:
    root = Path(root)
    files = list(iter_project_files(root))
    by_ext = Counter(path.suffix or "no_ext" for path in files)
    lines_by_top: Dict[str, int] = defaultdict(int)
    files_by_top: Counter = Counter()
    total_lines = 0

    for path in files:
        try:
            line_count = sum(1 for _ in path.open("rb"))
        except OSError:
            line_count = 0
        top = path.relative_to(root).parts[0] if path.relative_to(root).parts else "root"
        files_by_top[top] += 1
        lines_by_top[top] += line_count
        total_lines += line_count

    return {
        "root": str(root),
        "files": len(files),
        "lines": total_lines,
        "by_ext": dict(sorted(by_ext.items())),
        "files_by_top": dict(sorted(files_by_top.items())),
        "lines_by_top": dict(sorted(lines_by_top.items())),
        "excluded_names": sorted(GENERATED_PATH_NAMES),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Mnemos source audit inventory")
    parser.add_argument("root", nargs="?", default=".", help="repository root")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    inventory = build_inventory(Path(args.root).resolve())
    if args.json:
        print(json.dumps(inventory, ensure_ascii=False, indent=2))
    else:
        print(f"root: {inventory['root']}")
        print(f"files: {inventory['files']}")
        print(f"lines: {inventory['lines']}")
        print("excluded: " + ", ".join(inventory["excluded_names"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
