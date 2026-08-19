"""Small SQL/time primitives shared by cognitive schema authorities."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from core.db_utils import render_sql


def utc_now() -> str:
    """Return one timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def table_row_count(conn: sqlite3.Connection, table: str) -> int:
    """Count rows in a caller-selected, safely rendered table identifier."""
    return int(
        conn.execute(
            render_sql(
                "SELECT COUNT(*) FROM {table}",
                identifiers={"table": table},
            )
        ).fetchone()[0]
    )
