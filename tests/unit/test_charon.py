from datetime import datetime, timedelta
import sqlite3
from types import SimpleNamespace

import pytest


def _trusted_config(wiki, db, mode):
    return SimpleNamespace(
        wiki_dir=wiki,
        database_dir=db.parent,
        get=lambda key, default=None: {
            "trusted_push.mode": mode,
            "trusted_push.db_path": str(db),
        }.get(key, default),
    )


def test_safe_filename_adds_hash_suffix_for_long_names():
    from core.kia.charon import _safe_filename

    name = "a" * 90
    safe = _safe_filename(name)

    assert len(safe) == 67
    assert safe.startswith("a" * 60)
    assert safe[60] == "_"


def test_entity_extractor_bootstraps_existing_pages(tmp_path):
    from core.kia.charon import EntityExtractor

    # Wiki 目录统一为 03-Tech / 04-Concepts
    tech_dir = tmp_path / "03-Tech"
    concepts_dir = tmp_path / "04-Concepts"
    tech_dir.mkdir(parents=True)
    concepts_dir.mkdir(parents=True)
    tech_page = tech_dir / "CustomStack.md"
    concept_page = concepts_dir / "DecisionLoop.md"
    tech_page.write_text("# CustomStack\n", encoding="utf-8")
    concept_page.write_text("# DecisionLoop\n", encoding="utf-8")

    entities = EntityExtractor(wiki_base=tmp_path).extract("CustomStack uses DecisionLoop")

    assert "customstack" in entities["tech"]
    assert "decisionloop" in entities["concepts"]


def test_entity_extractor_detects_chinese_names_and_projects(tmp_path):
    from core.kia.charon import EntityExtractor

    text = "张伟说项目：蓝鲸，需要用 FastAPI。李雷认为平台「星河」接入 Redis。"
    entities = EntityExtractor(wiki_base=tmp_path, bootstrap_from_existing=False).extract(text)

    assert {"张伟", "李雷"} <= entities["people"]
    assert {"蓝鲸", "星河"} <= entities["projects"]
    assert {"fastapi", "redis"} <= entities["tech"]


def test_entity_extractor_filters_fragment_and_attachment_entities(tmp_path):
    from core.kia.charon import EntityExtractor

    text = (
        "通过系统性的修复后，系统图谱仍有 endpoint gap。"
        "---\nActive\n  Policy\n"
        "附件包含 a.png，但 CI/CD 与 FastAPI 仍是有效概念。"
    )

    entities = EntityExtractor(wiki_base=tmp_path, bootstrap_from_existing=False).extract(text)
    flat = set().union(*entities.values())

    assert "通过系统性的" not in flat
    assert "系统图谱仍有" not in flat
    assert "---" not in flat
    assert "Active\n  Policy" not in flat
    assert "a.png" not in flat
    assert "ci/cd" in entities["concepts"]
    assert "fastapi" in entities["tech"]


def test_entity_extractor_uses_project_indicator_matrix(tmp_path):
    from core.kia.charon import EntityExtractor

    text = "组件：灯塔 需要对接 Kafka。service: orbit 用于同步。"
    entities = EntityExtractor(wiki_base=tmp_path, bootstrap_from_existing=False).extract(text)

    assert {"灯塔", "orbit"} <= entities["projects"]


def test_relation_engine_uses_time_decay_and_persists(tmp_path):
    from core.kia.charon import RelationEngine

    db_path = tmp_path / "kg.db"
    engine = RelationEngine(half_life_days=30, db_path=db_path)
    engine.analyze_session("new", {"tech": {"react", "redis"}}, timestamp=datetime.now())
    engine.analyze_session(
        "old", {"tech": {"react", "docker"}}, timestamp=datetime.now() - timedelta(days=30)
    )

    relations = dict(engine.get_relations("react", min_count=0.1))

    assert relations["redis"] > relations["docker"]
    assert 0.45 <= relations["docker"] <= 0.55

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT weight, session_count FROM co_occurrence_relations WHERE entity_a=? AND entity_b=?",  # noqa: E501
            ("react", "redis"),
        ).fetchone()
    assert row[0] > 0.9
    assert row[1] == 1


