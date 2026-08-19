# -*- coding: utf-8 -*-
"""Distillation runtime quality metrics."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from core.hephaestus.distillation_json import (
    JSON_PARSE_BALANCED,
    JSON_PARSE_DIRECT,
    JSON_PARSE_FAILED,
    JSON_PARSE_FIXED,
    JSON_PARSE_MARKDOWN,
    JsonExtractionResult,
)

DISTILL_JSON_QUALITY_SCHEMA_VERSION = "mnemos.distill_json_quality.v1"
DISTILL_JSON_PARSE_PATHS = {
    JSON_PARSE_DIRECT,
    JSON_PARSE_MARKDOWN,
    JSON_PARSE_BALANCED,
    JSON_PARSE_FIXED,
    JSON_PARSE_FAILED,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metrics_db_path(database_dir: Path) -> Path:
    return Path(database_dir) / "distill_metrics.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(db_path), timeout=5)


def ensure_distill_metrics_schema(database_dir: Path) -> Path:
    """Create the distillation metrics schema if needed."""
    db_path = _metrics_db_path(database_dir)
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS distill_json_parse_events (
                event_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL,
                parse_path TEXT NOT NULL,
                fallback_used INTEGER NOT NULL,
                correction_attempts INTEGER NOT NULL DEFAULT 0,
                raw_length INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                error_class TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_distill_json_parse_events_created_at
            ON distill_json_parse_events(created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_distill_json_parse_events_path
            ON distill_json_parse_events(parse_path)
            """
        )
    return db_path


