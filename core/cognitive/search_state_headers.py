"""Canonical small ACL headers for pre-body cognitive-state authorization."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping

from core.cognitive.access_control import (
    cognitive_access_hash,
    validate_cognitive_access_envelope,
)

STATE_SEARCH_HEADER_VERSION = "mnemos.cognitive_search_state_headers.v4"
STATE_SEARCH_HEADER_COMPONENT = "cognitive_search_state_headers"

_DDL = """
CREATE TABLE typed_search_state_revision_bindings (
    revision_id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    access_control TEXT NOT NULL CHECK(
        json_valid(access_control) AND json_type(access_control)='object'
    ),
    access_control_hash TEXT NOT NULL CHECK(length(trim(access_control_hash)) > 0),
    revision_payload_hash TEXT NOT NULL CHECK(length(trim(revision_payload_hash)) > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(revision_id) REFERENCES cognitive_state_revisions(revision_id)
        ON DELETE RESTRICT,
    UNIQUE(object_type, object_id, revision_id)
);
CREATE TABLE typed_search_state_headers (
    revision_id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    access_control TEXT NOT NULL CHECK(
        json_valid(access_control) AND json_type(access_control)='object'
    ),
    access_control_hash TEXT NOT NULL,
    revision_payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(revision_id) REFERENCES cognitive_state_revisions(revision_id)
        ON DELETE RESTRICT,
    UNIQUE(object_type, object_id, revision_id)
);
CREATE INDEX idx_typed_search_state_headers_object
    ON typed_search_state_headers(object_type, object_id);
CREATE INDEX idx_typed_search_state_headers_scope
    ON typed_search_state_headers(scope_type, scope_id, object_type);
CREATE TABLE typed_search_state_exclusions (
    revision_id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    reason_code TEXT NOT NULL CHECK(
        reason_code='legacy_noncurrent_acl_unavailable'
    ),
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(revision_id) REFERENCES cognitive_state_revisions(revision_id)
        ON DELETE RESTRICT
);
CREATE TABLE typed_search_state_header_registry (
    component TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    ddl_hash TEXT NOT NULL
);
CREATE TRIGGER typed_search_state_revision_bindings_no_update
BEFORE UPDATE ON typed_search_state_revision_bindings BEGIN
    SELECT RAISE(ABORT, 'typed search state revision bindings are immutable');
END;
CREATE TRIGGER typed_search_state_revision_bindings_no_delete
BEFORE DELETE ON typed_search_state_revision_bindings BEGIN
    SELECT RAISE(ABORT, 'typed search state revision bindings are immutable');
END;
CREATE TRIGGER typed_search_state_revision_bindings_revision_binding
BEFORE INSERT ON typed_search_state_revision_bindings BEGIN
    SELECT CASE WHEN
        NEW.revision_payload_hash <> COALESCE((
            SELECT payload_hash FROM cognitive_state_revisions
            WHERE revision_id=NEW.revision_id
        ), '')
        OR NEW.object_type <> COALESCE((
            SELECT object_type FROM cognitive_state_revisions
            WHERE revision_id=NEW.revision_id
        ), '')
        OR NEW.object_id <> COALESCE((
            SELECT object_id FROM cognitive_state_revisions
            WHERE revision_id=NEW.revision_id
        ), '')
        OR NEW.scope_type <> COALESCE((
            SELECT scope_type FROM cognitive_state_revisions
            WHERE revision_id=NEW.revision_id
        ), '')
        OR NEW.scope_id <> COALESCE((
            SELECT scope_id FROM cognitive_state_revisions
            WHERE revision_id=NEW.revision_id
        ), '')
        OR json(NEW.access_control) <> COALESCE((
            SELECT json(json_extract(payload_json, '$.access_control'))
            FROM cognitive_state_revisions
            WHERE revision_id=NEW.revision_id
        ), '')
    THEN RAISE(ABORT, 'typed search revision binding mismatch') END;
END;
CREATE TRIGGER typed_search_state_headers_no_update
BEFORE UPDATE ON typed_search_state_headers BEGIN
    SELECT RAISE(ABORT, 'typed search state headers are immutable');
END;
CREATE TRIGGER typed_search_state_headers_no_delete
BEFORE DELETE ON typed_search_state_headers BEGIN
    SELECT RAISE(ABORT, 'typed search state headers are immutable');
END;
CREATE TRIGGER typed_search_state_headers_revision_binding
BEFORE INSERT ON typed_search_state_headers BEGIN
    SELECT CASE WHEN
        NEW.access_control_hash <> COALESCE((
            SELECT access_control_hash FROM typed_search_state_revision_bindings
            WHERE revision_id=NEW.revision_id
        ), '')
        OR NEW.revision_payload_hash <> COALESCE((
            SELECT revision_payload_hash FROM typed_search_state_revision_bindings
            WHERE revision_id=NEW.revision_id
        ), '')
        OR NEW.object_type <> COALESCE((
            SELECT object_type FROM typed_search_state_revision_bindings
            WHERE revision_id=NEW.revision_id
        ), '')
        OR NEW.object_id <> COALESCE((
            SELECT object_id FROM typed_search_state_revision_bindings
            WHERE revision_id=NEW.revision_id
        ), '')
        OR NEW.scope_type <> COALESCE((
            SELECT scope_type FROM typed_search_state_revision_bindings
            WHERE revision_id=NEW.revision_id
        ), '')
        OR NEW.scope_id <> COALESCE((
            SELECT scope_id FROM typed_search_state_revision_bindings
            WHERE revision_id=NEW.revision_id
        ), '')
        OR json(NEW.access_control) <> COALESCE((
            SELECT json(access_control) FROM typed_search_state_revision_bindings
            WHERE revision_id=NEW.revision_id
        ), '')
    THEN RAISE(ABORT, 'typed search header revision binding mismatch') END;
END;
CREATE TRIGGER typed_search_state_exclusions_no_update
BEFORE UPDATE ON typed_search_state_exclusions BEGIN
    SELECT RAISE(ABORT, 'typed search state exclusions are immutable');
END;
CREATE TRIGGER typed_search_state_exclusions_no_delete
BEFORE DELETE ON typed_search_state_exclusions BEGIN
    SELECT RAISE(ABORT, 'typed search state exclusions are immutable');
END;
"""


def _ddl_hash() -> str:
    canonical = " ".join(_DDL.split())
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


STATE_SEARCH_HEADER_DDL_HASH = _ddl_hash()


def _normalize_schema_sql(value: str) -> str:
    return " ".join(str(value).strip().removesuffix(";").lower().split())


def _expected_schema_sql() -> dict[tuple[str, str], str]:
    expected: dict[tuple[str, str], str] = {}
    statement = ""
    for line in _DDL.splitlines():
        statement += line + "\n"
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip()
        statement = ""
        match = re.match(
            r"create\s+(table|index|trigger)\s+([a-z_][a-z0-9_]*)",
            sql,
            flags=re.IGNORECASE,
        )
        if match is None:
            raise RuntimeError("unrecognized state search header DDL statement")
        key = (match.group(1).lower(), match.group(2))
        expected[key] = _normalize_schema_sql(sql)
    if statement.strip():
        raise RuntimeError("incomplete state search header DDL statement")
    return expected


_EXPECTED_SCHEMA_SQL = _expected_schema_sql()

_LEGACY_RECONCILABLE_SCHEMA_IDENTITIES = {
    (
        "mnemos.cognitive_search_state_headers.v1",
        "sha256:693ba1ffc6a70227844fc11fb06437653f5100389aba5019c7c882ba8ce52ae4",
        "sha256:6fddefe5539ca2188dbf6c7e1a7af3cf062d290ad2a393b3d64aa5f840305b6c",
    ),
    (
        "mnemos.cognitive_search_state_headers.v3",
        "sha256:dda4e6dad82755e5533ca1bb31f1189a3c216369d85efdff52846e90d4250595",
        "sha256:8aff80e26ee31a14a54280c38ad77e77840791e9ab7cfb2dc75a3b882ace7457",
    ),
}

_HEADER_COLUMNS_V1 = {
    "revision_id",
    "object_type",
    "object_id",
    "scope_type",
    "scope_id",
    "access_control",
    "access_control_hash",
    "created_at",
}
_HEADER_COLUMNS_V2 = _HEADER_COLUMNS_V1 | {"revision_payload_hash"}
_BINDING_COLUMNS_V3 = {
    "revision_id",
    "object_type",
    "object_id",
    "scope_type",
    "scope_id",
    "access_control_hash",
    "revision_payload_hash",
    "created_at",
}
_BINDING_COLUMNS_V4 = _BINDING_COLUMNS_V3 | {"access_control"}


class StateSearchHeaderSchemaError(RuntimeError):
    """The small-header projection is absent, stale, or incomplete."""


def _projection_schema_entries(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str]]:
    return [
        (str(row[0]), str(row[1]), _normalize_schema_sql(str(row[2] or "")))
        for row in conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger') "
            "AND (name LIKE 'typed_search_state_%' "
            "OR name LIKE 'idx_typed_search_state_headers_%') "
            "ORDER BY type, name"
        ).fetchall()
    ]


def _projection_schema_signature(entries: list[tuple[str, str, str]]) -> str:
    encoded = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


_CURRENT_SCHEMA_SIGNATURE = _projection_schema_signature(
    sorted((kind, name, sql) for (kind, name), sql in _EXPECTED_SCHEMA_SQL.items())
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _require_known_reconcilable_schema(
    conn: sqlite3.Connection,
    tables: set[str],
) -> None:
    """Refuse to destroy projection objects without an exact registered signature."""

    del tables
    try:
        registry_rows = conn.execute(
            "SELECT component, schema_version, ddl_hash "
            "FROM typed_search_state_header_registry ORDER BY component"
        ).fetchall()
    except sqlite3.Error as exc:
        raise StateSearchHeaderSchemaError(
            "unknown cognitive search header schema requires manual reconciliation"
        ) from exc
    if len(registry_rows) != 1 or str(registry_rows[0][0]) != STATE_SEARCH_HEADER_COMPONENT:
        raise StateSearchHeaderSchemaError(
            "unknown cognitive search header registry requires manual reconciliation"
        )
    identity = (
        str(registry_rows[0][1]),
        str(registry_rows[0][2]),
        _projection_schema_signature(_projection_schema_entries(conn)),
    )
    current_identity = (
        STATE_SEARCH_HEADER_VERSION,
        STATE_SEARCH_HEADER_DDL_HASH,
        _CURRENT_SCHEMA_SIGNATURE,
    )
    if identity != current_identity and identity not in _LEGACY_RECONCILABLE_SCHEMA_IDENTITIES:
        raise StateSearchHeaderSchemaError(
            "unknown cognitive search header schema physical signature requires manual reconciliation"
        )


def _schema_definition_mismatches(conn: sqlite3.Connection) -> list[str]:
    actual = {(kind, name): sql for kind, name, sql in _projection_schema_entries(conn)}
    missing_or_changed = {
        f"{kind}:{name}"
        for (kind, name), expected_sql in _EXPECTED_SCHEMA_SQL.items()
        if actual.get((kind, name)) != expected_sql
    }
    unexpected = {
        f"unexpected:{kind}:{name}"
        for kind, name in actual
        if (kind, name) not in _EXPECTED_SCHEMA_SQL
    }
    return sorted(missing_or_changed | unexpected)


def inspect_state_search_headers(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    required = {
        "typed_search_state_revision_bindings",
        "typed_search_state_headers",
        "typed_search_state_exclusions",
        "typed_search_state_header_registry",
    }
    revision_count = int(
        conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0]
    )
    current_revision_count = int(
        conn.execute("SELECT COUNT(*) FROM cognitive_state_heads").fetchone()[0]
    )
    if not required <= tables:
        header_present = "typed_search_state_headers" in tables
        binding_present = "typed_search_state_revision_bindings" in tables
        exclusion_present = "typed_search_state_exclusions" in tables
        header_columns = (
            _table_columns(conn, "typed_search_state_headers") if header_present else set()
        )
        header_count = (
            int(conn.execute("SELECT COUNT(*) FROM typed_search_state_headers").fetchone()[0])
            if header_present
            else 0
        )
        binding_count = (
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM typed_search_state_revision_bindings"
                ).fetchone()[0]
            )
            if binding_present
            else 0
        )
        exclusion_count = (
            int(conn.execute("SELECT COUNT(*) FROM typed_search_state_exclusions").fetchone()[0])
            if exclusion_present
            else 0
        )
        missing_headers = int(conn.execute("""
                    SELECT COUNT(*)
                    FROM cognitive_state_heads AS current
                    LEFT JOIN typed_search_state_headers AS h
                      ON h.revision_id=current.revision_id
                    WHERE h.revision_id IS NULL
                    """).fetchone()[0]) if header_present else current_revision_count
        missing_bindings = int(conn.execute("""
                    SELECT COUNT(*)
                    FROM cognitive_state_heads AS current
                    LEFT JOIN typed_search_state_revision_bindings AS b
                      ON b.revision_id=current.revision_id
                    WHERE b.revision_id IS NULL
                    """).fetchone()[0]) if binding_present else current_revision_count
        if header_present and binding_present:
            if exclusion_present:
                coverage_gap = int(conn.execute("""
                        SELECT COUNT(*)
                        FROM cognitive_state_revisions AS r
                        LEFT JOIN typed_search_state_headers AS h
                          ON h.revision_id=r.revision_id
                        LEFT JOIN typed_search_state_revision_bindings AS b
                          ON b.revision_id=r.revision_id
                        LEFT JOIN typed_search_state_exclusions AS x
                          ON x.revision_id=r.revision_id
                        WHERE x.revision_id IS NULL
                          AND (h.revision_id IS NULL OR b.revision_id IS NULL)
                        """).fetchone()[0])
            else:
                coverage_gap = int(conn.execute("""
                        SELECT COUNT(*)
                        FROM cognitive_state_revisions AS r
                        LEFT JOIN typed_search_state_headers AS h
                          ON h.revision_id=r.revision_id
                        LEFT JOIN typed_search_state_revision_bindings AS b
                          ON b.revision_id=r.revision_id
                        WHERE h.revision_id IS NULL OR b.revision_id IS NULL
                        """).fetchone()[0])
        else:
            coverage_gap = max(0, revision_count - exclusion_count)
        return {
            "schema_version": STATE_SEARCH_HEADER_VERSION,
            "schema_present": header_present,
            "registry_ok": False,
            "schema_upgrade_required": bool(
                header_present
                and header_columns in (_HEADER_COLUMNS_V1, _HEADER_COLUMNS_V2)
                and not binding_present
            ),
            "revision_count": revision_count,
            "current_revision_count": current_revision_count,
            "header_count": header_count,
            "binding_count": binding_count,
            "exclusion_count": exclusion_count,
            "missing_header_count": missing_headers,
            "missing_binding_count": missing_bindings,
            "extra_header_count": 0,
            "extra_binding_count": 0,
            "coverage_gap_count": coverage_gap,
            "header_exclusion_overlap_count": 0,
            "hash_mismatch_count": 0,
            "schema_definition_mismatch_count": len(_schema_definition_mismatches(conn)),
            "ok": False,
        }
    registry = conn.execute(
        "SELECT schema_version, ddl_hash FROM typed_search_state_header_registry "
        "WHERE component=?",
        (STATE_SEARCH_HEADER_COMPONENT,),
    ).fetchone()
    registry_ok = registry is not None and tuple(str(value) for value in registry) == (
        STATE_SEARCH_HEADER_VERSION,
        STATE_SEARCH_HEADER_DDL_HASH,
    )
    header_count = int(
        conn.execute("SELECT COUNT(*) FROM typed_search_state_headers").fetchone()[0]
    )
    binding_count = int(
        conn.execute("SELECT COUNT(*) FROM typed_search_state_revision_bindings").fetchone()[0]
    )
    exclusion_count = int(
        conn.execute("SELECT COUNT(*) FROM typed_search_state_exclusions").fetchone()[0]
    )
    header_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(typed_search_state_headers)").fetchall()
    }
    binding_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(typed_search_state_revision_bindings)"
        ).fetchall()
    }
    schema_definition_mismatches = _schema_definition_mismatches(conn)
    if header_columns != _HEADER_COLUMNS_V2 or binding_columns != _BINDING_COLUMNS_V4:
        missing_headers = int(conn.execute("""
                SELECT COUNT(*)
                FROM cognitive_state_heads AS current
                LEFT JOIN typed_search_state_headers AS h
                  ON h.revision_id=current.revision_id
                WHERE h.revision_id IS NULL
                """).fetchone()[0])
        missing_bindings = int(conn.execute("""
                SELECT COUNT(*)
                FROM cognitive_state_heads AS current
                LEFT JOIN typed_search_state_revision_bindings AS b
                  ON b.revision_id=current.revision_id
                WHERE b.revision_id IS NULL
                """).fetchone()[0])
        return {
            "schema_version": STATE_SEARCH_HEADER_VERSION,
            "schema_present": True,
            "registry_ok": False,
            "schema_upgrade_required": bool(
                header_columns in (_HEADER_COLUMNS_V1, _HEADER_COLUMNS_V2)
                and binding_columns in (_BINDING_COLUMNS_V3, _BINDING_COLUMNS_V4)
            ),
            "revision_count": revision_count,
            "current_revision_count": current_revision_count,
            "header_count": header_count,
            "binding_count": binding_count,
            "exclusion_count": exclusion_count,
            "missing_header_count": missing_headers,
            "missing_binding_count": missing_bindings,
            "binding_preimage_missing_count": (
                binding_count if "access_control" not in binding_columns else 0
            ),
            "extra_header_count": 0,
            "extra_binding_count": 0,
            "coverage_gap_count": max(0, revision_count - exclusion_count),
            "header_exclusion_overlap_count": 0,
            "hash_mismatch_count": 0,
            "schema_definition_mismatch_count": len(schema_definition_mismatches),
            "ok": False,
        }
    missing = int(conn.execute("""
            SELECT COUNT(*)
            FROM cognitive_state_heads AS current
            JOIN cognitive_state_revisions AS r
              ON r.revision_id=current.revision_id
            LEFT JOIN typed_search_state_headers AS h
              ON h.revision_id=r.revision_id
            WHERE h.revision_id IS NULL
            """).fetchone()[0])
    missing_binding = int(conn.execute("""
            SELECT COUNT(*)
            FROM cognitive_state_heads AS current
            JOIN cognitive_state_revisions AS r
              ON r.revision_id=current.revision_id
            LEFT JOIN typed_search_state_revision_bindings AS b
              ON b.revision_id=r.revision_id
            WHERE b.revision_id IS NULL
            """).fetchone()[0])
    extra = int(conn.execute("""
            SELECT COUNT(*)
            FROM typed_search_state_headers AS h
            LEFT JOIN cognitive_state_revisions AS r
              ON r.revision_id=h.revision_id
            WHERE r.revision_id IS NULL
            """).fetchone()[0])
    extra_binding = int(conn.execute("""
            SELECT COUNT(*)
            FROM typed_search_state_revision_bindings AS b
            LEFT JOIN cognitive_state_revisions AS r
              ON r.revision_id=b.revision_id
            WHERE r.revision_id IS NULL
            """).fetchone()[0])
    coverage_gap = int(conn.execute("""
            SELECT COUNT(*)
            FROM cognitive_state_revisions AS r
            LEFT JOIN typed_search_state_headers AS h
              ON h.revision_id=r.revision_id
            LEFT JOIN typed_search_state_revision_bindings AS b
              ON b.revision_id=r.revision_id
            LEFT JOIN typed_search_state_exclusions AS x
              ON x.revision_id=r.revision_id
            WHERE x.revision_id IS NULL
              AND (h.revision_id IS NULL OR b.revision_id IS NULL)
            """).fetchone()[0])
    overlap = int(conn.execute("""
            SELECT COUNT(*)
            FROM typed_search_state_exclusions AS x
            LEFT JOIN typed_search_state_headers AS h
              ON h.revision_id=x.revision_id
            LEFT JOIN typed_search_state_revision_bindings AS b
              ON b.revision_id=x.revision_id
            WHERE h.revision_id IS NOT NULL OR b.revision_id IS NOT NULL
            """).fetchone()[0])
    mismatches = 0
    header_rows = conn.execute("""
        SELECT h.access_control, h.access_control_hash,
               h.revision_payload_hash,
               h.object_type, h.object_id, h.scope_type, h.scope_id,
               b.access_control, b.access_control_hash, b.revision_payload_hash,
               b.object_type, b.object_id, b.scope_type, b.scope_id,
               r.object_type, r.object_id, r.scope_type, r.scope_id, r.payload_hash,
               json_extract(r.payload_json, '$.access_control')
        FROM typed_search_state_headers AS h
        JOIN typed_search_state_revision_bindings AS b ON b.revision_id=h.revision_id
        JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
        """).fetchall()
    for row in header_rows:
        try:
            access = validate_cognitive_access_envelope(
                json.loads(str(row[0])),
                expected_scope_type=str(row[5]),
                expected_scope_id=str(row[6]),
            )
            binding_access = validate_cognitive_access_envelope(
                json.loads(str(row[7])),
                expected_scope_type=str(row[12]),
                expected_scope_id=str(row[13]),
            )
            revision_access = validate_cognitive_access_envelope(
                json.loads(str(row[19])),
                expected_scope_type=str(row[16]),
                expected_scope_id=str(row[17]),
            )
            matches = (
                str(row[1]) == cognitive_access_hash(access)
                and str(row[8]) == cognitive_access_hash(binding_access)
                and binding_access == revision_access
                and access == binding_access
                and str(row[1]) == str(row[8])
                and str(row[2]) == str(row[9]) == str(row[18])
                and tuple(str(value) for value in row[3:7])
                == tuple(str(value) for value in row[10:14])
                == tuple(str(value) for value in row[14:18])
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            matches = False
        if not matches:
            mismatches += 1
    ok = bool(
        registry_ok
        and missing == 0
        and missing_binding == 0
        and extra == 0
        and extra_binding == 0
        and coverage_gap == 0
        and overlap == 0
        and mismatches == 0
        and not schema_definition_mismatches
    )
    return {
        "schema_version": STATE_SEARCH_HEADER_VERSION,
        "schema_present": True,
        "registry_ok": registry_ok,
        "revision_count": revision_count,
        "current_revision_count": current_revision_count,
        "header_count": header_count,
        "binding_count": binding_count,
        "exclusion_count": exclusion_count,
        "missing_header_count": missing,
        "missing_binding_count": missing_binding,
        "extra_header_count": extra,
        "extra_binding_count": extra_binding,
        "coverage_gap_count": coverage_gap,
        "header_exclusion_overlap_count": overlap,
        "hash_mismatch_count": mismatches,
        "schema_definition_mismatch_count": len(schema_definition_mismatches),
        "ok": ok,
    }


def initialize_state_search_headers(conn: sqlite3.Connection) -> None:
    """Create the header projection only inside an explicit provisioning transaction."""

    statement = ""
    for line in _DDL.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise StateSearchHeaderSchemaError("state search header DDL is incomplete")
    conn.execute(
        "INSERT INTO typed_search_state_header_registry(component, schema_version, ddl_hash) "
        "VALUES (?, ?, ?)",
        (
            STATE_SEARCH_HEADER_COMPONENT,
            STATE_SEARCH_HEADER_VERSION,
            STATE_SEARCH_HEADER_DDL_HASH,
        ),
    )


def _drop_state_search_header_projection(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS typed_search_state_header_registry")
    conn.execute("DROP TABLE IF EXISTS typed_search_state_exclusions")
    conn.execute("DROP TABLE IF EXISTS typed_search_state_headers")
    conn.execute("DROP TABLE IF EXISTS typed_search_state_revision_bindings")


def detach_state_search_headers_for_canonical_rebuild(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Validate and detach the coupled projection inside a caller transaction.

    SQLite retargets foreign keys and trigger SQL when their referenced table
    is renamed.  Canonical-state rebuilds must therefore remove this derived
    projection before renaming ``cognitive_state_revisions`` and restore it
    after the canonical tables have been copied.
    """

    if not conn.in_transaction:
        raise StateSearchHeaderSchemaError(
            "state search header detach requires a caller-owned transaction"
        )
    required = {
        "typed_search_state_revision_bindings",
        "typed_search_state_headers",
        "typed_search_state_exclusions",
        "typed_search_state_header_registry",
    }
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    present = required & tables
    revision_count = int(
        conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0]
    )
    if not present:
        if revision_count:
            raise StateSearchHeaderSchemaError(
                "canonical revisions without search projection require separate reconciliation"
            )
        return {"present": False, "rows": {}, "counts": {}}
    if present != required:
        raise StateSearchHeaderSchemaError(
            "partial cognitive search projection requires separate reconciliation"
        )
    before = inspect_state_search_headers(conn)
    if not before.get("ok"):
        raise StateSearchHeaderSchemaError(
            "noncanonical cognitive search projection requires separate reconciliation"
        )
    row_tables = (
        "typed_search_state_revision_bindings",
        "typed_search_state_headers",
        "typed_search_state_exclusions",
    )
    rows = {
        table: conn.execute(f'SELECT * FROM "{table}" ORDER BY revision_id').fetchall()
        for table in row_tables
    }
    columns = {
        table: tuple(
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        )
        for table in row_tables
    }
    _drop_state_search_header_projection(conn)
    return {
        "present": True,
        "rows": rows,
        "columns": columns,
        "counts": {table: len(values) for table, values in rows.items()},
    }


