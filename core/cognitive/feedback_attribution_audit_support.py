"""SQLite and finding primitives for feedback attribution audits."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any


def _revision(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = _json(result.pop("payload_json"), default={})
    result["is_head"] = bool(result["is_head"])
    return result


def _json(value: Any, *, default: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve(strict=True)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _finding(code: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": "blocking",
        "message": str(message),
        "repair_action": f"repair {code} and rerun strict audit",
    }
