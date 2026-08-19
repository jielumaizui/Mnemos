"""Coupled v4→v5 stage-receipt schema rebuild implementation."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from core.cognitive.state_schema_ddl import (
    CANONICAL_TABLES,
    REGISTRY_TABLE,
    SCHEMA_COMPONENT,
)


def upgrade_stage_receipt_schema(
    conn: sqlite3.Connection,
    *,
    error_type: type[RuntimeError],
    inspect_schema: Callable[[sqlite3.Connection], Any],
    table_row_count: Callable[[sqlite3.Connection, str], int],
    drop_historical_objects: Callable[[sqlite3.Connection, tuple[str, ...]], None],
    execute_canonical_ddl: Callable[[sqlite3.Connection], None],
    write_registry_row: Callable[[sqlite3.Connection], None],
) -> dict[str, int]:
    """Rebuild exact canonical v4 plus its coupled search projection."""
    if not conn.in_transaction:
        raise error_type(
            "stage-receipt schema upgrade requires a caller-owned transaction"
        )
    before = inspect_schema(conn)
    if before.classification != "canonical_v4_stage_receipt_upgrade_required":
        raise error_type(
            "stage-receipt schema upgrade source is not exact canonical v4"
        )
    source_counts = {
        table: table_row_count(conn, table)
        for table in CANONICAL_TABLES
    }
    from core.cognitive.search_state_headers import (
        detach_state_search_headers_for_canonical_rebuild,
        restore_state_search_headers_after_canonical_rebuild,
    )

    search_snapshot = detach_state_search_headers_for_canonical_rebuild(conn)
    legacy_names: dict[str, str] = {}
    for table in CANONICAL_TABLES:
        legacy = f"__stage_v4__{table}"
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')  # nosec B608
        legacy_names[table] = legacy
    drop_historical_objects(conn, tuple(legacy_names.values()))
    execute_canonical_ddl(conn)

    for table in tuple(item for item in CANONICAL_TABLES if item != REGISTRY_TABLE):
        legacy = legacy_names[table]
        source_columns = tuple(
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{legacy}")').fetchall()
        )
        target_columns = tuple(
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        )
        if source_columns != target_columns:
            raise error_type(f"stage-receipt schema upgrade column drift: {table}")
        column_sql = ", ".join(f'"{column}"' for column in source_columns)
        conn.execute(
            f'INSERT INTO "{table}" ({column_sql}) '  # nosec B608
            f'SELECT {column_sql} FROM "{legacy}"'  # nosec B608
        )

    legacy_registry = legacy_names[REGISTRY_TABLE]
    conn.execute(
        f"""
        INSERT INTO {REGISTRY_TABLE}(
            component, schema_version, ddl_hash, applied_at
        )
        SELECT component, schema_version, ddl_hash, applied_at
        FROM "{legacy_registry}"
        WHERE component != ?
        """,  # nosec B608
        (SCHEMA_COMPONENT,),
    )
    write_registry_row(conn)
    for legacy in reversed(tuple(legacy_names.values())):
        conn.execute(f'DROP TABLE "{legacy}"')  # nosec B608
    search_counts = restore_state_search_headers_after_canonical_rebuild(
        conn,
        search_snapshot,
    )
    for table, count in source_counts.items():
        if table_row_count(conn, table) != count:
            raise error_type(
                f"stage-receipt schema upgrade row-count drift: {table}"
            )
    if not inspect_schema(conn).ok:
        raise error_type(
            "stage-receipt schema upgrade did not produce canonical v5"
        )
    return {
        **source_counts,
        **{f"search_projection:{key}": value for key, value in search_counts.items()},
    }
