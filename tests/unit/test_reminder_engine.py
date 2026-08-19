# -*- coding: utf-8 -*-
"""
ReminderEngine 单元测试

验证统一提醒引擎的核心能力：
- 上下文提醒生成
- 新鲜度提醒生成
- 同页去重合并
- 统一冷却
- 应用层包装器向后兼容
"""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmp_wiki_dir(tmp_path: Path) -> Path:
    """提供独立的临时 Wiki 目录。"""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return wiki


@pytest.fixture
def patched_get_config(tmp_path: Path, tmp_wiki_dir: Path, monkeypatch):
    """隔离全局配置，使用临时目录。"""
    import core.config as _config_mod

    fake_cfg = MagicMock()
    fake_cfg.wiki_dir = tmp_wiki_dir
    fake_cfg.data_dir = tmp_path / "data"
    fake_cfg.database_dir = tmp_path / "data"
    fake_cfg.database_dir.mkdir(parents=True, exist_ok=True)
    fake_cfg.get = lambda key, default=None: {
        "reminder.enabled": True,
        "reminder.contextual_cooldown_seconds": 600,
        "reminder.freshness_cooldown_seconds": 86400,
        "reminder.max_contextual_per_turn": 3,
    }.get(key, default)
    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_cfg)
    return fake_cfg


def _make_page(wiki_dir: Path, rel_path: str, title: str = "", **fm) -> Path:
    """在临时 wiki 中创建带 frontmatter 的 markdown 页面。"""
    import yaml

    page = wiki_dir / rel_path
    page.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = dict(fm)
    content = "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True) + "---\n"
    content += f"# {title or page.stem}\n\n## 核心内容\n\n示例核心内容。\n"
    page.write_text(content, encoding="utf-8")
    return page


# ============================================================
# 1. 上下文提醒
# ============================================================


def test_contextual_reminders_return_reminder_objects(
    tmp_path: Path, tmp_wiki_dir: Path, patched_get_config, monkeypatch
):
    """contextual_reminders 应返回 Reminder 对象列表。"""
    from core.kia.reminder_engine import ReminderEngine, Reminder

    _make_page(
        tmp_wiki_dir,
        "03-Tech/redis.md",
        title="Redis 连接池",
        关键词={
            "核心概念": ["缓存", "连接池", "报错"],
            "工具实体": ["Redis"],
            "场景标签": ["故障排查", "解决"],
        },
    )

    engine = ReminderEngine(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_path / "cooldown.db"))
    reminders = engine.contextual_reminders("Redis 报错怎么解决")

    assert isinstance(reminders, list)
    assert all(isinstance(r, Reminder) for r in reminders)
    assert any(r.reminder_type == "contextual" for r in reminders)
    assert any("Redis" in r.title for r in reminders)


# ============================================================
# 2. 新鲜度提醒
# ============================================================


def test_freshness_check_returns_reminder_objects(
    tmp_path: Path, tmp_wiki_dir: Path, patched_get_config
):
    """check_freshness 应返回 Reminder 对象列表。"""
    from core.kia.reminder_engine import ReminderEngine, Reminder

    old_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    page_path = _make_page(
        tmp_wiki_dir,
        "03-Tech/legacy.md",
        title="旧知识",
        updated_at=old_date,
    )

    engine = ReminderEngine(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_path / "cooldown.db"))
    page = {
        "path": str(page_path),
        "title": "旧知识",
        "frontmatter": {"updated_at": old_date},
    }
    reminders = engine.check_freshness(page)

    assert isinstance(reminders, list)
    assert all(isinstance(r, Reminder) for r in reminders)
    assert any(r.reminder_type == "freshness" for r in reminders)
    assert any(r.priority == "medium" for r in reminders)


# ============================================================
# 3. 去重合并
# ============================================================


def test_deduplication_combines_contextual_and_freshness(
    tmp_path: Path, tmp_wiki_dir: Path, patched_get_config, monkeypatch
):
    """同一页面同时触发 contextual 和 freshness 时应合并为 combined。"""
    from core.kia.reminder_engine import ReminderEngine

    old_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
    page_path = _make_page(
        tmp_wiki_dir,
        "03-Tech/redis.md",
        title="Redis 连接池",
        updated_at=old_date,
        关键词={
            "核心概念": ["缓存", "连接池", "报错"],
            "工具实体": ["Redis"],
            "场景标签": ["故障排查", "解决"],
        },
    )

    engine = ReminderEngine(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_path / "cooldown.db"))

    # 强制跳过冷却，让同一页两次检查都能命中
    monkeypatch.setattr(engine, "_is_in_cooldown", lambda _p, _t: False)

    page = {
        "path": str(page_path),
        "title": "Redis 连接池",
        "frontmatter": {"updated_at": old_date},
    }
    reminders = engine.reminders_for("Redis 报错怎么解决", page)

    combined = [r for r in reminders if r.reminder_type == "combined"]
    assert combined, "应存在合并后的 combined 提醒"
    assert combined[0].priority in {"high", "medium"}
    assert "contextual" in combined[0].reason or "新鲜度" in combined[0].reason


# ============================================================
# 4. 冷却
# ============================================================


def test_cooldown_prevents_repeated_reminders(
    tmp_path: Path, tmp_wiki_dir: Path, patched_get_config, monkeypatch
):
    """同一页面在冷却期内不应重复返回提醒。"""
    from core.kia.reminder_engine import ReminderEngine

    _make_page(
        tmp_wiki_dir,
        "03-Tech/redis.md",
        title="Redis 连接池",
        关键词={
            "核心概念": ["缓存", "连接池", "报错"],
            "工具实体": ["Redis"],
            "场景标签": ["故障排查", "解决"],
        },
    )

    engine = ReminderEngine(wiki_base=str(tmp_wiki_dir), db_path=str(tmp_path / "cooldown.db"))

    first = engine.contextual_reminders("Redis 报错怎么解决")
    assert first, "首次应返回提醒"

    # 同一输入立即再次调用，应被冷却过滤
    second = engine.contextual_reminders("Redis 报错怎么解决")
    assert second == [], f"冷却期内不应重复提醒，但得到 {second}"


# ============================================================
# 5. 应用层包装器：FreshnessAlertChecker.check_knowledge_freshness
# ============================================================


def test_app_freshness_alert_wrapper_still_returns_freshness_result(
    tmp_path: Path, tmp_wiki_dir: Path, patched_get_config, monkeypatch
):
    """FreshnessAlertChecker.check_knowledge_freshness 仍应返回 FreshnessResult。"""
    from core.app.freshness_alert import FreshnessAlertChecker, FreshnessResult
    from core.kia.entity_manager import Entity

    old_date = (datetime.now() - timedelta(days=100)).isoformat()
    page_path = _make_page(
        tmp_wiki_dir,
        "03-Tech/legacy.md",
        title="旧知识",
        updated_at=old_date,
    )

    fake_entity = Entity(
        uid="legacy",
        name="旧知识",
        entity_type="concept",
        source_page=str(page_path.relative_to(tmp_wiki_dir)),
        last_updated=old_date,
    )

    checker = FreshnessAlertChecker(wiki_base=str(tmp_wiki_dir))
    with patch("core.kia.entity_manager.EntityManager.get_entity", return_value=fake_entity):
        result = checker.check_knowledge_freshness("旧知识")

    assert isinstance(result, FreshnessResult)
    assert result.status in {"stale", "fresh", "not_found", "error"}