def restore_state_search_headers_after_canonical_rebuild(
    conn: sqlite3.Connection,
    snapshot: Mapping[str, Any],
) -> dict[str, int]:
    """Restore a validated detached projection against rebuilt canonical tables."""

    if not conn.in_transaction:
        raise StateSearchHeaderSchemaError(
            "state search header restore requires a caller-owned transaction"
        )
    initialize_state_search_headers(conn)
    if snapshot.get("present"):
        rows = snapshot.get("rows")
        columns = snapshot.get("columns")
        if not isinstance(rows, Mapping) or not isinstance(columns, Mapping):
            raise StateSearchHeaderSchemaError("state search header snapshot is malformed")
        for table in (
            "typed_search_state_revision_bindings",
            "typed_search_state_headers",
            "typed_search_state_exclusions",
        ):
            table_rows = list(rows.get(table, ()))
            table_columns = tuple(str(value) for value in columns.get(table, ()))
            if table_rows:
                placeholders = ", ".join("?" for _ in table_columns)
                column_sql = ", ".join(f'"{column}"' for column in table_columns)
                conn.executemany(
                    f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',  # nosec B608
                    table_rows,
                )
    after = inspect_state_search_headers(conn)
    if not after.get("ok"):
        raise StateSearchHeaderSchemaError(
            "restored cognitive search projection is not canonical"
        )
    expected_counts = {
        str(key): int(value)
        for key, value in dict(snapshot.get("counts") or {}).items()
    }
    actual_counts = {
        table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in (
            "typed_search_state_revision_bindings",
            "typed_search_state_headers",
            "typed_search_state_exclusions",
        )
    }
    if expected_counts and actual_counts != expected_counts:
        raise StateSearchHeaderSchemaError(
            "state search header row-count conservation failed"
        )
    return actual_counts


