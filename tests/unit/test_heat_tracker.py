"""Characterization tests for scripts/heat_tracker.py output helpers."""

import sqlite3
from datetime import datetime, timedelta

from scripts import heat_tracker


class TestCountDomainPages:
    def test_counts_known_subdirs(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        (wiki_dir / "03-Tech").mkdir(parents=True)
        (wiki_dir / "04-Concepts").mkdir(parents=True)
        (wiki_dir / "03-Tech" / "a.md").write_text("a", encoding="utf-8")
        (wiki_dir / "03-Tech" / "b.md").write_text("b", encoding="utf-8")
        (wiki_dir / "04-Concepts" / "c.md").write_text("c", encoding="utf-8")

        total, distribution = heat_tracker._count_domain_pages(wiki_dir)
        assert total == 3
        assert distribution["03-Tech"] == 2
        assert distribution["04-Concepts"] == 1

    def test_missing_subdirs_ignored(self, tmp_path):
        total, distribution = heat_tracker._count_domain_pages(tmp_path / "empty")
        assert total == 0
        assert distribution == {}


class TestReadGraphCounts:
    def test_reads_entities_and_relations(self, tmp_path):
        graph_db = tmp_path / "graph.db"
        conn = sqlite3.connect(str(graph_db))
        conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE relations (id INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO entities (id) VALUES (?)", [(1,), (2,)])
        conn.executemany("INSERT INTO relations (id) VALUES (?)", [(1,), (2,), (3,)])
        conn.commit()
        conn.close()

        entities, relations = heat_tracker._read_graph_counts(graph_db)
        assert entities == 2
        assert relations == 3

    def test_returns_zero_for_missing_db(self, tmp_path):
        entities, relations = heat_tracker._read_graph_counts(tmp_path / "missing.db")
        assert entities == 0
        assert relations == 0


class TestScanPageFrontmatter:
    def test_extracts_type_heat_and_recent(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        md_file = wiki_dir / "note.md"
        today = datetime.now().strftime("%Y-%m-%d")
        md_file.write_text(
            f'---\ntype: concept\nheat: 0.9\ntitle: Note\nupdated: "{today}"\n---\nbody\n',
            encoding="utf-8",
        )

        info = heat_tracker._scan_page_frontmatter(md_file, wiki_dir)
        assert info["type"] == "concept"
        assert info["heat_entry"] == {"page": "note.md", "heat": 0.9, "title": "Note"}
        assert info["recent_entry"]["page"] == "note.md"
        assert info["recent_entry"]["days_ago"] == 0
        assert info["recent_entry"]["heat"] == 0.9

    def test_default_heat_for_recent(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        md_file = wiki_dir / "fresh.md"
        today = datetime.now().strftime("%Y-%m-%d")
        md_file.write_text(
            f'---\ntype: note\nupdated: "{today}"\n---\nbody\n',
            encoding="utf-8",
        )

        info = heat_tracker._scan_page_frontmatter(md_file, wiki_dir)
        assert info["heat_entry"] is None
        assert info["recent_entry"]["heat"] == 0.5

    def test_uses_freshness_score_when_heat_missing(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        md_file = wiki_dir / "note.md"
        md_file.write_text(
            "---\ntype: note\nfreshness_score: 0.7\ntitle: Fresh\n---\nbody\n",
            encoding="utf-8",
        )

        info = heat_tracker._scan_page_frontmatter(md_file, wiki_dir)
        assert info["heat_entry"]["heat"] == 0.7

    def test_skips_old_pages(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        md_file = wiki_dir / "old.md"
        old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
        md_file.write_text(
            f'---\ntype: note\nupdated: "{old}"\n---\nbody\n',
            encoding="utf-8",
        )

        info = heat_tracker._scan_page_frontmatter(md_file, wiki_dir)
        assert info["recent_entry"] is None

    def test_heat_defaults_to_zero_five_for_non_numeric(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        md_file = wiki_dir / "note.md"
        md_file.write_text(
            "---\ntype: note\nheat: hot\ntitle: Hot\n---\nbody\n",
            encoding="utf-8",
        )

        info = heat_tracker._scan_page_frontmatter(md_file, wiki_dir)
        assert info["heat_entry"]["heat"] == 0.5


class TestCollectHeatAndRecent:
    def test_filters_recent_to_thirty_days(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        today = datetime.now().strftime("%Y-%m-%d")
        old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
        (wiki_dir / "fresh.md").write_text(
            f'---\ntype: note\nupdated: "{today}"\n---\nbody\n', encoding="utf-8"
        )
        (wiki_dir / "stale.md").write_text(
            f'---\ntype: note\nupdated: "{old}"\n---\nbody\n', encoding="utf-8"
        )

        type_dist, heat_scores, recent_pages = heat_tracker._collect_heat_and_recent(
            wiki_dir
        )
        assert type_dist == {"note": 2}
        assert len(recent_pages) == 1
        assert recent_pages[0]["page"] == "fresh.md"

    def test_sorts_and_limits(self, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        for i in range(60):
            today = datetime.now().strftime("%Y-%m-%d")
            (wiki_dir / f"page{i}.md").write_text(
                f'---\ntype: note\nheat: {i / 100:.2f}\nupdated: "{today}"\n---\nbody\n',
                encoding="utf-8",
            )

        type_dist, heat_scores, recent_pages = heat_tracker._collect_heat_and_recent(
            wiki_dir
        )
        assert len(heat_scores) == 50
        assert heat_scores[0]["heat"] >= heat_scores[-1]["heat"]
        assert len(recent_pages) == 20


class TestReadTopEntities:
    def test_keyword_split_and_count(self, tmp_path):
        dna_db = tmp_path / "dna.db"
        conn = sqlite3.connect(str(dna_db))
        conn.execute(
            "CREATE TABLE knowledge_dna (page_path TEXT, keywords TEXT, created_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO knowledge_dna (page_path, keywords, created_at) VALUES (?, ?, ?)",
            [
                ("p1", "ai, ml, ai", "2026-01-01"),
                ("p2", "ml, data", "2026-01-02"),
            ],
        )
        conn.commit()
        conn.close()

        top = heat_tracker._read_top_entities(dna_db)
        assert top == [("ml", 2), ("ai", 2), ("data", 1)]

    def test_returns_empty_for_missing_db(self, tmp_path):
        assert heat_tracker._read_top_entities(tmp_path / "missing.db") == []


class TestCollectWikiStats:
    def test_returns_expected_keys(self, tmp_path, monkeypatch):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        monkeypatch.setattr(heat_tracker, "WIKI_DIR", wiki_dir)

        stats = heat_tracker.collect_wiki_stats()
        assert set(stats.keys()) == {
            "total_pages",
            "total_entities",
            "total_relations",
            "domain_distribution",
            "type_distribution",
            "heat_scores",
            "recent_pages",
            "top_entities",
        }

    def test_defaults_when_wiki_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(heat_tracker, "WIKI_DIR", tmp_path / "missing")

        stats = heat_tracker.collect_wiki_stats()
        assert stats["total_pages"] == 0
        assert stats["total_entities"] == 0
        assert stats["total_relations"] == 0
        assert stats["top_entities"] == []

    def test_integration(self, tmp_path, monkeypatch):
        wiki_dir = tmp_path / "wiki"
        (wiki_dir / "03-Tech").mkdir(parents=True)
        (wiki_dir / ".kg").mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (wiki_dir / "03-Tech" / "a.md").write_text(
            f'---\ntype: concept\nheat: 0.8\nupdated: "{today}"\n---\nbody\n',
            encoding="utf-8",
        )

        graph_db = wiki_dir / ".kg" / "graph.db"
        conn = sqlite3.connect(str(graph_db))
        conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE relations (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO entities (id) VALUES (1)")
        conn.execute("INSERT INTO relations (id) VALUES (1)")
        conn.commit()
        conn.close()

        monkeypatch.setattr(heat_tracker, "WIKI_DIR", wiki_dir)

        stats = heat_tracker.collect_wiki_stats()
        assert stats["total_pages"] == 1
        assert stats["total_entities"] == 1
        assert stats["total_relations"] == 1
        assert stats["type_distribution"] == {"concept": 1}
        assert stats["heat_scores"][0]["heat"] == 0.8
        assert len(stats["recent_pages"]) == 1