def record_json_parse_event(
    database_dir: Path,
    result: JsonExtractionResult,
    *,
    session_id: str = "",
    source: str = "",
    provider: str = "",
    model: str = "",
) -> str:
    """Persist a redacted JSON extraction event for quality trend reporting."""
    db_path = ensure_distill_metrics_schema(database_dir)
    event_id = f"distill-json-{uuid.uuid4().hex}"
    payload = result.as_dict()
    with sqlite3.connect(str(db_path), timeout=5) as conn:
        conn.execute(
            """
            INSERT INTO distill_json_parse_events (
                event_id, created_at, session_id, source, provider, model,
                success, parse_path, fallback_used, correction_attempts,
                raw_length, attempt_count, error_class, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                _now_iso(),
                session_id,
                source,
                provider,
                model,
                1 if result.success else 0,
                result.path,
                1 if result.fallback_used else 0,
                int(result.correction_attempts),
                int(result.raw_length),
                int(payload["attempt_count"]),
                str(payload["error_class"])[:120],
                str(payload["error_message"])[:500],
            ),
        )
    return event_id


def _safe_rate(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(part / total, 4)


def _empty_summary(database_dir: Path, status: str = "skipped") -> Dict[str, Any]:
    return {
        "schema_version": DISTILL_JSON_QUALITY_SCHEMA_VERSION,
        "status": status,
        "db_path": str(_metrics_db_path(database_dir)),
        "window_days": 0,
        "total_events": 0,
        "success": 0,
        "failed": 0,
        "direct_json_success": 0,
        "fallback_success": 0,
        "fixed_json_success": 0,
        "correction_attempts": 0,
        "by_parse_path": {path: 0 for path in sorted(DISTILL_JSON_PARSE_PATHS)},
        "rates": {
            "direct_success_rate": 0.0,
            "fallback_success_rate": 0.0,
            "final_failure_rate": 0.0,
            "fixed_success_rate": 0.0,
        },
        "trend": {
            "last_24h": {"total": 0, "failed": 0, "failure_rate": 0.0},
            "previous_24h": {"total": 0, "failed": 0, "failure_rate": 0.0},
            "direction": "insufficient_data",
        },
    }


def _window_counts(conn: sqlite3.Connection, since: datetime, until: datetime) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT COUNT(*), SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END)
        FROM distill_json_parse_events
        WHERE created_at >= ? AND created_at < ?
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone()
    total = int(rows[0] or 0) if rows else 0
    failed = int(rows[1] or 0) if rows else 0
    return {
        "total": total,
        "failed": failed,
        "failure_rate": _safe_rate(failed, total),
    }


def summarize_json_parse_metrics(
    database_dir: Path,
    *,
    window_days: int = 7,
    warning_failure_rate: float = 0.1,
) -> Dict[str, Any]:
    """Return aggregate JSON parse quality metrics for health/dashboard output."""
    db_path = _metrics_db_path(database_dir)
    if not db_path.exists():
        summary = _empty_summary(database_dir)
        summary["window_days"] = window_days
        return summary

    ensure_distill_metrics_schema(database_dir)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=window_days)
    try:
        with sqlite3.connect(str(db_path), timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT parse_path, success, fallback_used, correction_attempts, COUNT(*) AS count
                FROM distill_json_parse_events
                WHERE created_at >= ?
                GROUP BY parse_path, success, fallback_used, correction_attempts
                """,
                (since.isoformat(),),
            ).fetchall()
            by_path = {path: 0 for path in sorted(DISTILL_JSON_PARSE_PATHS)}
            total = 0
            success = 0
            failed = 0
            direct_success = 0
            fallback_success = 0
            fixed_success = 0
            correction_attempts = 0
            for row in rows:
                count = int(row["count"])
                path = str(row["parse_path"])
                by_path[path] = by_path.get(path, 0) + count
                total += count
                if int(row["success"]):
                    success += count
                    if path == JSON_PARSE_DIRECT:
                        direct_success += count
                    if int(row["fallback_used"]):
                        fallback_success += count
                    if path == JSON_PARSE_FIXED:
                        fixed_success += count
                else:
                    failed += count
                correction_attempts += int(row["correction_attempts"]) * count

            last_24h = _window_counts(conn, now - timedelta(hours=24), now)
            previous_24h = _window_counts(
                conn,
                now - timedelta(hours=48),
                now - timedelta(hours=24),
            )
    except sqlite3.Error as exc:
        summary = _empty_summary(database_dir, status="degraded")
        summary["window_days"] = window_days
        summary["error"] = f"distill metrics unreadable: {exc}"
        return summary

    if total == 0:
        summary = _empty_summary(database_dir)
        summary["window_days"] = window_days
        return summary

    final_failure_rate = _safe_rate(failed, total)
    if last_24h["total"] < 2 or previous_24h["total"] < 2:
        direction = "insufficient_data"
    elif last_24h["failure_rate"] > previous_24h["failure_rate"]:
        direction = "worse"
    elif last_24h["failure_rate"] < previous_24h["failure_rate"]:
        direction = "better"
    else:
        direction = "stable"

    status = "warning" if final_failure_rate > warning_failure_rate else "ok"
    summary = {
        "schema_version": DISTILL_JSON_QUALITY_SCHEMA_VERSION,
        "status": status,
        "db_path": str(db_path),
        "window_days": window_days,
        "total_events": total,
        "success": success,
        "failed": failed,
        "direct_json_success": direct_success,
        "fallback_success": fallback_success,
        "fixed_json_success": fixed_success,
        "correction_attempts": correction_attempts,
        "by_parse_path": by_path,
        "rates": {
            "direct_success_rate": _safe_rate(direct_success, total),
            "fallback_success_rate": _safe_rate(fallback_success, total),
            "final_failure_rate": final_failure_rate,
            "fixed_success_rate": _safe_rate(fixed_success, total),
        },
        "trend": {
            "last_24h": last_24h,
            "previous_24h": previous_24h,
            "direction": direction,
        },
    }
    if status == "warning":
        summary["warnings"] = [
            f"distill JSON final failure rate {final_failure_rate:.1%} exceeds {warning_failure_rate:.1%}"
        ]
    return summary
