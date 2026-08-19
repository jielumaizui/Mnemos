# -*- coding: utf-8 -*-
"""
Extended tests for core.cognitive_graph

Uses shared fixtures from tests/conftest.py and MagicMock for EventBus
to verify requested behaviors explicitly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.cognitive_graph import CognitiveGraphStore, CognitiveGraphUpdater
from core.mnemos_bus import Event
from tests.cognitive_decision_fixtures import canonical_material_action_scope

USER_VAULT = "/" + "Users/user/Documents/mnemos"


@pytest.fixture(autouse=True)
def _canonical_material_actions(tmp_path):
    """Exercise graph mutations through real canonical DecisionTrace permits."""

    with canonical_material_action_scope(tmp_path):
        yield


def _vault_path(relative: str) -> str:
    return f"{USER_VAULT}/{relative}"

# ─────────────────────────────────────────────
# CognitiveGraphStore — shared-fixture variants
# ─────────────────────────────────────────────


def test_store_add_relation_idempotent_with_tmp_db_path(tmp_db_path):
    """add_relation 对同一 (source, target, relation_type) 幂等。"""
    store = CognitiveGraphStore(str(tmp_db_path))

    rel1 = store.add_relation("kg://A", "kg://B", "related_to", strength=0.5)
    rel2 = store.add_relation("kg://A", "kg://B", "related_to", strength=0.9)

    assert rel1.id == rel2.id
    assert rel2.strength == pytest.approx(0.9)
    assert store.get_stats()["relations"] == 1


def test_store_mark_stale_and_get_relations_exclude_stale(tmp_db_path):
    """mark_stale 后，include_stale=False 应过滤掉 stale 关系。"""
    store = CognitiveGraphStore(str(tmp_db_path))
    rel = store.add_relation("kg://A", "kg://B", "related_to")

    assert store.mark_stale(rel.id) is True
    assert len(store.get_relations(include_stale=False)) == 0
    assert len(store.get_relations(include_stale=True)) == 1
    assert store.get_relations(include_stale=True)[0].stale == 1


def test_store_add_canonical_node_merges_aliases_with_tmp_db_path(tmp_db_path):
    """同一 canonical 节点多次写入应合并 aliases 与 source_ids。"""
    store = CognitiveGraphStore(str(tmp_db_path))

    node1 = store.add_canonical_node(
        "Python",
        aliases=["python", "py"],
        source_ids=["wiki://python.md"],
    )
    node2 = store.add_canonical_node(
        "Python",
        aliases=["py", "python3"],
        source_ids=["kg://Python"],
    )

    assert node1.canonical_id == node2.canonical_id
    assert set(node2.aliases) == {"python", "py", "python3"}
    assert set(node2.source_ids) == {"wiki://python.md", "kg://Python"}


def test_store_outbox_lifecycle_with_tmp_db_path(tmp_db_path):
    """add_sync_outbox / fetch_outbox / mark_outbox_processed 完整生命周期。"""
    store = CognitiveGraphStore(str(tmp_db_path))

    item = store.add_sync_outbox("knowledge_distilled", {"session_id": "s1"})
    assert item.id is not None
    assert item.event_type == "knowledge_distilled"
    assert item.payload == {"session_id": "s1"}
    assert item.processed_at is None

    pending = store.fetch_outbox(unprocessed_only=True)
    assert len(pending) == 1
    assert pending[0].id == item.id

    assert store.mark_outbox_processed(item.id) is True

    assert len(store.fetch_outbox(unprocessed_only=True)) == 0
    all_items = store.fetch_outbox(unprocessed_only=False)
    assert len(all_items) == 1
    assert all_items[0].processed_at is not None


# ─────────────────────────────────────────────
# CognitiveGraphUpdater — MagicMock EventBus
# ─────────────────────────────────────────────


def test_updater_subscribe_with_magicmock_bus(tmp_db_path):
    """subscribe 应向 MagicMock EventBus 注册 6 类事件处理器。"""
    store = CognitiveGraphStore(str(tmp_db_path))
    bus = MagicMock()
    updater = CognitiveGraphUpdater(store=store, bus=bus)

    updater.subscribe()

    assert bus.subscribe.call_count == 6
    subscribed_types = {call.args[0] for call in bus.subscribe.call_args_list}
    expected_types = {
        "knowledge_distilled",
        "distill_complete",
        "wiki_page_updated",
        "reflection.completed",
        "observation.updated",
        "persona.updated",
    }
    assert subscribed_types == expected_types
    assert updater._subscribed is True


def test_updater_on_knowledge_distilled_synthetic_payload(tmp_db_path):
    """on_knowledge_distilled 从合成 payload 创建预期的跨层关系。"""
    store = CognitiveGraphStore(str(tmp_db_path))
    updater = CognitiveGraphUpdater(store=store)

    event = Event(
        event_type="knowledge_distilled",
        source="synthetic-test",
        payload={
            "session_id": "sess-abc",
            "wiki_pages": [_vault_path("00-Inbox/python.md")],
            "kg_input": {
                "entities": ["Python", "Docker"],
                "relations": [
                    {
                        "source": "Python",
                        "target": "Docker",
                        "type": "related_to",
                        "confidence": 0.85,
                    }
                ],
            },
        },
    )
    updater.on_knowledge_distilled(event)

    # session -> wiki (derived_from)
    session_wiki = store.get_relations(
        source="session://sess-abc", target="wiki://00-Inbox/python.md"
    )
    assert len(session_wiki) == 1
    assert session_wiki[0].relation_type == "derived_from"
    assert session_wiki[0].source_layer == "session"
    assert session_wiki[0].target_layer == "wiki"

    # wiki -> session (反向 derived_from)
    wiki_session = store.get_relations(
        source="wiki://00-Inbox/python.md", target="session://sess-abc"
    )
    assert len(wiki_session) == 1
    assert wiki_session[0].relation_type == "derived_from"

    # session -> kg entity (related_to)
    session_kg = store.get_relations(source="session://sess-abc", target="kg://Python")
    assert len(session_kg) == 1
    assert session_kg[0].relation_type == "related_to"

    # wiki -> kg entity (related_to)
    wiki_kg = store.get_relations(source="wiki://00-Inbox/python.md", target="kg://Python")
    assert len(wiki_kg) == 1
    assert wiki_kg[0].relation_type == "related_to"

    # kg -> kg relation
    kg_rels = store.get_relations(source="kg://Python", target="kg://Docker")
    assert len(kg_rels) == 1
    assert kg_rels[0].relation_type == "related_to"
    assert kg_rels[0].confidence == pytest.approx(0.85)
    assert kg_rels[0].source_layer == "kg"
    assert kg_rels[0].target_layer == "kg"


# ─────────────────────────────────────────────
# Outbox fallback
# ─────────────────────────────────────────────


def test_outbox_fallback_handler_raises_then_process_outbox_drains(tmp_db_path, monkeypatch):
    """事件处理器抛异常时 _add_outbox 被调用，process_outbox 能消费并排空。"""
    store = CognitiveGraphStore(str(tmp_db_path))
    updater = CognitiveGraphUpdater(store=store)

    original_add_relation = store.add_relation
    call_count = {"n": 0}

    def raising_add_relation(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise RuntimeError("boom")
        return original_add_relation(*args, **kwargs)

    monkeypatch.setattr(store, "add_relation", raising_add_relation)

    event = Event(
        event_type="knowledge_distilled",
        source="test",
        payload={
            "session_id": "sess-fallback",
            "wiki_pages": [_vault_path("00-Inbox/fallback.md")],
            "kg_input": {"entities": [], "relations": []},
        },
    )
    updater.on_knowledge_distilled(event)

    # 异常应被捕获并写入 outbox
    pending = store.fetch_outbox(unprocessed_only=True)
    assert len(pending) == 1
    assert pending[0].event_type == "knowledge_distilled"
    assert pending[0].payload["session_id"] == "sess-fallback"

    # 恢复 store.add_relation，让 process_outbox 成功消费
    monkeypatch.setattr(store, "add_relation", original_add_relation)

    stats = updater.process_outbox(limit=10)
    assert stats["processed"] == 1
    assert stats["failed"] == 0
    assert len(store.fetch_outbox(unprocessed_only=True)) == 0

    # 确认关系已写入
    rels = store.get_relations(source="session://sess-fallback")
    assert len(rels) >= 1
