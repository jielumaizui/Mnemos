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


def test_entity_without_meta_does_not_crash():
    """Entity 没有 meta 字段时不应 AttributeError"""
    entity = FakeEntity(name="无meta实体", last_updated="")
    checker = _checker()

    # 直接测试 _check_context_expiry 不报错
    alert = checker._check_context_expiry(entity)
    assert alert is None  # last_updated 为空，不返回 alert
