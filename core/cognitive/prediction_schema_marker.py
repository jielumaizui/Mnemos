"""Canonical prediction-enforcement schema marker operations."""

from __future__ import annotations

import sqlite3
from typing import Callable

from core.cognitive.state_schema_ddl import (
    PREDICTION_ENFORCEMENT_COMPONENT,
    PREDICTION_ENFORCEMENT_HASH,
    PREDICTION_ENFORCEMENT_VERSION,
    REGISTRY_TABLE,
)


def write_prediction_marker(
    conn: sqlite3.Connection,
    *,
    applied_at: str,
    error_type: type[RuntimeError],
) -> None:
    """Insert or verify the canonical prediction-enforcement registry marker."""
    expected = (PREDICTION_ENFORCEMENT_VERSION, PREDICTION_ENFORCEMENT_HASH)
    existing = conn.execute(
        f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
        (PREDICTION_ENFORCEMENT_COMPONENT,),
    ).fetchone()
    if existing is None:
        conn.execute(
            f"INSERT INTO {REGISTRY_TABLE}(component, schema_version, ddl_hash, applied_at) "  # nosec B608
            "VALUES (?, ?, ?, ?)",
            (PREDICTION_ENFORCEMENT_COMPONENT, *expected, applied_at),
        )
    elif tuple(str(value) for value in existing) != expected:
        raise error_type(
            "prediction enforcement marker conflicts with the canonical contract"
        )


def prediction_marker_enabled(
    conn: sqlite3.Connection,
    *,
    table_names: Callable[[sqlite3.Connection], tuple[str, ...]],
) -> bool:
    """Return whether the exact canonical prediction marker is registered."""
    if REGISTRY_TABLE not in table_names(conn):
        return False
    row = conn.execute(
        f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
        (PREDICTION_ENFORCEMENT_COMPONENT,),
    ).fetchone()
    return row is not None and tuple(str(value) for value in row) == (
        PREDICTION_ENFORCEMENT_VERSION,
        PREDICTION_ENFORCEMENT_HASH,
    )
