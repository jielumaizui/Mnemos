# -*- coding: utf-8 -*-
"""KG consistency repair tests."""

from __future__ import annotations

import sqlite3

from core.kia.kg_consistency import audit_kg_consistency, repair_kg_consistency
from core.kia.relation_schema import Relation, RelationEvidence, RelationType
from tests.knowledge_graph_decision_fixtures import authorized_knowledge_graph


def _seed_relation(conn: sqlite3.Connection, source: str, target: str) -> int:
    cursor = conn.execute(
        """INSERT INTO relations
           (source, target, relation_type, strength, confidence, source_method, context)
           VALUES (?, ?, 'references', 0.8, 0.9, 'test', ?)""",
        (source, target, f"{source} references {target}"),
    )
    return int(cursor.lastrowid)


def test_repair_removes_hard_orphans_and_preserves_valid_rows(tmp_path, patched_get_config):
    db_path = tmp_path / "kg.db"
    graph = authorized_knowledge_graph(
        db_path=str(db_path),
        wiki_base=str(tmp_path / "wiki"),
    )

    with graph._conn() as conn:
        valid_rel_id = _seed_relation(conn, "a.md", "b.md")
        orphan_rel_id = valid_rel_id + 999
        conn.execute(
            "INSERT INTO relations_fts(rowid, content) VALUES (?, ?)",
            (valid_rel_id, "valid fts"),
        )
        conn.execute(
            "INSERT INTO relations_fts(rowid, content) VALUES (?, ?)",
            (orphan_rel_id, "orphan fts"),
        )
        conn.execute(
            """INSERT INTO relation_evidence (relation_id, evidence_type, content)
               VALUES (?, 'quote', 'valid evidence')""",
            (valid_rel_id,),
        )
        conn.execute(
            """INSERT INTO relation_evidence (relation_id, evidence_type, content)
               VALUES (?, 'quote', 'orphan evidence')""",
            (orphan_rel_id,),
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS relation_context_embeddings (
                id INTEGER PRIMARY KEY,
                relation_id INTEGER UNIQUE REFERENCES relations(id),
                embedding BLOB,
                model_version TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """INSERT INTO relation_context_embeddings
               (relation_id, embedding, model_version) VALUES (?, ?, 'test')""",
            (valid_rel_id, b"valid"),
        )
        conn.execute(
            """INSERT INTO relation_context_embeddings
               (relation_id, embedding, model_version) VALUES (?, ?, 'test')""",
            (orphan_rel_id, b"orphan"),
        )
        conn.commit()

    before = audit_kg_consistency(db_path)
    assert before["status"] == "degraded"
    assert before["hard_orphans"]["relation_evidence"] == 1
    assert before["hard_orphans"]["relation_context_embeddings"] == 1
    assert before["hard_orphans"]["relations_fts"] == 1

    dry_run = repair_kg_consistency(db_path, apply=False)
    assert dry_run["dry_run"] is True
    assert dry_run["would_delete"]["relation_evidence"] == 1
    assert dry_run["would_delete"]["relation_context_embeddings"] == 1
    assert dry_run["would_delete"]["relations_fts"] == 1

    applied = repair_kg_consistency(db_path, apply=True)
    assert applied["dry_run"] is False
    assert applied["deleted"]["relation_evidence"] == 1
    assert applied["deleted"]["relation_context_embeddings"] == 1
    assert applied["deleted"]["relations_fts"] == 1

    after = audit_kg_consistency(db_path)
    assert after["status"] == "ok"
    assert after["hard_orphans"]["relation_evidence"] == 0
    assert after["hard_orphans"]["relation_context_embeddings"] == 0
    assert after["hard_orphans"]["relations_fts"] == 0
    assert after["counts"]["relations"] == 1
    assert after["counts"]["relation_evidence"] == 1
    assert after["counts"]["relation_context_embeddings"] == 1
    assert after["counts"]["relations_fts"] == 1


def test_repair_rebuilds_missing_fts_rows(tmp_path, patched_get_config):
    db_path = tmp_path / "kg.db"
    graph = authorized_knowledge_graph(
        db_path=str(db_path),
        wiki_base=str(tmp_path / "wiki"),
    )

    with graph._conn() as conn:
        rel_id = _seed_relation(conn, "source.md", "target.md")
        conn.commit()

    before = audit_kg_consistency(db_path)
    assert before["search_index"]["relations_missing_fts"] == 1

    applied = repair_kg_consistency(db_path, apply=True)
    assert applied["inserted"]["relations_fts"] == 1

    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT content FROM relations_fts WHERE rowid=?", (rel_id,)
        ).fetchone()
    assert row is not None
    assert "source.md" in row[0]
    assert "target.md" in row[0]


def test_repeated_add_relation_does_not_create_hard_orphans(tmp_path, patched_get_config):
    db_path = tmp_path / "kg.db"
    graph = authorized_knowledge_graph(
        db_path=str(db_path),
        wiki_base=str(tmp_path / "wiki"),
    )
    relation = Relation(
        source="a.md",
        target="b.md",
        relation_type=RelationType.REFERENCES,
        strength=0.8,
        confidence=0.9,
        source_method="test",
        evidence=[RelationEvidence(evidence_type="quote", content="first")],
    )

    assert graph.add_relation(relation) is True
    relation.evidence = [RelationEvidence(evidence_type="quote", content="updated")]
    assert graph.add_relation(relation) is True

    report = audit_kg_consistency(db_path)
    assert report["status"] == "ok"
    assert report["counts"]["relations"] == 1
    assert report["counts"]["relation_evidence"] == 1
    assert report["counts"]["relations_fts"] == 1
    assert report["hard_orphans"]["relation_evidence"] == 0
    assert report["hard_orphans"]["relations_fts"] == 0
