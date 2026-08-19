"""Schema authority for cognition-episode terminal EventBus receipts."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

SCHEMA_VERSION = "mnemos.cognition_episode_event_delivery.v1"
INDEX_NAME = "uq_cognition_episode_terminal_handler_receipt"
_CREATE_INDEX = f"""CREATE UNIQUE INDEX {INDEX_NAME}
ON handler_receipts(trace_id, consumer)
WHERE event_type='cognition_episode_committed'
  AND disposition IN ('ack','noop')"""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def cognition_episode_event_schema_gaps(conn: sqlite3.Connection) -> list[str]:
    if "handler_receipts" not in _tables(conn):
        return ["missing_base_table:handler_receipts"]
    index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (INDEX_NAME,),
    ).fetchone()
    gaps: list[str] = []
    if index is None or not str(index[0] or "").strip():
        gaps.append(f"missing_unique_contract:{INDEX_NAME}")
    else:
        index_columns = tuple(
            str(row[2]) for row in conn.execute(f"PRAGMA index_info({INDEX_NAME})")  # nosec B608
        )
        normalized_sql = "".join(str(index[0]).lower().split())
        if (
            index_columns != ("trace_id", "consumer")
            or "createuniqueindex" not in normalized_sql
            or "event_type='cognition_episode_committed'" not in normalized_sql
            or "dispositionin('ack','noop')" not in normalized_sql
        ):
            gaps.append(f"invalid_unique_contract:{INDEX_NAME}")
    duplicates = int(conn.execute("""SELECT COUNT(*) FROM (
                   SELECT trace_id, consumer
                   FROM handler_receipts
                   WHERE event_type='cognition_episode_committed'
                     AND disposition IN ('ack','noop')
                   GROUP BY trace_id, consumer HAVING COUNT(*) != 1
               )""").fetchone()[0])
    if duplicates:
        gaps.append(f"duplicate_terminal_receipts:{duplicates}")
    return gaps


def validate_cognition_episode_event_schema(conn: sqlite3.Connection) -> None:
    gaps = cognition_episode_event_schema_gaps(conn)
    if gaps:
        raise RuntimeError(
            "cognition episode EventBus schema requires explicit reconciliation: " + ", ".join(gaps)
        )


def initialize_cognition_episode_event_schema_in_conn(conn: sqlite3.Connection) -> None:
    """Install the unique terminal-receipt contract in the caller transaction."""

    if "handler_receipts" not in _tables(conn):
        raise RuntimeError("EventBus handler receipt base schema must be initialized first")
    duplicates = conn.execute("""SELECT trace_id, consumer, COUNT(*) AS count
           FROM handler_receipts
           WHERE event_type='cognition_episode_committed'
             AND disposition IN ('ack','noop')
           GROUP BY trace_id, consumer HAVING COUNT(*) > 1
           ORDER BY trace_id, consumer LIMIT 1""").fetchone()
    if duplicates is not None:
        raise RuntimeError(
            "duplicate cognition episode terminal receipts require explicit classification"
        )
    conn.execute(_CREATE_INDEX.replace("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS"))
    validate_cognition_episode_event_schema(conn)


def initialize_cognition_episode_event_schema(db_path: Path) -> None:
    path = Path(db_path).expanduser().resolve(strict=False)
    if not path.is_file():
        raise RuntimeError("EventBus database must be initialized before reconciliation")
    with sqlite3.connect(str(path), timeout=60) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            initialize_cognition_episode_event_schema_in_conn(conn)
            conn.commit()
        except (RuntimeError, sqlite3.Error):
            conn.rollback()
            raise


def inspect_cognition_episode_event_schema(db_path: Path) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve(strict=False)
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "path": str(path),
            "ok": False,
            "gaps": ["database_missing"],
        }
    try:
        with sqlite3.connect(f"file:{path.resolve(strict=True)}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            gaps = cognition_episode_event_schema_gaps(conn)
    except (OSError, sqlite3.Error) as exc:
        gaps = [f"schema_read_error:{type(exc).__name__}:{exc}"]
    return {
        "schema_version": SCHEMA_VERSION,
        "path": str(path),
        "ok": not gaps,
        "gaps": gaps,
    }
