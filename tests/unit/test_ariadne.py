from datetime import datetime, timedelta


def test_effect_score_uses_ewma_and_counts(tmp_path):
    from core.kia.adaptive_config import AdaptiveConfig
    from core.kia.ariadne import KnowledgeTrail

    trail = KnowledgeTrail(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / "trail.db"),
        adaptive_config=AdaptiveConfig({"trail.effect_ewma_alpha": 0.5}),
    )

    assert trail.log_effect("page.md", solved=True) is True
    assert trail.log_effect("page.md", solved=False) is True

    page = trail.get_page_trail("page.md")

    assert page.first_accessed
    assert page.last_accessed
    assert page.effect_score == 0.5
    report = trail.get_effect_report(days=1)
    assert report["top_effective"][0]["effect_count"] == 2
    assert report["top_effective"][0]["solved_count"] == 1


def test_log_modification_records_event_and_page_stats(tmp_path):
    from core.kia.ariadne import KnowledgeTrail

    trail = KnowledgeTrail(wiki_base=str(tmp_path), db_path=str(tmp_path / "trail.db"))
    summary = "x" * 600

    assert trail.log_modification("page.md", change_summary=summary) is True

    page = trail.get_page_trail("page.md")
    assert page.total_modifications == 1
    assert page.events[0].event_type == "modify"
    assert page.events[0].context == "x" * 500


def test_weekly_report_includes_page_trail_activity_totals(tmp_path):
    from core.kia.ariadne import KnowledgeTrail

    trail = KnowledgeTrail(wiki_base=str(tmp_path), db_path=str(tmp_path / "trail.db"))

    trail.log_query("page.md", context="first")
    trail.log_reference("page.md", source="note.md")
    trail.log_modification("page.md", change_summary="frontmatter")

    report = trail.generate_weekly_report()

    assert "累计查询 1 / 引用 1 / 修改 1" in report


def test_page_trail_activity_totals_are_serialized_contract():
    """PageTrail 累计分项字段应稳定进入 dataclass 序列化契约。"""
    from dataclasses import asdict
    from core.kia.ariadne import PageTrail

    trail = PageTrail(
        page_path="page.md",
        total_queries=3,
        total_references=2,
        total_modifications=1,
    )

    serialized = asdict(trail)

    assert serialized["total_queries"] == 3
    assert serialized["total_references"] == 2
    assert serialized["total_modifications"] == 1


def test_log_knowledge_usage_dispatches_reference_and_modify(monkeypatch):
    from core.kia import ariadne

    calls = []

    class FakeTrail:
        def log_query(self, page_path, context=""):
            calls.append(("query", page_path, context))
            return True

        def log_reference(self, page_path, source="", quote="", session_id=""):
            calls.append(("reference", page_path, source, quote, session_id))
            return True

        def log_modification(self, page_path, change_summary=""):
            calls.append(("modify", page_path, change_summary))
            return True

        def log_effect(self, page_path, solved, context=""):
            calls.append(("effect", page_path, solved, context))
            return True

    monkeypatch.setattr(ariadne, "KnowledgeTrail", FakeTrail)

    assert ariadne.log_knowledge_usage("page.md", event_type="reference", context="source.md")
    assert ariadne.log_knowledge_usage("page.md", event_type="modify", context="frontmatter")

    assert calls == [
        ("reference", "page.md", "source.md", "", ""),
        ("modify", "page.md", "frontmatter"),
    ]


