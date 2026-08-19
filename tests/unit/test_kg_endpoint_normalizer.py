import json
import sqlite3
from pathlib import Path

from core.kia.kg_endpoint_normalizer import normalize_kg_endpoints


def _create_kg_db(path: Path) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE entities (
                uid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT DEFAULT 'concept',
                source_page TEXT DEFAULT '',
                quality_score REAL DEFAULT 0.5,
                confidence REAL DEFAULT 0.5,
                temporal_scope TEXT DEFAULT 'stable',
                version_info TEXT,
                status TEXT DEFAULT 'active',
                visit_count INTEGER DEFAULT 0,
                tags TEXT DEFAULT '[]',
                first_seen TEXT,
                last_updated TEXT,
                source_count INTEGER DEFAULT 1
            );
            CREATE TABLE relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                strength REAL DEFAULT 0.5,
                confidence REAL DEFAULT 0.5,
                source_method TEXT DEFAULT 'auto',
                context TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, target, relation_type)
            );
            CREATE VIRTUAL TABLE relations_fts USING fts5(content);
            CREATE TABLE relation_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                relation_id INTEGER,
                evidence_type TEXT,
                content TEXT
            );
            CREATE TABLE relation_context_embeddings (
                id INTEGER PRIMARY KEY,
                relation_id INTEGER UNIQUE,
                embedding BLOB,
                model_version TEXT
            );
            """
        )
        conn.execute(
            """INSERT INTO entities
               (uid, name, first_seen, last_updated)
               VALUES ('known', 'Known', 'now', 'now')"""
        )
        conn.commit()


def _insert_relation(
    db_path: Path,
    source: str,
    target: str,
    relation_type: str = "related_to",
) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            """INSERT INTO relations
               (source, target, relation_type, context)
               VALUES (?, ?, ?, 'ctx')""",
            (source, target, relation_type),
        )
        relation_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO relations_fts(rowid, content) VALUES (?, ?)",
            (relation_id, f"{source} {target} {relation_type} ctx"),
        )
        conn.commit()
        return relation_id


def test_normalize_endpoints_applies_path_migration_and_concept_entity(tmp_path):
    db_path = tmp_path / "knowledge_graph.db"
    wiki = tmp_path / "wiki"
    page = wiki / "03-Tech" / "old-page-title.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\n标题: Old Page\n---\nbody\n", encoding="utf-8")
    _create_kg_db(db_path)
    rel_id = _insert_relation(db_path, "old-page-title", "Clean Concept")
    _insert_relation(db_path, "---", "Known")

    dry_run = normalize_kg_endpoints(
        db_path,
        wiki_base=wiki,
        min_concept_refs=1,
        sample_limit=5,
    )

    assert dry_run["dry_run"] is True
    assert dry_run["would_apply"] == {"path_migrations": 1, "concept_entities": 1}
    assert dry_run["classification"]["samples"]["path_migrations"][0]["target"] == (
        "03-Tech/old-page-title.md"
    )
    assert dry_run["classification"]["samples"]["concept_entities"][0]["endpoint"] == (
        "Clean Concept"
    )

    result = normalize_kg_endpoints(
        db_path,
        wiki_base=wiki,
        apply=True,
        create_backup=True,
        min_concept_refs=1,
    )

    assert result["dry_run"] is False
    assert result["applied"]["relations_updated"] == 1
    assert result["applied"]["entities_inserted"] == 1
    assert result["backup_path"]
    assert Path(result["backup_path"]).exists()
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT source, target FROM relations WHERE id=?",
            (rel_id,),
        ).fetchone()
        assert row == ("03-Tech/old-page-title.md", "Clean Concept")
        content = conn.execute(
            "SELECT content FROM relations_fts WHERE rowid=?",
            (rel_id,),
        ).fetchone()[0]
        assert "03-Tech/old-page-title.md" in content
        entity = conn.execute(
            "SELECT entity_type, tags, source_count FROM entities WHERE name='Clean Concept'"
        ).fetchone()
        assert entity[0] == "concept"
        assert "kg_endpoint_auto" in json.loads(entity[1])
        assert entity[2] == 1


def test_normalize_endpoints_skips_relation_unique_conflicts(tmp_path):
    db_path = tmp_path / "knowledge_graph.db"
    wiki = tmp_path / "wiki"
    page = wiki / "04-Concepts" / "topic.md"
    page.parent.mkdir(parents=True)
    page.write_text("body\n", encoding="utf-8")
    _create_kg_db(db_path)
    old_id = _insert_relation(db_path, "topic", "Known")
    _insert_relation(db_path, "04-Concepts/topic.md", "Known")

    result = normalize_kg_endpoints(
        db_path,
        wiki_base=wiki,
        apply=True,
        min_concept_refs=1,
    )

    assert result["applied"]["relations_updated"] == 0
    assert result["applied"]["skipped_conflicts"][0]["relation_id"] == old_id
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT source, target FROM relations WHERE id=?",
            (old_id,),
        ).fetchone()
        assert row == ("topic", "Known")


def test_normalize_endpoints_prunes_invalid_relations_when_enabled(tmp_path):
    db_path = tmp_path / "knowledge_graph.db"
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    _create_kg_db(db_path)

    invalid_marker = _insert_relation(db_path, "---", "Known")
    invalid_multiline = _insert_relation(db_path, "Active\n  Policy", "Known")
    invalid_projection = _insert_relation(db_path, "L2.4-KG/Relations/rel-1.md", "Known")
    retained_legacy_entity = _insert_relation(db_path, "L2.4-KG/Entities/agent.md", "Known")

    with sqlite3.connect(str(db_path)) as conn:
        for relation_id in (invalid_marker, invalid_multiline, invalid_projection):
            conn.execute(
                """INSERT INTO relation_evidence (relation_id, evidence_type, content)
                   VALUES (?, 'test', 'evidence')""",
                (relation_id,),
            )
            conn.execute(
                """INSERT INTO relation_context_embeddings
                   (relation_id, embedding, model_version) VALUES (?, '[]', 'test')""",
                (relation_id,),
            )
        conn.commit()

    dry_run = normalize_kg_endpoints(
        db_path,
        wiki_base=wiki,
        prune_invalid=True,
    )
    assert dry_run["dry_run"] is True
    assert dry_run["would_apply"]["invalid_relations_deleted"] == 3
    assert dry_run["classification"]["counts"]["invalid_endpoints"] == 3

    result = normalize_kg_endpoints(
        db_path,
        wiki_base=wiki,
        apply=True,
        prune_invalid=True,
    )

    assert result["applied"]["invalid_relations_deleted"] == 3
    assert result["applied"]["invalid_fts_deleted"] == 3
    assert result["applied"]["invalid_evidence_deleted"] == 3
    assert result["applied"]["invalid_embeddings_deleted"] == 3
    with sqlite3.connect(str(db_path)) as conn:
        deleted = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE id IN (?, ?, ?)",
            (invalid_marker, invalid_multiline, invalid_projection),
        ).fetchone()[0]
        assert deleted == 0
        retained = conn.execute(
            "SELECT source, target FROM relations WHERE id=?",
            (retained_legacy_entity,),
        ).fetchone()
        assert retained == ("L2.4-KG/Entities/agent.md", "Known")
