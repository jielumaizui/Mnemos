# -*- coding: utf-8 -*-
"""Tests for daemon.maintenance."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from daemon import maintenance


def test_run_startup_compensation_counts_missed_items():
    scheduler = MagicMock()
    scheduler.startup_compensation.return_value = ["a", "b"]
    errors = []

    with patch("core.app.forced_retrospective.ForcedRetrospective", return_value=scheduler):
        result = maintenance.run_startup_compensation(
            lambda service_name, exc: errors.append((service_name, exc))
        )

    assert result == {"compensated": 2}
    assert errors == []


def test_format_model_status_uses_injected_runtime_values():
    status = maintenance.format_model_status(
        count_daemon_processes=lambda: 3,
        daemon_pid=42,
        now_func=lambda: datetime(2026, 6, 24, 10, 0, 0),
        platform_func=lambda: "TestOS",
    )

    assert "Mnemos Daemon Status @ 2026-06-24T10:00:00" in status
    assert "PID: 42" in status
    assert "Platform: TestOS" in status
    assert "Daemon processes: 3" in status


def test_generate_drift_report_skips_when_history_is_insufficient():
    store = MagicMock()
    store.load_recent_personas.return_value = []
    errors = []

    with patch("core.persona.delphi.PersonaStore", return_value=store):
        result = maintenance.generate_drift_report(
            lambda service_name, exc: errors.append((service_name, exc)),
            now_func=lambda: datetime(2026, 6, 24, 10, 0, 0),
        )

    assert result["drift_count"] == 0
    assert result["note"] == "画像历史版本不足，跳过漂移检测"
    assert errors == []


def test_generate_drift_report_detects_drift_and_logs():
    current = SimpleNamespace(version="v2", signal_count=5)
    previous = SimpleNamespace(version="v1", signal_count=4)
    store = MagicMock()
    store.load_recent_personas.return_value = [current, previous]
    analyzer = MagicMock()
    analyzer.detect_drift.return_value = [{"field": "tone"}]
    info_calls = []

    with (
        patch("core.persona.delphi.PersonaStore", return_value=store),
        patch("core.persona.pythia.PreferenceAnalyzer", return_value=analyzer),
    ):
        result = maintenance.generate_drift_report(
            lambda service_name, exc: None,
            log_info=lambda *args: info_calls.append(args),
            now_func=lambda: datetime(2026, 6, 24, 10, 0, 0),
        )

    assert result["drifts"] == [{"field": "tone"}]
    assert result["profile_version"] == "v2"
    assert result["previous_version"] == "v1"
    assert result["drift_count"] == 1
    assert len(info_calls) == 1


def test_run_preflight_checks_reports_config_paths(tmp_path):
    cfg = SimpleNamespace(
        wiki_dir=tmp_path / "wiki",
        data_dir=tmp_path / "data",
        database_dir=tmp_path / "database",
    )
    cfg.wiki_dir.mkdir()
    cfg.data_dir.mkdir()

    with patch("core.config.get_config", return_value=cfg):
        result = maintenance.run_preflight_checks(
            now_func=lambda: datetime(2026, 6, 24, 10, 0, 0)
        )

    assert result["timestamp"] == "2026-06-24T10:00:00"
    assert result["wiki_dir_exists"] is True
    assert result["data_dir_exists"] is True
    assert result["database_dir_exists"] is False
    assert result["config_ok"] is True


def test_build_push_context_preserves_shape():
    result = maintenance.build_push_context(
        "hello",
        now_func=lambda: datetime(2026, 6, 24, 10, 0, 0),
    )

    assert result == {
        "user_message": "hello",
        "timestamp": "2026-06-24T10:00:00",
        "recommendations": [],
    }


def test_run_preflight_checks_propagates_programming_errors():
    with (
        patch("core.config.get_config", side_effect=AssertionError("config bug")),
        pytest.raises(AssertionError, match="config bug"),
    ):
        maintenance.run_preflight_checks()
