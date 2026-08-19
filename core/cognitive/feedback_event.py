# -*- coding: utf-8 -*-
"""Read-only compatibility view for pre-COG-038 feedback event rows."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from core.config import get_config


SCHEMA_VERSION = "mnemos.feedback_event.legacy_read_only.v1"
LEGACY_WRITE_RETIRED = "legacy_feedback_event_write_retired"


class FeedbackEventLedger:
    """Inspect historical rows without retaining a second feedback owner."""

    def __init__(self, db_path: Path | None = None, *, ensure_db: bool = False):
        del ensure_db
        configured = db_path or Path(get_config().database_dir) / "delivery_events.db"
        self.db_path = Path(configured).expanduser()

    def begin_feedback(self, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(LEGACY_WRITE_RETIRED)

    def claim_consumer(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(LEGACY_WRITE_RETIRED)

    def complete_consumer(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(LEGACY_WRITE_RETIRED)

    def fail_consumer(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(LEGACY_WRITE_RETIRED)

    def finalize(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(LEGACY_WRITE_RETIRED)

    def get_feedback(self, feedback_event_id: str) -> dict[str, Any]:
        if not self.db_path.is_file():
            return {}
        with self._connect() as conn:
            if not _table_exists(conn, "feedback_events"):
                return {}
            row = conn.execute(
                "SELECT * FROM feedback_events WHERE feedback_event_id=?",
                (str(feedback_event_id or "").strip(),),
            ).fetchone()
        return _feedback_row(row) if row else {}

    def list_receipts(self, feedback_event_id: str) -> list[dict[str, Any]]:
        if not self.db_path.is_file():
            return []
        with self._connect() as conn:
            if not _table_exists(conn, "feedback_receipts"):
                return []
            rows = conn.execute(
                """
                SELECT * FROM feedback_receipts
                WHERE feedback_event_id=? ORDER BY consumer
                """,
                (str(feedback_event_id or "").strip(),),
            ).fetchall()
        return [_receipt_row(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        )
        conn.row_factory = sqlite3.Row
        return conn


def _feedback_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["required_consumers"] = _json_list(
        result.pop("required_consumers_json", "[]")
    )
    result["metadata"] = _json_mapping(result.pop("metadata_json", "{}"))
    return result


def _receipt_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["receipt"] = _json_mapping(result.pop("receipt_json", "{}"))
    return result


def _json_list(value: str) -> list[str]:
    try:
        loaded = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def _json_mapping(value: str) -> dict[str, Any]:
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None