def test_effect_score_decays_toward_neutral(tmp_path):
    from core.kia.adaptive_config import AdaptiveConfig
    from core.kia.ariadne import KnowledgeTrail

    trail = KnowledgeTrail(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / "trail.db"),
        adaptive_config=AdaptiveConfig(
            {
                "trail.effect_ewma_alpha": 0.5,
                "trail.effect_half_life_days": 30,
            }
        ),
    )
    old = (datetime.now() - timedelta(days=30)).isoformat()[:19]
    with trail._conn() as conn:
        conn.execute(
            """INSERT INTO page_stats
               (
                   page_path, page_title, first_accessed, last_accessed,
                   effect_score, effect_count, effect_solved_count
               )
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("page.md", "page", old, old, 1.0, 1, 1),
        )

    trail.log_effect("page.md", solved=False)
    page = trail.get_page_trail("page.md")

    assert page.effect_score == 0.375


def test_forgotten_pages_sort_by_priority(tmp_path):
    from core.kia.ariadne import KnowledgeTrail

    trail = KnowledgeTrail(wiki_base=str(tmp_path), db_path=str(tmp_path / "trail.db"))
    old = (datetime.now() - timedelta(days=100)).isoformat()[:19]
    recent = (datetime.now() - timedelta(days=20)).isoformat()[:19]
    with trail._conn() as conn:
        conn.execute(
            """INSERT INTO page_stats
               (page_path, page_title, first_accessed, last_accessed, effect_score)
               VALUES (?, ?, ?, ?, ?)""",
            ("valuable.md", "valuable", old, old, 0.9),
        )
        conn.execute(
            """INSERT INTO page_stats
               (page_path, page_title, first_accessed, last_accessed, effect_score)
               VALUES (?, ?, ?, ?, ?)""",
            ("less.md", "less", recent, recent, 0.2),
        )

    forgotten = trail.get_forgotten_pages(days=7, min_age_days=7)

    assert forgotten[0]["page_path"] == "valuable.md"
    assert forgotten[0]["priority"] > forgotten[1]["priority"]


def test_user_journey_feeds_weekly_report(tmp_path):
    from core.kia.ariadne import KnowledgeTrail, TrailEvent

    trail = KnowledgeTrail(wiki_base=str(tmp_path), db_path=str(tmp_path / "trail.db"))
    first = (datetime.now() - timedelta(hours=2)).isoformat()[:19]
    second = (datetime.now() - timedelta(hours=1)).isoformat()[:19]

    trail.log_event(
        TrailEvent(
            event_type="query",
            page_path="alpha.md",
            timestamp=first,
            session_id="session-1",
            context="search alpha",
        )
    )
    trail.log_event(
        TrailEvent(
            event_type="reference",
            page_path="beta.md",
            timestamp=second,
            session_id="session-1",
            context="from alpha",
        )
    )
    trail.log_event(
        TrailEvent(
            event_type="query",
            page_path="other.md",
            timestamp=second,
            session_id="session-2",
            context="other session",
        )
    )

    journey = trail.get_user_journey(session_id="session-1", hours=24)
    report = trail.generate_weekly_report()

    assert [item["page_path"] for item in journey] == ["alpha.md", "beta.md"]
    assert "## 最近知识路径" in report
    assert "- query: alpha.md — search alpha" in report
    assert "- reference: beta.md — from alpha" in report


def test_existing_db_migrates_effect_columns(tmp_path):
    import sqlite3
    from core.kia.ariadne import KnowledgeTrail

    db_path = tmp_path / "trail.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""CREATE TABLE page_stats (
                page_path TEXT PRIMARY KEY,
                page_title TEXT,
                total_queries INTEGER DEFAULT 0,
                total_references INTEGER DEFAULT 0,
                total_modifications INTEGER DEFAULT 0,
                first_accessed TEXT,
                last_accessed TEXT,
                effect_score REAL DEFAULT 0.0
            )""")
        conn.execute("""CREATE TABLE trail_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                page_path TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                session_id TEXT,
                context TEXT,
                source TEXT,
                quote TEXT,
                success BOOLEAN,
                metadata TEXT
            )""")

    KnowledgeTrail(wiki_base=str(tmp_path), db_path=str(db_path))
    with sqlite3.connect(str(db_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(page_stats)")}

    assert "effect_count" in columns
    assert "effect_solved_count" in columns
