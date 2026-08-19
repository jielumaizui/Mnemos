"""
知识图谱节点写入集成测试

验证 Iter2 修复后：
- EntityManager.add_entity 公共方法可用
- KGEventHandler.on_distilled 消费 kg_input.entities
- Charon 把提取的实体同步写入 EntityManager
- _emit_knowledge_distilled 同步兜底触发 KG/CG 更新
- mnemos kg doctor / rebuild-entities CLI 可用
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class _FakeConfig:
    """为测试提供隔离配置。"""

    def __init__(self, tmpdir: Path, wiki_dir: Path):
        self.data_dir = tmpdir / "data"
        self.database_dir = self.data_dir
        self.mnemos_dir = tmpdir / ".mnemos"
        self._wiki_dir = wiki_dir
        self._cg_db = tmpdir / "cognitive_graph.db"

    @property
    def wiki_dir(self) -> Path:
        return self._wiki_dir

    @property
    def cognitive_graph_db_path(self) -> Path:
        return self._cg_db

    def get(self, key, default=None):
        return default


@pytest.fixture
def wiki_dir(tmp_path):
    d = tmp_path / "wiki"
    d.mkdir()
    return d


@pytest.fixture
def fake_config(tmp_path, wiki_dir):
    return _FakeConfig(tmp_path, wiki_dir)


class TestEntityManagerAddEntity:
    """EntityManager.add_entity 公共方法测试。"""

    def test_add_entity_creates_entity(self, tmp_path, fake_config):
        db_path = tmp_path / "kg.db"

        def _fake_get_db_path():
            return db_path

        with (
            patch("core.kia.entity_manager._get_db_path", _fake_get_db_path),
            patch("core.config.get_config", lambda: fake_config),
        ):
            from core.kia.entity_manager import EntityManager

            em = EntityManager()
            entity = em.add_entity(name="Python", entity_type="tech", wiki_page="/wiki/py.md")

        assert entity is not None
        assert entity.name == "Python"
        assert entity.uid == "python"

        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT name FROM entities WHERE uid = ?", ("python",)).fetchone()
            assert row[0] == "Python"

    def test_add_entity_filters_invalid_name(self, tmp_path, fake_config):
        db_path = tmp_path / "kg.db"

        def _fake_get_db_path():
            return db_path

        with (
            patch("core.kia.entity_manager._get_db_path", _fake_get_db_path),
            patch("core.config.get_config", lambda: fake_config),
        ):
            from core.kia.entity_manager import EntityManager

            em = EntityManager()
            assert em.add_entity(name="a") is None
            assert em.add_entity(name="123") is None


class TestKGEventHandlerEntities:
    """KGEventHandler 消费 kg_input.entities 创建实体。"""

    def test_on_distilled_creates_entities_from_kg_input(self, wiki_dir, fake_config, tmp_path):
        page = wiki_dir / "test.md"
        page.write_text("---\ntype: note\n---\n\n# Test\n", encoding="utf-8")

        db_path = tmp_path / "kg.db"

        def _fake_get_db_path():
            return db_path

        with (
            patch("core.kia.entity_manager._get_db_path", _fake_get_db_path),
            patch("core.config.get_config", lambda: fake_config),
            patch("core.kia.knowledge_graph.get_config", lambda: fake_config),
            patch("core.kia.kg_event_handler.get_config", lambda: fake_config),
        ):
            from core.kia.kg_event_handler import KGEventHandler

            handler = KGEventHandler()
            result = handler.on_distilled(
                {
                    "session_id": "s1",
                    "wiki_pages": [str(page)],
                    "kg_input": {
                        "entities": ["Python", "Machine Learning"],
                        "relations": [],
                    },
                }
            )

        assert result["entities_created"] >= 2


class TestCharonEntityPersistence:
    """Charon 把实体同步写入 EntityManager。"""

    def test_incremental_process_persists_entities(self, wiki_dir, fake_config, tmp_path):
        page = wiki_dir / "test.md"
        page.write_text("# Project Alpha\n\n使用 Python 和 Rust。\n", encoding="utf-8")

        db_path = tmp_path / "kg.db"

        def _fake_get_db_path():
            return db_path

        with (
            patch("core.kia.entity_manager._get_db_path", _fake_get_db_path),
            patch("core.config.get_config", lambda: fake_config),
            patch("core.kia.charon.WIKI_DIR", str(wiki_dir)),
            patch("core.kia.charon.INBOX_DIR", wiki_dir),
            patch("core.kia.charon._KG_SUBDIR", "."),
        ):
            from core.kia.charon import ConnectModule
            from core.kia.entity_manager import EntityManager

            module = ConnectModule(wiki_base=str(wiki_dir), db_path=str(db_path))
            result = module._incremental_process(page)

        assert result["status"] == "ok"
        assert len(result["added"]) > 0

        with (
            patch("core.kia.entity_manager._get_db_path", _fake_get_db_path),
            patch("core.config.get_config", lambda: fake_config),
        ):
            em = EntityManager()
            for name in result["added"]:
                assert em.get_entity(em._slugify(name)) is not None


class TestDistillationLegacyTopologyEvent:
    """Legacy topology events publish once and never write graphs directly."""

    def test_emit_does_not_trigger_direct_kg_cg_writes(self, wiki_dir, fake_config):
        from core.hephaestus.distillation_engine import (
            DistillationResult,
            KnowledgeFragment,
            _emit_knowledge_distilled,
        )

        frag = KnowledgeFragment(
            title="Test",
            form="note",
            frontmatter={},
            background="",
            core_content="Python and AI.",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
            keywords=["Python", "AI"],
        )
        result = DistillationResult(session_id="s1", fragments=[frag])
        page = wiki_dir / "test.md"
        page.write_text("# Test\n", encoding="utf-8")

        with (
            patch("core.mnemos_bus.publish_event") as mock_pub,
            patch("core.kia.kg_event_handler.KGEventHandler") as mock_kg,
            patch("core.cognitive_graph.updater.CognitiveGraphUpdater") as mock_cg,
            patch("core.config.get_config", lambda: fake_config),
        ):
            mock_kg_instance = MagicMock()
            mock_kg.return_value = mock_kg_instance
            mock_cg_instance = MagicMock()
            mock_cg.return_value = mock_cg_instance

            _emit_knowledge_distilled("s1", result, [str(page)])

            mock_pub.assert_called_once()
            mock_kg.assert_not_called()
            mock_cg.assert_not_called()
            mock_kg_instance.on_distilled.assert_not_called()
            mock_cg_instance.on_knowledge_distilled.assert_not_called()


class TestKGCLI:
    """mnemos kg doctor / rebuild-entities CLI 测试。"""

    def test_kg_doctor_reports_empty_entities(self, fake_config, tmp_path, monkeypatch):
        from core.cli.commands.kg import cmd_kg_doctor

        monkeypatch.setattr("core.cli.commands.kg.get_config", lambda: fake_config)

        class Args:
            pass

        assert cmd_kg_doctor(Args()) == 0

    def test_kg_doctor_warns_when_relation_projection_empty(
        self, fake_config, tmp_path, monkeypatch, capsys
    ):
        from core.cli.commands.kg import cmd_kg_doctor

        fake_config.database_dir.mkdir(parents=True, exist_ok=True)
        kg_db = fake_config.database_dir / "knowledge_graph.db"
        with sqlite3.connect(str(kg_db)) as conn:
            conn.execute("CREATE TABLE entities (uid TEXT, name TEXT)")
            conn.execute("CREATE TABLE relations (id TEXT)")
            conn.execute("INSERT INTO relations (id) VALUES ('rel-1')")

        monkeypatch.setattr("core.cli.commands.kg.get_config", lambda: fake_config)

        class Args:
            pass

        assert cmd_kg_doctor(Args()) == 0

        captured = capsys.readouterr()
        assert "Relations 投影为空" in captured.out

    def test_kg_rebuild_entities_scans_wiki(self, wiki_dir, fake_config, tmp_path, monkeypatch):
        from core.cli.commands.kg import cmd_kg_rebuild_entities

        page = wiki_dir / "note.md"
        page.write_text(
            "---\n关键词:\n  核心概念: [Python, Rust]\n---\n\n# Note\n",
            encoding="utf-8",
        )

        db_path = tmp_path / "data" / "knowledge_graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        def _fake_get_db_path():
            return db_path

        with (
            patch("core.kia.entity_manager._get_db_path", _fake_get_db_path),
            patch("core.cli.commands.kg.get_config", lambda: fake_config),
        ):

            class Args:
                pass

            assert cmd_kg_rebuild_entities(Args()) == 0

        assert db_path.exists()
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT name FROM entities").fetchall()
            names = {r[0] for r in rows}
            assert "Python" in names or "Rust" in names
