from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from core.cognitive_graph.store import (
    COGNITIVE_RELATION_ACTION,
    COGNITIVE_RELATION_EXECUTOR,
    COGNITIVE_RELATION_OWNER,
    CognitiveGraphStore,
    cognitive_relation_material_action_binding,
)
from core.cognitive_graph.updater import CognitiveGraphUpdater
from core.kia.entity_manager import EntityManager
from core.kia.kg_event_handler import KGEventHandler
from core.kia.knowledge_graph import KnowledgeGraph
from core.kia.relation_schema import Relation, RelationType
from core.mnemos_bus import Event
from core.wiki_projection_lifecycle import WikiProjectionLedger
from tests.cognitive_decision_fixtures import material_action_authorization


def test_kg_consumer_migrates_then_deletes_exact_page_endpoints(tmp_path, monkeypatch):
    embedding_manager = MagicMock()
    embedding_manager.add_relation_context.return_value = True
    embedding_manager.remove_relation_projection.return_value = True
    embedding_manager.flush.return_value = True
    monkeypatch.setattr(
        KnowledgeGraph,
        "_rel_emb_mgr",
        property(lambda _self: embedding_manager),
    )
    vault = tmp_path / "vault"
    old = vault / "00-Inbox" / "old.md"
    new = vault / "04-Concepts" / "new.md"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_text("# Old\n", encoding="utf-8")
    db = tmp_path / "knowledge_graph.db"
    em = EntityManager(db_path=db)
    em.add_entity("Old entity", wiki_page=str(old.resolve()))
    kg = KnowledgeGraph(db_path=str(db), wiki_base=str(vault))
    assert kg.add_relation(
        Relation(
            source="00-Inbox/old.md",
            target="04-Concepts/target.md",
            relation_type=RelationType.RELATED_TO,
            confidence=0.9,
        )
    )
    old.rename(new)
    handler = KGEventHandler(db_path=db, wiki_base=vault)
    monkeypatch.setattr(
        handler,
        "_project_kg_to_vault",
        lambda _kg: {
            "projection_enabled": True,
            "projection_entities": 1,
            "projection_relations": 1,
            "projection_errors": 0,
        },
    )

    moved = handler.on_page_updated(
        {
            "page_path": str(new),
            "previous_path": str(old),
            "mutation_type": "move",
        }
    )
    assert moved["status"] == "ok"
    with sqlite3.connect(db) as conn:
        source_page = conn.execute(
            "SELECT source_page FROM entities WHERE name='Old entity'"
        ).fetchone()[0]
        source_rows = conn.execute(
            "SELECT source_page FROM entity_sources WHERE entity_uid='old-entity'"
        ).fetchall()
        endpoints = conn.execute("SELECT source, target FROM relations").fetchall()
    assert source_page == str(new.resolve())
    assert source_rows == [(str(new.resolve()),)]
    assert ("04-Concepts/new.md", "04-Concepts/target.md") in endpoints

    new.unlink()
    deleted = handler.on_page_updated(
        {"page_path": str(new), "previous_path": str(new), "mutation_type": "delete"}
    )
    assert deleted["status"] == "ok"
    with sqlite3.connect(db) as conn:
        entity = conn.execute(
            "SELECT source_page, status, source_count FROM entities WHERE name='Old entity'"
        ).fetchone()
        source_count = conn.execute(
            "SELECT COUNT(*) FROM entity_sources WHERE entity_uid='old-entity'"
        ).fetchone()[0]
        relation_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    assert entity == ("", "source_missing", 0)
    assert source_count == 0
    assert relation_count == 0
    assert embedding_manager.add_relation_context.call_count >= 2
    embedding_manager.remove_relation_projection.assert_called()


