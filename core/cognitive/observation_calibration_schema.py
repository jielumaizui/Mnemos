"""Schema authority for Observation-to-CalibrationRecord projection bindings."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any

from core.db_utils import render_sql


OBSERVATION_CALIBRATION_SCHEMA_VERSION = "mnemos.observation_calibration_binding.v2"
REGISTRY_TABLE = "mnemos_observation_schema_registry"
REGISTRY_COMPONENT = "observation_calibration_binding"

BASE_OBSERVATION_COLUMN_DDL = {
    "content_source": "TEXT DEFAULT 'unknown'",
    "user_intent_signal": "TEXT DEFAULT 'unknown'",
    "user_notes": "TEXT DEFAULT ''",
    "access_control": "TEXT NOT NULL DEFAULT ''",
}
BASE_OBSERVATION_COLUMN_SIGNATURES = {
    "content_source": ("TEXT", 0, "'unknown'", 0),
    "user_intent_signal": ("TEXT", 0, "'unknown'", 0),
    "user_notes": ("TEXT", 0, "''", 0),
    "access_control": ("TEXT", 1, "''", 0),
}
CALIBRATION_COLUMN_DDL = {
    "base_confidence": "REAL NOT NULL DEFAULT 1.0",
    "base_measurement_status": (
        "TEXT NOT NULL DEFAULT 'historical_unverified' "
        "CHECK(base_measurement_status IN ('verified', 'historical_unverified'))"
    ),
    "calibration_revision_id": "TEXT NOT NULL DEFAULT ''",
    "calibration_input_hash": "TEXT NOT NULL DEFAULT ''",
    "calibration_spec_hash": "TEXT NOT NULL DEFAULT ''",
    "calibration_record_hash": "TEXT NOT NULL DEFAULT ''",
    "source_span_ids": "TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(source_span_ids))",
}
CALIBRATION_COLUMN_SIGNATURES = {
    "base_confidence": ("REAL", 1, "1.0", 0),
    "base_measurement_status": ("TEXT", 1, "'historical_unverified'", 0),
    "calibration_revision_id": ("TEXT", 1, "''", 0),
    "calibration_input_hash": ("TEXT", 1, "''", 0),
    "calibration_spec_hash": ("TEXT", 1, "''", 0),
    "calibration_record_hash": ("TEXT", 1, "''", 0),
    "source_span_ids": ("TEXT", 1, "'[]'", 0),
}
CALIBRATION_INDEX_NAME = "idx_obs_calibration_revision"
CALIBRATION_INDEX_COLUMNS = ("calibration_revision_id",)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


OBSERVATION_CALIBRATION_DDL_HASH = "sha256:" + hashlib.sha256(
    _canonical_json(
        {
            "columns": CALIBRATION_COLUMN_DDL,
            "index": (
                "CREATE INDEX idx_obs_calibration_revision "
                "ON observations(calibration_revision_id)"
            ),
        }
    ).encode("utf-8")
).hexdigest()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
    )


def inspect_observation_calibration_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "observations"):
        return {
            "classification": "absent",
            "ok": False,
            "missing_base_columns": sorted(BASE_OBSERVATION_COLUMN_DDL),
            "base_column_mismatches": {},
            "missing_columns": sorted(CALIBRATION_COLUMN_DDL),
            "column_mismatches": {},
            "index_ok": False,
            "registry_ok": False,
            "partial_pointer_count": 0,
            "unbound_posterior_count": 0,
            "historical_unverified_base_count": 0,
            "calibrated_unverified_base_count": 0,
            "invalid_base_measurement_status_count": 0,
            "invalid_source_span_json_count": 0,
        }
    table_info = {
        str(row[1]): (
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
        )
        for row in conn.execute("PRAGMA table_info(observations)")
    }
    columns = set(table_info)
    missing_base = sorted(set(BASE_OBSERVATION_COLUMN_DDL) - columns)
    base_column_mismatches = {
        name: {
            "expected": list(expected),
            "actual": list(table_info[name]),
        }
        for name, expected in BASE_OBSERVATION_COLUMN_SIGNATURES.items()
        if name in table_info and table_info[name] != expected
    }
    missing = sorted(set(CALIBRATION_COLUMN_DDL) - columns)
    column_mismatches = {
        name: {
            "expected": list(expected),
            "actual": list(table_info[name]),
        }
        for name, expected in CALIBRATION_COLUMN_SIGNATURES.items()
        if name in table_info and table_info[name] != expected
    }
    index_rows = {
        str(row[1]): row for row in conn.execute("PRAGMA index_list(observations)")
    }
    index_row = index_rows.get(CALIBRATION_INDEX_NAME)
    index_columns = tuple(
        str(row[2])
        for row in conn.execute(f"PRAGMA index_info({CALIBRATION_INDEX_NAME})")
    ) if index_row is not None else ()
    index_ok = bool(
        index_row is not None
        and int(index_row[2]) == 0
        and index_columns == CALIBRATION_INDEX_COLUMNS
    )
    registry = None
    if _table_exists(conn, REGISTRY_TABLE):
        registry_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({REGISTRY_TABLE})")
        }
        if {"component", "schema_version", "ddl_hash"}.issubset(registry_columns):
            registry = conn.execute(
                render_sql(
                    "SELECT schema_version, ddl_hash FROM {table} WHERE component=?",
                    identifiers={"table": REGISTRY_TABLE},
                ),
                (REGISTRY_COMPONENT,),
            ).fetchone()
    registry_ok = bool(
        registry
        and str(registry[0]) == OBSERVATION_CALIBRATION_SCHEMA_VERSION
        and str(registry[1]) == OBSERVATION_CALIBRATION_DDL_HASH
    )
    partial_pointer_count = 0
    unbound_posterior_count = 0
    historical_unverified_base_count = 0
    calibrated_unverified_base_count = 0
    invalid_base_measurement_status_count = 0
    invalid_source_span_json_count = 0
    if (
        not missing_base
        and not base_column_mismatches
        and not missing
        and not column_mismatches
    ):
        partial_pointer_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM observations
                WHERE ((calibration_revision_id != '')
                     + (calibration_input_hash != '')
                     + (calibration_spec_hash != '')
                     + (calibration_record_hash != '')) NOT IN (0, 4)
                """
            ).fetchone()[0]
        )
        unbound_posterior_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM observations
                WHERE calibration_revision_id='' AND confidence != base_confidence
                """
            ).fetchone()[0]
        )
        historical_unverified_base_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM observations
                WHERE base_measurement_status='historical_unverified'
                """
            ).fetchone()[0]
        )
        calibrated_unverified_base_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM observations
                WHERE base_measurement_status!='verified'
                  AND calibration_revision_id!=''
                """
            ).fetchone()[0]
        )
        invalid_base_measurement_status_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM observations
                WHERE base_measurement_status NOT IN (
                    'verified', 'historical_unverified'
                )
                """
            ).fetchone()[0]
        )
        invalid_source_span_json_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM observations
                WHERE CASE
                    WHEN NOT json_valid(source_span_ids) THEN 1
                    WHEN json_type(source_span_ids) != 'array' THEN 1
                    WHEN EXISTS (
                        SELECT 1 FROM json_each(observations.source_span_ids)
                        WHERE json_each.type != 'text'
                           OR trim(CAST(json_each.value AS TEXT)) = ''
                    ) THEN 1
                    ELSE 0
                END = 1
                """
            ).fetchone()[0]
        )
    ok = bool(
        not missing_base
        and not base_column_mismatches
        and not missing
        and not column_mismatches
        and index_ok
        and registry_ok
        and partial_pointer_count == 0
        and unbound_posterior_count == 0
        and calibrated_unverified_base_count == 0
        and invalid_base_measurement_status_count == 0
        and invalid_source_span_json_count == 0
    )
    return {
        "classification": "canonical" if ok else "migration_required",
        "ok": ok,
        "missing_base_columns": missing_base,
        "base_column_mismatches": base_column_mismatches,
        "missing_columns": missing,
        "column_mismatches": column_mismatches,
        "index_ok": index_ok,
        "registry_ok": registry_ok,
        "partial_pointer_count": partial_pointer_count,
        "unbound_posterior_count": unbound_posterior_count,
        "historical_unverified_base_count": historical_unverified_base_count,
        "calibrated_unverified_base_count": calibrated_unverified_base_count,
        "invalid_base_measurement_status_count": (
            invalid_base_measurement_status_count
        ),
        "invalid_source_span_json_count": invalid_source_span_json_count,
    }


def register_observation_calibration_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
            component TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            ddl_hash TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        render_sql(
            """
        INSERT INTO {table}(component, schema_version, ddl_hash, applied_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET
            schema_version=excluded.schema_version,
            ddl_hash=excluded.ddl_hash,
            applied_at=excluded.applied_at
        """,
            identifiers={"table": REGISTRY_TABLE},
        ),
        (
            REGISTRY_COMPONENT,
            OBSERVATION_CALIBRATION_SCHEMA_VERSION,
            OBSERVATION_CALIBRATION_DDL_HASH,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def initialize_observation_calibration_schema(conn: sqlite3.Connection) -> None:
    """Initialize only a freshly created empty Observation table.

    Runtime constructors may call this immediately after creating the base
    table.  Existing databases must use explicit reconciliation instead.
    """

    report = inspect_observation_calibration_schema(conn)
    if report["classification"] == "absent":
        raise ValueError("observations table is absent")
    if report["base_column_mismatches"] or report["column_mismatches"]:
        raise ValueError(
            "observation columns have non-canonical signatures: "
            + _canonical_json(
                {
                    "base": report["base_column_mismatches"],
                    "calibration": report["column_mismatches"],
                }
            )
        )
    row_count = int(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
    if row_count:
        raise ValueError("fresh Observation calibration initialization requires an empty table")
    for column_name in sorted(report["missing_base_columns"]):
        conn.execute(
            f"ALTER TABLE observations ADD COLUMN {column_name} "
            f"{BASE_OBSERVATION_COLUMN_DDL[column_name]}"
        )
    for column_name in sorted(report["missing_columns"]):
        conn.execute(
            f"ALTER TABLE observations ADD COLUMN {column_name} "
            f"{CALIBRATION_COLUMN_DDL[column_name]}"
        )
    conn.execute(
        f"CREATE INDEX {CALIBRATION_INDEX_NAME} "
        "ON observations(calibration_revision_id)"
    )
    register_observation_calibration_schema(conn)
    validate_observation_calibration_schema(conn)


def reconcile_observation_calibration_schema(
    conn: sqlite3.Connection,
    *,
    apply: bool,
) -> dict[str, Any]:
    before = inspect_observation_calibration_schema(conn)
    if not apply:
        return {"before": before, "after": before, "changed": False}
    if before["classification"] == "absent":
        raise ValueError("observations table is absent")

    missing_base_before = set(before["missing_base_columns"])
    missing_before = set(before["missing_columns"])
    if before["base_column_mismatches"] or before["column_mismatches"]:
        raise ValueError(
            "observation columns have non-canonical signatures: "
            + _canonical_json(
                {
                    "base": before["base_column_mismatches"],
                    "calibration": before["column_mismatches"],
                }
            )
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        for column_name in sorted(missing_base_before):
            ddl = BASE_OBSERVATION_COLUMN_DDL[column_name]
            conn.execute(f"ALTER TABLE observations ADD COLUMN {column_name} {ddl}")
        for column_name in sorted(missing_before):
            ddl = CALIBRATION_COLUMN_DDL[column_name]
            conn.execute(f"ALTER TABLE observations ADD COLUMN {column_name} {ddl}")
        if "base_confidence" in missing_before:
            conn.execute("UPDATE observations SET base_confidence=confidence")
        if "base_measurement_status" in missing_before:
            conn.execute(
                "UPDATE observations SET base_measurement_status='historical_unverified'"
            )
        if "source_span_ids" in missing_before:
            conn.execute("UPDATE observations SET source_span_ids='[]'")
        # A partial pointer is not proof.  Keep the base measurement and clear
        # the untrusted projection rather than manufacturing a state record.
        conn.execute(
            """
            UPDATE observations SET
                confidence=base_confidence,
                calibration_revision_id='',
                calibration_input_hash='',
                calibration_spec_hash='',
                calibration_record_hash=''
            WHERE ((calibration_revision_id != '')
                 + (calibration_input_hash != '')
                 + (calibration_spec_hash != '')
                 + (calibration_record_hash != '')) NOT IN (0, 4)
               OR (calibration_revision_id='' AND confidence != base_confidence)
            """
        )
        if not before["index_ok"]:
            conn.execute(f"DROP INDEX IF EXISTS {CALIBRATION_INDEX_NAME}")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {CALIBRATION_INDEX_NAME} "
            "ON observations(calibration_revision_id)"
        )
        register_observation_calibration_schema(conn)
        after = inspect_observation_calibration_schema(conn)
        if not after["ok"]:
            raise RuntimeError("observation calibration schema verification failed")
        conn.commit()
    except (sqlite3.Error, RuntimeError, ValueError):
        conn.rollback()
        raise
    return {"before": before, "after": after, "changed": before != after}


def validate_observation_calibration_schema(conn: sqlite3.Connection) -> None:
    report = inspect_observation_calibration_schema(conn)
    if not report["ok"]:
        raise RuntimeError(
            "Observation calibration schema migration required; run "
            "scripts/reconcile_observation_calibration_state.py: "
            + _canonical_json(report)
        )


__all__ = [
    "BASE_OBSERVATION_COLUMN_DDL",
    "BASE_OBSERVATION_COLUMN_SIGNATURES",
    "CALIBRATION_COLUMN_DDL",
    "CALIBRATION_COLUMN_SIGNATURES",
    "OBSERVATION_CALIBRATION_DDL_HASH",
    "OBSERVATION_CALIBRATION_SCHEMA_VERSION",
    "initialize_observation_calibration_schema",
    "inspect_observation_calibration_schema",
    "reconcile_observation_calibration_schema",
    "register_observation_calibration_schema",
    "validate_observation_calibration_schema",
]
