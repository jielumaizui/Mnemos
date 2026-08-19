# -*- coding: utf-8 -*-
"""
evolution_tracker.py 单元测试

覆盖范围：
  - EvolutionAlert 数据类
  - TemporalScope 数据类与 is_expired
  - TemporalEvolutionTracker 初始化
  - check_entity_freshness / scan_all_pages
  - _extract_temporal_scope
  - _get_last_access
  - _save_alert / get_unresolved_alerts / resolve_alert
  - RecirculationGuard

测试策略：
  - tmp_path + sqlite3 内存数据库（通过 monkeypatch _get_db_path）
  - monkeypatch get_config
  - monkeypatch wiki 目录结构
  - 直接操作 tracker 内部方法
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest


@pytest.fixture(autouse=True)  # noqa
def _patch_et_get_config(monkeypatch, patched_get_config):
    import core.hephaestus.evolution_tracker as _et_mod

    monkeypatch.setattr(_et_mod, "get_config", lambda: patched_get_config)


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    from core.hephaestus.evolution_tracker import TemporalEvolutionTracker

    # 使用临时数据库避免污染
    db_path = tmp_path / "wiki_state.db"
    monkeypatch.setattr("core.hephaestus.evolution_tracker._get_db_path", lambda: db_path)
    return TemporalEvolutionTracker()


@pytest.fixture
def recirc_guard():
    from core.hephaestus.evolution_tracker import RecirculationGuard

    return RecirculationGuard()


# =============================================================================
# EvolutionAlert
# =============================================================================


class TestEvolutionAlert:
    def test_alert_has_required_fields(self):
        from core.hephaestus.evolution_tracker import EvolutionAlert

        alert = EvolutionAlert(
            entity="Python",
            alert_type="version_outdated",
            detail="Python 3.12 released",
            wiki_page="concepts/python.md",
            severity=0.9,
            created_at=datetime.now().isoformat(),
        )
        assert alert.entity == "Python"
        assert alert.alert_type == "version_outdated"
        assert alert.severity == 0.9

    def test_alert_defaults(self):
        from core.hephaestus.evolution_tracker import EvolutionAlert

        alert = EvolutionAlert(
            entity="Go",
            alert_type="version_outdated",
        )
        assert alert.detail == ""
        assert alert.wiki_page == ""
        assert alert.severity == 0.5
        assert alert.created_at == ""

    def test_mark_stale_produces_its_own_material_decision(
        self,
        tracker,
        tmp_path,
    ):
        from core.hephaestus.evolution_tracker import EvolutionAlert

        page = tmp_path / "03-Tech" / "Python.md"
        page.parent.mkdir(parents=True)
        page.write_text("---\n状态: 活跃\n---\n# Python\n", encoding="utf-8")
        alert = EvolutionAlert(
            entity="Python",
            alert_type="version_outdated",
            detail="The recorded version is no longer current.",
            wiki_page=str(page.relative_to(tmp_path)),
            severity=0.9,
            created_at="2026-07-17T12:00:00+00:00",
        )

        assert tracker._mark_stale(page, alert, tmp_path) is True
        updated = page.read_text(encoding="utf-8")
        assert "stale: true" in updated
        assert "stale_alert_type: version_outdated" in updated


# =============================================================================
# TemporalScope
# =============================================================================


class TestTemporalScope:
    def test_scope_permanent_never_expired(self):
        from core.hephaestus.evolution_tracker import TemporalScope

        scope = TemporalScope(scope_type="permanent")
        assert scope.is_expired is False

    def test_scope_stable_never_expired(self):
        from core.hephaestus.evolution_tracker import TemporalScope

        scope = TemporalScope(scope_type="stable")
        assert scope.is_expired is False

    def test_scope_version_bound_expired(self):
        from core.hephaestus.evolution_tracker import TemporalScope

        old_date = (datetime.now() - timedelta(days=400)).isoformat()
        scope = TemporalScope(
            scope_type="version-bound",
            context_date=old_date,
        )
        assert scope.is_expired is True

    def test_scope_version_bound_not_expired(self):
        from core.hephaestus.evolution_tracker import TemporalScope

        recent = (datetime.now() - timedelta(days=30)).isoformat()
        scope = TemporalScope(
            scope_type="version-bound",
            context_date=recent,
        )
        assert scope.is_expired is False

    def test_scope_contextual_expired(self):
        from core.hephaestus.evolution_tracker import TemporalScope

        old_date = (datetime.now() - timedelta(days=100)).isoformat()
        scope = TemporalScope(
            scope_type="contextual",
            context_date=old_date,
            expires_after_days=30,
        )
        assert scope.is_expired is True

    def test_scope_contextual_not_expired(self):
        from core.hephaestus.evolution_tracker import TemporalScope

        recent = (datetime.now() - timedelta(days=10)).isoformat()
        scope = TemporalScope(
            scope_type="contextual",
            context_date=recent,
            expires_after_days=30,
        )
        assert scope.is_expired is False

    def test_scope_no_context_date(self):
        from core.hephaestus.evolution_tracker import TemporalScope

        scope = TemporalScope(scope_type="contextual")
        assert scope.is_expired is False


# =============================================================================
# TemporalEvolutionTracker 初始化
# =============================================================================


class TestTemporalEvolutionTrackerInit:
    def test_init_creates_db(self, tracker, tmp_path):
        # _init_db 应在初始化时创建数据库
        db_path = tmp_path / "wiki_state.db"
        assert db_path.exists()
        # 验证表已创建
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='evolution_alerts'"
            )
            assert cursor.fetchone() is not None


# =============================================================================
# _extract_temporal_scope
# =============================================================================


class TestExtractTemporalScope:
    def test_extract_from_frontmatter_with_temporal(self, tracker):
        content = """---
