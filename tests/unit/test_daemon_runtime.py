# -*- coding: utf-8 -*-
"""Tests for daemon.runtime helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from daemon import runtime


def test_ensure_project_on_path_inserts_once(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    monkeypatch.setattr(sys, "path", [])

    runtime.ensure_project_on_path(project_root)
    runtime.ensure_project_on_path(project_root)

    assert sys.path == [str(project_root)]


def test_resolve_data_dirs_uses_config(monkeypatch, tmp_path):
    cfg = MagicMock(data_dir=tmp_path / "data", database_dir=tmp_path / "db")
    monkeypatch.setattr("core.config.get_config", lambda: cfg)

    assert runtime.resolve_data_dirs() == (cfg.data_dir, cfg.database_dir)


def test_runtime_paths_from_config_derives_daemon_files(tmp_path):
    cfg = MagicMock(data_dir=tmp_path / "data", database_dir=tmp_path / "db")

    paths = runtime.RuntimePaths.from_config(cfg)

    assert paths.data_dir == tmp_path / "data"
    assert paths.database_dir == tmp_path / "db"
    assert paths.pid_file == tmp_path / "db" / "daemon.pid"
    assert paths.status_file == tmp_path / "db" / "daemon.status"
    assert paths.daemon_log == tmp_path / "db" / "logs" / "daemon.log"
    assert paths.heartbeat_file == tmp_path / "db" / "daemon_heartbeat.json"


def test_resolve_data_dirs_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr("core.config.get_config", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    fallback = tmp_path / ".mnemos"
    assert runtime.resolve_data_dirs() == (fallback, fallback)
