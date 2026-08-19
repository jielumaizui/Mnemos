# -*- coding: utf-8 -*-
"""Tests for daemon.intervals."""

from __future__ import annotations

from unittest.mock import MagicMock

from daemon import intervals


def test_build_default_intervals_uses_capture_tick():
    table = intervals.build_default_intervals(capture_tick=12)

    assert table["capture_worker"] == 12
    assert table["heartbeat"] == 60
    assert "l1_sync" not in table
    assert "raw_sync" in table
    assert table["raw_projection"] == 300
    assert table["wiki_route"] == 3600
    assert table["operational_incidents"] == 60


def test_resolve_capture_tick_reads_config(monkeypatch):
    cfg = MagicMock()
    cfg.get.return_value = "42"
    monkeypatch.setattr("core.config.get_config", lambda: cfg)

    assert intervals.resolve_capture_tick() == 42
    cfg.get.assert_called_once_with("capture.tick_interval_seconds", 5)


def test_resolve_capture_tick_falls_back(monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda: (_ for _ in ()).throw(RuntimeError()))

    assert intervals.resolve_capture_tick(default=7) == 7
