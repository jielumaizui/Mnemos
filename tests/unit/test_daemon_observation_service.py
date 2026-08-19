# -*- coding: utf-8 -*-
"""Tests for daemon.observation_service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from core.cognitive.models import ObservationBatch
from daemon import observation_service


class FakeConfig:
    obsidian_vault_path = Path("/tmp/raw")
    wiki_dir = Path("/tmp/wiki")
    database_dir = Path("/tmp/mnemos")

    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def test_run_service_uses_incremental_when_latest_update_exists():
    cfg = FakeConfig(
        {
            "observation.enabled": True,
            "daemon.services.observation_engine": True,
        }
    )
    engine = MagicMock()
    engine.get_store_stats.return_value = {"latest_update": "2026-06-01T00:00:00"}
    engine.run_incremental.return_value = ObservationBatch(
        observations=["a", "b"],
        observation_total=2,
        dimension_counts={"tone": 2},
        source_count=3,
        extraction_status="ok",
        extraction_reason="observations_extracted",
    )

    with (
        patch("core.config.get_config", return_value=cfg),
        patch("core.cognitive.observation_engine.ObservationEngine", return_value=engine),
    ):
        result = observation_service.run_service(lambda service_name, exc: None)

    assert result == {
        "observations": 2,
        "dimensions": 1,
        "errors": 0,
        "processed_items": 3,
        "status": "ok",
        "reason": "observations_extracted",
    }
    engine.run_incremental.assert_called_once()
    engine.run.assert_not_called()


def test_run_service_falls_back_to_recent_incremental_without_latest_update():
    """无 latest_update 时，daemon 应回退到最近 24 小时增量，避免全量扫描。"""
    cfg = FakeConfig(
        {
            "observation.enabled": True,
            "daemon.services.observation_engine": True,
        }
    )
    engine = MagicMock()
    engine.get_store_stats.return_value = {"latest_update": None}
    engine.run_incremental.return_value = ObservationBatch(
        observations=["a"],
        observation_total=1,
        dimension_counts={},
        source_count=1,
        extraction_status="ok",
        extraction_reason="observations_extracted",
    )

    with (
        patch("core.config.get_config", return_value=cfg),
        patch("core.cognitive.observation_engine.ObservationEngine", return_value=engine),
    ):
        result = observation_service.run_service(lambda service_name, exc: None)

    assert result["observations"] == 1
    assert result["dimensions"] == 0
    assert result["processed_items"] == 1
    assert result["status"] == "ok"
    assert result["reason"] == "observations_extracted"
    engine.run.assert_not_called()
    engine.run_incremental.assert_called_once()
    call_kwargs = engine.run_incremental.call_args.kwargs
    assert call_kwargs["persist"] is True
    since = call_kwargs["since"]
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert now - timedelta(hours=25) < since < now


def test_run_service_respects_disabled_flag():
    cfg = FakeConfig({"observation.enabled": False})

    with patch("core.config.get_config", return_value=cfg):
        result = observation_service.run_service(lambda service_name, exc: None)

    assert result == {
        "observations": 0,
        "dimensions": 0,
        "errors": 0,
        "processed_items": 0,
        "status": "skipped",
        "reason": "observation_disabled",
    }


def test_run_service_explains_zero_output_with_sources():
    cfg = FakeConfig(
        {
            "observation.enabled": True,
            "daemon.services.observation_engine": True,
        }
    )
    engine = MagicMock()
    engine.get_store_stats.return_value = {"latest_update": "2026-06-01T00:00:00"}
    engine.run_incremental.return_value = ObservationBatch(
        observations=[],
        observation_total=0,
        dimension_counts={},
        source_count=2,
        extraction_status="empty",
        extraction_reason="no_observations_extracted",
    )
    log_info = MagicMock()

    with (
        patch("core.config.get_config", return_value=cfg),
        patch("core.cognitive.observation_engine.ObservationEngine", return_value=engine),
    ):
        result = observation_service.run_service(
            lambda service_name, exc: None,
            log_info=log_info,
        )

    assert result["observations"] == 0
    assert result["processed_items"] == 2
    assert result["status"] == "empty"
    assert result["reason"] == "no_observations_extracted"
    log_info.assert_called_once()
