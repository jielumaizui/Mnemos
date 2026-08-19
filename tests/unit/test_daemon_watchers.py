# -*- coding: utf-8 -*-
"""Tests for daemon watcher services."""

from pathlib import Path

import pytest

import mnemos_daemon


class _WatchersEnabledConfig:
    """Minimal config stub that enables watcher services."""

    def __init__(self, tmp_path=None):
        self._tmp = tmp_path

    def get(self, key: str, default=None):
        if key in (
            "watchers.enabled",
            "watchers.agent_paths.enabled",
        ):
            return True
        return default

    @property
    def obsidian_vault_path(self):
        return self._tmp / "raw" if self._tmp else None

    @property
    def data_dir(self):
        return self._tmp / ".mnemos" if self._tmp else None

    @property
    def database_dir(self):
        return self._tmp / ".mnemos" if self._tmp else None


@pytest.fixture(autouse=True)
def watchers_enabled(monkeypatch):
    """Patch get_config so watcher services see their feature flags enabled."""
    monkeypatch.setattr("core.config.get_config", lambda: _WatchersEnabledConfig())


@pytest.fixture(autouse=True)
def reset_watcher_globals(tmp_path):
    """Reset watcher singletons and dirty-source set between tests."""
    mnemos_daemon._agent_path_watcher = None
    mnemos_daemon._agent_path_watcher_primed = False
    with mnemos_daemon._trigger_lock:
        mnemos_daemon._trigger_dirty_sources.clear()
    yield
    mnemos_daemon._agent_path_watcher = None
    mnemos_daemon._agent_path_watcher_primed = False
    with mnemos_daemon._trigger_lock:
        mnemos_daemon._trigger_dirty_sources.clear()


def test_service_agent_path_watch_detects_changes_and_marks_dirty(tmp_path, monkeypatch):
    monkeypatch.setattr(mnemos_daemon, "_service_enabled", lambda _cfg, name: True)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()

    def _discover(agent: str):
        return agent_dir if agent == "test_agent" else None

    watcher = mnemos_daemon.AgentPathWatcher(["test_agent"], discoverer=_discover)
    monkeypatch.setattr(mnemos_daemon, "_get_agent_path_watcher", lambda _cfg: watcher)

    # First call primes the watcher and reports no changes.
    result = mnemos_daemon.service_agent_path_watch()
    assert result["enabled"] is False  # priming run
    assert result["changed"] == 0

    # Modify the directory contents to change mtime.
    (agent_dir / "new.md").write_text("changed")
    result = mnemos_daemon.service_agent_path_watch()
    assert result["enabled"] is True
    assert result["changed"] == 1
    assert result["marked_dirty"] == 1
    assert result["errors"] == 0
    assert "test_agent" in mnemos_daemon._trigger_dirty_sources


def test_service_agent_path_watch_does_not_prime_an_unavailable_root(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(mnemos_daemon, "_service_enabled", lambda _cfg, _name: True)
    agent_dir = tmp_path / "agent"
    original_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == agent_dir:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    watcher = mnemos_daemon.AgentPathWatcher(
        ["test_agent"],
        discoverer=lambda _agent: agent_dir,
    )
    monkeypatch.setattr(Path, "stat", denied)
    monkeypatch.setattr(
        mnemos_daemon,
        "_get_agent_path_watcher",
        lambda _cfg: watcher,
    )

    result = mnemos_daemon.service_agent_path_watch()

    assert result["errors"] == 1
    assert mnemos_daemon._agent_path_watcher_primed is False


def test_agent_path_watcher_factory_propagates_registry_unavailability(
    monkeypatch,
):
    from core.sync_framework.registry import SourceRegistryUnavailableError

    monkeypatch.setattr(
        "core.sync_framework.registry.SourceRegistry.register_builtin_agents",
        lambda: None,
    )
    monkeypatch.setattr(
        "core.sync_framework.registry.SourceRegistry.auto_discover",
        lambda: (_ for _ in ()).throw(
            SourceRegistryUnavailableError("codex", "auto_discover")
        ),
    )

    with pytest.raises(
        SourceRegistryUnavailableError,
        match="source_registry_unavailable:codex:auto_discover",
    ):
        mnemos_daemon._get_agent_path_watcher(object())


def test_service_agent_path_watch_disabled_by_daemon_services(monkeypatch):
    monkeypatch.setattr(
        mnemos_daemon, "_service_enabled", lambda _cfg, name: name != "agent_path_watch"
    )
    result = mnemos_daemon.service_agent_path_watch()
    assert result["enabled"] is False
    assert result["changed"] == 0
