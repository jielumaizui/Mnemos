# -*- coding: utf-8 -*-
"""Tests for daemon.link_probe."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from daemon import link_probe


def test_run_service_disabled_by_feature_flag():
    result = link_probe.run_service(
        {"features.enable_link_probe": False},
        log_service_error=lambda service_name, exc: None,
    )

    assert result == {"enabled": False, "probed": 0, "broken": 0, "updated": 0, "errors": 0}


def test_run_service_disabled_by_daemon_service_flag():
    result = link_probe.run_service(
        {
            "features.enable_link_probe": True,
            "daemon.services.link_probe": False,
        },
        log_service_error=lambda service_name, exc: None,
    )

    assert result == {"enabled": False, "probed": 0, "broken": 0, "updated": 0, "errors": 0}


def test_run_service_probes_and_updates_unique_pages():
    worker = MagicMock()
    worker.probe_batch.return_value = [
        {"status": "ok", "page_path": "a.md"},
        {"status": "broken", "page_path": "b.md"},
        {"status": "broken", "page_path": "b.md"},
        {"status": "ok"},
    ]
    worker.update_wiki_frontmatter.side_effect = lambda page_path: page_path == "b.md"
    errors = []
    info_calls = []

    with patch("core.hephaestus.link_probe_worker.LinkProbeWorker", return_value=worker):
        result = link_probe.run_service(
            {"features.enable_link_probe": True, "daemon.services.link_probe": True},
            log_service_error=lambda service_name, exc: errors.append((service_name, exc)),
            log_info=lambda *args: info_calls.append(args),
        )

    assert result == {"enabled": True, "probed": 4, "broken": 2, "updated": 1, "errors": 0}
    assert errors == []
    assert len(info_calls) == 1
    worker.probe_batch.assert_called_once_with(batch_size=50)
    assert worker.update_wiki_frontmatter.call_count == 3


def test_run_service_records_worker_errors():
    errors = []

    with patch("core.hephaestus.link_probe_worker.LinkProbeWorker", side_effect=RuntimeError("boom")):
        result = link_probe.run_service(
            {"features.enable_link_probe": True, "daemon.services.link_probe": True},
            log_service_error=lambda service_name, exc: errors.append((service_name, str(exc))),
        )

    assert result == {"enabled": True, "probed": 0, "broken": 0, "updated": 0, "errors": 1}
    assert errors == [("link_probe", "boom")]