def test_kg_move_embedding_failure_is_repaired_on_retry(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    old = vault / "old.md"
    new = vault / "new.md"
    vault.mkdir()
    old.write_text("# Old", encoding="utf-8")
    db = tmp_path / "knowledge_graph.db"
    kg = KnowledgeGraph(db_path=str(db), wiki_base=str(vault))
    monkeypatch.setattr(kg._rel_emb_mgr, "add_relation_context", lambda *args, **kwargs: True)
    assert kg.add_relation(
        Relation(
            source="old.md",
            target="target.md",
            relation_type=RelationType.DEPENDS_ON,
            context="old.md depends on target.md",
        )
    )
    old.rename(new)
    attempts = {"count": 0}

    def sync(relation_id, _context, *, force=False):
        assert force is True
        attempts["count"] += 1
        if attempts["count"] == 1:
            return False
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO relation_context_embeddings "
                "(relation_id, embedding, model_version) VALUES (?, ?, 'test')",
                (relation_id, json.dumps([1.0])),
            )
        return True

    monkeypatch.setattr(kg._rel_emb_mgr, "add_relation_context", sync)
    monkeypatch.setattr(kg._rel_emb_mgr, "flush", lambda: True)
    with pytest.raises(RuntimeError, match="embedding reconciliation failed"):
        kg.reconcile_page_lifecycle(
            previous_path=old, page_path=new, mutation_type="move"
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kg_embedding_outbox").fetchone()[0] == 1
        assert conn.execute("SELECT source FROM relations").fetchone()[0] == "new.md"

    repaired = kg.reconcile_page_lifecycle(
        previous_path=old, page_path=new, mutation_type="move"
    )
    assert repaired["projection_errors"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kg_embedding_outbox").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM relation_context_embeddings"
        ).fetchone()[0] == 1


def test_kg_delete_embedding_failure_stays_pending_until_removed(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    page = vault / "page.md"
    vault.mkdir()
    page.write_text("# Page", encoding="utf-8")
    db = tmp_path / "knowledge_graph.db"
    kg = KnowledgeGraph(db_path=str(db), wiki_base=str(vault))
    monkeypatch.setattr(kg._rel_emb_mgr, "add_relation_context", lambda *args, **kwargs: True)
    assert kg.add_relation(
        Relation(
            source="page.md",
            target="target.md",
            relation_type=RelationType.DEPENDS_ON,
            context="page.md depends on target.md",
        )
    )
    with sqlite3.connect(db) as conn:
        relation_id = conn.execute("SELECT id FROM relations").fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO relation_context_embeddings "
            "(relation_id, embedding, model_version) VALUES (?, ?, 'test')",
            (relation_id, json.dumps([1.0])),
        )
    attempts = {"count": 0}

    def remove(_relation_id, *, hnsw_id=None):
        assert hnsw_id is not None
        attempts["count"] += 1
        return attempts["count"] > 1

    monkeypatch.setattr(kg._rel_emb_mgr, "remove_relation_projection", remove)
    monkeypatch.setattr(kg._rel_emb_mgr, "flush", lambda: True)
    page.unlink()
    with pytest.raises(RuntimeError, match="embedding reconciliation failed"):
        kg.reconcile_page_lifecycle(
            previous_path=page, page_path=page, mutation_type="delete"
        )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT operation FROM kg_embedding_outbox").fetchone()[0] == "delete"
        assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0

    repaired = kg.reconcile_page_lifecycle(
        previous_path=page, page_path=page, mutation_type="delete"
    )
    assert repaired["projection_errors"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM kg_embedding_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("mutation_type", ["create", "update"])
def test_kg_create_or_update_replaces_only_the_page_owned_graph_contributions(
    tmp_path, monkeypatch, mutation_type
):
    """An update must retract stale output without deleting unrelated incoming edges."""

    embedding_manager = MagicMock()
    embedding_manager.add_relation_context.return_value = True
    embedding_manager.remove_relation_projection.return_value = True
    embedding_manager.flush.return_value = True
    monkeypatch.setattr(
        KnowledgeGraph,
        "_rel_emb_mgr",
        property(lambda _self: embedding_manager),
    )
    vault = tmp_path / "vault"
    page = vault / "page.md"
    vault.mkdir()
    page.write_text("# Page\n", encoding="utf-8")
    db = tmp_path / "knowledge_graph.db"
    em = EntityManager(db_path=db)
    em.add_entity("Page entity", wiki_page=str(page.resolve()))
    kg = KnowledgeGraph(db_path=str(db), wiki_base=str(vault))
    assert kg.add_relation(
        Relation(
            source="page.md",
            target="outgoing.md",
            relation_type=RelationType.DEPENDS_ON,
            context="page.md depends on outgoing.md",
        )
    )
    assert kg.add_relation(
        Relation(
            source="incoming.md",
            target="page.md",
            relation_type=RelationType.DEPENDS_ON,
            context="incoming.md depends on page.md",
        )
    )
    assert kg.add_relation(
        Relation(
            source="page.md",
            target="related.md",
            relation_type=RelationType.RELATED_TO,
            context="page.md is related to related.md",
        )
    )

    handler = KGEventHandler(db_path=db, wiki_base=vault)
    monkeypatch.setattr(
        handler,
        "_project_kg_to_vault",
        lambda _kg: {
            "projection_enabled": True,
            "projection_entities": 0,
            "projection_relations": 0,
            "projection_errors": 0,
        },
    )
    result = handler.on_page_updated(
        {"page_path": str(page), "mutation_type": mutation_type}
    )

    with sqlite3.connect(db) as conn:
        endpoints = conn.execute(
            "SELECT source, target, relation_type FROM relations ORDER BY source, target"
        ).fetchall()
        entity = conn.execute(
            "SELECT source_page, source_count, status FROM entities WHERE name='Page entity'"
        ).fetchone()
        source_count = conn.execute(
            "SELECT COUNT(*) FROM entity_sources WHERE entity_uid='page-entity'"
        ).fetchone()[0]
    assert endpoints == [("incoming.md", "page.md", "depends_on")]
    assert entity == ("", 0, "source_missing")
    assert source_count == 0
    assert result["relations_deleted"] == 3
    assert result["entity_sources_retracted"] == 1
    assert result["entities_updated"] == 0


def test_kg_consumer_does_not_reingest_derived_vault_projections(tmp_path):
    vault = tmp_path / "vault"
    db = tmp_path / "knowledge_graph.db"
    handler = KGEventHandler(db_path=db, wiki_base=vault)

    for relative_path in (
        "L2.4-KG/Entities/kg-python.md",
        "07-Shadow/proposal.md",
        "99-Reports/generated.md",
        "06-Retrospectives/entropy/entropy-suggestions-20260711.md",
        "05-MOCs/Mnemos-Navigation/Vault-导航-001.md",
    ):
        projection = vault / relative_path
        projection.parent.mkdir(parents=True, exist_ok=True)
        projection.write_text("# Python\n\nDerived projection only.\n", encoding="utf-8")

        result = handler.on_page_updated(
            {"page_path": str(projection), "mutation_type": "update"}
        )

        assert result == {
            "status": "skipped",
            "reason": "knowledge graph projection is not an ingestion source",
        }
    assert not db.exists()


def test_kg_consumer_reconciles_moves_across_derived_source_boundary(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    formal = vault / "04-Concepts" / "rust.md"
    derived = vault / "07-Shadow" / "rust.md"
    formal.parent.mkdir(parents=True)
    derived.parent.mkdir(parents=True)
    formal.write_text("---\n关键词:\n- Rust语言\n---\n# Rust\n", encoding="utf-8")
    db = tmp_path / "knowledge_graph.db"
    handler = KGEventHandler(db_path=db, wiki_base=vault)
    monkeypatch.setattr(
        handler,
        "_project_kg_to_vault",
        lambda _kg: {
            "projection_enabled": True,
            "projection_entities": 0,
            "projection_relations": 0,
            "projection_errors": 0,
        },
    )
    assert handler.on_page_updated(
        {"page_path": str(formal), "mutation_type": "create"}
    )["status"] == "ok"

    formal.rename(derived)
    moved_out = handler.on_page_updated(
        {
            "page_path": str(derived),
            "previous_path": str(formal),
            "mutation_type": "move",
        }
    )
    assert moved_out["status"] == "ok"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT source_count, status FROM entities WHERE name='Rust语言'"
        ).fetchone() == (0, "source_missing")
        assert conn.execute("SELECT COUNT(*) FROM entity_sources").fetchone()[0] == 0

    derived.rename(formal)
    moved_in = handler.on_page_updated(
        {
            "page_path": str(formal),
            "previous_path": str(derived),
            "mutation_type": "move",
        }
    )
    assert moved_in["status"] == "ok"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT source_count, status FROM entities WHERE name='Rust语言'"
        ).fetchone() == (1, "active")
        assert conn.execute("SELECT COUNT(*) FROM entity_sources").fetchone()[0] == 1


def test_kg_batch_reconciliation_is_idempotent_per_wiki_source(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.embeddings.relation_manager.RelationEmbeddingManager.add_relation_contexts",
        lambda _self, contexts, **_kwargs: {
            "total": len(contexts),
            "added": len(contexts),
            "skipped": 0,
            "failed": 0,
        },
    )
    vault = tmp_path / "vault"
    page = vault / "04-Concepts" / "rust.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\n关键词:\n- Rust语言\n---\n# Rust\n\n[[Cargo工具]] supports builds.\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_graph.db"
    handler = KGEventHandler(db_path=db, wiki_base=vault)
    monkeypatch.setattr(
        handler,
        "_project_kg_to_vault",
        lambda _kg: {
            "projection_enabled": True,
            "projection_entities": 2,
            "projection_relations": 0,
            "projection_errors": 0,
        },
    )

    first = handler.reconcile_pages([page])
    second = handler.reconcile_pages([page])

    assert first["status"] == second["status"] == "ok"
    assert first["pages_processed"] == second["pages_processed"] == 1
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT name, source_count FROM entities ORDER BY name"
        ).fetchall()
    assert rows == [("Cargo工具", 1), ("Rust语言", 1)]


