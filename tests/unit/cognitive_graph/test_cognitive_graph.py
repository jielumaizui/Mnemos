# -*- coding: utf-8 -*-
"""
Tests for core.cognitive_graph

Covers: model serialization, store CRUD, idempotency,
        updater event translation, outbox reconciliation.
"""

from __future__ import annotations

import datetime

import pytest

from core.cognitive_graph import (
    CognitiveGraphStore,
    CognitiveGraphUpdater,
    CognitiveRelation,
)
from core.cognitive_graph.store import (
    COGNITIVE_RELATION_ACTION,
    COGNITIVE_RELATION_EXECUTOR,
    COGNITIVE_RELATION_OWNER,
    _relation_id,
    cognitive_relation_material_action_binding,
)
from core.cognitive_graph.updater import REL_CONTRADICTS
from core.mnemos_bus import Event
from tests.cognitive_graph_decision_fixtures import (
    AuthorizedCognitiveGraphStore,
    cognitive_relation_resolver,
)
from tests.cognitive_decision_fixtures import material_action_authorization

USER_VAULT = "/" + "Users/user/Documents/mnemos"
ALT_VAULT = "/" + "Users/u/Documents/mnemos"


def _vault_path(relative: str) -> str:
    return f"{USER_VAULT}/{relative}"


def _alt_vault_path(relative: str) -> str:
    return f"{ALT_VAULT}/{relative}"


@pytest.fixture
def store(tmp_path):
    db_file = tmp_path / "cognitive_graph.db"
    return AuthorizedCognitiveGraphStore(str(db_file))


@pytest.fixture
def updater(store):
    return CognitiveGraphUpdater(
        store=store,
        material_action_resolver=cognitive_relation_resolver(store),
    )


@pytest.mark.no_canonical_material_actions
def test_add_relation_requires_canonical_material_action(tmp_path):
    raw_store = CognitiveGraphStore(str(tmp_path / "raw-cognitive-graph.db"))

    with pytest.raises(
        PermissionError,
        match="canonical material-action authorization is required",
    ):
        raw_store.add_relation("kg://A", "kg://B", "related_to")


@pytest.mark.no_canonical_material_actions
def test_all_public_graph_material_mutations_fail_closed_without_action(tmp_path):
    raw_store = CognitiveGraphStore(str(tmp_path / "raw-cognitive-graph.db"))
    relation_kwargs = {
        "source": "kg://A",
        "target": "kg://B",
        "relation_type": "related_to",
    }
    binding = cognitive_relation_material_action_binding(**relation_kwargs)
    relation = raw_store.add_relation(
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

    with pytest.raises(PermissionError, match="canonical material-action"):
        raw_store.mark_stale(relation.id)
    with pytest.raises(PermissionError, match="canonical material-action"):
        raw_store.delete_relation(relation.id)
    with pytest.raises(PermissionError, match="canonical material-action"):
        raw_store.add_relations_atomic(
            [
                {
                    "source": "kg://B",
                    "target": "kg://C",
                    "relation_type": "related_to",
                }
            ]
        )
    with pytest.raises(PermissionError, match="canonical material-action"):
        raw_store.add_canonical_node("Standalone")


def test_default_store_uses_fake_config_cognitive_graph_path(
    monkeypatch, fake_config, tmp_path
):
    """Default store construction must stay inside the injected test config."""
    import core.cognitive_graph.store as store_mod

    fake_home = tmp_path / "home"
    monkeypatch.setattr(store_mod, "get_config", lambda: fake_config)
    monkeypatch.setattr(store_mod.Path, "home", lambda: fake_home)

    store = CognitiveGraphStore()

    assert store.db_path == fake_config.database_dir / "cognitive_graph.db"


def test_default_store_fails_closed_for_magicmock_config_path(monkeypatch, tmp_path):
    """Invalid injected config must not fall back to operator-global state."""
    from unittest.mock import MagicMock

    import core.cognitive_graph.store as store_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store_mod, "get_config", lambda: MagicMock())

    with pytest.raises(RuntimeError, match="requires cognitive_graph_db_path or database_dir"):
        CognitiveGraphStore()
    assert not list(tmp_path.glob("*MagicMock*"))


# ─────────────────────────────────────────────
# Store basic CRUD
# ─────────────────────────────────────────────


def test_store_initializes_schema(store):
    stats = store.get_stats()
    assert stats["relations"] == 0
    assert stats["canonical_nodes"] == 0
    assert stats["outbox_pending"] == 0


