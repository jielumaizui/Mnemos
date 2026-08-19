"""
P0-1: RelationManager 编译与导入回归测试
确保 relation_manager.py 能正常编译、导入，基础方法可调用。
"""

import py_compile
import tempfile
import unittest
from pathlib import Path

import pytest

from tests.relation_manager_decision_fixtures import authorized_relation_manager


class TestRelationManagerCompileAndImport(unittest.TestCase):
    """最小导入测试：编译通过 + 类可实例化 + 基础方法可调用"""

    def test_relation_manager_py_compiles(self):
        """编译不报错"""
        path = Path("core/kia/relation_manager.py")
        py_compile.compile(str(path), doraise=True)

    def test_relation_manager_imports(self):
        """模块能正常导入"""
        from core.kia.relation_manager import RelationManager

        self.assertTrue(callable(RelationManager))

    def test_relation_manager_basic_ops(self):
        """基础方法不抛异常"""
        from core.kia.relation_manager import RelationManager

        with tempfile.TemporaryDirectory() as directory:
            rm = RelationManager(str(Path(directory) / "knowledge_graph.db"))
            # 空输入 distill 应返回空列表
            results = rm.add_from_distill({"entities": [], "relations": []})
            self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()


def test_relation_manager_material_identity_is_opaque_and_stable():
    from core.kia.relation_manager import RelationManager
    from core.kia.relation_schema import Relation, RelationType
    from core.privacy.content_redaction import redact_persistence_value

    relation = Relation(
        source="复盘提醒-system-recap-20260703011227-存在-1-个显著偏差",
        target="api_key=example-value",
        relation_type=RelationType.REFERENCES,
        confidence=0.9,
        context="first context",
    )
    binding = RelationManager.relation_material_action_binding(
        relation,
        reason="relation_manager.test",
    )
    changed_context = RelationManager.relation_material_action_binding(
        Relation(
            source=relation.source,
            target=relation.target,
            relation_type=relation.relation_type,
            confidence=relation.confidence,
            context="changed context",
        ),
        reason="relation_manager.test",
    )

    assert binding["target_ref"].startswith("kg-relation:sha256:")
    assert relation.source not in binding["target_ref"]
    assert relation.target not in binding["target_ref"]
    assert changed_context["target_ref"] == binding["target_ref"]
    assert changed_context["input_hash"] != binding["input_hash"]
    assert redact_persistence_value({"target_ref": binding["target_ref"]}).value == {
        "target_ref": binding["target_ref"]
    }