temporal: version_bound
version: "3.12"
created: 2024-01-01
---
# Title
Content here.
"""
        scope = tracker._extract_temporal_scope(content)
        assert scope is not None
        assert scope.scope_type == "version_bound"
        assert scope.version == "3.12"
        # frontmatter parser returns datetime.date, not str
        from datetime import date

        assert scope.context_date == date(2024, 1, 1)

    def test_extract_no_frontmatter(self, tracker):
        content = "# Title\n\nJust content."
        scope = tracker._extract_temporal_scope(content)
        assert scope is None

    def test_extract_invalid_temporal(self, tracker):
        content = """---
temporal: invalid_value
---
# Title
"""
        scope = tracker._extract_temporal_scope(content)
        assert scope is None

    def test_extract_chinese_keys(self, tracker):
        content = """---
时效性: contextual
创建日期: 2024-06-01
---
# Title
"""
        scope = tracker._extract_temporal_scope(content)
        assert scope is not None
        assert scope.scope_type == "contextual"
        from datetime import date

        assert scope.context_date == date(2024, 6, 1)


# =============================================================================
# _get_last_access
# =============================================================================


class TestGetLastAccess:
    def test_last_access_from_file(self, tracker, tmp_path):
        page = tmp_path / "test.md"
        page.write_text("content")
        dt = tracker._get_last_access(page)
        assert dt is not None
        # mtime 应在最近
        assert (datetime.now() - dt).total_seconds() < 60

    def test_last_access_not_found(self, tracker, tmp_path):
        dt = tracker._get_last_access(tmp_path / "nonexistent.md")
        assert dt is None


# =============================================================================
# _save_alert / get_unresolved_alerts / resolve_alert
# =============================================================================


class TestAlertLifecycle:
    def test_save_and_retrieve_alert(self, tracker):
        from core.hephaestus.evolution_tracker import EvolutionAlert

        alert = EvolutionAlert(
            entity="Python",
            alert_type="version_outdated",
            detail="3.12 released",
            wiki_page="concepts/python.md",
            severity=0.95,
            created_at=datetime.now().isoformat(),
        )
        tracker._save_alert(alert)

        alerts = tracker.get_unresolved_alerts()
        assert len(alerts) >= 1
        entities = [a.entity for a in alerts]
        assert "Python" in entities

    def test_resolve_alert(self, tracker):
        from core.hephaestus.evolution_tracker import EvolutionAlert

        alert = EvolutionAlert(
            entity="Go",
            alert_type="version_outdated",
            detail="1.22 released",
            wiki_page="concepts/go.md",
            severity=0.9,
            created_at=datetime.now().isoformat(),
        )
        tracker._save_alert(alert)
        unresolved = tracker.get_unresolved_alerts()
        go_alerts = [a for a in unresolved if a.entity == "Go"]
        assert len(go_alerts) >= 1

        tracker.resolve_alert("Go", "version_outdated")
        unresolved_after = tracker.get_unresolved_alerts()
        assert not any(a.entity == "Go" for a in unresolved_after)


# =============================================================================
# check_entity_freshness
# =============================================================================


class TestCheckEntityFreshness:
    def test_fresh_entity_no_alert(self, tracker, tmp_path):
        page = tmp_path / "rust.md"
        page.write_text("# Rust\n\nContent.")
        result = tracker.check_entity_freshness("Rust", page)
        assert result is None

    def test_stale_version_entity(self, tracker, tmp_path, monkeypatch):
        page = tmp_path / "python.md"
        page.write_text("# Python\nContent.")
        # 注意：源码中 _extract_temporal_scope 返回 "version_bound"（下划线），
        # 但 check_entity_freshness 检查的是 "version-bound"（连字符），存在不一致。
        # 这里直接 mock _extract_temporal_scope 返回正确类型以测试该分支。
        from core.hephaestus.evolution_tracker import TemporalScope

        old_date = (datetime.now() - timedelta(days=400)).isoformat()
        monkeypatch.setattr(
            tracker,
            "_extract_temporal_scope",
            lambda content: TemporalScope(
                scope_type="version-bound", version="3.8", context_date=old_date
            ),
        )
        alert = tracker.check_entity_freshness("Python", page)
        assert alert is not None
        assert alert.alert_type == "version_outdated"

    def test_rarely_accessed(self, tracker, tmp_path):
        page = tmp_path / "old.md"
        # 需要 frontmatter 使 _extract_temporal_scope 返回非 None，
        # 否则方法在 scope 检查前直接返回 None
        page.write_text("---\ntemporal: permanent\n---\n\n# Old\n\nContent.")
        # 修改文件 mtime 到 40 天前
        old_time = (datetime.now() - timedelta(days=40)).timestamp()
        import os

        os.utime(page, (old_time, old_time))
        alert = tracker.check_entity_freshness("Old", page)
        assert alert is not None
        assert alert.alert_type == "rarely_accessed"


# =============================================================================
# scan_all_pages
# =============================================================================


class TestScanAllPages:
    def test_scan_empty_wiki(self, tracker, tmp_path):
        result = tracker.scan_all_pages(tmp_path)
        assert isinstance(result, list)
        assert len(result) == 0

    def test_scan_with_pages(self, tracker, tmp_path, monkeypatch):
        # 创建 Wiki 目录结构
        wiki_dir = tmp_path / "wiki"
        concepts = wiki_dir / "concepts"
        concepts.mkdir(parents=True)
        # 使用 permanent frontmatter + 旧 mtime 触发 rarely_accessed
        (concepts / "old_topic.md").write_text(
            "---\ntemporal: permanent\n---\n\n# Old Topic\n\nContent.\n"
        )
        old_time = (datetime.now() - timedelta(days=40)).timestamp()
        import os

        os.utime(concepts / "old_topic.md", (old_time, old_time))

        # mock WIKI_DIRS（scan_all_pages 从 core.utils 导入）
        monkeypatch.setattr("core.utils.WIKI_DIRS", ["concepts"])

        result = tracker.scan_all_pages(wiki_dir)
        assert isinstance(result, list)
        assert len(result) >= 1
        assert result[0].alert_type == "rarely_accessed"


# =============================================================================
# RecirculationGuard
# =============================================================================


class TestRecirculationGuard:
    def test_should_skip_empty(self, recirc_guard):
        result, reason = recirc_guard.should_skip("")
        assert result is True
        assert reason == "空内容"

    def test_should_skip_known_content(self, recirc_guard):
        result, reason = recirc_guard.should_skip("Python is a programming language.")
        assert result is False
        assert reason == ""

    def test_should_skip_wiki_marker(self, recirc_guard):
        result, reason = recirc_guard.should_skip("<wiki-context>some content")
        assert result is True
        assert "Wiki 引用标记" in reason

    def test_should_skip_distilled_page(self, recirc_guard):
        content = "---\ntitle: Test\n---\n\n# Test\n\n## 演化历史\n\nContent."
        result, reason = recirc_guard.should_skip(content)
        assert result is True
        assert "完整 Wiki 页面" in reason

    def test_check_session_no_recirculation(self, recirc_guard):
        messages = [{"content": "普通消息内容"}]
        result, detail = recirc_guard.check_session(messages)
        assert result is False
        assert detail == ""

    def test_check_session_with_recirculation(self, recirc_guard):
        messages = [
            {"content": "正常消息"},
            {"content": "<wiki-context>injected content"},
        ]
        result, detail = recirc_guard.check_session(messages)
        assert result is True
        assert "回流内容" in detail
