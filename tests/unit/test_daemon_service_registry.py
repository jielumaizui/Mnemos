# -*- coding: utf-8 -*-
"""Tests for daemon.service_registry."""

from __future__ import annotations

import pytest

from daemon import service_registry


def test_resolve_direct_service():
    def service_heartbeat():
        return {"ok": True}

    fn = service_registry.resolve_service_call(
        "heartbeat",
        {"service_heartbeat": service_heartbeat},
    )

    assert fn is service_heartbeat
    assert fn() == {"ok": True}


def test_resolve_legacy_l1_sync_aliases_raw_sync():
    def service_raw_sync():
        return {"synced": 1}

    fn = service_registry.resolve_service_call(
        "l1_sync",
        {"service_raw_sync": service_raw_sync},
    )

    assert fn is service_raw_sync


def test_resolve_raw_projection_service():
    def service_raw_projection():
        return {"status": "applied"}

    fn = service_registry.resolve_service_call(
        "raw_projection",
        {"service_raw_projection": service_raw_projection},
    )

    assert fn is service_raw_projection
    assert fn() == {"status": "applied"}


def test_resolve_wiki_route_service():
    def service_wiki_route():
        return {"status": "ok", "moved": 1, "review": 0}

    fn = service_registry.resolve_service_call(
        "wiki_route",
        {"service_wiki_route": service_wiki_route},
    )

    assert fn is service_wiki_route
    assert fn() == {"status": "ok", "moved": 1, "review": 0}


def test_resolve_cfg_bound_service():
    calls = []

    def service_retry_failed(cfg):
        calls.append(cfg)
        return {"retried": 1}

    cfg = object()
    fn = service_registry.resolve_service_call(
        "retry_failed",
        {"service_retry_failed": service_retry_failed},
        cfg,
    )

    assert fn() == {"retried": 1}
    assert calls == [cfg]


def test_unknown_service_raises():
    with pytest.raises(ValueError, match="未知服务"):
        service_registry.resolve_service_call("missing", {})
