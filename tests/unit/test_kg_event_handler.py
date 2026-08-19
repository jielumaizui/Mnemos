# -*- coding: utf-8 -*-
"""KGEventHandler 单元测试"""

from pathlib import Path
from contextlib import contextmanager

import pytest


@pytest.fixture
def em(tmp_path, monkeypatch):
    """隔离 EntityManager 数据库。"""
    monkeypatch.setattr(
        "core.kia.entity_manager._get_db_path",
        lambda: tmp_path / "knowledge_graph.db",
    )
    from core.kia.entity_manager import EntityManager

    return EntityManager()


def test_on_entity_accessed_updates_quality(em):
    """on_entity_accessed 应更新实体的质量分与置信度。"""
    from core.kia.kg_event_handler import KGEventHandler

    entity = em._upsert_entity("Redis", entity_type="technology")
    original_confidence = entity.confidence
    original_quality = entity.quality_score

    handler = KGEventHandler()
    handler.on_entity_accessed("Redis")

    updated = em.get_entity(entity.uid)
    assert updated.confidence > original_confidence
    assert updated.quality_score > original_quality


def test_on_entity_accessed_resolves_alias(em):
    """on_entity_accessed 应支持别名解析。"""
    from core.kia.kg_event_handler import KGEventHandler

    entity = em._upsert_entity("Redis", entity_type="technology")
    em.add_alias(entity.uid, "redis")
    original_confidence = entity.confidence

    handler = KGEventHandler()
    handler.on_entity_accessed("redis")

    updated = em.get_entity(entity.uid)
    assert updated.confidence > original_confidence


def test_on_entity_accessed_accepts_uid(em):
    """on_entity_accessed 应支持 UID 输入。"""
    from core.kia.kg_event_handler import KGEventHandler

    entity = em._upsert_entity("Docker", entity_type="technology")
    original_confidence = entity.confidence

    handler = KGEventHandler()
    handler.on_entity_accessed(entity.uid)

    updated = em.get_entity(entity.uid)
    assert updated.confidence > original_confidence


def test_material_plan_is_read_only_before_projection_authorization(
    tmp_path: Path,
):
    from core.kia.kg_event_handler import KGEventHandler

    database_dir = tmp_path / "database"
    wiki_dir = tmp_path / "vault"
    wiki_dir.mkdir()
    page = wiki_dir / "redis.md"
    page.write_text("# Redis\n", encoding="utf-8")

    class FakeConfig:
        def __init__(self):
            self.database_dir = database_dir
            self.wiki_dir = wiki_dir

        def get(self, key, default=None):
            if key == "knowledge_graph.implicit_relation_discovery_enabled":
                return False
            return default

    db_path = database_dir / "knowledge_graph.db"
    handler = KGEventHandler(
        db_path=db_path,
        wiki_base=wiki_dir,
        config=FakeConfig(),
    )

    plan = handler.plan_on_distilled(
        {"wiki_pages": [str(page)], "kg_input": {}}
    )

    assert plan.event_kind == "knowledge_distilled"
    assert not db_path.exists()
    assert not database_dir.exists()