def test_kg_batch_ingests_all_entities_before_freezing_relation_candidates(
    tmp_path, monkeypatch
):
    vault = tmp_path / "vault"
    pages = [vault / "first.md", vault / "second.md"]
    vault.mkdir()
    for page in pages:
        page.write_text(f"# {page.stem}\n", encoding="utf-8")
    ingested = []

    class EntityManager:
        def ingest_from_wiki(self, page, *, content):
            ingested.append(page)
            return []

    class KnowledgeGraph:
        def normalize_entity_primary_sources(self):
            return None

        def _candidate_existing_pages(self):
            assert ingested == pages
            return pages

        def prepare_relation_candidates(self, existing_pages):
            assert existing_pages == pages
            return {}

        @contextmanager
        def defer_relation_embeddings(self):
            yield {"total": 0, "added": 0, "skipped": 0, "failed": 0}

        def discover_relations(self, *_args, **_kwargs):
            return []

        def apply_discovered(self, *_args, **_kwargs):
            return 0

        def close(self):
            return None

    handler = KGEventHandler(db_path=tmp_path / "knowledge_graph.db", wiki_base=vault)
    monkeypatch.setattr(handler, "_get_entity_manager", lambda: EntityManager())
    monkeypatch.setattr(handler, "_new_knowledge_graph", lambda: KnowledgeGraph())
    monkeypatch.setattr(
        handler,
        "_project_kg_to_vault",
        lambda _kg: {
            "projection_enabled": True,
            "projection_entities": 0,
            "projection_relations": 0,
            "projection_errors": 0,
        },
    )

    result = handler.reconcile_pages(pages)

    assert result["status"] == "ok"
    assert result["pages_processed"] == 2


