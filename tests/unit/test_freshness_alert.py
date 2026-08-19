# -*- coding: utf-8 -*-
"""
P1-1 单元测试 — Freshness Check 不得假绿

验证：
- 不存在实体返回 not_found，不能返回 fresh
- last_updated 超过 90 天返回 stale
- 正常实体返回 fresh
- Entity 没有 meta 字段时不报错
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)  # noqa
def patch_config_for_freshness(tmp_path, monkeypatch):
    """隔离 FreshnessAlertChecker 使用的数据库目录，避免跨测试污染 cooldown DB。"""
    import core.config as _config_mod

    fake_cfg = MagicMock()
    fake_cfg.wiki_dir = tmp_path / "wiki"
    fake_cfg.wiki_dir.mkdir(parents=True, exist_ok=True)
    fake_cfg.data_dir = tmp_path / "data"
    fake_cfg.database_dir = tmp_path / "data"
    fake_cfg.database_dir.mkdir(parents=True, exist_ok=True)
    fake_cfg.get = lambda key, default=None: default
    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_cfg)
    # 这些模块在导入时缓存了 get_config 引用，需要一并补丁
    monkeypatch.setattr("core.app.freshness_alert.get_config", lambda: fake_cfg)
    monkeypatch.setattr("core.kia.reminder_engine.get_config", lambda: fake_cfg)


class FakeEntity:
    """模拟 Entity，无 meta 字段（与真实 Entity 一致）"""

    def __init__(self, name, entity_type="concept", last_updated="", version_info=None):
        self.name = name
        self.entity_type = entity_type
        self.last_updated = last_updated
        self.version_info = version_info


def _checker():
    from core.app.freshness_alert import FreshnessAlertChecker

    return FreshnessAlertChecker()


def test_not_found_entity_returns_not_found():
    """不存在实体必须返回 not_found，不能 fresh"""
    checker = _checker()
    with patch("core.kia.entity_manager.EntityManager.get_entity", return_value=None):
        result = checker.check_knowledge_freshness("不存在实体XYZ")

    assert result is not None
    assert result.status == "not_found"
    assert "未找到" in result.message


def test_context_expired_returns_stale():
    """last_updated 超过 90 天应返回 stale"""
    old_date = (datetime.now() - timedelta(days=100)).isoformat()
    entity = FakeEntity(name="过期知识", entity_type="concept", last_updated=old_date)
    checker = _checker()

    with patch("core.kia.entity_manager.EntityManager.get_entity", return_value=entity):
        result = checker.check_knowledge_freshness("过期知识")

    assert result is not None
    assert result.status == "stale"
    assert result.alert_type == "context_expired"
    assert "100" in result.message or "过时" in result.message


def test_fresh_entity_returns_fresh():
    """正常实体返回 fresh"""
    recent = (datetime.now() - timedelta(days=10)).isoformat()
    entity = FakeEntity(name="新鲜知识", entity_type="concept", last_updated=recent)
    checker = _checker()

    with patch("core.kia.entity_manager.EntityManager.get_entity", return_value=entity):
        result = checker.check_knowledge_freshness("新鲜知识")

    assert result is not None
    assert result.status == "fresh"
    assert "新鲜" in result.message


def test_entity_without_page_or_metadata_returns_not_found():
    """实体存在但无 source_page、无文件、无元数据时应返回 not_found"""
    entity = FakeEntity(name="孤儿实体", entity_type="concept")
    checker = _checker()

    with patch("core.kia.entity_manager.EntityManager.get_entity", return_value=entity):
        result = checker.check_knowledge_freshness("孤儿实体")

    assert result is not None
    assert result.status == "not_found"
    assert "缺少可检查页面" in result.message


def test_entity_resolved_by_name():
    """uid 未命中时应回退到 get_entity_by_name"""
    entity = FakeEntity(name="Redis", entity_type="concept")
    checker = _checker()

    with patch("core.kia.entity_manager.EntityManager.get_entity", return_value=None):
        with patch("core.kia.entity_manager.EntityManager.get_entity_by_name", return_value=entity):
            result = checker.check_knowledge_freshness("Redis")

    assert result is not None
    assert result.entity_name == "Redis"


def test_denied_candidate_has_no_entity_access_or_freshness_side_effect(
    tmp_path,
    monkeypatch,
):
    wiki = tmp_path / "wiki"
    page = wiki / "private.md"
    page.write_text("---\nupdated_at: 2026-01-01\n---\nprivate\n", encoding="utf-8")
    entity = FakeEntity(name="Private", last_updated="2026-01-01")
    entity.source_page = "private.md"
    checker = _checker()
    monkeypatch.setattr(
        checker,
        "_get_reminder_engine",
        lambda: (_ for _ in ()).throw(
            AssertionError("denied candidate must not reach freshness engine")
        ),
    )
    monkeypatch.setattr(
        "core.kia.kg_event_handler.KGEventHandler.on_entity_accessed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("denied candidate must not emit entity access")
        ),
    )

    with patch("core.kia.entity_manager.EntityManager.get_entity", return_value=entity):
        result = checker.check_knowledge_freshness(
            "Private",
            candidate_filter=lambda _page: False,
        )

    assert result is not None
    assert result.status == "access_denied"
    assert not (tmp_path / "data" / "reminder_cooldown.db").exists()


def test_version_outdated_maps_version_fields(monkeypatch):
    """version_outdated 类型应补全 current_version / latest_version"""
    from core.kia.reminder_engine import Reminder

    entity = FakeEntity(
        name="过期组件",
        entity_type="concept",
        version_info={"current_version": "1.0", "latest_version": "2.0"},
    )
    checker = _checker()

    fake_reminder = Reminder(
        reminder_type="freshness",
        page_path="entity://过期组件",
        title="过期组件",
        message="发现新版本",
        reason="新鲜度检查：newer_version",
        confidence=0.9,
        priority="high",
    )

    with patch("core.kia.entity_manager.EntityManager.get_entity", return_value=entity):
        monkeypatch.setattr(
            checker._get_reminder_engine(), "check_freshness", lambda page: [fake_reminder]
        )
        result = checker.check_knowledge_freshness("过期组件")

    assert result is not None
    assert result.status == "stale"
    assert result.alert_type == "version_outdated"
    assert result.current_version == "1.0"
    assert result.latest_version == "2.0"
