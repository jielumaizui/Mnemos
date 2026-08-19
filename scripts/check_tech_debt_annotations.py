#!/usr/bin/env python3
"""检查代码中的 TODO/FIXME/DEBT 注释是否带元数据。

规范：
- # TODO(2026-06-25): ...
- # FIXME(2026-06-25,owner): ...
- # DEBT(S25): ...

括号内至少包含日期（YYYY-MM-DD）或审计项编号（Sxx/Pxx）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

PATTERN = re.compile(
    r"#\s*(TODO|FIXME|DEBT)(?:\s*\(\s*([^)]+)\s*\))?",
    re.IGNORECASE,
)
VALID_MARKER = re.compile(r"\d{4}-\d{2}-\d{2}|S\d+|P\d+")

SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
}


def iter_python_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        p = Path(path)
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            for f in p.rglob("*.py"):
                if any(part in SKIP_DIRS for part in f.parts):
                    continue
                yield f


def check_file(path: Path) -> List[Tuple[int, str, str]]:
    violations: List[Tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"WARN: cannot read {path}: {exc}", file=sys.stderr)
        return violations

    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip().startswith("#"):
            continue
        for m in PATTERN.finditer(line):
            kind = m.group(1)
            marker = m.group(2)
            if marker is None or not VALID_MARKER.search(marker):
                violations.append((lineno, kind, marker or ""))
            # 一行可能多个标记，处理完即可跳出本次匹配
            break
    return violations


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check tech-debt annotation format")
    parser.add_argument(
        "paths",
        nargs="+",
        help="Python files or directories to scan",
    )
    args = parser.parse_args(argv)

    all_violations: List[Tuple[Path, int, str, str]] = []
    for path in iter_python_files(args.paths):
        for lineno, kind, marker in check_file(path):
            all_violations.append((path, lineno, kind, marker))

    if not all_violations:
        print("OK: all TODO/FIXME/DEBT annotations carry a date or audit id")
        return 0

    print("FAIL: missing date/audit id in tech-debt annotations")
    for path, lineno, kind, marker in all_violations:
        print(f"  {path}:{lineno}  {kind}({marker})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
