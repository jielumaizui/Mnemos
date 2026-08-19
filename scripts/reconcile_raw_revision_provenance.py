#!/usr/bin/env python3
"""Back up and reconcile immutable raw revisions with Wiki provenance."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from core.frontmatter import fm_get, parse_frontmatter
from core.sync_framework.raw_event_store import RawEventStore


GAP_REASON = "legacy_page_lacks_provable_revision_span_refs"


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def inspect_database(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        tables = _table_names(conn)
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(raw_turns)")
        }
        raw_turns = int(conn.execute("SELECT COUNT(*) FROM raw_turns").fetchone()[0])
        revisions = (
            int(conn.execute("SELECT COUNT(*) FROM raw_turn_revisions").fetchone()[0])
            if "raw_turn_revisions" in tables
            else 0
        )
        missing_current = raw_turns
        if "current_revision_id" in columns:
            missing_current = int(
                conn.execute(
                    "SELECT COUNT(*) FROM raw_turns "
                    "WHERE current_revision_id IS NULL OR current_revision_id=''"
                ).fetchone()[0]
            )
        return {
            "raw_turns": raw_turns,
            "revisions": revisions,
            "missing_current_revision": missing_current,
            "schema_migration_required": (
                "raw_turn_revisions" not in tables or missing_current > 0
            ),
        }


def _known_revisions(db_path: Path) -> set[str]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        if "raw_turn_revisions" not in _table_names(conn):
            return set()
        return {
            str(row[0])
            for row in conn.execute("SELECT revision_id FROM raw_turn_revisions")
        }


def _page_provenance(path: Path, known_revisions: set[str]) -> dict[str, Any] | None:
    try:
        frontmatter, _body = parse_frontmatter(
            path.read_text(encoding="utf-8", errors="ignore")
        )
    except (OSError, TypeError, ValueError):
        return {"path": path, "parse_error": True, "edges": [], "gap": False}
    frontmatter = frontmatter or {}
    source_agent = str(
        fm_get(frontmatter, "source_agent") or fm_get(frontmatter, "source") or ""
    )
    session_id = str(
        fm_get(frontmatter, "source_session")
        or fm_get(frontmatter, "session_id")
        or ""
    )
    if not session_id:
        return None
    refs = frontmatter.get("raw_event_refs") or []
    edges: list[dict[str, Any]] = []
    invalid_ref = False
    if not isinstance(refs, list):
        invalid_ref = True
        refs = []
    for ref in refs:
        try:
            revision_id = str(ref["revision_id"])
            span_start = int(ref.get("span_start") or 0)
            span_end = int(ref.get("span_end") or 0)
        except (KeyError, TypeError, ValueError):
            invalid_ref = True
            continue
        if (
            revision_id not in known_revisions
            or span_start < 0
            or span_end <= span_start
        ):
            invalid_ref = True
            continue
        edges.append(
            {
                "revision_id": revision_id,
                "span_start": span_start,
                "span_end": span_end,
            }
        )
    return {
        "path": path,
        "parse_error": False,
        "source_agent": source_agent,
        "session_id": session_id,
        "edges": edges,
        "gap": invalid_ref or not edges,
    }


def scan_wiki(wiki_dir: Path, known_revisions: set[str]) -> list[dict[str, Any]]:
    if not wiki_dir.exists():
        return []
    records = []
    for path in sorted(wiki_dir.rglob("*.md")):
        record = _page_provenance(path, known_revisions)
        if record is not None:
            records.append(record)
    return records


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / "raw_events.db"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as source:
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
    return backup_path


def reconcile(
    *, db_path: Path, wiki_dir: Path, apply: bool, backup_root: Path
) -> dict[str, Any]:
    before = inspect_database(db_path)
    known_revisions = _known_revisions(db_path)
    records = scan_wiki(wiki_dir, known_revisions)
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "database": str(db_path),
        "wiki_dir": str(wiki_dir),
        "before": before,
        "wiki_pages_with_session_identity": len(records),
        "parse_errors": sum(bool(item.get("parse_error")) for item in records),
        "provable_edges": sum(len(item.get("edges") or []) for item in records),
        "provenance_gaps": sum(bool(item.get("gap")) for item in records),
    }
    if not apply:
        return report

    stamp = datetime.now().strftime("root004-%Y%m%d-%H%M%S")
    backup_path = backup_database(db_path, backup_root / stamp)
    store = RawEventStore(db_path=db_path)
    try:
        known_revisions = _known_revisions(db_path)
        records = scan_wiki(wiki_dir, known_revisions)
        edges_recorded = 0
        gaps_recorded = 0
        for item in records:
            if item.get("parse_error"):
                continue
            consumer_id = str(Path(item["path"]).expanduser().resolve())
            for edge in item["edges"]:
                store.record_provenance_edge(
                    source_revision_id=edge["revision_id"],
                    span_start=edge["span_start"],
                    span_end=edge["span_end"],
                    consumer_type="wiki_page",
                    consumer_id=consumer_id,
                )
                edges_recorded += 1
            if item["gap"]:
                store.record_provenance_gap(
                    consumer_type="wiki_page",
                    consumer_id=consumer_id,
                    reason=GAP_REASON,
                    source_agent=item["source_agent"],
                    session_id=item["session_id"],
                )
                gaps_recorded += 1
            elif item["edges"]:
                store.resolve_provenance_gaps(
                    consumer_type="wiki_page", consumer_id=consumer_id
                )
        report.update(
            {
                "backup": str(backup_path),
                "edges_recorded": edges_recorded,
                "gaps_recorded": gaps_recorded,
                "gap_status": store.provenance_gap_counts(),
            }
        )
    finally:
        store.close()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        report["integrity_check"] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        report["after"] = inspect_database(db_path)
    return report


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(
            config.get("raw_event_store.db_path")
            or (config.database_dir / "raw_events.db")
        ),
    )
    parser.add_argument("--wiki-dir", type=Path, default=config.wiki_dir)
    parser.add_argument(
        "--backup-root", type=Path, default=config.database_dir / "backups"
    )
    args = parser.parse_args()
    report = reconcile(
        db_path=args.db.expanduser(),
        wiki_dir=args.wiki_dir.expanduser(),
        apply=args.apply,
        backup_root=args.backup_root.expanduser(),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
