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

    assert page.effect_score == 0.5
    report = trail.get_effect_report(days=1)
    assert report["top_effective"][0]["effect_count"] == 2
    assert report["top_effective"][0]["solved_count"] == 1


def test_effect_score_decays_toward_neutral(tmp_path):
    from core.kia.adaptive_config import AdaptiveConfig
    from core.kia.ariadne import KnowledgeTrail

    trail = KnowledgeTrail(
        wiki_base=str(tmp_path),
        db_path=str(tmp_path / "trail.db"),
        adaptive_config=AdaptiveConfig({
            "trail.effect_ewma_alpha": 0.5,
            "trail.effect_half_life_days": 30,
        }),
    )
    old = (datetime.now() - timedelta(days=30)).isoformat()[:19]
    with trail._conn() as conn:
        conn.execute(
            """INSERT INTO page_stats
               (page_path, page_title, first_accessed, last_accessed, effect_score, effect_count, effect_solved_count)
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


def test_existing_db_migrates_effect_columns(tmp_path):
    import sqlite3
    from core.kia.ariadne import KnowledgeTrail

    db_path = tmp_path / "trail.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """CREATE TABLE page_stats (
                page_path TEXT PRIMARY KEY,
                page_title TEXT,
                total_queries INTEGER DEFAULT 0,
                total_references INTEGER DEFAULT 0,
                total_modifications INTEGER DEFAULT 0,
                first_accessed TEXT,
                last_accessed TEXT,
                effect_score REAL DEFAULT 0.0
            )"""
        )
        conn.execute(
            """CREATE TABLE trail_events (
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
            )"""
        )

    KnowledgeTrail(wiki_base=str(tmp_path), db_path=str(db_path))
    with sqlite3.connect(str(db_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(page_stats)")}

    assert "effect_count" in columns
    assert "effect_solved_count" in columns