def test_on_distilled_runs_implicit_relation_discovery(tmp_path, monkeypatch):
    """on_distilled 应对涉及实体调用隐式关系发现（P1-11）。"""
    from unittest.mock import MagicMock
    from core.kia.kg_event_handler import KGEventHandler
    from core.kia.relation_manager import RelationSuggestion

    handler = KGEventHandler()
    monkeypatch.setattr(
        handler,
        "_project_kg_to_vault",
        lambda _kg: {
            "projection_enabled": False,
            "projection_entities": 0,
            "projection_relations": 0,
            "projection_errors": 0,
        },
    )

    fake_entity = MagicMock()
    fake_entity.name = "Redis"
    fake_entity.source_count = 1
    fake_em = MagicMock()
    fake_em.ingest_from_wiki.return_value = [fake_entity]
    handler._entity_manager = fake_em

    fake_suggestion = RelationSuggestion(
        source="Redis",
        target="Docker",
        relation_type="co_occurs",
        confidence=0.6,
        reason="在页面中同时出现",
    )
    fake_rm = MagicMock()
    fake_rm.plan_distill_relations.return_value = []
    fake_rm.discover_implicit_relations_batch.return_value = {"Redis": [fake_suggestion]}
    fake_rm.plan_implicit_relations.return_value = [MagicMock()]
    fake_rm.apply_planned_relations.return_value = 1
    handler._relation_manager = fake_rm

    class FakeKG:
        def __init__(self, **_kwargs):
            pass

        def _candidate_existing_pages(self):
            return []

        def discover_relations(self, page, existing_pages=None, new_content=None):
            return []

        def apply_discovered(self, discovered, min_confidence=0.7):
            return 0

        def prepare_discovered_relations(self, relations, min_confidence=0.7):
            return relations

        def close(self):
            return None

        def export_to_vault(self):
            pass

    monkeypatch.setattr("core.kia.knowledge_graph.KnowledgeGraph", FakeKG)

    page_path = tmp_path / "redis.md"
    page_path.write_text("# Redis\n[[Redis]]", encoding="utf-8")
    event = {
        "session_id": "s1",
        "wiki_pages": [str(page_path)],
        "kg_input": {},
    }
    result = handler.on_distilled(event)

    assert result["relations_implicit"] == 1
    fake_rm.discover_implicit_relations_batch.assert_called_once()
    fake_rm.apply_planned_relations.assert_called_once()


def test_on_distilled_allows_disabling_implicit_relation_batch_by_limit(tmp_path, monkeypatch):
    """max_entities_per_event=0 应完全跳过隐式关系批处理。"""
    from unittest.mock import MagicMock
    from core.kia.kg_event_handler import KGEventHandler

    page_path = tmp_path / "redis.md"
    page_path.write_text("# Redis\n[[Redis]]", encoding="utf-8")

    class FakeConfig:
        database_dir = tmp_path
        wiki_dir = tmp_path / "vault"

        def get(self, key, default=None):
            values = {
                "knowledge_graph.implicit_relation_discovery_enabled": True,
                "knowledge_graph.implicit_relation_max_entities_per_event": 0,
                "knowledge_graph.projection_enabled": False,
            }
            return values.get(key, default)

    fake_entity = MagicMock()
    fake_entity.name = "Redis"
    fake_entity.source_count = 1

    handler = KGEventHandler(
        config=FakeConfig(),
        wiki_base=FakeConfig.wiki_dir,
    )
    fake_em = MagicMock()
    fake_em.ingest_from_wiki.return_value = [fake_entity]
    handler._entity_manager = fake_em

    fake_rm = MagicMock()
    fake_rm.plan_distill_relations.return_value = []
    fake_rm.apply_planned_relations.return_value = 0
    handler._relation_manager = fake_rm

    class FakeKG:
        def __init__(self, **_kwargs):
            pass

        def _candidate_existing_pages(self):
            return []

        def discover_relations(self, page, existing_pages=None, new_content=None):
            return []

        def apply_discovered(self, discovered, min_confidence=0.7):
            return 0

        def prepare_discovered_relations(self, relations, min_confidence=0.7):
            return relations

        def close(self):
            return None

    monkeypatch.setattr("core.kia.knowledge_graph.KnowledgeGraph", FakeKG)

    result = handler.on_distilled({"wiki_pages": [str(page_path)], "kg_input": {}})

    assert result["relations_implicit"] == 0
    fake_rm.discover_implicit_relations_batch.assert_not_called()
    fake_rm.plan_implicit_relations.assert_not_called()