def require_state_search_headers(conn: sqlite3.Connection) -> None:
    try:
        registry = conn.execute(
            "SELECT schema_version, ddl_hash FROM typed_search_state_header_registry "
            "WHERE component=?",
            (STATE_SEARCH_HEADER_COMPONENT,),
        ).fetchone()
        gap = conn.execute("""
            SELECT 1
            FROM cognitive_state_heads AS current
            JOIN cognitive_state_revisions AS r
              ON r.revision_id=current.revision_id
            LEFT JOIN typed_search_state_headers AS h
              ON h.revision_id=r.revision_id
            LEFT JOIN typed_search_state_revision_bindings AS b
              ON b.revision_id=r.revision_id
            WHERE h.revision_id IS NULL OR b.revision_id IS NULL
               OR h.revision_payload_hash <> r.payload_hash
               OR b.revision_payload_hash <> r.payload_hash
               OR h.access_control_hash <> b.access_control_hash
               OR h.object_type <> r.object_type
               OR h.object_id <> r.object_id
               OR h.scope_type <> r.scope_type
               OR h.scope_id <> r.scope_id
               OR b.object_type <> r.object_type
               OR b.object_id <> r.object_id
               OR b.scope_type <> r.scope_type
               OR b.scope_id <> r.scope_id
            LIMIT 1
            """).fetchone()
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(typed_search_state_headers)").fetchall()
        }
        binding_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(typed_search_state_revision_bindings)"
            ).fetchall()
        }
        schema_definition_mismatches = _schema_definition_mismatches(conn)
    except sqlite3.Error as exc:
        raise StateSearchHeaderSchemaError(
            "cognitive search header reconciliation required"
        ) from exc
    if (
        registry is None
        or tuple(str(value) for value in registry)
        != (
            STATE_SEARCH_HEADER_VERSION,
            STATE_SEARCH_HEADER_DDL_HASH,
        )
        or gap is not None
        or columns != _HEADER_COLUMNS_V2
        or binding_columns != _BINDING_COLUMNS_V4
        or schema_definition_mismatches
    ):
        raise StateSearchHeaderSchemaError(
            "cognitive search header reconciliation required; run "
            "scripts/reconcile_cognitive_search_state_headers.py"
        )


