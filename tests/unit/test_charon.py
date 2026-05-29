from datetime import datetime, timedelta
import sqlite3


def test_safe_filename_adds_hash_suffix_for_long_names():
    from core.kia.charon import _safe_filename

    name = "a" * 90
    safe = _safe_filename(name)

    assert len(safe) == 67
    assert safe.startswith("a" * 60)
    assert safe[60] == "_"


def test_entity_extractor_bootstraps_existing_pages(tmp_path):
    from core.kia.charon import EntityExtractor

    tech_page = tmp_path / "03-Tech" / "CustomStack.md"
    concept_page = tmp_path / "04-Concepts" / "DecisionLoop.md"
    tech_page.parent.mkdir(parents=True)
    concept_page.parent.mkdir(parents=True)
    tech_page.write_text("# CustomStack\n", encoding="utf-8")
    concept_page.write_text("# DecisionLoop\n", encoding="utf-8")

    entities = EntityExtractor(wiki_base=tmp_path).extract("CustomStack uses DecisionLoop")

    assert "customstack" in entities["tech"]
    assert "decisionloop" in entities["concepts"]


def test_entity_extractor_detects_chinese_names_and_projects(tmp_path):
    from core.kia.charon import EntityExtractor

    text = "张伟说项目：蓝鲸，需要用 FastAPI。李雷建议平台「星河」接入 Redis。"
    entities = EntityExtractor(wiki_base=tmp_path, bootstrap_from_existing=False).extract(text)

    assert {"张伟", "李雷"} <= entities["people"]
    assert {"蓝鲸", "星河"} <= entities["projects"]
    assert {"fastapi", "redis"} <= entities["tech"]


def test_relation_engine_uses_time_decay_and_persists(tmp_path):
    from core.kia.charon import RelationEngine

    db_path = tmp_path / "kg.db"
    engine = RelationEngine(half_life_days=30, db_path=db_path)
    engine.analyze_session("new", {"tech": {"react", "redis"}}, timestamp=datetime.now())
    engine.analyze_session("old", {"tech": {"react", "docker"}}, timestamp=datetime.now() - timedelta(days=30))

    relations = dict(engine.get_relations("react", min_count=0.1))

    assert relations["redis"] > relations["docker"]
    assert 0.45 <= relations["docker"] <= 0.55

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT weight, session_count FROM co_occurrence_relations WHERE entity_a=? AND entity_b=?",
            ("react", "redis"),
        ).fetchone()
    assert row[0] > 0.9
    assert row[1] == 1


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
