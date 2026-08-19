"""
Tests for core.app.weekly_report

Covers: WeeklyReportGenerator init, generate_weekly_report format,
        all section methods (with mocked DBs).
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from core.app.weekly_report import WeeklyReportGenerator


def _prepare_report_sources(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    with sqlite3.connect(str(data_dir / "wiki_state.db")) as conn:
        conn.execute("CREATE TABLE wiki_pages (id INTEGER, created_at TEXT)")
        conn.execute("INSERT INTO wiki_pages VALUES (1, datetime('now'))")
    with sqlite3.connect(str(data_dir / "user_signals.db")) as conn:
        conn.execute("CREATE TABLE session_signals (id INTEGER, timestamp TEXT, task_type TEXT)")
    with sqlite3.connect(str(data_dir / "sync_log.db")) as conn:
        conn.execute(
            "CREATE TABLE sync_log (id INTEGER, synced_at TEXT, status TEXT, error TEXT, distill_error TEXT)"
        )
    return data_dir


class TestWeeklyReportInit:
    def test_init_with_default_wiki(self):
        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.wiki_dir = Path("/tmp/wiki")
            gen = WeeklyReportGenerator()
            assert gen.wiki_base == Path("/tmp/wiki")

    def test_init_with_explicit_wiki(self):
        gen = WeeklyReportGenerator(wiki_base="/custom/wiki")
        assert gen.wiki_base == Path("/custom/wiki").expanduser()


class TestGenerateWeeklyReport:
    @pytest.fixture
    def gen(self, tmp_path):
        return WeeklyReportGenerator(wiki_base=str(tmp_path))

    def test_report_format(self, gen, tmp_path):
        data_dir = _prepare_report_sources(tmp_path)
        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = data_dir
            mock_cfg.return_value.database_dir = data_dir
            content = gen.generate_weekly_report()
        assert "# 画像周报" in content
        assert "report_type: weekly_persona" in content
        assert "data_reliability: db_backed" in content
        assert "知识增长" in content
        assert "领域注意力变化" in content
        assert "盲点发现" in content
        assert "演化信号" in content
        assert "系统指标" in content
        assert "反复遇到的问题" in content
        assert "下周行动建议" in content

        # 文件应已写入
        report_files = list((tmp_path / "99-Reports").glob("*.md"))
        assert len(report_files) == 1

    def test_report_id_format(self, gen, tmp_path):
        data_dir = _prepare_report_sources(tmp_path)
        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = data_dir
            mock_cfg.return_value.database_dir = data_dir
            content = gen.generate_weekly_report()
        import re

        match = re.search(r"# 画像周报 (\d{4}-W\d{2})", content)
        assert match is not None

    def test_report_without_sources_does_not_write(self, gen, tmp_path):
        data_dir = tmp_path / "missing"
        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = data_dir
            mock_cfg.return_value.database_dir = data_dir
            content = gen.generate_weekly_report()

        assert "data_reliability: unavailable" in content
        assert not (tmp_path / "99-Reports").exists()


class TestSectionKnowledgeGrowth:
    @pytest.fixture
    def gen(self, tmp_path):
        return WeeklyReportGenerator(wiki_base=str(tmp_path))

    def test_with_wiki_state_db(self, gen, tmp_path):
        # 创建 wiki_state.db
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "wiki_state.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE wiki_pages (id INTEGER, created_at TEXT)")
            conn.execute("INSERT INTO wiki_pages VALUES (1, datetime('now'))")
            conn.execute("INSERT INTO wiki_pages VALUES (2, datetime('now', '-8 days'))")
            conn.commit()

        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = data_dir
            mock_cfg.return_value.database_dir = data_dir
            lines = gen._section_knowledge_growth()

        content = "\n".join(lines)
        assert "总 Wiki 页面：2" in content
        assert "本周新增：1" in content

    def test_without_db(self, gen):
        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = Path("/nonexistent")
            mock_cfg.return_value.database_dir = Path("/nonexistent")
            lines = gen._section_knowledge_growth()
        content = "\n".join(lines)
        assert "0" in content  # 默认无页面


class TestSectionDomainShifts:
    @pytest.fixture
    def gen(self, tmp_path):
        return WeeklyReportGenerator(wiki_base=str(tmp_path))

    def test_with_signals(self, gen, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "user_signals.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("""
                CREATE TABLE session_signals (
                    id INTEGER, timestamp TEXT, task_type TEXT
                )
            """)
            conn.execute("INSERT INTO session_signals VALUES (1, datetime('now'), 'coding')")
            conn.execute("INSERT INTO session_signals VALUES (2, datetime('now'), 'coding')")
            conn.execute("INSERT INTO session_signals VALUES (3, datetime('now'), 'review')")
            conn.commit()

        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = data_dir
            mock_cfg.return_value.database_dir = data_dir
            lines = gen._section_domain_shifts()

        content = "\n".join(lines)
        assert "coding" in content
        assert "review" in content
        assert "Session 数" in content

    def test_without_signals_db(self, gen):
        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = Path("/nonexistent")
            mock_cfg.return_value.database_dir = Path("/nonexistent")
            lines = gen._section_domain_shifts()
        assert "信号数据库未就绪" in "\n".join(lines)


class TestSectionSystemMetrics:
    @pytest.fixture
    def gen(self, tmp_path):
        return WeeklyReportGenerator(wiki_base=str(tmp_path))

    def test_with_sync_log(self, gen, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "sync_log.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("""
                CREATE TABLE sync_log (
                    id INTEGER, synced_at TEXT, status TEXT,
                    error TEXT, distill_error TEXT
                )
            """)
            conn.execute("INSERT INTO sync_log VALUES (1, datetime('now'), 'synced', NULL, NULL)")
            conn.execute(
                "INSERT INTO sync_log VALUES (2, datetime('now'), 'failed', 'error', NULL)"
            )
            conn.commit()

        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = data_dir
            mock_cfg.return_value.database_dir = data_dir
            lines = gen._section_system_metrics()

        content = "\n".join(lines)
        assert "本周同步" in content
        assert "synced" in content
        assert "失败/错误" in content


class TestSectionRepeatedIssues:
    @pytest.fixture
    def gen(self, tmp_path):
        return WeeklyReportGenerator(wiki_base=str(tmp_path))

    def test_with_errors(self, gen, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "sync_log.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("""
                CREATE TABLE sync_log (
                    id INTEGER, synced_at TEXT, error TEXT,
                    distill_error TEXT, status TEXT
                )
            """)
            conn.execute(
                "INSERT INTO sync_log VALUES (1, datetime('now'), 'connection timeout', NULL, 'failed')"  # noqa: E501
            )
            conn.execute(
                "INSERT INTO sync_log VALUES (2, datetime('now'), 'connection timeout', NULL, 'failed')"  # noqa: E501
            )
            conn.commit()

        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = data_dir
            mock_cfg.return_value.database_dir = data_dir
            lines = gen._section_repeated_issues()

        content = "\n".join(lines)
        assert "connection timeout" in content

    def test_no_errors(self, gen):
        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = Path("/nonexistent")
            mock_cfg.return_value.database_dir = Path("/nonexistent")
            lines = gen._section_repeated_issues()
        assert "未找到" in "\n".join(lines) or "未就绪" in "\n".join(lines)


class TestSectionActionItems:
    @pytest.fixture
    def gen(self, tmp_path):
        return WeeklyReportGenerator(wiki_base=str(tmp_path))

    def test_error_threshold_suggestion(self, gen, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "sync_log.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE sync_log (id INTEGER, synced_at TEXT, error TEXT)")
            for i in range(5):
                conn.execute("INSERT INTO sync_log VALUES (?, datetime('now'), 'err')", (i,))
            conn.commit()

        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = data_dir
            mock_cfg.return_value.database_dir = data_dir
            lines = gen._section_action_items()

        content = "\n".join(lines)
        assert "错误较多" in content or "API" in content

    def test_no_suggestions(self, gen):
        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = Path("/nonexistent")
            mock_cfg.return_value.database_dir = Path("/nonexistent")
            lines = gen._section_action_items()
        assert "平稳" in "\n".join(lines) or "暂无特别建议" in "\n".join(lines)


class TestSectionBlindspots:
    @pytest.fixture
    def gen(self, tmp_path):
        return WeeklyReportGenerator(wiki_base=str(tmp_path))

    def test_with_no_result_queries(self, gen, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "wiki_metrics.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("""
                CREATE TABLE query_log (
                    id INTEGER, query_text TEXT, matched_pages TEXT, created_at TEXT
                )
            """)
            conn.execute("INSERT INTO query_log VALUES (1, 'unknown topic', '[]', datetime('now'))")
            conn.commit()

        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = data_dir
            mock_cfg.return_value.database_dir = data_dir
            lines = gen._section_blindspots()

        content = "\n".join(lines)
        assert "unknown topic" in content

    def test_fallback_to_blindspot_discovery(self, gen):
        with patch("core.app.weekly_report.get_config") as mock_cfg:
            mock_cfg.return_value.data_dir = Path("/nonexistent")
            mock_cfg.return_value.database_dir = Path("/nonexistent")
            with patch("core.app.blindspot_discovery.BlindspotDiscovery") as mock_bd:
                mock_bd.return_value.get_weekly_summary.return_value = [
                    {"topic": "T1", "description": "D1"}
                ]
                lines = gen._section_blindspots()
        assert "T1" in "\n".join(lines) or "未发现" in "\n".join(lines)
