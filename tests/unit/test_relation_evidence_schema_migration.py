from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.kia.knowledge_graph import KnowledgeGraph
from core.kia.relation_evidence_schema import (
    CANONICAL_DDL_HASH,
    SCHEMA_VERSION,
    RelationEvidenceSchemaError,
    inspect_relation_evidence_schema,
    reconcile_relation_evidence_schema,
)
from core.kia.relation_manager import RelationManager
from scripts.audit_schema_registry import build_report
from scripts.reconcile_relation_evidence_schema import main as reconcile_main


RELATIONS_DDL = """
CREATE TABLE relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    UNIQUE(source, target, relation_type)
)
"""

LEGACY_KG_DDL = """
CREATE TABLE relation_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    content TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (relation_id) REFERENCES relations(id) ON DELETE CASCADE
)
"""

LEGACY_RM_DDL = """
CREATE TABLE relation_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER NOT NULL,
    evidence_type TEXT DEFAULT 'quote',
    content TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (relation_id) REFERENCES relations(id) ON DELETE CASCADE
)
"""


def _legacy_db(path: Path, ddl: str, *, evidence_type: str | None = "quote") -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(RELATIONS_DDL)
        conn.execute(ddl)
        relation_id = conn.execute(
            "INSERT INTO relations(source, target, relation_type) VALUES ('a', 'b', 'related_to')"
        ).lastrowid
        conn.execute(
            "INSERT INTO relation_evidence(relation_id, evidence_type, content) VALUES (?, ?, ?)",
            (relation_id, evidence_type, "proof"),
        )


@pytest.mark.parametrize("first", ["knowledge_graph", "relation_manager"])
def test_fresh_initialization_order_has_one_registered_schema(tmp_path: Path, first: str) -> None:
    db = tmp_path / f"{first}.db"
    if first == "knowledge_graph":
        KnowledgeGraph(db_path=str(db), wiki_base=str(tmp_path / "wiki"))
        RelationManager(str(db))
    else:
        RelationManager(str(db))
        KnowledgeGraph(db_path=str(db), wiki_base=str(tmp_path / "wiki"))

    with sqlite3.connect(db) as conn:
        state = inspect_relation_evidence_schema(conn)
    assert state.ok is True
    assert state.ddl_hash == CANONICAL_DDL_HASH
    assert state.registry_version == SCHEMA_VERSION


@pytest.mark.parametrize(
    ("ddl", "classification"),
    [
        (LEGACY_KG_DDL, "legacy_knowledge_graph_v0"),
        (LEGACY_RM_DDL, "legacy_relation_manager_v0"),
    ],
)
def test_recognized_legacy_schemas_migrate_without_losing_rows(
    tmp_path: Path, ddl: str, classification: str
) -> None:
    db = tmp_path / "legacy.db"
    _legacy_db(db, ddl)
    with sqlite3.connect(db) as conn:
        preview = reconcile_relation_evidence_schema(conn)
        assert preview["applied"] is False
        assert preview["before"]["classification"] == classification
        assert preview["before"]["migration_required"] is True
        report = reconcile_relation_evidence_schema(conn, apply=True)
        assert report["row_count_preserved"] is True
        assert report["after"]["row_count"] == 1
        assert report["after"]["ok"] is True


def test_missing_evidence_type_blocks_without_guessing_or_mutating(tmp_path: Path) -> None:
    db = tmp_path / "null.db"
    _legacy_db(db, LEGACY_RM_DDL, evidence_type=None)
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT sql FROM sqlite_master WHERE name='relation_evidence'").fetchone()[0]
        with pytest.raises(RelationEvidenceSchemaError, match="manual classification"):
            reconcile_relation_evidence_schema(conn, apply=True)
        after = conn.execute("SELECT sql FROM sqlite_master WHERE name='relation_evidence'").fetchone()[0]
        assert after == before
        assert conn.execute("SELECT evidence_type FROM relation_evidence").fetchone()[0] is None


def test_unknown_schema_is_not_automatically_rewritten(tmp_path: Path) -> None:
    db = tmp_path / "unknown.db"
    with sqlite3.connect(db) as conn:
        conn.execute(RELATIONS_DDL)
        conn.execute(LEGACY_KG_DDL.replace("created_at TEXT", "unexpected TEXT, created_at TEXT"))
        before = conn.execute("PRAGMA table_info(relation_evidence)").fetchall()
        with pytest.raises(RelationEvidenceSchemaError, match="unknown"):
            reconcile_relation_evidence_schema(conn, apply=True)
        assert conn.execute("PRAGMA table_info(relation_evidence)").fetchall() == before


def test_unknown_schema_missing_required_columns_reports_without_query_crash(
    tmp_path: Path,
) -> None:
    db = tmp_path / "missing-columns.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE relation_evidence(id INTEGER PRIMARY KEY, payload TEXT)")
        state = inspect_relation_evidence_schema(conn)
        assert state.classification == "unknown"
        assert state.migration_required is True
        assert "automatic migration is refused" in state.errors[0]


