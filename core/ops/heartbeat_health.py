"""Public, content-free projection of the daemon heartbeat for health checks.

Daemon state deliberately retains local recovery detail.  The operational
health report is a separate shareable surface, so this module owns its fixed
allowlist projection and instance-identity verification.  The caller supplies
the small report-construction seam, keeping health aggregation independent
from daemon-file parsing.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def read_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def heartbeat_service_error_active(service: dict[str, Any]) -> bool:
    if not service.get("last_error"):
        return False
    if "error_active" in service:
        return bool(service.get("error_active"))
    last_run_at = read_iso_datetime(service.get("last_run_at"))
    last_error_at = read_iso_datetime(service.get("last_error_at"))
    if (
        service.get("last_ok") is True
        and last_run_at is not None
        and last_error_at is not None
    ):
        if bool(last_run_at.tzinfo) != bool(last_error_at.tzinfo):
            last_run_at = last_run_at.replace(tzinfo=None)
            last_error_at = last_error_at.replace(tzinfo=None)
        return last_run_at < last_error_at
    return True


def safe_heartbeat_timestamp(value: Any) -> str:
    """Keep only a valid heartbeat timestamp, never arbitrary diagnostic text."""
    return str(value) if read_iso_datetime(value) is not None else ""


def safe_heartbeat_count(value: Any) -> int:
    """Return a bounded public counter from an untrusted heartbeat payload."""
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def project_heartbeat_service(service: Any) -> dict[str, Any]:
    """Project one daemon service to fixed, content-free health evidence."""
    source = service if isinstance(service, dict) else {}
    has_error = bool(source.get("last_error"))
    error_active = heartbeat_service_error_active(source) if has_error else False
    result: dict[str, Any] = {
        "enabled": bool(source.get("enabled", False)),
        "ok": source.get("ok") if isinstance(source.get("ok"), bool) else None,
        "last_ok": (
            source.get("last_ok")
            if isinstance(source.get("last_ok"), bool)
            else None
        ),
        "last_run_at": safe_heartbeat_timestamp(source.get("last_run_at")),
        "error_count": safe_heartbeat_count(source.get("error_count")),
        "error_state": (
            "current"
            if has_error and error_active
            else "historical"
            if has_error
            else "none"
        ),
        "error_active": bool(has_error and error_active),
        "last_error_at": safe_heartbeat_timestamp(source.get("last_error_at")),
        "last_recovered_at": safe_heartbeat_timestamp(source.get("last_recovered_at")),
    }
    if has_error:
        # Do not infer categories from raw exception text or class names:
        # either can be caller-controlled.
        result["error_category"] = "daemon_service_error"
    return result


def expected_heartbeat_service_ids() -> frozenset[str]:
    """Load only daemon-owned service identifiers permitted in health."""
    try:
        from daemon import intervals

        return frozenset(intervals.build_default_intervals(capture_tick=300))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
    ):
        # If the static registry cannot load, health can report a count but
        # must not echo arbitrary durable JSON keys.
        return frozenset()


def project_heartbeat_services(
    services: dict[str, Any],
    *,
    allowed_service_ids: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """Return public projections only for fixed daemon-owned service IDs."""
    return {
        str(name): project_heartbeat_service(service)
        for name, service in services.items()
        if str(name) in allowed_service_ids
    }


def verify_heartbeat_identity(
    payload: dict[str, Any],
    config: Any,
    services: dict[str, Any],
) -> dict[str, Any]:
    """Cross-check heartbeat, PID file, process, and current runtime context."""
    from daemon import instance_identity, intervals, process_control

    if payload.get("schema_version") != instance_identity.HEARTBEAT_SCHEMA_VERSION:
        return {"ok": False, "reason": "unsupported_heartbeat_schema", "identity_match": False}
    heartbeat_identity = payload.get("instance_identity")
    if not isinstance(heartbeat_identity, dict):
        return {"ok": False, "reason": "missing_instance_identity", "identity_match": False}

    expected_services = intervals.build_default_intervals(capture_tick=300)
    expected_manifest = sorted(expected_services)
    if sorted(services) != expected_manifest:
        return {
            "ok": False,
            "reason": "heartbeat_service_set_mismatch",
            "identity_match": False,
            "expected_service_count": len(expected_manifest),
            "actual_service_count": len(services),
            "missing_service_count": len(set(expected_manifest) - set(services)),
            "unexpected_service_count": len(set(services) - set(expected_manifest)),
        }

    pid_record = process_control.read_pid_record(config.database_dir / "daemon.pid")
    if not pid_record:
        return {"ok": False, "reason": "daemon_pid_record_missing", "identity_match": False}
    identity_fields = (
        "schema_version",
        "instance_id",
        "pid",
        "pid_start_time",
        "boot_id",
        "executable",
        "command_line_hash",
        "commit",
        "build_fingerprint",
        "config_hash",
        "config_fingerprint",
        "database_identity",
        "service_manifest",
        "service_manifest_hash",
        "python",
    )
    mismatched_fields = [
        key for key in identity_fields if heartbeat_identity.get(key) != pid_record.get(key)
    ]
    if mismatched_fields:
        return {
            "ok": False,
            "reason": "heartbeat_pid_identity_mismatch",
            "identity_match": False,
            "mismatched_fields": mismatched_fields,
        }

    verification = instance_identity.verify_instance_record(
        pid_record,
        database_dir=config.database_dir,
        service_names=expected_manifest,
        project_root=Path(__file__).resolve().parents[2],
        require_current_context=True,
    )
    result = verification.to_dict()
    if verification.ok:
        verification_details = dict(verification.details or {})
        result.update(
            {
                "commit": pid_record.get("commit"),
                "current_commit": verification_details.get("current_commit"),
                "commit_match": verification_details.get("commit_match"),
                "build_compatible": verification_details.get("build_compatible"),
                "build_fingerprint": pid_record.get("build_fingerprint"),
                "config_hash": pid_record.get("config_hash"),
                "config_fingerprint": pid_record.get("config_fingerprint"),
                "database_identity": pid_record.get("database_identity"),
                "service_manifest_hash": pid_record.get("service_manifest_hash"),
                "pid_start_time": pid_record.get("pid_start_time"),
            }
        )
    return result


def build_heartbeat_report(
    config: Any,
    *,
    item: Callable[..., dict[str, Any]],
    verify_identity: Callable[[dict[str, Any], Any, dict[str, Any]], dict[str, Any]],
    expected_service_ids: Callable[[], frozenset[str]],
    project_services: Callable[..., dict[str, dict[str, Any]]],
    service_error_active: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    """Read one heartbeat snapshot and return its public health projection."""
    heartbeat_file = config.database_dir / "daemon_heartbeat.json"
    stale_after = int(config.get("daemon.heartbeat_stale_seconds", 180) or 180)
    if not heartbeat_file.exists():
        return item(
            "degraded",
            running=False,
            heartbeat_file=str(heartbeat_file),
            error="daemon heartbeat file not found",
            note="Start the daemon or wait for the heartbeat service to run.",
        )

    try:
        payload = json.loads(heartbeat_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return item(
            "degraded",
            running=False,
            heartbeat_file=str(heartbeat_file),
            error="daemon heartbeat unreadable",
            error_category="daemon_heartbeat_unreadable",
        )
    if not isinstance(payload, dict):
        return item(
            "degraded",
            running=False,
            heartbeat_file=str(heartbeat_file),
            error="daemon heartbeat unreadable",
            error_category="daemon_heartbeat_unreadable",
        )

    timestamp = read_iso_datetime(payload.get("timestamp"))
    if timestamp is None:
        return item(
            "degraded",
            running=False,
            heartbeat_file=str(heartbeat_file),
            error="daemon heartbeat timestamp missing or invalid",
            error_category="daemon_heartbeat_timestamp_invalid",
            timestamp_present=bool(payload.get("timestamp")),
        )

    now = datetime.now(timestamp.tzinfo) if timestamp.tzinfo else datetime.now()
    age_seconds = max(0, int((now - timestamp).total_seconds()))
    services = payload.get("services") or {}
    if not isinstance(services, dict):
        services = {}
    allowed_service_ids = expected_service_ids()
    public_services = project_services(
        services,
        allowed_service_ids=allowed_service_ids,
    )
    unrecognized_service_count = len(services) - len(public_services)
    identity = verify_identity(payload, config, services)
    if not identity.get("ok"):
        return item(
            "degraded",
            running=False,
            heartbeat_file=str(heartbeat_file),
            timestamp=payload.get("timestamp"),
            age_seconds=age_seconds,
            stale_after_seconds=stale_after,
            services_count=len(services),
            services=public_services,
            unrecognized_service_count=unrecognized_service_count,
            source="daemon_heartbeat_file",
            identity_match=bool(identity.get("identity_match")),
            identity_reason=identity.get("reason"),
            identity=identity,
            error=f"daemon instance identity invalid: {identity.get('reason')}",
        )

    status = "ok" if age_seconds <= stale_after else "degraded"
    active_service_errors = {
        name: public_services[name]
        for name, service in services.items()
        if name in public_services
        and isinstance(service, dict)
        and service_error_active(service)
    }
    historical_service_errors = {
        name: public_services[name]
        for name, service in services.items()
        if name in public_services
        and isinstance(service, dict)
        and service.get("last_error")
        and not service_error_active(service)
    }
    details = {
        "running": status == "ok",
        "heartbeat_file": str(heartbeat_file),
        "timestamp": payload.get("timestamp"),
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after,
        "services_count": len(services),
        "services": public_services,
        "unrecognized_service_count": unrecognized_service_count,
        "source": "daemon_heartbeat_file",
        "identity_match": True,
        "instance_id": identity.get("instance_id"),
        "pid": identity.get("pid"),
        "pid_start_time": identity.get("pid_start_time"),
        "commit": identity.get("commit"),
        "current_commit": identity.get("current_commit"),
        "commit_match": identity.get("commit_match"),
        "build_compatible": identity.get("build_compatible"),
        "build_fingerprint": identity.get("build_fingerprint"),
        "config_hash": identity.get("config_hash"),
        "config_fingerprint": identity.get("config_fingerprint"),
        "database_identity": identity.get("database_identity"),
        "service_manifest_hash": identity.get("service_manifest_hash"),
        "active_service_errors": active_service_errors,
        "historical_service_errors": historical_service_errors,
    }
    if status != "ok":
        details["error"] = "daemon heartbeat stale"
    elif active_service_errors:
        status = "degraded"
        details["running"] = False
        details["error"] = "daemon services have current errors"
    return item(status, **details)
