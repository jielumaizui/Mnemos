# -*- coding: utf-8 -*-
"""Tests for daemon.heartbeat helpers."""

from __future__ import annotations

import json
import stat

from daemon import heartbeat


def test_build_heartbeat_snapshot_summarizes_results_and_errors():
    snapshot = heartbeat.build_heartbeat_snapshot(
        instance_identity={"instance_id": "instance-1"},
        intervals={"signal_collector": 300, "link_probe": 60},
        service_results={
            "signal_collector": {
                "at": "2026-01-01T00:00:00",
                "ok": True,
                "result": {"enabled": True, "collected": 42, "errors": 1},
            }
        },
        service_error_state={
            "link_probe": {
                "count": 2,
                "last_error": "timeout",
                "last_error_type": "TimeoutError",
                "last_error_at": "2026-01-01T00:00:01",
                "last_context": "link_probe",
            }
        },
        cfg=object(),
        service_enabled=lambda cfg, name: name != "link_probe",
    )

    assert "timestamp" in snapshot
    assert snapshot["schema_version"] == "mnemos.daemon_heartbeat.v3"
    assert snapshot["instance_identity"] == {"instance_id": "instance-1"}
    assert snapshot["services"]["signal_collector"]["metrics"] == {"collected": 42}
    assert snapshot["services"]["signal_collector"]["errors"] == 1
    assert snapshot["services"]["link_probe"]["enabled"] is False
    assert snapshot["services"]["link_probe"]["error_count"] == 2
    assert snapshot["services"]["link_probe"]["error_state"] == "current"
    assert snapshot["services"]["link_probe"]["error_active"] is True
    assert snapshot["service_errors"]["link_probe"]["last_error"] == "timeout"


def test_build_heartbeat_snapshot_marks_recovered_error_historical():
    snapshot = heartbeat.build_heartbeat_snapshot(
        instance_identity={"instance_id": "instance-1"},
        intervals={"raw_projection": 300},
        service_results={
            "raw_projection": {
                "at": "2026-01-01T00:00:10",
                "ok": True,
                "result": {"enabled": True, "status": "skipped", "reason": "up_to_date"},
            }
        },
        service_error_state={
            "raw_projection": {
                "count": 4,
                "last_error": "database is locked",
                "last_error_type": "OperationalError",
                "last_error_at": "2026-01-01T00:00:00",
                "last_context": "raw_projection",
            }
        },
        cfg=object(),
        service_enabled=lambda cfg, name: True,
    )

    raw_projection = snapshot["services"]["raw_projection"]
    assert raw_projection["error_count"] == 4
    assert raw_projection["error_state"] == "historical"
    assert raw_projection["error_active"] is False
    assert raw_projection["last_recovered_at"] == "2026-01-01T00:00:10"


def test_build_heartbeat_snapshot_includes_module_health():
    module_health = {
        "eris": {
            "state": "running",
            "enabled": True,
            "dependencies": ["genos"],
        }
    }

    snapshot = heartbeat.build_heartbeat_snapshot(
        instance_identity={"instance_id": "instance-1"},
        intervals={},
        service_results={},
        service_error_state={},
        cfg=object(),
        service_enabled=lambda cfg, name: True,
        module_health=module_health,
    )

    assert snapshot["modules"] == module_health


def test_write_daemon_heartbeat(tmp_path):
    heartbeat_file = tmp_path / "daemon_heartbeat.json"

    heartbeat.write_daemon_heartbeat(heartbeat_file, {"ok": True})

    assert json.loads(heartbeat_file.read_text(encoding="utf-8")) == {"ok": True}
    assert stat.S_IMODE(heartbeat_file.stat().st_mode) == 0o600


def test_build_heartbeat_snapshot_avoids_recursive_nesting():
    """心跳服务自身的 result 不应被递归嵌套进 metrics，防止心跳文件指数膨胀。"""
    previous_snapshot = {
        "timestamp": "2026-01-01T00:00:00",
        "services": {
            "heartbeat": {
                "enabled": True,
                "last_run_at": "2026-01-01T00:00:00",
                "last_ok": True,
                "metrics": {"timestamp": "2026-01-01T00:00:00"},
            }
        },
        "service_errors": {},
    }
    snapshot = heartbeat.build_heartbeat_snapshot(
        instance_identity={"instance_id": "instance-1"},
        intervals={"heartbeat": 60},
        service_results={
            "heartbeat": {
                "at": "2026-01-01T00:00:00",
                "ok": True,
                "result": previous_snapshot,
            }
        },
        service_error_state={},
        cfg=object(),
        service_enabled=lambda cfg, name: True,
    )

    # 必须能被正常 JSON 序列化，不会递归爆炸
    serialized = json.dumps(snapshot)
    parsed = json.loads(serialized)
    heartbeat_metrics = parsed["services"]["heartbeat"]["metrics"]
    # 嵌套 dict 不应进入 metrics
    assert "services" not in heartbeat_metrics
    assert "service_errors" not in heartbeat_metrics
    # 标量字段仍保留
    assert heartbeat_metrics.get("timestamp") == "2026-01-01T00:00:00"
