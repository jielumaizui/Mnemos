# -*- coding: utf-8 -*-
"""Tests for daemon.service_state helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from daemon import service_state


def test_record_service_error_groups_contextual_service_names():
    state = {}

    service_state.record_service_error(state, "raw_sync:codex", RuntimeError("boom"))
    service_state.record_service_error(state, "raw_sync:kimi", ValueError("bad"))

    assert set(state) == {"raw_sync"}
    assert state["raw_sync"]["count"] == 2
    assert state["raw_sync"]["last_error"] == "bad"
    assert state["raw_sync"]["last_context"] == "raw_sync:kimi"


def test_clear_service_error_returns_previous_state():
    state = {}
    service_state.record_service_error(
        state, "raw_projection", RuntimeError("database is locked")
    )

    previous = service_state.clear_service_error(state, "raw_projection")

    assert previous["last_error"] == "database is locked"
    assert "raw_projection" not in state


def test_is_module_missing_detects_import_errors():
    assert service_state.is_module_missing(ModuleNotFoundError("No module named x")) is True
    assert service_state.is_module_missing(ImportError("cannot import name y")) is True
    assert service_state.is_module_missing(RuntimeError("boom")) is False


def test_done_callback_records_success(monkeypatch):
    service_futures = {"heartbeat": object()}
    service_results = {}
    error_state = {}
    cfg = object()
    monkeypatch.setattr("core.config.get_config", lambda: cfg)

    callback = service_state.make_service_done_callback(
        "heartbeat",
        service_futures=service_futures,
        service_results=service_results,
        error_state=error_state,
        service_enabled=lambda got_cfg, name: got_cfg is cfg and name == "heartbeat",
    )
    future = MagicMock()
    future.result.return_value = {"ok": 1}

    callback(future)

    assert service_futures == {}
    assert service_results["heartbeat"]["ok"] is True
    assert service_results["heartbeat"]["result"] == {"ok": 1, "enabled": True}
    assert error_state == {}


def test_done_callback_records_failure(monkeypatch):
    service_futures = {"heartbeat": object()}
    service_results = {}
    error_state = {}
    monkeypatch.setattr("core.config.get_config", lambda: object())

    callback = service_state.make_service_done_callback(
        "heartbeat",
        service_futures=service_futures,
        service_results=service_results,
        error_state=error_state,
        service_enabled=lambda cfg, name: True,
    )
    future = MagicMock()
    future.result.side_effect = RuntimeError("crash")

    callback(future)

    assert service_futures == {}
    assert service_results["heartbeat"]["ok"] is False
    assert service_results["heartbeat"]["error"] == "RuntimeError: crash"
    assert error_state["heartbeat"]["count"] == 1
