from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import core.cognitive.material_effect_schema as schema_module

from core.cognitive.material_effect_schema import (
    CANONICAL_SCHEMA_HASH,
    REGISTRY_TABLE,
    MaterialEffectSchemaError,
    inspect_material_effect_schema,
    initialize_material_effect_schema,
    reconcile_material_effect_schema,
    validate_material_effect_schema,
)
from core.cognitive.material_effect_ledger import record_target_effect


def test_fresh_target_initialization_registers_exact_canonical_schema() -> None:
    with sqlite3.connect(":memory:") as conn:
        initialize_material_effect_schema(conn)
        state = inspect_material_effect_schema(conn)

    assert state.ok is True
    assert state.classification == "canonical"
    assert state.schema_hash == CANONICAL_SCHEMA_HASH


def test_relation_embedding_can_initialize_before_knowledge_graph(tmp_path) -> None:
    from core.embeddings.relation_manager import RelationEmbeddingManager
    from core.kia.knowledge_graph import KnowledgeGraph

    db_path = tmp_path / "knowledge_graph.db"
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    config = SimpleNamespace(
        database_dir=tmp_path,
        wiki_dir=wiki_dir,
        get=lambda _key, default=None: default,
    )
    manager = RelationEmbeddingManager(
        db_path=db_path,
        index_dir=tmp_path / "embedding_index",
        client=MagicMock(),
        config=config,
    )
    try:
        with sqlite3.connect(db_path) as conn:
            assert inspect_material_effect_schema(conn).ok is True
            assert conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='relation_context_embeddings'"
            ).fetchone() is not None

        graph = KnowledgeGraph(
            db_path=str(db_path),
            wiki_base=str(wiki_dir),
            config=config,
        )
        graph.close()
    finally:
        manager.close()


def test_schema_signature_rejects_noncanonical_table_identifier() -> None:
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(
            MaterialEffectSchemaError,
            match="unsupported material-effect schema table",
        ):
            schema_module._table_signature(conn, "unknown; DROP TABLE x")


def test_existing_domain_database_cannot_silently_create_effect_schema() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE domain_rows (id TEXT PRIMARY KEY)")

        with pytest.raises(MaterialEffectSchemaError, match="migration required"):
            initialize_material_effect_schema(conn)

        assert inspect_material_effect_schema(conn).classification == "absent"


def test_effect_recording_never_creates_schema_at_write_time() -> None:
    permit = SimpleNamespace(
        command_id="command-1",
        effect_id="effect-1",
        decision_revision_id="decision-1",
        action_id="action-1",
        owner="test-owner",
        executor_id="test-executor",
        action_type="test-action",
        target_ref="target-1",
        input_hash="sha256:" + "1" * 64,
    )
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE domain_rows (id TEXT PRIMARY KEY)")

        with pytest.raises(MaterialEffectSchemaError, match="migration required"):
            record_target_effect(
                conn,
                permit,
                status="committed",
                before_hash="sha256:" + "2" * 64,
                after_hash="sha256:" + "3" * 64,
                evidence_refs=("evidence:test",),
                observed_at="2026-07-17T00:00:00+00:00",
            )

        assert inspect_material_effect_schema(conn).classification == "absent"


def test_explicit_reconciliation_owns_absent_schema_creation() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE domain_rows (id TEXT PRIMARY KEY)")
        preview = reconcile_material_effect_schema(conn, apply=False)
        assert preview["migration_required"] is True
        assert preview["applied"] is False
        assert inspect_material_effect_schema(conn).classification == "absent"

        applied = reconcile_material_effect_schema(conn, apply=True)
        assert applied["applied"] is True
        validate_material_effect_schema(conn)


def test_unregistered_exact_legacy_schema_requires_explicit_registry_migration() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(
            """
            CREATE TABLE material_target_effects (
                command_id TEXT PRIMARY KEY,
                effect_id TEXT NOT NULL UNIQUE,
                decision_revision_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                executor_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                before_hash TEXT NOT NULL,
                after_hash TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                reason_code TEXT NOT NULL DEFAULT '',
                retry_exhausted INTEGER NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                schema_version TEXT NOT NULL
            );
            CREATE INDEX idx_material_target_effect_family
            ON material_target_effects(owner, executor_id, action_type);
            """
        )
        state = inspect_material_effect_schema(conn)
        assert state.classification == "canonical_unregistered"

        with pytest.raises(MaterialEffectSchemaError, match="migration required"):
            validate_material_effect_schema(conn)
        reconcile_material_effect_schema(conn, apply=True)
        validate_material_effect_schema(conn)


def test_unknown_effect_schema_fails_closed() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE material_target_effects (command_id TEXT PRIMARY KEY)"
        )

        with pytest.raises(MaterialEffectSchemaError, match="unknown"):
            reconcile_material_effect_schema(conn, apply=True)


def test_corrupt_or_orphan_registry_fails_closed_before_target_creation() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            f"CREATE TABLE {REGISTRY_TABLE} ("
            "component TEXT, schema_version TEXT, schema_hash TEXT, applied_at TEXT)"
        )

        state = inspect_material_effect_schema(conn)
        assert state.classification == "unknown"
        with pytest.raises(MaterialEffectSchemaError, match="unknown"):
            reconcile_material_effect_schema(conn, apply=True)