def test_on_distilled_resolves_blindspots_by_wiki_page(tmp_path, monkeypatch):
    """on_distilled 应在 wiki 页面生成后自动关闭相关盲区（P1-1）。"""
    import hashlib
    from unittest.mock import MagicMock, patch
    from core.kia.kg_event_handler import KGEventHandler

    class FakeConfig:
        database_dir = tmp_path
        wiki_dir = tmp_path / "vault"

        def get(self, _key, default=None):
            return default

    handler = KGEventHandler(config=FakeConfig(), wiki_base=FakeConfig.wiki_dir)
    monkeypatch.setattr(
        handler,
        "_project_kg_to_vault",
        lambda _kg: {
            "projection_enabled": False,
            "projection_entities": 0,
            "projection_relations": 0,
            "projection_errors": 0,
        },
    )

    fake_entity = MagicMock()
    fake_entity.name = "Redis"
    fake_entity.source_count = 1
    fake_em = MagicMock()
    fake_em.ingest_from_wiki.return_value = [fake_entity]
    handler._entity_manager = fake_em

    fake_rm = MagicMock()
    fake_rm.plan_distill_relations.return_value = []
    fake_rm.discover_implicit_relations_batch.return_value = {"Redis": []}
    fake_rm.plan_implicit_relations.return_value = []
    fake_rm.apply_planned_relations.return_value = 0
    handler._relation_manager = fake_rm

    class FakeKG:
        def __init__(self, **_kwargs):
            pass

        def _candidate_existing_pages(self):
            return []

        def discover_relations(self, page, existing_pages=None, new_content=None):
            return []

        def apply_discovered(self, discovered, min_confidence=0.7):
            return 0

        def prepare_discovered_relations(self, relations, min_confidence=0.7):
            return relations

        def close(self):
            return None

    monkeypatch.setattr("core.kia.knowledge_graph.KnowledgeGraph", FakeKG)

    # Mock BlindspotDiscovery.resolve_by_wiki_page 验证被调用
    with patch("core.app.blindspot_discovery.BlindspotDiscovery") as MockBD:
        fake_bd = MagicMock()
        fake_bd.resolve_by_wiki_page.return_value = 1
        MockBD.return_value = fake_bd

        page_path = tmp_path / "redis.md"
        page_path.write_text("# Redis\n[[Redis]]", encoding="utf-8")
        content_hash = "sha256:" + hashlib.sha256(page_path.read_bytes()).hexdigest()
        event = {
            "session_id": "s1",
            "wiki_pages": [str(page_path)],
            "kg_input": {},
            "wiki_projection_receipts": {
                str(page_path): {
                    "canonical_revision_id": "wiki-revision-1",
                    "projection_receipt_id": "projection-receipt-1",
                    "content_hash": content_hash,
                }
            },
            "knowledge_coverage_receipts": {
                str(page_path): {
                    "resolution_evidence": [
                        {
                            "receipt_id": "coverage-recheck-1",
                            "asset_id": "kcg-test-1",
                            "gap_revision_id": "kcg-test-1:r2",
                            "scope_key": "sha256:scope",
                            "verifier_id": "knowledge-coverage-auditor-v1",
                            "verification_method": "authorized-context-requery",
                            "content_hash": content_hash,
                            "verified_at": "2026-07-23T00:00:00+00:00",
                            "outcome": "covered",
                        }
                    ],
                }
            },
        }
        result = handler.on_distilled(event)

        assert result.get("blindspots_resolved") == 1
        MockBD.assert_called_once_with(
            wiki_base=str(FakeConfig.wiki_dir),
            db_path=str(tmp_path / "blindspots.db"),
        )
        fake_bd.resolve_by_wiki_page.assert_called_once_with(
            str(page_path),
            canonical_revision_id="wiki-revision-1",
            projection_receipt_id="projection-receipt-1",
            content_hash=content_hash,
            coverage_evidence=(
                {
                    "receipt_id": "coverage-recheck-1",
                    "asset_id": "kcg-test-1",
                    "gap_revision_id": "kcg-test-1:r2",
                    "scope_key": "sha256:scope",
                    "verifier_id": "knowledge-coverage-auditor-v1",
                    "verification_method": "authorized-context-requery",
                    "content_hash": content_hash,
                    "verified_at": "2026-07-23T00:00:00+00:00",
                    "outcome": "covered",
                },
            ),
        )

        fake_bd.resolve_by_wiki_page.side_effect = RuntimeError("coverage store unavailable")
        retry_result = handler.on_distilled(event)
        from core.event_outcome import HandlerOutcome

        outcome = HandlerOutcome.from_result(retry_result, consumer="knowledge_graph")
        assert retry_result["status"] == "retry"
        assert retry_result["success"] is False
        assert outcome.disposition == "retry"


