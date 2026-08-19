# -*- coding: utf-8 -*-
"""Low-level relation row writing helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from core.kia.relation_endpoint_quality import relation_endpoint_rejection_reason
from core.kia.relation_schema import Relation


def upsert_relation_row(
    conn: sqlite3.Connection,
    relation: Relation,
    *,
    source: str,
    target: str,
    insert_evidence: bool,
) -> tuple[int, bool, bool]:
    """Insert or update a relation without changing an existing rowid."""
    source_reason = relation_endpoint_rejection_reason(source)
    target_reason = relation_endpoint_rejection_reason(target)
    if source_reason or target_reason:
        raise ValueError(
            "invalid relation endpoint: "
            f"source={source_reason or 'ok'} target={target_reason or 'ok'}"
        )

    relation_type = relation.relation_type.value
    row = conn.execute(
        """SELECT id, strength, confidence, source_method, context
           FROM relations WHERE source=? AND target=? AND relation_type=?""",
        (source, target, relation_type),
    ).fetchone()
    updated_at = datetime.now(timezone.utc).isoformat()[:19]
    if row is not None:
        rel_id = int(row[0])
        if not insert_evidence:
            # Synthetic reverse completion is INSERT-OR-IGNORE. Once the
            # opposite direction exists, only its own primary discovery may
            # update context, score, method, or evidence.
            return rel_id, True, False
        existing_evidence = [
            (str(item[0]), str(item[1]))
            for item in conn.execute(
                """SELECT evidence_type, content FROM relation_evidence
                   WHERE relation_id=? ORDER BY id""",
                (rel_id,),
            ).fetchall()
        ]
        desired_evidence = (
            [(str(ev.evidence_type), str(ev.content)) for ev in (relation.evidence or [])]
            if insert_evidence
            else []
        )
        unchanged = (
            float(row[1]) == float(relation.strength)
            and float(row[2]) == float(relation.confidence)
            and str(row[3] or "") == str(relation.source_method or "")
            and str(row[4] or "") == str(relation.context or "")
            and existing_evidence == desired_evidence
        )
        if unchanged:
            return rel_id, True, False
        conn.execute(
            """UPDATE relations
               SET strength=?, confidence=?, source_method=?, context=?, updated_at=?
               WHERE id=?""",
            (
                relation.strength,
                relation.confidence,
                relation.source_method,
                relation.context,
                updated_at,
                rel_id,
            ),
        )
        conn.execute("DELETE FROM relations_fts WHERE rowid=?", (rel_id,))
        if insert_evidence:
            conn.execute("DELETE FROM relation_evidence WHERE relation_id=?", (rel_id,))
        existed = True
        changed = True
    else:
        cursor = conn.execute(
            """INSERT INTO relations
               (
                   source, target, relation_type, strength,
                   confidence, source_method, context, updated_at
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source,
                target,
                relation_type,
                relation.strength,
                relation.confidence,
                relation.source_method,
                relation.context,
                updated_at,
            ),
        )
        if cursor.lastrowid is None:
            raise sqlite3.DatabaseError("relation insert did not return rowid")
        rel_id = int(cursor.lastrowid)
        existed = False
        changed = True

    fts_text = f"{source} {target} {relation_type} {relation.context or ''}"
    conn.execute("INSERT INTO relations_fts(rowid, content) VALUES (?, ?)", (rel_id, fts_text))

    if insert_evidence:
        for ev in relation.evidence or []:
            conn.execute(
                """INSERT INTO relation_evidence (relation_id, evidence_type, content)
                   VALUES (?, ?, ?)""",
                (rel_id, ev.evidence_type, ev.content),
            )
    return rel_id, existed, changed