def test_relation_engine_decrement_clamps_and_persists(tmp_path):
    from core.kia.charon import RelationEngine

    db_path = tmp_path / "kg.db"
    engine = RelationEngine(half_life_days=30, db_path=db_path)
    engine.analyze_session("doc", {"tech": {"react", "redis"}}, timestamp=datetime.now())

    engine.decrement("redis", "react")
    engine.decrement("redis", "react")

    assert "redis" not in dict(engine.get_relations("react", min_count=0.001))
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT weight, co_occurrence_count FROM co_occurrence_relations WHERE entity_a=? AND entity_b=?",  # noqa: E501
            ("react", "redis"),
        ).fetchone()
    assert row == (0.0, 0)

    with pytest.raises(ValueError, match="amount must be non-negative"):
        engine.decrement("react", "redis", amount=-0.1)


def test_relation_engine_does_not_expose_retired_introspection_helpers():
    """退休共现主链路不再暴露无调用方的内存态查询 helper。"""
    from core.kia.charon import RelationEngine

    for helper_name in ("get_related_sessions", "get_weight", "get_total_mentions"):
        assert not hasattr(RelationEngine, helper_name)


def test_connect_module_incremental_process_tracks_added_and_removed(tmp_path):
    from core.kia.charon import ConnectModule

    page = tmp_path / "00-Inbox" / "session.md"
    page.parent.mkdir(parents=True)
    page.write_text("React 和 Docker 用在项目：蓝鲸。", encoding="utf-8")

    module = ConnectModule(wiki_base=tmp_path, db_path=tmp_path / "kg.db")
    first = module._incremental_process(page)
    assert {"react", "docker", "蓝鲸"} <= set(first["added"])

    page.write_text("React 和 Redis 用在项目：蓝鲸。", encoding="utf-8")
    second = module._incremental_process(page)

    assert "redis" in second["added"]
    assert "docker" in second["removed"]


def test_connect_module_does_not_boot_retired_relation_engine(tmp_path):
    from core.kia.charon import ConnectModule

    db_path = tmp_path / "kg.db"
    module = ConnectModule(wiki_base=tmp_path, db_path=db_path)

    assert not hasattr(module, "relation_engine")
    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "co_occurrence_relations" not in table_names


def test_process_inbox_file_dry_run_does_not_write_kg_relations(tmp_path, monkeypatch):
    from core.kia import charon

    page = tmp_path / "session.md"
    page.write_text("React 和 Redis", encoding="utf-8")

    class Extractor:
        def extract(self, text, cwd=""):
            return {
                "people": set(),
                "projects": set(),
                "tech": {"react", "redis"},
                "concepts": set(),
            }

    monkeypatch.setattr(
        charon,
        "_write_cooccurrence_relations",
        lambda *args, **kwargs: pytest.fail("dry-run must not write KG relations"),
    )
    all_entities, file_entities, project_tech = charon._init_connect_state()

    charon._process_inbox_file(
        page,
        Extractor(),
        all_entities,
        file_entities,
        project_tech,
        dry_run=True,
    )

    assert file_entities[page]["tech"] == {"react", "redis"}
    assert all_entities["tech"]["react"] == {"session"}


def test_process_inbox_file_can_skip_relation_writes_for_daemon_route(
    tmp_path, monkeypatch
):
    from core.kia import charon

    page = tmp_path / "session.md"
    page.write_text("React 和 Redis", encoding="utf-8")

    class Extractor:
        def extract(self, text, cwd=""):
            return {
                "people": set(),
                "projects": set(),
                "tech": {"react", "redis"},
                "concepts": set(),
            }

    monkeypatch.setattr(
        charon,
        "_write_cooccurrence_relations",
        lambda *args, **kwargs: pytest.fail("daemon route must not write KG relations"),
    )
    all_entities, file_entities, project_tech = charon._init_connect_state()

    charon._process_inbox_file(
        page,
        Extractor(),
        all_entities,
        file_entities,
        project_tech,
        dry_run=False,
        write_relations=False,
    )

    assert file_entities[page]["tech"] == {"react", "redis"}
    assert all_entities["tech"]["redis"] == {"session"}


def test_resolve_page_folder_tech_subfolder_by_title():
    from core.kia.charon import resolve_page_folder

    page = __import__("pathlib").Path("/tmp/test.md")
    fm = {"类型": "technology", "名称": "百度千帆 coding plan 的 base_url"}
    folder = resolve_page_folder(page, fm)
    assert "03-Tech" in folder.parts
    assert "百度千帆" in folder.parts