def test_add_relation_and_get(store):
    from core.trust.formal_cognitive_mutation import FormalCognitiveMutationJournal

    rel = store.add_relation(
        source="wiki://00-Inbox/python.md",
        target="kg://Python",
        relation_type="related_to",
        strength=0.8,
        confidence=0.9,
        source_layer="wiki",
        target_layer="kg",
    )
    assert isinstance(rel, CognitiveRelation)
    assert rel.id == _relation_id("wiki://00-Inbox/python.md", "kg://Python", "related_to")
    assert rel.source_layer == "wiki"
    assert rel.target_layer == "kg"

    fetched = store.get_relation(rel.id)
    assert fetched is not None
    assert fetched.strength == pytest.approx(0.8)
    assert fetched.confidence == pytest.approx(0.9)
    events = FormalCognitiveMutationJournal.for_database(store.db_path).list_events(
        asset_kind="cognitive_graph_relation",
    )
    assert len(events) == 1
    assert events[0]["target_ref"] == rel.id


def test_cognitive_relation_recovers_target_commit_without_duplicate(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    import core.cognitive_graph.store_mutations as store_module
    from core.cognitive_graph.store import (
        COGNITIVE_RELATION_ACTION,
        COGNITIVE_RELATION_EXECUTOR,
        COGNITIVE_RELATION_OWNER,
        cognitive_relation_material_action_binding,
    )
    from core.trust.formal_cognitive_mutation import FormalCognitiveMutationJournal
    from tests.cognitive_decision_fixtures import material_action_authorization

    graph = CognitiveGraphStore(str(tmp_path / "crash-cognitive-graph.db"))
    kwargs = {
        "source": "wiki://00-Inbox/crash.md",
        "target": "kg://CrashRecovery",
        "relation_type": "related_to",
        "strength": 0.8,
        "confidence": 0.9,
        "source_layer": "wiki",
        "target_layer": "kg",
    }
    binding = cognitive_relation_material_action_binding(**kwargs)
    authorization = material_action_authorization(
        tmp_path,
        action_type=COGNITIVE_RELATION_ACTION,
        owner=COGNITIVE_RELATION_OWNER,
        executor=COGNITIVE_RELATION_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
    original = store_module.recover_recorded_target_effect
    crashed = False

    def crash_after_target(auth, oracle):
        nonlocal crashed
        if not crashed and oracle.observe(auth.permit) is not None:
            crashed = True
            raise OSError("crash after cognitive relation target commit")
        return original(auth, oracle)

    monkeypatch.setattr(
        store_module,
        "recover_recorded_target_effect",
        crash_after_target,
    )
    with pytest.raises(OSError, match="after cognitive relation target commit"):
        graph.add_relation(**kwargs, material_action=authorization)

    monkeypatch.setattr(
        store_module,
        "recover_recorded_target_effect",
        original,
    )
    recovered = graph.add_relation(**kwargs, material_action=authorization)

    with sqlite3.connect(graph.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_relations"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM material_target_effects"
        ).fetchone()[0] == 1
    events = FormalCognitiveMutationJournal.for_database(graph.db_path).list_events(
        asset_kind="cognitive_graph_relation"
    )
    assert recovered.id == binding["target_ref"]
    assert len(events) == 1
    assert events[0]["actor"] == "system"
    assert events[0]["decision"].startswith("cogrev-")
    assert (
        events[0]["metadata"]["material_action"]["decision_revision_id"]
        == events[0]["decision"]
    )
    assert "relation_type:related_to" in events[0]["evidence_refs"]


def test_relation_idempotent(store):
    rel1 = store.add_relation("kg://A", "kg://B", "related_to", strength=0.5)
    rel2 = store.add_relation("kg://A", "kg://B", "related_to", strength=0.9)
    assert rel1.id == rel2.id
    assert rel2.strength == pytest.approx(0.9)
    # 只应存在一条记录
    assert store.get_stats()["relations"] == 1


def test_get_relations_filtered(store):
    store.add_relation("wiki://a.md", "kg://A", "derived_from")
    store.add_relation("wiki://b.md", "kg://B", "derived_from")
    store.add_relation("kg://A", "kg://B", "related_to")

    assert len(store.get_relations(relation_type="derived_from")) == 2
    assert len(store.get_relations(source="kg://A")) == 1
    assert len(store.get_relations(target="kg://B")) == 2


def test_mark_stale(store):
    rel = store.add_relation("kg://A", "kg://B", "related_to")
    assert store.mark_stale(rel.id)
    stale = store.get_relations(include_stale=True, relation_type="related_to")
    assert len(stale) == 1
    assert stale[0].stale == 1
    assert len(store.get_relations(relation_type="related_to")) == 0


def test_delete_relation(store):
    rel = store.add_relation("kg://A", "kg://B", "related_to")
    assert store.delete_relation(rel.id)
    assert store.get_relation(rel.id) is None


# ─────────────────────────────────────────────
# Canonical nodes
# ─────────────────────────────────────────────


def test_add_canonical_node_merges_aliases(store):
    node1 = store.add_canonical_node(
        "Python", aliases=["python", "py"], source_ids=["wiki://python.md"]
    )
    node2 = store.add_canonical_node(
        "Python", aliases=["py", "python3"], source_ids=["kg://Python"]
    )
    assert node1.canonical_id == node2.canonical_id
    assert set(node2.aliases) == {"python", "py", "python3"}
    assert set(node2.source_ids) == {"wiki://python.md", "kg://Python"}


def test_find_canonical_node_by_alias(store):
    store.add_canonical_node("Docker", aliases=["container"], source_ids=[])
    results = store.find_canonical_nodes(alias="container")
    assert len(results) == 1
    assert results[0].canonical_name == "Docker"


# ─────────────────────────────────────────────
# Outbox
# ─────────────────────────────────────────────


def test_add_and_fetch_outbox(store):
    item = store.add_sync_outbox("knowledge_distilled", {"session_id": "s1"})
    assert item.id is not None
    pending = store.fetch_outbox(unprocessed_only=True)
    assert len(pending) == 1
    assert pending[0].payload["session_id"] == "s1"


def test_mark_outbox_processed(store):
    item = store.add_sync_outbox("reflection.completed", {"record_id": "r1"})
    assert store.mark_outbox_processed(item.id)
    pending = store.fetch_outbox(unprocessed_only=True)
    assert len(pending) == 0


# ─────────────────────────────────────────────
# Updater event translation
# ─────────────────────────────────────────────


def test_updater_knowledge_distilled_creates_relations(updater, store):
    event = Event(
        event_type="knowledge_distilled",
        source="hephaestus",
        payload={
            "session_id": "sess-123",
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

    # session -> wiki, session -> kg x2, wiki -> kg x2, kg relation (forward+reverse by store? no, updater adds one)  # noqa: E501
    rels = store.get_relations(limit=100)
    sources = {r.source for r in rels}
    assert any("session://" in s for s in sources)
    assert any("wiki://" in s for s in sources)
    assert any("kg://" in s for s in sources)

    # KG relation should be stored
    kg_rels = store.get_relations(relation_type="related_to")
    assert len(kg_rels) >= 1


def test_updater_knowledge_distilled_normalizes_contradict_relation(updater, store):
    event = Event(
        event_type="knowledge_distilled",
        source="hephaestus",
        payload={
            "session_id": "sess-contradict",
            "wiki_pages": [],
            "kg_input": {
                "entities": [],
                "relations": [
                    {
                        "source": "旧判断",
                        "target": "新证据",
                        "type": "contradict",
                        "confidence": 0.9,
                    }
                ],
            },
        },
    )

    updater.on_knowledge_distilled(event)

    rels = store.get_relations(source="kg://旧判断", target="kg://新证据")
    assert len(rels) == 1
    assert rels[0].relation_type == REL_CONTRADICTS


def test_updater_distill_complete(updater, store):
    event = Event(
        event_type="distill_complete",
        source="hephaestus",
        payload={
            "session_id": "sess-456",
            "page_path": _vault_path("00-Inbox/docker.md"),
            "title": "Docker",
        },
    )
    updater.on_distill_complete(event)

    rels = store.get_relations(relation_type="derived_from")
    assert len(rels) == 1
    assert rels[0].source == "session://sess-456"
    assert "wiki://00-Inbox/docker.md" in rels[0].target


def test_updater_reflection_completed(updater, store):
    event = Event(
        event_type="reflection.completed",
        source="daemon",
        payload={
            "record_id": "abcd1234",
            "insight_summary": "用户偏好简洁回答",
        },
    )
    updater.on_reflection_completed(event)

    rels = store.get_relations(source="ref://abcd1234")
    assert len(rels) == 2
    targets = {r.target for r in rels}
    assert any("L4-Reflections/insights" in t for t in targets)
    assert any("feedback://persona" in t for t in targets)


def test_updater_observation_updated(updater, store):
    event = Event(
        event_type="observation.updated",
        source="observation_engine",
        payload={
            "observation_ids": ["obs-1", "obs-2"],
            "wiki_path": _vault_path("L3-Observations/attention.md"),
            "session_id": "sess-789",
        },
    )
    updater.on_observation_updated(event)

    rels = store.get_relations(relation_type="observed_in")
    assert len(rels) == 4  # 2 obs x (wiki + session)


def test_updater_persona_updated(updater, store):
    event = Event(
        event_type="persona.updated",
        source="persona_store",
        payload={
            "version": "v42",
            "wiki_path": _vault_path("L5-Feedback/user-persona.md"),
        },
    )
    updater.on_persona_updated(event)

    rels = store.get_relations(relation_type="derived_from")
    assert len(rels) == 1
    assert rels[0].source == "feedback://persona/v42"
    assert "L5-Feedback/user-persona.md" in rels[0].target


def test_updater_failure_goes_to_outbox(updater, store, monkeypatch):
    """模拟 store.add_relation 抛异常，事件应进入 outbox"""
    monkeypatch.setattr(
        store,
        "add_relation",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    event = Event(
        event_type="distill_complete",
        source="hephaestus",
        payload={"session_id": "s", "page_path": "/x.md"},
    )
    updater.on_distill_complete(event)
    pending = store.fetch_outbox(unprocessed_only=True)
    assert len(pending) == 1


def test_updater_process_outbox(updater, store):
    store.add_sync_outbox("distill_complete", {"session_id": "s2", "page_path": "/a.md"})
    stats = updater.process_outbox(limit=10)
    assert stats["processed"] == 1
    assert stats["failed"] == 0
    assert store.get_stats()["relations"] == 1


def test_updater_reconcile(updater, store):
    store.add_sync_outbox("reflection.completed", {"record_id": "r9", "insight_summary": "x"})
    result = updater.reconcile()
    assert result["outbox"]["processed"] == 1
    assert result["stats"]["relations"] >= 2


# ─────────────────────────────────────────────
# URN helpers
# ─────────────────────────────────────────────


def test_relation_id_deterministic():
    id1 = _relation_id("a", "b", "related_to")
    id2 = _relation_id("a", "b", "related_to")
    assert id1 == id2
    assert len(id1) == 32


# ─────────────────────────────────────────────
# rebuild_missing_relations
# ─────────────────────────────────────────────


def test_rebuild_missing_relations_processes_outbox(store):
    store.add_sync_outbox(
        "distill_complete",
        {"session_id": "s3", "page_path": _alt_vault_path("00-Inbox/x.md")},
    )
    store.add_sync_outbox("reflection.completed", {"record_id": "r10", "insight_summary": "y"})
    stats = store.rebuild_missing_relations()
    assert stats["outbox_processed"] == 2
    assert stats["relations_added"] >= 3
    assert store.get_stats()["outbox_pending"] == 0


def test_rebuild_missing_relations_from_knowledge_distilled(store):
    store.add_sync_outbox(
        "knowledge_distilled",
        {
            "session_id": "sess-rebuild",
            "wiki_pages": [_alt_vault_path("00-Inbox/python.md")],
            "kg_input": {
                "entities": ["Python"],
                "relations": [],
            },
        },
    )
    stats = store.rebuild_missing_relations()
    assert stats["outbox_processed"] == 1
    rels = store.get_relations(limit=100)
    assert any(
        r.source == "session://sess-rebuild" and "wiki://00-Inbox/python.md" in r.target
        for r in rels
    )
    assert any(r.source == "wiki://00-Inbox/python.md" and r.target == "kg://Python" for r in rels)


def test_rebuild_missing_relations_cross_layer_canonical(store):
    store.add_canonical_node(
        "Python",
        source_ids=["wiki://00-Inbox/python.md", "kg://Python"],
    )
    stats = store.rebuild_missing_relations()
    assert stats["cross_layer_added"] == 2
    assert stats["relations_added"] == 2
    rels = store.get_relations(relation_type="related_to")
    assert any(r.source == "wiki://00-Inbox/python.md" and r.target == "kg://Python" for r in rels)
    assert any(r.source == "kg://Python" and r.target == "wiki://00-Inbox/python.md" for r in rels)


def test_rebuild_missing_relations_idempotent(store):
    store.add_canonical_node(
        "Python",
        source_ids=["wiki://00-Inbox/python.md", "kg://Python"],
    )
    stats1 = store.rebuild_missing_relations()
    stats2 = store.rebuild_missing_relations()
    assert stats1["cross_layer_added"] == 2
    assert stats2["cross_layer_added"] == 0
    assert store.get_stats()["relations"] == 2


# ─────────────────────────────────────────────
# P111: canonical_nodes 缺失修复
# ─────────────────────────────────────────────


def test_add_relation_creates_canonical_nodes(store):
    """add_relation 应同步维护 source/target 的 canonical 节点（同名会合并）。"""
    store.add_relation(
        source="wiki://00-Inbox/python.md",
        target="kg://Python",
        relation_type="related_to",
        source_layer="wiki",
        target_layer="kg",
    )
    stats = store.get_stats()
    # wiki 页面 stem "python" 与 kg 实体 "Python" 归一为同一 canonical 节点
    assert stats["canonical_nodes"] == 1
    nodes = store.find_canonical_nodes(source_id="kg://Python")
    assert len(nodes) == 1
    assert set(nodes[0].source_ids) == {"wiki://00-Inbox/python.md", "kg://Python"}


def test_rebuild_missing_relations_derives_canonical_nodes(store):
    """历史关系中缺少 canonical 节点时，rebuild_missing_relations 应能反推回填。"""
    # 直接通过底层 SQL 插入关系，模拟历史数据（绕过 add_relation 的同步节点维护）
    import sqlite3

    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            """INSERT INTO cognitive_relations
               (id, source, target, relation_type, strength, confidence,
                source_layer, target_layer, created_at, updated_at, stale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "rel-1",
                "wiki://00-Inbox/python.md",
                "kg://Python",
                "related_to",
                0.8,
                0.9,
                "wiki",
                "kg",
                "2026-06-01",
                "2026-06-01",
                0,
            ),
        )
        conn.commit()

    assert store.get_stats()["canonical_nodes"] == 0

    stats = store.rebuild_missing_relations()
    assert stats["canonical_nodes_added"] == 1
    assert store.get_stats()["canonical_nodes"] == 1
    nodes = store.find_canonical_nodes(source_id="wiki://00-Inbox/python.md")
    assert len(nodes) == 1


def test_rebuild_missing_relations_does_not_reauthorize_existing_nodes(
    store, monkeypatch
):
    store.add_relation(
        source="wiki://00-Inbox/existing.md",
        target="kg://Existing",
        relation_type="related_to",
        source_layer="wiki",
        target_layer="kg",
    )
    calls = []

    def unexpected_authorization(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("existing canonical nodes must not be rebuilt")

    monkeypatch.setattr(store, "_authorize_maintenance_node", unexpected_authorization)

    added = store._derive_canonical_nodes_from_relations()

    assert added == 0
    assert calls == []


# ─────────────────────────────────────────────
# Data explosion guards
# ─────────────────────────────────────────────


def test_cleanup_outbox_removes_old_processed_items(store):
    """已处理超过 retention_days 的 outbox 条目应被清理"""
    item = store.add_sync_outbox("wiki_page_updated", {"page_path": "a.md"})
    store.mark_outbox_processed(item.id)

    # 直接修改 processed_at 到 40 天前
    old_time = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)
    ).isoformat()[:19]
    with store._conn() as conn:
        conn.execute("UPDATE sync_outbox SET processed_at = ? WHERE id = ?", (old_time, item.id))
        conn.commit()

    removed = store.cleanup_outbox(retention_days=30)
    assert removed == 1
    assert store.fetch_outbox(unprocessed_only=False, limit=100) == []


def test_cleanup_outbox_keeps_recent_processed_items(store):
    """近期已处理条目应保留"""
    item = store.add_sync_outbox("wiki_page_updated", {"page_path": "a.md"})
    store.mark_outbox_processed(item.id)

    removed = store.cleanup_outbox(retention_days=30)
    assert removed == 0
    assert len(store.fetch_outbox(unprocessed_only=False, limit=100)) == 1


def test_cross_layer_relations_limited_per_layer_pair(store):
    """单个 canonical 节点在每对层之间生成的关系数应受上限约束"""
    # 构造一个 canonical 节点，在 wiki 层有 20 个来源，在 session 层有 20 个来源
    source_ids = [f"wiki://page-{i}.md" for i in range(20)] + [f"session://sess-{i}" for i in range(20)]
    store.add_canonical_node(
        canonical_name="Python",
        aliases=[],
        source_ids=source_ids,
    )
    rels = store._build_canonical_cross_layer_relations()
    # 上限是 MAX_CROSS_LAYER_PER_PAIR=10，所以每方向最多 10*10=100，双向 200
    assert len(rels) <= 2 * store.MAX_CROSS_LAYER_PER_PAIR * store.MAX_CROSS_LAYER_PER_PAIR
