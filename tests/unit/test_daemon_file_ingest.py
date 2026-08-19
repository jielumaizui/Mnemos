# -*- coding: utf-8 -*-
"""Tests for daemon.file_ingest."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from daemon import file_ingest


class FakeConfig:
    def __init__(self, **kwargs):
        self._data = kwargs

    def get(self, key, default=None):
        keys = key.split(".")
        value = self._data
        for key_part in keys:
            if isinstance(value, dict) and key_part in value:
                value = value[key_part]
            else:
                return default
        return value


def test_resolve_ingest_dir_uses_config_path(tmp_path):
    configured = tmp_path / "watch"
    cfg = FakeConfig(file_ingestor={"watch_dir": str(configured)})

    assert file_ingest.resolve_ingest_dir(cfg, tmp_path) == configured


def test_resolve_ingest_dir_defaults_to_data_dir(tmp_path):
    cfg = FakeConfig(file_ingestor={"watch_dir": ""})

    assert file_ingest.resolve_ingest_dir(cfg, tmp_path) == tmp_path / "file_ingest"


def test_run_service_ingests_existing_directory(tmp_path):
    ingest_dir = tmp_path / "watch"
    ingest_dir.mkdir()
    cfg = FakeConfig(file_ingestor={"watch_dir": str(ingest_dir)})
    ingestor = MagicMock()
    ingestor.ingest_directory.return_value = 2
    errors = []

    with patch("core.sync_framework.file_ingestor.FileIngestor", return_value=ingestor):
        result = file_ingest.run_service(
            cfg,
            data_dir=tmp_path,
            log_service_error=lambda service_name, exc: errors.append((service_name, exc)),
        )

    assert result == {"ingested": 2, "errors": 0}
    assert errors == []
    ingestor.ingest_directory.assert_called_once_with(
        ingest_dir,
        agent_name="file",
        recursive=True,
    )


def test_run_service_ignores_missing_directory(tmp_path):
    cfg = FakeConfig(file_ingestor={"watch_dir": str(tmp_path / "missing")})

    result = file_ingest.run_service(
        cfg,
        data_dir=tmp_path,
        log_service_error=lambda service_name, exc: None,
    )

    assert result == {"ingested": 0, "errors": 0}


def test_run_service_records_errors(tmp_path):
    ingest_dir = tmp_path / "watch"
    ingest_dir.mkdir()
    cfg = FakeConfig(file_ingestor={"watch_dir": str(ingest_dir)})
    errors = []

    with patch("core.sync_framework.file_ingestor.FileIngestor", side_effect=RuntimeError("boom")):
        result = file_ingest.run_service(
            cfg,
            data_dir=tmp_path,
            log_service_error=lambda service_name, exc: errors.append((service_name, str(exc))),
        )

    assert result == {"ingested": 0, "errors": 1}
    assert errors == [("file_ingestor", "boom")]
