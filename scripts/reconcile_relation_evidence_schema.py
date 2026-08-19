#!/usr/bin/env python3
"""Inspect or explicitly migrate the relation_evidence schema."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import get_config
from core.kia.relation_evidence_schema import reconcile_relation_evidence_schema


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _backup_database(source: Path, backup_dir: Path) -> dict[str, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"knowledge_graph-before-relation-evidence-v1-{stamp}.db"
    if target.exists():
        raise FileExistsError(f"backup already exists: {target}")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"backup integrity check failed: {integrity}")
    return {"path": str(target), "sha256": _sha256(target), "integrity_check": "ok"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = args.db_path or Path(get_config().database_dir) / "knowledge_graph.db"
    if not db_path.exists():
        payload = {"ok": False, "error": f"database not found: {db_path}"}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    if args.apply and args.backup_dir is None:
        print(json.dumps({"ok": False, "error": "--apply requires --backup-dir"}))
        return 2

    backup: dict[str, str] | None = None
    try:
        if args.apply:
            backup = _backup_database(db_path, args.backup_dir)
            conn = sqlite3.connect(db_path)
        else:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            report = reconcile_relation_evidence_schema(conn, apply=args.apply)
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()
        payload = {
            "ok": bool(report["after"]["ok"] if args.apply else not report["before"]["errors"]),
            "db_path": str(db_path),
            "backup": backup,
            "integrity_check": integrity,
            **report,
        }
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        payload = {"ok": False, "db_path": str(db_path), "backup": backup, "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
