#!/usr/bin/env python3
"""Audit the canonical relation_evidence schema owner and live registry state."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import get_config
from core.kia.relation_evidence_schema import (
    SCHEMA_VERSION,
    inspect_database,
)


def build_report(*, db_path: Path, root: Path | None = None) -> dict[str, object]:
    root = root or Path(__file__).resolve().parents[1]
    owners: list[str] = []
    ddl_pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?relation_evidence\b",
        re.IGNORECASE,
    )
    for directory in ("core", "integrations", "daemon", "scripts"):
        for path in sorted((root / directory).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if ddl_pattern.search(text):
                owners.append(str(path.relative_to(root)))
    if db_path.exists():
        state = inspect_database(db_path).as_dict()
    else:
        state = {
            "schema_version": SCHEMA_VERSION,
            "classification": "not_initialized",
            "migration_required": False,
            "errors": [],
            "ok": True,
        }
    errors: list[str] = []
    expected_owner = "core/kia/relation_evidence_schema.py"
    if owners != [expected_owner]:
        errors.append(f"relation_evidence DDL owners must be [{expected_owner}], got {owners}")
    if not state["ok"]:
        errors.extend(str(item) for item in state["errors"])
        if state["migration_required"] and not state["errors"]:
            errors.append("relation_evidence schema migration/registration is required")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "db_path": str(db_path),
        "ddl_owners": owners,
        "state": state,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.db_path or Path(get_config().database_dir) / "knowledge_graph.db"
    try:
        report = build_report(db_path=db_path)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "db_path": str(db_path),
            "errors": [str(exc)],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
