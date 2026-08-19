#!/usr/bin/env python3
"""Audit KG relation storage contract.

Production code must write `relations` rows through `core.kia.relation_writer`
so endpoint quality gates, FTS rows, and evidence cleanup stay consistent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("core", "scripts")
DIRECT_RELATION_INSERT = re.compile(
    r"\bINSERT\s+(?:OR\s+(?:REPLACE|IGNORE)\s+)?INTO\s+relations\b",
    re.IGNORECASE,
)
OLD_METHOD_COLUMN_QUERY = re.compile(
    r"\brelations\.method\b"
    r"|\bSELECT\s+method\s+FROM\s+relations\b"
    r"|\bFROM\s+relations\b[^\"']{0,200}\bWHERE\s+method\b",
    re.IGNORECASE | re.DOTALL,
)

ALLOWED_DIRECT_INSERT_FILES = {
    "core/kia/relation_writer.py",
}
SELF = "scripts/audit_kg_relation_contract.py"


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root_name in SCAN_ROOTS:
        root = ROOT / root_name
        if root.exists():
            files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def main() -> int:
    direct_insert_violations: list[str] = []
    old_method_query_violations: list[str] = []

    for path in _python_files():
        rel = _rel(path)
        if rel == SELF:
            continue
        text = path.read_text(encoding="utf-8")
        if DIRECT_RELATION_INSERT.search(text) and rel not in ALLOWED_DIRECT_INSERT_FILES:
            direct_insert_violations.append(rel)
        if OLD_METHOD_COLUMN_QUERY.search(text):
            old_method_query_violations.append(rel)

    if direct_insert_violations or old_method_query_violations:
        print("KG relation contract audit failed.")
        if direct_insert_violations:
            print("Direct INSERT INTO relations outside relation_writer.py:")
            for rel in direct_insert_violations:
                print(f"  - {rel}")
        if old_method_query_violations:
            print("Queries reference legacy relations.method instead of source_method:")
            for rel in old_method_query_violations:
                print(f"  - {rel}")
        return 1

    print("KG relation contract audit passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