def test_cognitive_graph_consumer_rekeys_move_and_stales_delete(
    tmp_path,
    monkeypatch,
):
    vault = tmp_path / "mnemos"
    vault.mkdir()
    monkeypatch.chdir(vault)
    store = CognitiveGraphStore(db_path=str(tmp_path / "cognitive_graph.db"))
    relation_kwargs = {
        "source": "session://s1",
        "target": "wiki://00-Inbox/old.md",
        "relation_type": "derived_from",
        "source_layer": "session",
        "target_layer": "wiki",
    }
    binding = cognitive_relation_material_action_binding(**relation_kwargs)
    store.add_relation(
        **relation_kwargs,
        material_action=material_action_authorization(
            tmp_path,
            action_type=COGNITIVE_RELATION_ACTION,
            owner=COGNITIVE_RELATION_OWNER,
            executor=COGNITIVE_RELATION_EXECUTOR,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
        ),
    )
    old_page = vault / "00-Inbox" / "old.md"
    new_page = vault / "04-Concepts" / "new.md"
    old_page.parent.mkdir(parents=True)
    new_page.parent.mkdir(parents=True)
    old_page.write_text("# Old\n", encoding="utf-8")
    ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
    ledger.record_mutation("00-Inbox/old.md", mutation_type="create")
    old_page.rename(new_page)
    move = ledger.record_mutation(
        "04-Concepts/new.md",
        mutation_type="move",
        previous_path="00-Inbox/old.md",
    )
    updater = CognitiveGraphUpdater(store=store)
    moved = updater.on_wiki_page_updated(
        Event(
            "wiki_page_updated",
            "test",
            {
                "page_path": move.page_path,
                "previous_path": move.previous_path,
                "page_id": move.page_id,
                "page_revision": move.page_revision,
                "mutation_id": move.mutation_id,
                "mutation_type": move.mutation_type,
                "tombstone": move.tombstone,
            },
        )
    )
    assert moved.disposition == "ack"
    active = store.get_relations(include_stale=False)
    assert any(rel.target == "wiki://04-Concepts/new.md" for rel in active)

    new_page.unlink()
    delete = ledger.record_mutation(
        "04-Concepts/new.md",
        mutation_type="delete",
    )
    deleted = updater.on_wiki_page_updated(
        Event(
            "wiki_page_updated",
            "test",
            {
                "page_path": delete.page_path,
                "previous_path": delete.previous_path,
                "page_id": delete.page_id,
                "page_revision": delete.page_revision,
                "mutation_id": delete.mutation_id,
                "mutation_type": delete.mutation_type,
                "tombstone": delete.tombstone,
            },
        )
    )
    assert deleted.disposition == "ack"
    assert not store.get_relations(include_stale=False)
