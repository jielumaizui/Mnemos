# -*- coding: utf-8 -*-
"""Heartbeat snapshot helpers for the Mnemos daemon."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

from daemon.instance_identity import HEARTBEAT_SCHEMA_VERSION

logger = logging.getLogger("mnemos.daemon")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_historical_error(summary: Dict[str, Any], error_state: Dict[str, Any]) -> bool:
    if summary.get("last_ok") is not True:
        return False
    last_run_at = _parse_iso(summary.get("last_run_at"))
    last_error_at = _parse_iso(error_state.get("last_error_at"))
    if last_run_at is None or last_error_at is None:
        return False
    if bool(last_run_at.tzinfo) != bool(last_error_at.tzinfo):
        last_run_at = last_run_at.replace(tzinfo=None)
        last_error_at = last_error_at.replace(tzinfo=None)
    return last_run_at >= last_error_at


def write_daemon_heartbeat(
    heartbeat_file: Path,
    snapshot: Dict[str, Any],
    *,
    log: logging.Logger | None = None,
) -> None:
    """Persist the latest daemon heartbeat for out-of-process health checks."""
    log = log or logger
    try:
        import json

        from core.utils import atomic_write_text

        heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            heartbeat_file,
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(heartbeat_file, 0o600)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        log.warning("写入 daemon 心跳文件失败", exc_info=True)


def build_heartbeat_snapshot(
    *,
    instance_identity: Mapping[str, Any],
    intervals: Dict[str, int],
    service_results: Dict[str, Dict[str, Any]],
    service_error_state: Dict[str, Dict[str, Any]],
    cfg: Any,
    service_enabled: Callable[[Any, str], bool],
    module_health: Dict[str, Dict[str, Any]] | None = None,
    persisted_source_coverage: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build the daemon heartbeat payload shown by health/status checks."""
    services: Dict[str, Any] = {}
    for name in intervals:
        last = service_results.get(name)
        if last:
            result = last.get("result", {})
            summary = {
                "enabled": result.get("enabled", service_enabled(cfg, name)),
                "last_run_at": last.get("at"),
                "last_ok": last.get("ok"),
            }
            if not last.get("ok", True):
                summary["last_error"] = last.get("error")

            def _is_primitive(value: Any) -> bool:
                """仅保留可安全序列化的标量指标，避免嵌套结构递归膨胀。"""
                return value is None or isinstance(value, (bool, int, float, str))

            metric_keys = [
                key
                for key in result
                if key not in ("enabled", "errors")
                and not key.startswith("_")
                and _is_primitive(result[key])
            ]
            summary["metrics"] = {key: result[key] for key in metric_keys[:3]}
            if result.get("errors", 0):
                summary["errors"] = result["errors"]
            source_coverage = result.get("source_coverage")
            if isinstance(source_coverage, Mapping):
                from daemon.agent_source_coverage import source_coverage_for_heartbeat

                projected_coverage = source_coverage_for_heartbeat(source_coverage)
                if projected_coverage:
                    summary["source_coverage"] = projected_coverage
        else:
            summary = {"enabled": service_enabled(cfg, name)}

        error_state = service_error_state.get(name)
        if error_state:
            historical = _is_historical_error(summary, error_state)
            summary["error_count"] = error_state.get("count", 0)
            summary["last_error"] = error_state.get("last_error", "")
            summary["last_error_type"] = error_state.get("last_error_type", "")
            summary["last_error_at"] = error_state.get("last_error_at", "")
            summary["last_error_context"] = error_state.get("last_context", name)
            summary["error_state"] = "historical" if historical else "current"
            summary["error_active"] = not historical
            if historical:
                summary["last_recovered_at"] = summary.get("last_run_at")
                summary["note"] = "last error is historical; a later service run succeeded"
        services[name] = summary

    if persisted_source_coverage is not None:
        from daemon.agent_source_coverage import source_coverage_for_heartbeat

        projected_coverage = source_coverage_for_heartbeat(persisted_source_coverage)
        if projected_coverage:
            raw_sync_summary = services.setdefault(
                "raw_sync",
                {"enabled": service_enabled(cfg, "raw_sync")},
            )
            raw_sync_summary.setdefault("source_coverage", projected_coverage)

    snapshot = {
        "schema_version": HEARTBEAT_SCHEMA_VERSION,
        "timestamp": datetime.now().isoformat(),
        "instance_identity": dict(instance_identity),
        "services": services,
        "service_errors": dict(service_error_state),
    }
    if module_health is not None:
        snapshot["modules"] = module_health
    return snapshot