def test_on_distilled_runs_bounded_kg_projection(tmp_path, monkeypatch):
    """on_distilled 应在 KG 更新后运行受控 L2.4-KG 投影。"""
    from unittest.mock import MagicMock
    from core.kia.kg_event_handler import KGEventHandler

    page_path = tmp_path / "redis.md"
    page_path.write_text("# Redis\n[[Redis]]", encoding="utf-8")

    class FakeConfig:
        wiki_dir = tmp_path / "vault"

        def get(self, key, default=None):
            values = {
                "knowledge_graph.projection_enabled": True,
                "knowledge_graph.projection_max_relations": 7,
                "knowledge_graph.projection_max_relations_per_entity": 2,
            }
            return values.get(key, default)

    fake_entity = MagicMock()
    fake_entity.name = "Redis"
    fake_entity.source_count = 1

    handler = KGEventHandler(
        config=FakeConfig(),
        wiki_base=FakeConfig.wiki_dir,
    )
    fake_em = MagicMock()
    fake_em.ingest_from_wiki.return_value = [fake_entity]
    handler._entity_manager = fake_em

    fake_rm = MagicMock()
    fake_rm.plan_distill_relations.return_value = []
    fake_rm.discover_implicit_relations_batch.return_value = {"Redis": []}
    fake_rm.plan_implicit_relations.return_value = []
    fake_rm.apply_planned_relations.return_value = 0
    handler._relation_manager = fake_rm

    class FakeKG:
        def __init__(self, **_kwargs):
            pass

        def _candidate_existing_pages(self):
            return []

        def discover_relations(self, page, existing_pages=None, new_content=None):
            return []

        def apply_discovered(self, discovered, min_confidence=0.7):
            return 0

        def prepare_discovered_relations(self, relations, min_confidence=0.7):
            return relations

        def close(self):
            return None

    class FakeExporter:
        def __init__(self, vault_dir, kg):
            self.vault_dir = vault_dir
            self.kg = kg
            self.MAX_EXPORTED_RELATIONS = 200
            self.MAX_RELATIONS_PER_ENTITY = 5

        def export_to_vault(self):
            assert self.vault_dir == str(FakeConfig.wiki_dir)
            assert self.MAX_EXPORTED_RELATIONS == 7
            assert self.MAX_RELATIONS_PER_ENTITY == 2
            return {"entities": 3, "relations": 1}

    monkeypatch.setattr("core.kia.knowledge_graph.KnowledgeGraph", FakeKG)
    monkeypatch.setattr("core.kia.kg_exporter.KGExporter", FakeExporter)

    result = handler.on_distilled({"wiki_pages": [str(page_path)], "kg_input": {}})

    assert result["projection_enabled"] is True
    assert result["projection_entities"] == 3
    assert result["projection_relations"] == 1
    assert result["projection_errors"] == 0


def test_kg_projection_prefers_injected_runtime_config(tmp_path, monkeypatch):
    from core.kia.kg_event_handler import KGEventHandler

    class InjectedConfig:
        wiki_dir = tmp_path / "injected-vault"

        def get(self, key, default=None):
            if key == "knowledge_graph.projection_enabled":
                return False
            return default

    class GlobalConfig:
        wiki_dir = tmp_path / "global-vault"

        def get(self, key, default=None):
            if key == "knowledge_graph.projection_enabled":
                return True
            return default

    class ForbiddenExporter:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("disabled injected config must prevent projection")

    monkeypatch.setattr(
        "core.kia.kg_event_handler.get_config",
        lambda: GlobalConfig(),
    )
    monkeypatch.setattr("core.kia.kg_exporter.KGExporter", ForbiddenExporter)
    handler = KGEventHandler(
        config=InjectedConfig(),
        wiki_base=InjectedConfig.wiki_dir,
    )

    result = handler._project_kg_to_vault(object())

    assert result == {
        "projection_enabled": False,
        "projection_entities": 0,
        "projection_relations": 0,
        "projection_errors": 0,
    }


