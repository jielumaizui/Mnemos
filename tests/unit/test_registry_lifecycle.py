# -*- coding: utf-8 -*-
"""Unit tests for core.sync_framework.registry AgentLifecycleManager."""

from __future__ import annotations


import pytest
from unittest.mock import PropertyMock, patch

from core.sync_framework.registry import AgentLifecycleManager, SourceRegistry
from integrations.sources.claude_source import ClaudeSource
from integrations.sources.codex_source import CodexSource
from integrations.sources.hermes_source import HermesSource


@pytest.fixture
def lifecycle():
    # clear registry state
    SourceRegistry._registry.clear()
    SourceRegistry._instances.clear()
    return AgentLifecycleManager(refresh_interval=300)


class TestAgentLifecycleManager:
    def test_start_discovers_agents(self, lifecycle, tmp_path):
        with patch.object(
            CodexSource,
            "data_dir",
            new_callable=PropertyMock,
            return_value=tmp_path,
        ):
            SourceRegistry.register("codex", CodexSource)
            lifecycle.start()
            assert lifecycle._running is True
            assert "codex" in lifecycle.get_active_agents()
            lifecycle.stop()

    def test_start_idempotent(self, lifecycle):
        lifecycle.start()
        lifecycle.start()
        assert lifecycle._running is True
        lifecycle.stop()

    def test_stop_sets_running_false(self, lifecycle):
        lifecycle.start()
        lifecycle.stop()
        assert lifecycle._running is False

    def test_report_error_and_success(self, lifecycle):
        lifecycle.report_error("alpha")
        lifecycle.report_error("alpha")
        assert lifecycle._error_counts["alpha"] == 2
        lifecycle.report_success("alpha")
        assert lifecycle._error_counts["alpha"] == 0

    def test_discover_agents_manually(self, lifecycle, tmp_path):
        with patch.object(
            ClaudeSource,
            "data_dir",
            new_callable=PropertyMock,
            return_value=tmp_path,
        ):
            SourceRegistry.register("claude", ClaudeSource)
            lifecycle.discover_agents()
            assert "claude" in lifecycle.get_active_agents()

    def test_get_active_agents_returns_copy(self, lifecycle, tmp_path):
        with patch.object(
            HermesSource,
            "data_dir",
            new_callable=PropertyMock,
            return_value=tmp_path,
        ):
            SourceRegistry.register("hermes", HermesSource)
            lifecycle.discover_agents()
            agents = lifecycle.get_active_agents()
            agents.clear()
            assert "hermes" in lifecycle.get_active_agents()
