#!/usr/bin/env python3
"""Check that FIXME/DEBT/marker comments include an owner/date/issue marker.

Corresponds to S39 in MNEMOS_CODE_AUDIT_2026_06_24.md.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCAN_DIRS = ["core", "integrations", "daemon", "scripts"]
DEFAULT_ROOT_FILES = ["mnemos_cli.py", "mnemos_daemon.py"]

_MARKER_RE = re.compile(r"#\s*(TODO|FIXME|DEBT|XXX|HACK)\b", re.IGNORECASE)
# A valid marker must contain one of:
#   - a date like 2026-06-25
#   - an owner prefix like (owner/...
#   - an issue reference like #123 or S25
_VALID_RE = re.compile(
    r"(TODO|FIXME|DEBT|XXX|HACK)\s*\(\s*(\d{4}-\d{2}-\d{2}|[^\)]+/[^\)]+|#[0-9]+|S[0-9]+).*\)",
    re.IGNORECASE,
)


def scan_file(path: Path) -> List[Tuple[int, str]]:
    """Return (line_no, line) for each unmarked debt comment."""
    violations: List[Tuple[int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return violations

    for lineno, line in enumerate(text.splitlines(), start=1):
        if not _MARKER_RE.search(line):
            continue
        if _VALID_RE.search(line):
            continue
        violations.append((lineno, line.strip()))
    return violations


def scan_project() -> List[Tuple[Path, int, str]]:
    violations: List[Tuple[Path, int, str]] = []
    for dirname in DEFAULT_SCAN_DIRS:
        for path in (PROJECT_ROOT / dirname).rglob("*.py"):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            for lineno, line in scan_file(path):
                violations.append((path, lineno, line))
    for filename in DEFAULT_ROOT_FILES:
        path = PROJECT_ROOT / filename
        if path.exists():
            for lineno, line in scan_file(path):
                violations.append((path, lineno, line))
    return violations


def main() -> int:
    violations = scan_project()
    if not violations:
        print("No unmarked TODO/FIXME/DEBT comments found.")
        return 0

    print(f"Found {len(violations)} unmarked TODO/FIXME/DEBT comment(s):")
    for path, lineno, line in violations:
        rel = path.relative_to(PROJECT_ROOT)
        print(f"  {rel}:{lineno}: {line}")
    print("\nExpected format: # TODO(2026-06-25): ...  or  # DEBT(S25): ...")
    return 1


if __name__ == "__main__":
    sys.exit(main())