def test_kg_projection_forwards_injected_lifecycle_without_runtime_receipts(
    tmp_path, monkeypatch
):
    """An isolated rebuild must not fall back to process-global publishers."""

    from core.kia.kg_event_handler import KGEventHandler

    class FakeConfig:
        wiki_dir = tmp_path / "isolated-vault"

        def get(self, key, default=None):
            if key == "knowledge_graph.projection_enabled":
                return True
            return default

    lifecycle = object()
    captured = {}

    class FakeExporter:
        MAX_EXPORTED_RELATIONS = 200
        MAX_RELATIONS_PER_ENTITY = 5

        def __init__(self, vault_dir, *, kg, **kwargs):
            captured.update(vault_dir=vault_dir, kg=kg, **kwargs)

        def export_to_vault(self):
            return {"entities": 1, "relations": 2}

    monkeypatch.setattr("core.kia.kg_exporter.KGExporter", FakeExporter)
    kg = object()
    handler = KGEventHandler(
        config=FakeConfig(),
        wiki_base=FakeConfig.wiki_dir,
        projection_lifecycle=lifecycle,
        emit_projection_runtime_consumption=False,
    )

    result = handler._project_kg_to_vault(kg)

    assert captured == {
        "vault_dir": str(FakeConfig.wiki_dir),
        "kg": kg,
        "lifecycle": lifecycle,
        "emit_runtime_consumption": False,
    }
    assert result["projection_entities"] == 1
    assert result["projection_relations"] == 2


def test_kg_projection_failure_propagates_to_the_event_consumer(tmp_path, monkeypatch):
    """A failed formal projection must remain retryable, never look successful."""

    from core.kia.kg_event_handler import KGEventHandler

    class FakeConfig:
        wiki_dir = tmp_path / "vault"

        def get(self, key, default=None):
            if key == "knowledge_graph.projection_enabled":
                return True
            return default

    class FailingExporter:
        MAX_EXPORTED_RELATIONS = 200
        MAX_RELATIONS_PER_ENTITY = 5

        def __init__(self, *_args, **_kwargs):
            pass

        def export_to_vault(self):
            raise OSError("injected projection failure")

    monkeypatch.setattr("core.kia.kg_exporter.KGExporter", FailingExporter)
    handler = KGEventHandler(config=FakeConfig(), wiki_base=FakeConfig.wiki_dir)

    with pytest.raises(OSError, match="injected projection failure"):
        handler._project_kg_to_vault(object())


def test_deferred_page_update_replay_batches_embeddings_and_projects_once(
    tmp_path, monkeypatch
):
    from core.kia.kg_event_handler import KGEventHandler

    class FakeConfig:
        wiki_dir = tmp_path / "vault"

        def get(self, _key, default=None):
            return default

    class FakeKG:
        def __init__(self):
            self.embedding_batches = 0
            self.closed = 0

        @contextmanager
        def defer_relation_embeddings(self):
            self.embedding_batches += 1
            yield {}

        def close(self):
            self.closed += 1

    projections = []

    class FakeExporter:
        MAX_EXPORTED_RELATIONS = 200
        MAX_RELATIONS_PER_ENTITY = 5

        def __init__(self, _vault_dir, *, kg):
            self.kg = kg

        def export_to_vault(self):
            projections.append(self.kg)
            return {"entities": 2, "relations": 3}

    kg = FakeKG()
    handler = KGEventHandler(config=FakeConfig(), wiki_base=FakeConfig.wiki_dir)
    monkeypatch.setattr(handler, "_new_knowledge_graph", lambda **_kwargs: kg)
    monkeypatch.setattr("core.kia.kg_exporter.KGExporter", FakeExporter)

    with handler.deferred_page_update_replay():
        first = handler._project_kg_to_vault(kg)
        second = handler._project_kg_to_vault(kg)
        assert first["projection_deferred"] is True
        assert second["projection_deferred"] is True
        assert projections == []

    assert kg.embedding_batches == 1
    assert kg.closed == 1
    assert projections == [kg]