def test_apply_implicit_relations_persists_above_threshold(tmp_path, monkeypatch):
    """apply_implicit_relations 应只持久化置信度 >= 阈值的关系建议。"""
    monkeypatch.setattr(
        "core.kia.relation_manager._get_db_path",
        lambda: tmp_path / "knowledge_graph.db",
    )
    import sqlite3
    from core.kia.relation_manager import RelationSuggestion

    rm = authorized_relation_manager(tmp_path / "knowledge_graph.db")
    suggestions = [
        RelationSuggestion(
            source="Redis",
            target="Docker",
            relation_type="co_occurs",
            confidence=0.6,
            reason="在 3 个页面中同时出现",
        ),
        RelationSuggestion(
            source="Redis",
            target="Python",
            relation_type="similar_to",
            confidence=0.3,
            reason="关键词重叠度低",
        ),
    ]

    applied = rm.apply_implicit_relations(suggestions, min_confidence=0.5)
    assert applied == 1

    with sqlite3.connect(str(rm._db_path)) as conn:
        rows = conn.execute(
            "SELECT source, target, relation_type, source_method FROM relations"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("Redis", "Docker", "co_occurs", "implicit_discover")


@pytest.mark.no_canonical_material_actions
def test_apply_implicit_relations_seals_canonical_decision(tmp_path):
    from core.kia.relation_manager import RelationManager, RelationSuggestion

    rm = RelationManager(str(tmp_path / "knowledge_graph.db"))
    applied = rm.apply_implicit_relations(
        [
            RelationSuggestion(
                source="Redis",
                target="Docker",
                relation_type="co_occurs",
                confidence=0.8,
                reason="validated relation",
            )
        ]
    )

    assert applied == 1
    import sqlite3

    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        assert conn.execute(
            "SELECT status FROM cognitive_state_effect_receipts"
        ).fetchone() == ("committed",)


@pytest.mark.no_canonical_material_actions
def test_relation_manager_repeated_exact_request_replays_original_generation(
    tmp_path,
):
    import sqlite3

    from core.kia.relation_manager import RelationManager, RelationSuggestion

    manager = RelationManager(str(tmp_path / "knowledge_graph.db"))
    suggestions = [
        RelationSuggestion(
            source="RepeatSource",
            target="RepeatTarget",
            relation_type="co_occurs",
            confidence=0.8,
            reason="validated repeat relation",
        )
    ]

    assert manager.apply_implicit_relations(suggestions) == 1
    assert manager.apply_implicit_relations(suggestions) == 0

    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        rows = conn.execute(
            "SELECT status FROM cognitive_state_effect_receipts ORDER BY created_at"
        ).fetchall()
    assert rows == [("committed",)]


@pytest.mark.no_canonical_material_actions
def test_relation_manager_recreates_deleted_object_in_a_new_decision_generation(
    tmp_path,
):
    import sqlite3

    from core.kia.relation_manager import RelationManager, RelationSuggestion

    manager = RelationManager(str(tmp_path / "knowledge_graph.db"))
    suggestions = [
        RelationSuggestion(
            source="RecreatedSource",
            target="RecreatedTarget",
            relation_type="co_occurs",
            confidence=0.8,
            reason="validated recreated relation",
        )
    ]

    assert manager.apply_implicit_relations(suggestions) == 1
    with sqlite3.connect(manager._db_path) as conn:
        relation_id = int(
            conn.execute(
                "SELECT id FROM relations WHERE source=? AND target=?",
                ("RecreatedSource", "RecreatedTarget"),
            ).fetchone()[0]
        )
        conn.execute(
            "DELETE FROM relation_evidence WHERE relation_id=?",
            (relation_id,),
        )
        conn.execute("DELETE FROM relations WHERE id=?", (relation_id,))
        conn.commit()

    assert manager.apply_implicit_relations(suggestions) == 1

    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        rows = conn.execute(
            "SELECT status FROM cognitive_state_effect_receipts ORDER BY created_at"
        ).fetchall()
    assert rows == [("committed",), ("committed",)]


def test_relation_manager_recovers_target_commit_without_duplicate(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    import core.kia.relation_manager as relation_module
    from core.kia.relation_manager import (
        KG_RELATION_ACTION,
        KG_RELATION_EXECUTOR,
        KG_RELATION_OWNER,
        RelationManager,
        RelationSuggestion,
    )
    from core.trust.formal_cognitive_mutation import FormalCognitiveMutationJournal
    from tests.cognitive_decision_fixtures import material_action_authorization

    manager = RelationManager(str(tmp_path / "knowledge_graph.db"))
    suggestions = [
        RelationSuggestion(
            source="CrashSource",
            target="CrashTarget",
            relation_type="co_occurs",
            confidence=0.8,
            reason="exact crash recovery evidence",
        )
    ]
    relation = manager.plan_implicit_relations(suggestions)[0]
    binding = manager.relation_material_action_binding(
        relation,
        reason="relation_manager.apply_implicit_relations",
    )
    authorization = material_action_authorization(
        tmp_path,
        action_type=KG_RELATION_ACTION,
        owner=KG_RELATION_OWNER,
        executor=KG_RELATION_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
    commands = {binding["target_ref"]: authorization.permit.command_id}
    original = relation_module.recover_recorded_target_effect
    crashed = False

    def crash_after_target(auth, oracle):
        nonlocal crashed
        if not crashed and oracle.observe(auth.permit) is not None:
            crashed = True
            raise OSError("crash after relation-manager target commit")
        return original(auth, oracle)

    monkeypatch.setattr(
        relation_module,
        "recover_recorded_target_effect",
        crash_after_target,
    )
    with pytest.raises(OSError, match="after relation-manager target commit"):
        manager.apply_implicit_relations(
            suggestions,
            material_action_commands=commands,
        )

    monkeypatch.setattr(
        relation_module,
        "recover_recorded_target_effect",
        original,
    )
    assert manager.apply_implicit_relations(
        suggestions,
        material_action_commands=commands,
    ) == 0

    with sqlite3.connect(manager._db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM material_target_effects"
        ).fetchone()[0] == 1
    events = FormalCognitiveMutationJournal.for_database(
        manager._db_path
    ).list_events(asset_kind="kg_relation")
    assert len(events) == 1


def test_relation_feedback_replay_uses_original_effect_once(
    tmp_path,
    monkeypatch,
):
    import sqlite3

    import core.kia.relation_manager as relation_module
    from core.kia.relation_manager import (
        KG_RELATION_ACTION,
        KG_RELATION_EXECUTOR,
        KG_RELATION_OWNER,
        RelationManager,
        RelationSuggestion,
    )
    from core.kia.relation_schema import Relation, RelationType
    from tests.cognitive_decision_fixtures import material_action_authorization

    manager = authorized_relation_manager(tmp_path / "knowledge_graph.db")
    manager.apply_implicit_relations(
        [
            RelationSuggestion(
                source="FeedbackSource",
                target="FeedbackTarget",
                relation_type="related_to",
                confidence=0.5,
                reason="seed relation",
            )
        ]
    )
    updated = Relation(
        source="FeedbackSource",
        target="FeedbackTarget",
        relation_type=RelationType.RELATED_TO,
        strength=0.5,
        confidence=0.2 * 1.0 + 0.8 * 0.5,
        source_method="implicit_discover",
        context="seed relation",
    )
    binding = RelationManager.relation_material_action_binding(
        updated,
        reason="relation_manager.update_confidence",
    )
    authorization = material_action_authorization(
        tmp_path,
        action_type=KG_RELATION_ACTION,
        owner=KG_RELATION_OWNER,
        executor=KG_RELATION_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
    original = relation_module.recover_recorded_target_effect
    crashed = False

    def crash_after_target(auth, oracle):
        nonlocal crashed
        if (
            auth.permit.command_id == authorization.permit.command_id
            and not crashed
            and oracle.observe(auth.permit) is not None
        ):
            crashed = True
            raise OSError("crash after relation feedback target commit")
        return original(auth, oracle)

    monkeypatch.setattr(
        relation_module,
        "recover_recorded_target_effect",
        crash_after_target,
    )
    with pytest.raises(OSError, match="after relation feedback target commit"):
        manager.update_confidence(
            "FeedbackSource",
            "FeedbackTarget",
            "related_to",
            1.0,
            material_action=authorization,
        )

    monkeypatch.setattr(
        relation_module,
        "recover_recorded_target_effect",
        original,
    )
    manager.update_confidence(
        "FeedbackSource",
        "FeedbackTarget",
        "related_to",
        1.0,
        material_action=authorization,
    )

    with sqlite3.connect(manager._db_path) as conn:
        confidence = conn.execute(
            """SELECT confidence FROM relations
               WHERE source='FeedbackSource' AND target='FeedbackTarget'
                 AND relation_type='related_to'"""
        ).fetchone()[0]
    assert confidence == pytest.approx(0.6)
    with pytest.raises(PermissionError, match="another request"):
        manager.update_confidence(
            "FeedbackSource",
            "FeedbackTarget",
            "related_to",
            0.0,
            material_action=authorization,
        )


def test_apply_implicit_relations_rejects_invalid_endpoints(tmp_path, monkeypatch):
    """隐式关系持久化不能绕过 KG endpoint quality gate。"""
    monkeypatch.setattr(
        "core.kia.relation_manager._get_db_path",
        lambda: tmp_path / "knowledge_graph.db",
    )
    import sqlite3
    from core.kia.relation_manager import RelationManager, RelationSuggestion

    rm = RelationManager()
    suggestions = [
        RelationSuggestion(
            source="Redis",
            target="系统图谱仍有",
            relation_type="co_occurs",
            confidence=0.9,
            reason="非法短句片段不应持久化",
        )
    ]

    applied = rm.apply_implicit_relations(suggestions, min_confidence=0.5)
    assert applied == 0

    with sqlite3.connect(str(rm._db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM relation_evidence").fetchone()[0] == 0


def test_add_from_distill_rejects_invalid_endpoints(tmp_path, monkeypatch):
    """蒸馏关系写入也必须复用统一 endpoint quality gate。"""
    monkeypatch.setattr(
        "core.kia.relation_manager._get_db_path",
        lambda: tmp_path / "knowledge_graph.db",
    )
    import sqlite3
    from core.kia.relation_manager import RelationManager

    rm = RelationManager()
    relations = rm.add_from_distill(
        {
            "relations": [
                {
                    "source": "通过系统性的",
                    "target": "Docker",
                    "type": "references",
                    "confidence": 0.9,
                    "reason": "非法短句片段不应持久化",
                }
            ]
        }
    )
    assert relations == []

    with sqlite3.connect(str(rm._db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0


def test_add_from_distill_rejects_unknown_relation_type(tmp_path, monkeypatch):
    """蒸馏 relation type 无效时不能静默降级为 references。"""
    monkeypatch.setattr(
        "core.kia.relation_manager._get_db_path",
        lambda: tmp_path / "knowledge_graph.db",
    )
    import sqlite3
    from core.kia.relation_manager import RelationManager

    rm = RelationManager()
    relations = rm.add_from_distill(
        {
            "relations": [
                {
                    "source": "Redis",
                    "target": "Docker",
                    "type": "not_a_real_relation",
                    "confidence": 0.9,
                    "reason": "非法关系类型不应自动降级。",
                }
            ]
        }
    )

    assert relations == []
    with sqlite3.connect(str(rm._db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0


def test_apply_implicit_relations_rejects_unknown_relation_type(tmp_path, monkeypatch):
    """隐式关系建议 relation type 无效时不能静默降级为 related_to。"""
    monkeypatch.setattr(
        "core.kia.relation_manager._get_db_path",
        lambda: tmp_path / "knowledge_graph.db",
    )
    import sqlite3
    from core.kia.relation_manager import RelationManager, RelationSuggestion

    rm = RelationManager()
    applied = rm.apply_implicit_relations(
        [
            RelationSuggestion(
                source="Redis",
                target="Docker",
                relation_type="not_a_real_relation",
                confidence=0.9,
                reason="非法关系类型不应自动降级。",
            )
        ],
        min_confidence=0.5,
    )

    assert applied == 0
    with sqlite3.connect(str(rm._db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0


def _write_wiki_page(wiki_dir: Path, name: str, content: str) -> Path:
    page_dir = wiki_dir / "00-Inbox"
    page_dir.mkdir(parents=True, exist_ok=True)
    page = page_dir / name
    page.write_text(content, encoding="utf-8")
    return page


def test_discover_implicit_relations_batch_reuses_one_wiki_index(tmp_path, monkeypatch):
    """批量隐式关系发现应单次构建 Wiki 索引，避免每个实体反复全库扫 Markdown。"""
    from core.kia.relation_manager import RelationManager

    wiki_dir = tmp_path / "wiki"
    _write_wiki_page(wiki_dir, "redis-a.md", "Redis is often deployed with [[Docker]].")
    _write_wiki_page(wiki_dir, "redis-b.md", "Redis cache runs beside [[Docker]].")
    _write_wiki_page(wiki_dir, "docker.md", "Docker containers can host [[Redis]].")

    rm = RelationManager(str(tmp_path / "knowledge_graph.db"))
    monkeypatch.setattr(rm, "_get_all_entity_names", lambda: ["Redis", "Docker"])

    build_calls = 0
    original_build = rm._build_implicit_relation_index

    def counted_build(wiki_path, all_entities):
        nonlocal build_calls
        build_calls += 1
        return original_build(wiki_path, all_entities)

    monkeypatch.setattr(rm, "_build_implicit_relation_index", counted_build)
    suggestions_by_entity = rm.discover_implicit_relations_batch(
        ["Redis", "Docker"],
        wiki_dir=wiki_dir,
    )

    assert build_calls == 1
    assert set(suggestions_by_entity) == {"Redis", "Docker"}
    assert any(s.target == "Docker" for s in suggestions_by_entity["Redis"])


def test_discover_implicit_relations_single_uses_index_path(tmp_path, monkeypatch):
    """单实体入口也应走索引路径，而不是回退到旧的逐页扫描 helper。"""
    from core.kia.relation_manager import RelationManager

    wiki_dir = tmp_path / "wiki"
    _write_wiki_page(wiki_dir, "redis-a.md", "Redis is often deployed with [[Docker]].")
    _write_wiki_page(wiki_dir, "redis-b.md", "Redis cache runs beside [[Docker]].")
    _write_wiki_page(wiki_dir, "docker.md", "Docker containers can host [[Redis]].")

    rm = RelationManager(str(tmp_path / "knowledge_graph.db"))
    monkeypatch.setattr(rm, "_get_all_entity_names", lambda: ["Redis", "Docker"])

    build_calls = 0
    original_build = rm._build_implicit_relation_index

    def counted_build(wiki_path, all_entities):
        nonlocal build_calls
        build_calls += 1
        return original_build(wiki_path, all_entities)

    monkeypatch.setattr(rm, "_build_implicit_relation_index", counted_build)

    suggestions = rm.discover_implicit_relations("Redis", wiki_dir=wiki_dir)

    assert build_calls == 1
    assert any(s.target == "Docker" for s in suggestions)


def test_discover_implicit_relations_batch_sees_new_markdown_files(tmp_path, monkeypatch):
    """批量索引应按本次调用重建，新增 Markdown 不会被旧缓存遮住。"""
    from core.kia.relation_manager import RelationManager

    wiki_dir = tmp_path / "wiki"
    _write_wiki_page(wiki_dir, "redis-a.md", "Redis is often deployed with [[Docker]].")
    _write_wiki_page(wiki_dir, "redis-b.md", "Redis cache runs beside [[Docker]].")

    rm = RelationManager(str(tmp_path / "knowledge_graph.db"))
    monkeypatch.setattr(rm, "_get_all_entity_names", lambda: ["Redis", "Docker", "Kafka"])

    first = rm.discover_implicit_relations_batch(["Redis"], wiki_dir=wiki_dir)
    assert not any(s.target == "Kafka" for s in first["Redis"])

    _write_wiki_page(wiki_dir, "redis-kafka-a.md", "Redis streams can feed [[Kafka]].")
    _write_wiki_page(wiki_dir, "redis-kafka-b.md", "Redis events are bridged into [[Kafka]].")

    second = rm.discover_implicit_relations_batch(["Redis"], wiki_dir=wiki_dir)
    assert any(s.target == "Kafka" for s in second["Redis"])


def test_implicit_relation_index_precomputes_entity_pages(tmp_path):
    """建索引时应预计算 entity->pages，查找阶段不再遍历 page_lower。"""
    from core.kia.relation_manager import RelationManager

    wiki_dir = tmp_path / "wiki"
    _write_wiki_page(
        wiki_dir,
        "redis-cache.md",
        "Redis cache uses [[Docker]], and Redis is also mentioned separately.",
    )

    rm = RelationManager(str(tmp_path / "knowledge_graph.db"))
    index = rm._build_implicit_relation_index(wiki_dir, ["Redis", "Redis cache", "Docker"])

    class NoItemsDict(dict):
        def items(self):
            raise AssertionError("entity page lookup should use precomputed index cache")

    index.page_lower = NoItemsDict(index.page_lower)

    redis_pages = rm._find_pages_mentioning_in_index("Redis", index)
    redis_cache_pages = rm._find_pages_mentioning_in_index("Redis cache", index)

    assert redis_pages
    assert redis_cache_pages
    assert redis_pages == redis_cache_pages


def test_implicit_relation_index_skips_derived_kg_artifacts(tmp_path):
    """隐式关系索引不应扫描 Shadow/Relations/报告等派生产物。"""
    from core.kia.relation_manager import RelationManager

    wiki_dir = tmp_path / "wiki"
    _write_wiki_page(
        wiki_dir,
        "redis-cache.md",
        "Redis cache uses [[Docker]], and Redis is mentioned in canonical content.",
    )
    derived_files = [
        wiki_dir / "07-Shadow" / "shadow.md",
        wiki_dir / "L2.4-KG" / "Entities" / "kg-redis.md",
        wiki_dir / "L2.4-KG" / "Relations" / "rel.md",
        wiki_dir / "L2.4-KG" / "MOCs" / "entities.md",
        wiki_dir / "05-MOCs" / "Mnemos-Navigation" / "Vault-导航-001.md",
        wiki_dir / "99-Reports" / "report.md",
        wiki_dir / "06-Retrospectives" / "entropy" / "entropy-suggestions-2026.md",
    ]
    for path in derived_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Redis and Kafka appear only in derived artifacts.", encoding="utf-8")

    rm = RelationManager(str(tmp_path / "knowledge_graph.db"))
    index = rm._build_implicit_relation_index(wiki_dir, ["Redis", "Kafka", "Docker"])

    indexed_rel_paths = {str(path.relative_to(wiki_dir)) for path in index.page_text}
    assert "00-Inbox/redis-cache.md" in indexed_rel_paths
    assert all("07-Shadow" not in path for path in indexed_rel_paths)
    assert all("L2.4-KG" not in path for path in indexed_rel_paths)
    assert all("Mnemos-Navigation" not in path for path in indexed_rel_paths)
    assert all("99-Reports" not in path for path in indexed_rel_paths)
    assert all("entropy-suggestions" not in path for path in indexed_rel_paths)
    assert rm._find_pages_mentioning_in_index("Kafka", index) == []
