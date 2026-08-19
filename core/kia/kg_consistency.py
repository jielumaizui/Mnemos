# -*- coding: utf-8 -*-
"""Knowledge graph consistency audit and repair helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.db_utils import sqlite_conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}  # nosec B608


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0] if row else 0)


def _safe_count(conn: sqlite3.Connection, table_name: str) -> int:
    if not _table_exists(conn, table_name):
        return 0
    return _count(conn, f"SELECT COUNT(*) FROM {table_name}")  # nosec B608: fixed table names


def _integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "unknown")


def _endpoint_exists_in_wiki(endpoint: str, wiki_base: Path | None) -> bool:
    if not endpoint or wiki_base is None:
        return False
    candidates = [wiki_base / endpoint]
    if not endpoint.endswith(".md"):
        candidates.append(wiki_base / f"{endpoint}.md")
    return any(candidate.exists() for candidate in candidates)


def _endpoint_gap_summary(
    conn: sqlite3.Connection,
    *,
    wiki_base: Path | None = None,
    sample_limit: int = 10,
) -> dict[str, Any]:
    if not (_table_exists(conn, "relations") and _table_exists(conn, "entities")):
        return {"count": 0, "samples": [], "checked": False}
    relation_columns = _table_columns(conn, "relations")
    entity_columns = _table_columns(conn, "entities")
    if not {"source", "target"}.issubset(relation_columns) or not {
        "name",
        "uid",
    }.issubset(entity_columns):
        return {"count": 0, "samples": [], "checked": False}

    endpoints = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT source FROM relations UNION SELECT DISTINCT target FROM relations"
        ).fetchall()
        if row[0]
    }
    entity_rows = conn.execute("SELECT name, uid FROM entities").fetchall()
    entity_names = {str(row[0]) for row in entity_rows if row[0]}
    entity_uids = {str(row[1]) for row in entity_rows if row[1]}
    missing: list[str] = []
    for endpoint in sorted(endpoints):
        if endpoint in entity_names or endpoint in entity_uids:
            continue
        if _endpoint_exists_in_wiki(endpoint, wiki_base):
            continue
        missing.append(endpoint)
    return {
        "count": len(missing),
        "samples": missing[:sample_limit],
        "checked": True,
    }


def audit_kg_consistency(
    db_path: str | Path,
    *,
    wiki_base: str | Path | None = None,
    sample_limit: int = 10,
) -> dict[str, Any]:
    """Return a machine-readable consistency report for knowledge_graph.db."""
    path = Path(db_path).expanduser()
    if not path.exists():
        return {
            "schema_version": "mnemos.kg_consistency.v1",
            "status": "missing",
            "db_path": str(path),
            "errors": ["knowledge_graph.db missing"],
        }

    wiki_path = Path(wiki_base).expanduser() if wiki_base else None
    with sqlite_conn(str(path), timeout=30) as conn:
        conn.row_factory = sqlite3.Row  # noqa: PLW2901
        required_tables = ("relations", "relation_evidence", "relations_fts")
        missing_required = [
            table for table in required_tables if not _table_exists(conn, table)
        ]
        integrity = _integrity_check(conn)
        counts = {
            "relations": _safe_count(conn, "relations"),
            "relation_evidence": _safe_count(conn, "relation_evidence"),
            "relation_context_embeddings": _safe_count(
                conn, "relation_context_embeddings"
            ),
            "relations_fts": _safe_count(conn, "relations_fts"),
            "entities": _safe_count(conn, "entities"),
        }
        hard_orphans = {
            "relation_evidence": 0,
            "relation_context_embeddings": 0,
            "relations_fts": 0,
        }
        search_index = {"relations_missing_fts": 0}
        if _table_exists(conn, "relations"):
            if _table_exists(conn, "relation_evidence"):
                hard_orphans["relation_evidence"] = _count(
                    conn,
                    """SELECT COUNT(*)
                       FROM relation_evidence e
                       LEFT JOIN relations r ON r.id = e.relation_id
                       WHERE r.id IS NULL""",
                )
            if _table_exists(conn, "relation_context_embeddings"):
                hard_orphans["relation_context_embeddings"] = _count(
                    conn,
                    """SELECT COUNT(*)
                       FROM relation_context_embeddings e
                       LEFT JOIN relations r ON r.id = e.relation_id
                       WHERE r.id IS NULL""",
                )
            if _table_exists(conn, "relations_fts"):
                hard_orphans["relations_fts"] = _count(
                    conn,
                    """SELECT COUNT(*)
                       FROM relations_fts
                       WHERE rowid NOT IN (SELECT id FROM relations)""",
                )
                search_index["relations_missing_fts"] = _count(
                    conn,
                    """SELECT COUNT(*)
                       FROM relations
                       WHERE id NOT IN (SELECT rowid FROM relations_fts)""",
                )
        endpoint_gaps = _endpoint_gap_summary(
            conn,
            wiki_base=wiki_path,
            sample_limit=sample_limit,
        )

    errors: list[str] = []
    if integrity != "ok":
        errors.append(f"integrity_check={integrity}")
    if missing_required:
        errors.append(f"missing_required_tables={','.join(missing_required)}")
    for name, value in hard_orphans.items():
        if value:
            errors.append(f"{name}_orphans={value}")
    if search_index["relations_missing_fts"]:
        errors.append(f"relations_missing_fts={search_index['relations_missing_fts']}")

    return {
        "schema_version": "mnemos.kg_consistency.v1",
        "status": "ok" if not errors else "degraded",
        "db_path": str(path),
        "integrity_check": integrity,
        "counts": counts,
        "hard_orphans": hard_orphans,
        "search_index": search_index,
        "endpoint_gaps": endpoint_gaps,
        "errors": errors,
    }


def _backup_database(db_path: Path, backup_root: Path | None = None) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    root = backup_root or db_path.parent / "backups" / "kg-consistency"
    root.mkdir(parents=True, exist_ok=True)
    backup_path = root / f"{db_path.stem}-{timestamp}.db"
    with sqlite3.connect(str(db_path), timeout=30) as source:
        with sqlite3.connect(str(backup_path), timeout=30) as target:
            source.backup(target)
    return backup_path


def _repair_missing_fts(conn: sqlite3.Connection, *, apply: bool) -> int:
    missing = _count(
        conn,
        """SELECT COUNT(*)
           FROM relations
           WHERE id NOT IN (SELECT rowid FROM relations_fts)""",
    )
    if not apply or missing == 0:
        return missing
    conn.execute(
        """INSERT INTO relations_fts(rowid, content)
           SELECT id,
                  source || ' ' || target || ' ' || relation_type || ' ' || COALESCE(context, '')
           FROM relations
           WHERE id NOT IN (SELECT rowid FROM relations_fts)"""
    )
    return missing


def repair_kg_consistency(
    db_path: str | Path,
    *,
    apply: bool = False,
    wiki_base: str | Path | None = None,
    create_backup: bool = False,
    backup_root: str | Path | None = None,
) -> dict[str, Any]:
    """Repair hard KG consistency drift.

    The repair is intentionally narrow: it removes child rows that point to
    missing relations and rebuilds missing FTS rows. Relation endpoint semantic
    gaps are reported by audit_kg_consistency but are not destructively fixed.
    """
    path = Path(db_path).expanduser()
    before = audit_kg_consistency(path, wiki_base=wiki_base)
    deleted = {
        "relation_evidence": 0,
        "relation_context_embeddings": 0,
        "relations_fts": 0,
    }
    inserted = {"relations_fts": 0}
    backup_path = ""

    if apply and create_backup:
        backup_path = str(
            _backup_database(
                path,
                Path(backup_root).expanduser() if backup_root else None,
            )
        )

    with sqlite_conn(str(path), timeout=30) as conn:
        if _table_exists(conn, "relations") and _table_exists(conn, "relation_evidence"):
            count = _count(
                conn,
                """SELECT COUNT(*)
                   FROM relation_evidence
                   WHERE relation_id NOT IN (SELECT id FROM relations)""",
            )
            if apply and count:
                cursor = conn.execute(
                    """DELETE FROM relation_evidence
                       WHERE relation_id NOT IN (SELECT id FROM relations)"""
                )
                deleted["relation_evidence"] = int(cursor.rowcount or 0)
            else:
                deleted["relation_evidence"] = count

        if _table_exists(conn, "relations") and _table_exists(
            conn, "relation_context_embeddings"
        ):
            count = _count(
                conn,
                """SELECT COUNT(*)
                   FROM relation_context_embeddings
                   WHERE relation_id NOT IN (SELECT id FROM relations)""",
            )
            if apply and count:
                cursor = conn.execute(
                    """DELETE FROM relation_context_embeddings
                       WHERE relation_id NOT IN (SELECT id FROM relations)"""
                )
                deleted["relation_context_embeddings"] = int(cursor.rowcount or 0)
            else:
                deleted["relation_context_embeddings"] = count

        if _table_exists(conn, "relations") and _table_exists(conn, "relations_fts"):
            count = _count(
                conn,
                """SELECT COUNT(*)
                   FROM relations_fts
                   WHERE rowid NOT IN (SELECT id FROM relations)""",
            )
            if apply and count:
                cursor = conn.execute(
                    """DELETE FROM relations_fts
                       WHERE rowid NOT IN (SELECT id FROM relations)"""
                )
                deleted["relations_fts"] = int(cursor.rowcount or 0)
            else:
                deleted["relations_fts"] = count
            inserted["relations_fts"] = _repair_missing_fts(conn, apply=apply)

        if apply:
            conn.commit()

    after = audit_kg_consistency(path, wiki_base=wiki_base)
    dry_run = not apply
    return {
        "schema_version": "mnemos.kg_consistency_repair.v1",
        "dry_run": dry_run,
        "db_path": str(path),
        "backup_path": backup_path,
        "before": before,
        "after": after if apply else before,
        "would_delete": deleted if dry_run else {},
        "deleted": {} if dry_run else deleted,
        "would_insert": inserted if dry_run else {},
        "inserted": {} if dry_run else inserted,
        "status": after["status"] if apply else before["status"],
    }


def emit_report(payload: dict[str, Any], *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))
