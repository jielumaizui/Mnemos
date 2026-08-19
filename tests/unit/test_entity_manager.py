"""
Tests for core.kia.entity_manager

Covers: Entity dataclass, _is_valid_entity_name, _slugify, _parse_frontmatter,
        ingest_from_wiki, CRUD operations, alias resolution, quality update.
"""

import sqlite3
from pathlib import Path
import pytest

from core.cognitive.material_effect_schema import reconcile_material_effect_schema
from core.kia.entity_manager import (
    Entity,
    EntityManager,
)


class TestEntity:
    def test_entity_defaults(self):
        e = Entity(uid="test", name="Test")
        assert e.entity_type == "concept"
        assert e.quality_score == 0.5
        assert e.confidence == 0.5
        assert e.temporal_scope == "stable"
        assert e.status == "active"
        assert e.visit_count == 0
        assert e.tags == set()
        assert e.aliases == []

    def test_source_page(self):
        e = Entity(uid="t", name="T")
        e.source_page = "page.md"
        assert e.source_page == "page.md"


class TestIsValidEntityName:
    def test_too_short(self):
        assert EntityManager._is_valid_entity_name("A") is False

    def test_too_long(self):
        assert EntityManager._is_valid_entity_name("x" * 51) is False

    def test_pure_number(self):
        assert EntityManager._is_valid_entity_name("12345") is False

    def test_stop_word(self):
        assert EntityManager._is_valid_entity_name("的") is False

    def test_bad_start(self):
        assert EntityManager._is_valid_entity_name("在被攻击") is False

    def test_bad_end(self):
        assert EntityManager._is_valid_entity_name("问题是") is False

    def test_function_word_in_middle(self):
        assert EntityManager._is_valid_entity_name("A与B") is False

    def test_valid_chinese(self):
        assert EntityManager._is_valid_entity_name("知识图谱") is True

    def test_valid_english(self):
        assert EntityManager._is_valid_entity_name("Kubernetes") is True

    def test_valid_mixed(self):
        assert EntityManager._is_valid_entity_name("Python编程") is True


class TestSlugify:
    def test_basic(self):
        assert EntityManager._slugify("Hello World") == "hello-world"

    def test_chinese(self):
        assert EntityManager._slugify("知识图谱") == "知识图谱"

    def test_special_chars(self):
        assert EntityManager._slugify("a@b#c") == "a-b-c"

    def test_trailing_dashes_removed(self):
        assert EntityManager._slugify("-test-") == "test"

    def test_max_length(self):
        long_name = "a" * 100
        slug = EntityManager._slugify(long_name)
        assert len(slug) <= 64


class TestParseFrontmatter:
    def test_no_frontmatter(self):
        assert EntityManager._parse_frontmatter("plain text") == {}

    def test_simple_frontmatter(self):
        content = "---\nkey: value\n---\nbody"
        fm = EntityManager._parse_frontmatter(content)
        assert fm == {"key": "value"}

    def test_list_value(self):
        content = '---\ntags: ["a", "b", "c"]\n---\nbody'
        fm = EntityManager._parse_frontmatter(content)
        assert fm["tags"] == ["a", "b", "c"]

    def test_malformed_json_list(self):
        content = "---\ntags: [a, b\n---\nbody"
        fm = EntityManager._parse_frontmatter(content)
        assert fm["tags"] == "[a, b"  # 解析失败，保留原字符串


