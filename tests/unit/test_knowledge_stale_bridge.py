"""
knowledge_stale → FreshnessRefreshWorker 事件驱动桥接测试

验证 knowledge_stale 事件能自动触发过期知识页面刷新。
"""

from unittest.mock import MagicMock, patch


from core.app.freshness_refresh_worker import RefreshResult
from core.mnemos_bus import Event


def _make_event(wiki_pages):
    return Event(
        event_type="knowledge_stale",
        source="evolution",
        payload={
            "stale_count": len(wiki_pages),
            "entities": [Path(p).stem for p in wiki_pages],
            "wiki_pages": wiki_pages,
            "alert_types": ["version_outdated"],
        },
    )


from pathlib import Path  # noqa: E402


def test_on_knowledge_stale_triggers_refresh():
    """knowledge_stale 事件应自动调用 FreshnessRefreshWorker.refresh_page。"""
    fake_config = MagicMock()
    fake_config.get = MagicMock(
        side_effect=lambda key, default=None: {
            "daemon.services.freshness_refresh": True,
            "freshness_refresh.auto_refresh_on_stale": True,
            "freshness_refresh.auto_refresh_limit": 3,
        }.get(key, default)
    )
    fake_config.wiki_dir = Path("/tmp/wiki")

    refreshed = RefreshResult(status="refreshed", path="03-Tech/python.md")
    mock_worker = MagicMock()
    mock_worker.refresh_page = MagicMock(return_value=refreshed)

    with (
        patch("core.config.get_config", return_value=fake_config),
        patch("core.app.freshness_refresh_worker.FreshnessRefreshWorker", return_value=mock_worker),
    ):
        from mnemos_daemon import _on_knowledge_stale

        event = _make_event(["03-Tech/python.md", "04-Concepts/api.md"])
        _on_knowledge_stale(event)

    assert mock_worker.refresh_page.call_count == 2
    mock_worker.refresh_page.assert_any_call("03-Tech/python.md")
    mock_worker.refresh_page.assert_any_call("04-Concepts/api.md")


def test_on_knowledge_stale_honors_limit():
    """自动刷新数量受 auto_refresh_limit 限制。"""
    fake_config = MagicMock()
    fake_config.get = MagicMock(
        side_effect=lambda key, default=None: {
            "daemon.services.freshness_refresh": True,
            "freshness_refresh.auto_refresh_on_stale": True,
            "freshness_refresh.auto_refresh_limit": 2,
        }.get(key, default)
    )
    fake_config.wiki_dir = Path("/tmp/wiki")

    refreshed = RefreshResult(status="refreshed", path="p.md")
    mock_worker = MagicMock()
    mock_worker.refresh_page = MagicMock(return_value=refreshed)

    with (
        patch("core.config.get_config", return_value=fake_config),
        patch("core.app.freshness_refresh_worker.FreshnessRefreshWorker", return_value=mock_worker),
    ):
        from mnemos_daemon import _on_knowledge_stale

        event = _make_event(["a.md", "b.md", "c.md", "d.md"])
        _on_knowledge_stale(event)

    assert mock_worker.refresh_page.call_count == 2


def test_on_knowledge_stale_disabled_by_config():
    """freshness_refresh.auto_refresh_on_stale=False 时不刷新。"""
    fake_config = MagicMock()
    fake_config.get = MagicMock(
        side_effect=lambda key, default=None: {
            "daemon.services.freshness_refresh": True,
            "freshness_refresh.auto_refresh_on_stale": False,
            "freshness_refresh.auto_refresh_limit": 3,
        }.get(key, default)
    )
    fake_config.wiki_dir = Path("/tmp/wiki")

    mock_worker = MagicMock()

    with (
        patch("core.config.get_config", return_value=fake_config),
        patch("core.app.freshness_refresh_worker.FreshnessRefreshWorker", return_value=mock_worker),
    ):
        from mnemos_daemon import _on_knowledge_stale

        event = _make_event(["a.md"])
        _on_knowledge_stale(event)

    mock_worker.refresh_page.assert_not_called()


def test_on_knowledge_stale_no_wiki_pages():
    """空 wiki_pages 时不创建 worker。"""
    fake_config = MagicMock()
    fake_config.get = MagicMock(
        side_effect=lambda key, default=None: {
            "daemon.services.freshness_refresh": True,
            "freshness_refresh.auto_refresh_on_stale": True,
            "freshness_refresh.auto_refresh_limit": 3,
        }.get(key, default)
    )

    mock_worker_cls = MagicMock()

    with (
        patch("core.config.get_config", return_value=fake_config),
        patch("core.app.freshness_refresh_worker.FreshnessRefreshWorker", mock_worker_cls),
    ):
        from mnemos_daemon import _on_knowledge_stale

        event = Event(event_type="knowledge_stale", source="evolution", payload={"wiki_pages": []})
        _on_knowledge_stale(event)

    mock_worker_cls.assert_not_called()