def test_resolve_page_folder_retrospective_default():
    from core.kia.charon import resolve_page_folder

    page = __import__("pathlib").Path("/tmp/retro_20260617.md")
    fm = {"类型": "retrospective", "名称": "知识蒸馏复盘"}
    folder = resolve_page_folder(page, fm)
    assert folder.parts[-2:] == ("06-Retrospectives", "复盘")


def test_resolve_page_folder_flywheel_by_title():
    from core.kia.charon import resolve_page_folder

    page = __import__("pathlib").Path("/tmp/flywheel_report.md")
    fm = {"名称": "flywheel_report_2026-06-24"}
    folder = resolve_page_folder(page, fm)
    assert folder.parts[-2:] == ("06-Retrospectives", "flywheel")


def test_resolve_page_folder_concept_subfolder():
    from core.kia.charon import resolve_page_folder

    page = __import__("pathlib").Path("/tmp/test.md")
    fm = {"type": "concept", "name": "数据口径"}
    folder = resolve_page_folder(page, fm)
    assert folder.parts[-2:] == ("04-Concepts", "数据")


def test_resolve_page_folder_project_subfolder(tmp_path):
    from core.kia.charon import resolve_page_folder, EntityExtractor

    page = tmp_path / "test.md"
    entities = EntityExtractor(wiki_base=tmp_path, bootstrap_from_existing=False).extract(
        "项目：蓝鲸，需要用 FastAPI。"
    )
    folder = resolve_page_folder(page, {}, entities)
    assert "02-Projects" in folder.parts
    assert "蓝鲸" in folder.parts


def test_resolve_page_folder_fallback_entity_count(tmp_path):
    from core.kia.charon import resolve_page_folder, EntityExtractor

    page = tmp_path / "test.md"
    entities = EntityExtractor(wiki_base=tmp_path, bootstrap_from_existing=False).extract(
        "React 和 Docker 配置 CI/CD。"
    )
    folder = resolve_page_folder(page, {}, entities)
    assert "03-Tech" in folder.parts


def test_move_page_to_category_creates_subfolder(
    tmp_path,
    monkeypatch,
):
    import core.kia.charon as charon

    monkeypatch.setattr(charon, "WIKI_DIR", tmp_path)

    page = tmp_path / "test.md"
    page.write_text("# Test", encoding="utf-8")
    target_dir = tmp_path / "03-Tech" / "codex"

    result = charon._move_page_to_category(page, target_dir)

    assert result["status"] == "moved"
    assert (target_dir / "test.md").exists()
    assert "auto_classified" in (target_dir / "test.md").read_text(encoding="utf-8")


def test_move_page_to_category_already_there(
    tmp_path,
    monkeypatch,
):
    import core.kia.charon as charon

    monkeypatch.setattr(charon, "WIKI_DIR", tmp_path)

    target_dir = tmp_path / "04-Concepts" / "数据"
    target_dir.mkdir(parents=True)
    page = target_dir / "test.md"
    page.write_text("# Test", encoding="utf-8")

    result = charon._move_page_to_category(page, target_dir)

    assert result["status"] == "already_there"


def test_move_page_to_category_enforce_proposes_without_mutating(
    monkeypatch,
    tmp_path,
):
    import core.kia.charon as charon
    from core.trust.proposal_queue import ProposalQueue

    db = tmp_path / "trusted.db"
    monkeypatch.setattr(charon, "WIKI_DIR", tmp_path)
    monkeypatch.setattr(
        "core.trust.config.get_config",
        lambda: _trusted_config(tmp_path, db, "enforce"),
    )
    page = tmp_path / "00-Inbox" / "test.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Test\n", encoding="utf-8")
    target_dir = tmp_path / "03-Tech" / "codex"

    result = charon._move_page_to_category(page, target_dir)

    assert result["status"] == "proposed"
    assert page.read_text(encoding="utf-8") == "# Test\n"
    assert not (target_dir / "test.md").exists()
    proposals = ProposalQueue(db, wiki_base=tmp_path).list()
    assert len(proposals) == 1
    assert proposals[0].candidate.target_path == str(target_dir / "test.md")