class TestEntityManagerCRUD:
    @pytest.fixture
    def mgr(self, tmp_path, fake_config):
        fake_config.database_dir = tmp_path
        db_path = tmp_path / "test_entities.db"
        m = EntityManager(db_path=db_path, config=fake_config)
        yield m

    def test_init_creates_tables(self, mgr, tmp_path):
        # init 已在 fixture 中完成
        db_path = tmp_path / "test_entities.db"
        assert db_path.exists()
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor}
            assert "entities" in tables
            assert "entity_aliases" in tables

    def test_read_only_manager_rejects_entity_mutation(
        self,
        tmp_path,
        fake_config,
    ):
        db_path = tmp_path / "entities.db"
        writable = EntityManager(db_path=db_path, config=fake_config)
        read_only = EntityManager(
            db_path=db_path,
            config=fake_config,
            initialize=False,
            read_only=True,
        )

        with pytest.raises(PermissionError, match="read-only EntityManager"):
            read_only.add_entity("Rust语言")

        assert writable.get_entity_by_name("Rust语言") is None

    def test_init_applies_declared_migrations(self, tmp_path, fake_config, monkeypatch):
        db_path = tmp_path / "legacy_entities.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE entities (
                    uid TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    entity_type TEXT DEFAULT 'concept',
                    quality_score REAL DEFAULT 0.5,
                    status TEXT DEFAULT 'active',
                    visit_count INTEGER DEFAULT 0
                )
                """
            )
            reconcile_material_effect_schema(conn, apply=True)
            conn.commit()

        monkeypatch.setattr(
            EntityManager,
            "MIGRATIONS",
            EntityManager.MIGRATIONS
            + ["ALTER TABLE entities ADD COLUMN migration_marker TEXT DEFAULT 'ok';"],
        )

        EntityManager(db_path=db_path, config=fake_config)

        with sqlite3.connect(str(db_path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(entities)")}
        assert "migration_marker" in columns

    def test_upsert_and_get(self, mgr):
        e = mgr._upsert_entity("Python", entity_type="technology", wiki_page="python.md")
        assert e.uid == "python"
        assert e.name == "Python"

        fetched = mgr.get_entity("python")
        assert fetched is not None
        assert fetched.name == "Python"
        assert fetched.source_page == "python.md"

    def test_get_nonexistent(self, mgr):
        assert mgr.get_entity("nonexistent") is None

    def test_get_by_name(self, mgr):
        mgr._upsert_entity("Docker", wiki_page="docker.md")
        e = mgr.get_entity_by_name("Docker")
        assert e is not None
        assert e.name == "Docker"

    def test_upsert_increments_source_count(self, mgr):
        mgr._upsert_entity("Go")
        mgr._upsert_entity("Go")
        e = mgr.get_entity("go")
        assert e.source_count == 2

    def test_upsert_counts_distinct_wiki_sources_idempotently(self, mgr):
        first = mgr._upsert_entity("Rust", wiki_page="03-Tech/rust.md")
        replay = mgr._upsert_entity("Rust", wiki_page="03-Tech/rust.md")
        assert replay.last_updated == first.last_updated
        mgr._upsert_entity("Rust", wiki_page="04-Concepts/rust.md")

        entity = mgr.get_entity("rust")
        assert entity.source_count == 2
        with sqlite3.connect(str(mgr._db_path)) as conn:
            sources = conn.execute(
                "SELECT source_page FROM entity_sources WHERE entity_uid='rust' ORDER BY source_page"
            ).fetchall()
        assert sources == [("03-Tech/rust.md",), ("04-Concepts/rust.md",)]

    def test_update_quality(self, mgr):
        mgr._upsert_entity("TestEntity")
        mgr.update_quality("testentity", _feedback_expected=1.0, feedback_actual=0.9)
        e = mgr.get_entity("testentity")
        assert e.quality_score > 0.5
        assert e.confidence > 0.5
        assert e.last_updated

    def test_update_quality_uses_expected_feedback_as_calibration(self, mgr):
        mgr._upsert_entity("CalibratedEntity")
        mgr.update_quality("calibratedentity", _feedback_expected=0.5, feedback_actual=0.45)
        e = mgr.get_entity("calibratedentity")
        assert e.quality_score > 0.5

    def test_add_alias_and_resolve(self, mgr):
        mgr._upsert_entity("Kubernetes")
        mgr.add_alias("kubernetes", "k8s")
        e = mgr.resolve_alias("k8s")
        assert e is not None
        assert e.name == "Kubernetes"

    def test_resolve_alias_by_name_fallback(self, mgr):
        mgr._upsert_entity("Redis")
        e = mgr.resolve_alias("Redis")
        assert e is not None
        assert e.name == "Redis"

    def test_get_all_entities(self, mgr):
        mgr._upsert_entity("A", entity_type="concept")
        mgr._upsert_entity("B", entity_type="technology")
        all_entities = mgr.get_all_entities()
        assert len(all_entities) == 2

    def test_get_all_entities_by_type(self, mgr):
        mgr._upsert_entity("A", entity_type="concept")
        mgr._upsert_entity("B", entity_type="technology")
        concepts = mgr.get_all_entities(entity_type="concept")
        assert len(concepts) == 1
        assert concepts[0].name == "A"

    def test_get_all_entities_min_quality(self, mgr):
        mgr._upsert_entity("A")
        mgr.update_quality("a", 1.0, 0.9)
        mgr._upsert_entity("B")
        # B 默认 quality_score 0.5
        high_quality = mgr.get_all_entities(min_quality=0.5)
        assert len(high_quality) == 2
        names = {e.name for e in high_quality}
        assert "A" in names
        assert "B" in names


class TestIngestFromWiki:
    @pytest.fixture
    def mgr(self, tmp_path, fake_config):
        fake_config.database_dir = tmp_path
        db_path = tmp_path / "test_entities.db"
        m = EntityManager(db_path=db_path, config=fake_config)
        yield m

    def test_ingest_from_frontmatter_keywords(self, mgr, tmp_path):
        page = tmp_path / "test.md"
        # _parse_frontmatter 是简单行解析器，不支持嵌套 YAML dict
        page.write_text(
            '---\n关键词: ["Python", "Docker"]\n---\nbody',
            encoding="utf-8",
        )
        entities = mgr.ingest_from_wiki(page)
        assert len(entities) == 2
        names = {e.name for e in entities}
        assert "Python" in names
        assert "Docker" in names

    def test_ingest_from_wiki_links(self, mgr, tmp_path):
        page = tmp_path / "test.md"
        page.write_text(
            "---\n---\nsee [[Kubernetes]] and [[03-Tech/Redis]]",
            encoding="utf-8",
        )
        entities = mgr.ingest_from_wiki(page)
        names = {e.name for e in entities}
        assert "Kubernetes" in names
        assert "Redis" in names

    def test_ingest_invalid_entities_filtered(self, mgr, tmp_path):
        page = tmp_path / "test.md"
        page.write_text(
            "---\n---\nsee [[的]] and [[12345]]",
            encoding="utf-8",
        )
        entities = mgr.ingest_from_wiki(page)
        assert len(entities) == 0

    def test_ingest_read_error_returns_empty(self, mgr, tmp_path):
        page = tmp_path / "nonexistent.md"
        entities = mgr.ingest_from_wiki(page)
        assert entities == []

    def test_ingest_with_content_param(self, mgr):
        content = '---\n关键词: ["Go"]\n---\nbody'
        entities = mgr.ingest_from_wiki(Path("dummy.md"), content=content)
        assert len(entities) == 1
        assert entities[0].name == "Go"


class TestStatusTransitions:
    @pytest.fixture
    def mgr(self, tmp_path, fake_config):
        fake_config.database_dir = tmp_path
        db_path = tmp_path / "test_entities.db"
        m = EntityManager(db_path=db_path, config=fake_config)
        yield m

    def test_raw_to_refined(self, mgr):
        mgr._upsert_entity("E")
        e = mgr.get_entity("e")
        e.status = "raw"
        e.source_count = 3
        e.confidence = 0.6
        mgr._save_entity(e)
        mgr.update_quality("e", 1.0, 0.9)
        updated = mgr.get_entity("e")
        assert updated.status == "refined"

    def test_refined_to_mature(self, mgr):
        e = Entity(uid="e2", name="E2", status="refined", source_count=5, confidence=0.8)
        mgr._save_entity(e)
        mgr.update_quality("e2", 1.0, 1.0)
        updated = mgr.get_entity("e2")
        assert updated.status == "mature"