@pytest.mark.parametrize("replace_existing", [False, True])
def test_dependency_closure_batch_retracts_before_shared_candidate_discovery(
    tmp_path, monkeypatch, replace_existing
):
    from core.kia.kg_event_handler import KGEventHandler

    wiki = tmp_path / "wiki"
    first = wiki / "first.md"
    second = wiki / "second.md"
    wiki.mkdir()
    first.write_text("# First\n", encoding="utf-8")
    second.write_text("# Second\n", encoding="utf-8")
    calls = []

    class FakeConfig:
        wiki_dir = wiki

        def get(self, _key, default=None):
            return default

    class FakeEntityManager:
        def ingest_from_wiki(self, page, *, content):
            calls.append(("ingest", page.name, content.strip()))
            return []

    class FakeKG:
        def defer_relation_embeddings(self):
            @contextmanager
            def scope():
                calls.append(("batch_start",))
                yield {}
                calls.append(("batch_end",))

            return scope()

        def reconcile_page_lifecycle(self, **kwargs):
            calls.append(("retract", kwargs["page_path"].name))
            return {"relations_deleted": 0}

        def _candidate_existing_pages(self):
            return [first, second]

        def prepare_relation_candidates(self, pages):
            calls.append(("cache", tuple(page.name for page in pages)))
            return {page: {"cached": True} for page in pages}

        def discover_relations(self, page, **kwargs):
            calls.append(("discover", page.name, kwargs["candidate_cache"]))
            return []

        def apply_discovered(self, _relations, min_confidence):
            assert min_confidence == 0.4
            return 0

        def normalize_entity_primary_sources(self):
            calls.append(("normalize_sources",))
            return 0

        def close(self):
            calls.append(("close",))

    handler = KGEventHandler(config=FakeConfig(), wiki_base=wiki)
    monkeypatch.setattr(handler, "_new_knowledge_graph", lambda **_kwargs: FakeKG())
    monkeypatch.setattr(handler, "_get_entity_manager", lambda: FakeEntityManager())
    monkeypatch.setattr(handler, "_project_kg_to_vault", lambda _kg: {"projected": True})

    result = handler.reconcile_pages(
        [first, second], replace_existing=replace_existing
    )

    assert result["status"] == "ok"
    assert [call[:2] for call in calls if call[0] == "retract"] == (
        [("retract", "first.md"), ("retract", "second.md")]
        if replace_existing
        else []
    )
    assert calls.index(("normalize_sources",)) < next(
        index for index, call in enumerate(calls) if call[0] == "discover"
    )
    cache_calls = [call for call in calls if call[0] == "cache"]
    assert cache_calls == [("cache", ("first.md", "second.md"))]


def test_knowledge_graph_projection_target_includes_exported_markdown(
    tmp_path,
):
    from daemon.wiki_projection_handlers import (
        _projection_state_hash,
        _projection_target_paths,
    )

    database_dir = tmp_path / "database"
    embedding_dir = database_dir / "embedding_index"
    wiki_dir = tmp_path / "wiki"
    paths = _projection_target_paths(
        "knowledge_graph",
        database_dir=database_dir,
        embedding_index_dir=embedding_dir,
        wiki_dir=wiki_dir,
    )

    assert paths == (
        database_dir / "knowledge_graph.db",
        database_dir / "blindspots.db",
        wiki_dir / "L2.4-KG",
    )
    before_hash = _projection_state_hash(paths)
    (wiki_dir / "L2.4-KG").mkdir(parents=True)
    (wiki_dir / "L2.4-KG" / "entity.md").write_text(
        "# Entity\n",
        encoding="utf-8",
    )
    assert _projection_state_hash(paths) != before_hash