def insert_state_search_header(
    conn: sqlite3.Connection,
    *,
    revision_id: str,
    object_type: str,
    object_id: str,
    scope_type: str,
    scope_id: str,
    payload: Mapping[str, Any],
    revision_payload_hash: str,
    created_at: str,
) -> None:
    """Write one immutable header in the same transaction as its revision."""

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if (
        not {
            "typed_search_state_headers",
            "typed_search_state_revision_bindings",
        }
        <= tables
    ):
        raise StateSearchHeaderSchemaError(
            "cognitive search header reconciliation required before state writes"
        )
    raw_access = payload.get("access_control")
    if not isinstance(raw_access, Mapping):
        raise ValueError("cognitive state payload lacks access_control")
    access = validate_cognitive_access_envelope(
        raw_access,
        expected_scope_type=scope_type,
        expected_scope_id=scope_id,
    )
    access_json = json.dumps(
        access,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    access_hash = cognitive_access_hash(access)
    conn.execute(
        """
        INSERT INTO typed_search_state_revision_bindings(
            revision_id, object_type, object_id, scope_type, scope_id,
            access_control, access_control_hash, revision_payload_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            object_type,
            object_id,
            scope_type,
            scope_id,
            access_json,
            access_hash,
            str(revision_payload_hash),
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO typed_search_state_headers(
            revision_id, object_type, object_id, scope_type, scope_id,
            access_control, access_control_hash, revision_payload_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision_id,
            object_type,
            object_id,
            scope_type,
            scope_id,
            access_json,
            access_hash,
            str(revision_payload_hash),
            created_at,
        ),
    )


def reconcile_state_search_headers(
    conn: sqlite3.Connection,
    *,
    apply: bool,
    failpoint: str = "",
) -> dict[str, Any]:
    """Backfill exact ACL headers without modifying immutable revisions."""

    before = inspect_state_search_headers(conn)
    candidates: list[tuple[Any, ...]] = []
    exclusions: list[tuple[str, str, str, str]] = []
    invalid_current = 0
    current_heads = {
        str(row[0])
        for row in conn.execute("SELECT revision_id FROM cognitive_state_heads").fetchall()
    }
    revision_rows = conn.execute("""
        SELECT revision_id, object_type, object_id, scope_type, scope_id,
               json_extract(payload_json, '$.access_control'), created_at,
               payload_hash
        FROM cognitive_state_revisions ORDER BY revision_id
        """).fetchall()
    for row in revision_rows:
        try:
            access = validate_cognitive_access_envelope(
                json.loads(str(row[5] or "")),
                expected_scope_type=str(row[3]),
                expected_scope_id=str(row[4]),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            if str(row[0]) in current_heads:
                invalid_current += 1
            else:
                exclusions.append((str(row[0]), str(row[1]), str(row[7]), str(row[6])))
            continue
        candidates.append(
            (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                access,
                str(row[7]),
                str(row[6]),
            )
        )
    report: dict[str, Any] = {
        "schema_version": STATE_SEARCH_HEADER_VERSION,
        "mode": "apply" if apply else "dry_run",
        "before": before,
        "candidate_count": len(candidates),
        "typed_exclusion_count": len(exclusions),
        "invalid_current_acl_count": invalid_current,
        "applied": False,
    }
    if not apply:
        report["after"] = before
        return report
    if invalid_current:
        raise StateSearchHeaderSchemaError(
            "invalid current cognitive ACL prevents header reconciliation"
        )

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "typed_search_state_headers" not in tables:
            if tables & {
                "typed_search_state_revision_bindings",
                "typed_search_state_exclusions",
                "typed_search_state_header_registry",
            }:
                _require_known_reconcilable_schema(conn, tables)
                _drop_state_search_header_projection(conn)
            initialize_state_search_headers(conn)
        elif not before.get("ok"):
            _require_known_reconcilable_schema(conn, tables)
            _drop_state_search_header_projection(conn)
            initialize_state_search_headers(conn)
        if failpoint == "after_schema":
            raise RuntimeError("injected state-search-header reconciliation failure")
        existing = {
            str(row[0])
            for row in conn.execute("SELECT revision_id FROM typed_search_state_headers").fetchall()
        }
        existing_exclusions = {
            str(row[0])
            for row in conn.execute(
                "SELECT revision_id FROM typed_search_state_exclusions"
            ).fetchall()
        }
        inserted = 0
        for (
            revision_id,
            object_type,
            object_id,
            scope_type,
            scope_id,
            access,
            revision_payload_hash,
            created_at,
        ) in candidates:
            if revision_id in existing:
                continue
            insert_state_search_header(
                conn,
                revision_id=revision_id,
                object_type=object_type,
                object_id=object_id,
                scope_type=scope_type,
                scope_id=scope_id,
                payload={"access_control": access},
                revision_payload_hash=revision_payload_hash,
                created_at=created_at,
            )
            inserted += 1
        excluded = 0
        for revision_id, object_type, payload_hash, created_at in exclusions:
            if revision_id in existing_exclusions:
                continue
            conn.execute(
                """
                INSERT INTO typed_search_state_exclusions(
                    revision_id, object_type, reason_code, payload_hash, created_at
                ) VALUES (?, ?, 'legacy_noncurrent_acl_unavailable', ?, ?)
                """,
                (revision_id, object_type, payload_hash, created_at),
            )
            excluded += 1
        if failpoint == "after_copy":
            raise RuntimeError("injected state-search-header reconciliation failure")
        after = inspect_state_search_headers(conn)
        if not after["ok"]:
            raise StateSearchHeaderSchemaError("state search header verification failed")
    report["after"] = after
    report["applied"] = True
    report["inserted_count"] = inserted
    report["inserted_exclusion_count"] = excluded
    return report


__all__ = [
    "STATE_SEARCH_HEADER_DDL_HASH",
    "STATE_SEARCH_HEADER_VERSION",
    "StateSearchHeaderSchemaError",
    "initialize_state_search_headers",
    "insert_state_search_header",
    "inspect_state_search_headers",
    "reconcile_state_search_headers",
    "require_state_search_headers",
]