def test_registered_schema_with_missing_index_is_not_canonical(tmp_path: Path) -> None:
    db = tmp_path / "missing-index.db"
    RelationManager(str(db))
    with sqlite3.connect(db) as conn:
        conn.execute("DROP INDEX idx_evidence_relation")
        state = inspect_relation_evidence_schema(conn)
        assert state.classification == "legacy_knowledge_graph_v0"
        assert state.migration_required is True


def test_invalid_registry_schema_fails_closed_with_repair_message(tmp_path: Path) -> None:
    db = tmp_path / "invalid-registry.db"
    _legacy_db(db, LEGACY_KG_DDL)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE mnemos_schema_registry(component TEXT PRIMARY KEY)")
        with pytest.raises(RelationEvidenceSchemaError, match="explicit repair is required"):
            inspect_relation_evidence_schema(conn)


def test_failed_rebuild_rolls_back_to_legacy_schema(tmp_path: Path) -> None:
    db = tmp_path / "rollback.db"
    _legacy_db(db, LEGACY_RM_DDL)

    def fail_copy(_conn: sqlite3.Connection) -> None:
        raise RuntimeError("injected copy failure")

    with sqlite3.connect(db) as conn:
        with pytest.raises(RuntimeError, match="injected"):
            reconcile_relation_evidence_schema(conn, apply=True, copy_rows=fail_copy)
        state = inspect_relation_evidence_schema(conn)
        assert state.classification == "legacy_relation_manager_v0"
        assert state.row_count == 1


@pytest.mark.parametrize("constructor", ["knowledge_graph", "relation_manager"])
def test_constructor_blocks_legacy_schema_before_other_ddl(
    tmp_path: Path, constructor: str
) -> None:
    db = tmp_path / f"blocked-{constructor}.db"
    _legacy_db(db, LEGACY_RM_DDL)
    with sqlite3.connect(db) as conn:
        before_tables = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
        ).fetchall()
        before_relations = conn.execute("PRAGMA table_info(relations)").fetchall()

    with pytest.raises(RelationEvidenceSchemaError, match="before database initialization"):
        if constructor == "knowledge_graph":
            KnowledgeGraph(db_path=str(db), wiki_base=str(tmp_path / "wiki"))
        else:
            RelationManager(str(db))

    with sqlite3.connect(db) as conn:
        after_tables = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
        ).fetchall()
        assert after_tables == before_tables
        assert conn.execute("PRAGMA table_info(relations)").fetchall() == before_relations


def test_schema_audit_requires_single_owner_and_registered_hash(tmp_path: Path) -> None:
    db = tmp_path / "canonical.db"
    RelationManager(str(db))
    report = build_report(db_path=db)
    assert report["ok"] is True
    assert report["ddl_owners"] == ["core/kia/relation_evidence_schema.py"]


def test_schema_audit_allows_not_yet_initialized_ci_database(tmp_path: Path) -> None:
    report = build_report(db_path=tmp_path / "absent.db")
    assert report["ok"] is True
    assert report["state"]["classification"] == "not_initialized"


def test_schema_audit_rejects_if_not_exists_duplicate_owner(tmp_path: Path) -> None:
    authority = tmp_path / "core" / "kia" / "relation_evidence_schema.py"
    duplicate = tmp_path / "scripts" / "duplicate.py"
    authority.parent.mkdir(parents=True)
    duplicate.parent.mkdir(parents=True)
    authority.write_text("CREATE TABLE relation_evidence (id INTEGER)", encoding="utf-8")
    duplicate.write_text(
        "CREATE TABLE IF NOT EXISTS relation_evidence (id INTEGER)", encoding="utf-8"
    )

    report = build_report(db_path=tmp_path / "absent.db", root=tmp_path)
    assert report["ok"] is False
    assert report["ddl_owners"] == [
        "core/kia/relation_evidence_schema.py",
        "scripts/duplicate.py",
    ]


def test_reconcile_cli_requires_backup_dir_for_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "legacy.db"
    _legacy_db(db, LEGACY_KG_DDL)
    assert reconcile_main(["--db-path", str(db), "--apply", "--json"]) == 2
    assert "--apply requires --backup-dir" in capsys.readouterr().out


def test_reconcile_cli_creates_verified_backup_and_preserves_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "legacy.db"
    backup_dir = tmp_path / "backups"
    _legacy_db(db, LEGACY_RM_DDL)

    assert (
        reconcile_main(
            [
                "--db-path",
                str(db),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    backup = Path(payload["backup"]["path"])
    assert backup.exists()
    assert payload["backup"]["integrity_check"] == "ok"
    assert payload["row_count_preserved"] is True
    with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM relation_evidence").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='mnemos_schema_registry'"
            ).fetchone()[0]
            == 0
        )
