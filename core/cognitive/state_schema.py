"""Canonical SQLite schema authority for cognitive state and flow envelopes.

The schema is created only by the explicit initializer for a fresh database.
Existing non-canonical databases fail closed and must be handled by the
reviewable reconciliation command; constructors never add columns or tables.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Callable, Iterator, Mapping

from core.cognitive.state_contract import (
    COGNITIVE_OBJECT_TYPES,
    CognitiveStateRevision,
    canonical_json,
    sha256_json,
)
from core.cognitive.state_migration_candidate import historical_revision as _historical_revision
from core.cognitive.state_schema_sql import (
    table_row_count as _table_row_count,
    utc_now as _now,
)
from core.privacy.content_redaction import redact_persistence_value
from core.cognitive.state_schema_ddl import (  # noqa: F401
    CANONICAL_DDL,
    CANONICAL_TABLES,
    DECISION_TRACE_ENFORCEMENT_COMPONENT,
    DECISION_TRACE_ENFORCEMENT_HASH,
    DECISION_TRACE_ENFORCEMENT_VERSION,
    FEEDBACK_COMMAND_ATTEMPT_DDL,
    LEGACY_CANONICAL_TABLES,
    LEGACY_CANONICAL_V1_DDL_HASH,
    LEGACY_CANONICAL_V1_SCHEMA_VERSION,
    LEGACY_CANONICAL_V2_DDL,
    LEGACY_CANONICAL_V2_DDL_HASH,
    LEGACY_CANONICAL_V2_SCHEMA_VERSION,
    LEGACY_CANONICAL_V3_DDL,
    LEGACY_CANONICAL_V3_DDL_HASH,
    LEGACY_CANONICAL_V3_SCHEMA_VERSION,
    LEGACY_CANONICAL_V4_DDL_HASH,
    LEGACY_CANONICAL_V4_SCHEMA_VERSION,
    PREDICTION_ENFORCEMENT_COMPONENT,
    PREDICTION_ENFORCEMENT_HASH,
    PREDICTION_ENFORCEMENT_VERSION,
    REGISTRY_TABLE,
    RUNTIME_LEDGER_SCHEMA_VERSION,
    SCHEMA_COMPONENT,
    STATE_SCHEMA_VERSION,
)


class CognitiveStateSchemaError(RuntimeError):
    """The ledger schema cannot be used without explicit reconciliation."""


@dataclass(frozen=True)
class CognitiveStateSchemaState:
    classification: str
    schema_version: str
    ddl_hash: str
    canonical_ddl_hash: str
    registry_version: str
    registry_ddl_hash: str
    tables: tuple[str, ...]
    migration_required: bool
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.migration_required

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "schema_version": self.schema_version,
            "ddl_hash": self.ddl_hash,
            "canonical_ddl_hash": self.canonical_ddl_hash,
            "registry_version": self.registry_version,
            "registry_ddl_hash": self.registry_ddl_hash,
            "tables": list(self.tables),
            "migration_required": self.migration_required,
            "errors": list(self.errors),
            "ok": self.ok,
        }


def _table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    )


def _schema_signature(conn: sqlite3.Connection) -> str:
    objects: list[dict[str, Any]] = []
    for object_type, name, sql in conn.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall():
        if str(name) not in CANONICAL_TABLES and not any(
            str(name).startswith(prefix)
            for prefix in (
                "idx_runtime_",
                "idx_cognitive_",
                "cognitive_data_",
                "cognitive_feedback_",
                "cognitive_state_",
            )
        ):
            continue
        item: dict[str, Any] = {
            "type": str(object_type),
            "name": str(name),
            "sql": " ".join(str(sql).split()),
        }
        if object_type == "table":
            item["columns"] = [
                tuple(row) for row in conn.execute(f"PRAGMA table_xinfo('{name}')").fetchall()
            ]
            item["foreign_keys"] = [
                tuple(row) for row in conn.execute(f"PRAGMA foreign_key_list('{name}')").fetchall()
            ]
        objects.append(item)
    raw = json.dumps(objects, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_hash() -> str:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(CANONICAL_DDL)
        return _schema_signature(conn)
    finally:
        conn.close()


CANONICAL_DDL_HASH = _canonical_hash()


def _registry_row(conn: sqlite3.Connection) -> tuple[str, str]:
    if REGISTRY_TABLE not in _table_names(conn):
        return "", ""
    try:
        row = conn.execute(
            f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
            (SCHEMA_COMPONENT,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise CognitiveStateSchemaError("invalid cognitive state schema registry") from exc
    return (str(row[0]), str(row[1])) if row else ("", "")


def _write_registry_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"INSERT INTO {REGISTRY_TABLE}(component, schema_version, ddl_hash, applied_at) "  # nosec B608
        "VALUES (?, ?, ?, ?)",
        (SCHEMA_COMPONENT, STATE_SCHEMA_VERSION, CANONICAL_DDL_HASH, _now()),
    )


def write_decision_trace_enforcement_marker(conn: sqlite3.Connection) -> None:
    """Activate strict material-action enforcement in an explicit transaction."""

    existing = conn.execute(
        f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
        (DECISION_TRACE_ENFORCEMENT_COMPONENT,),
    ).fetchone()
    expected = (
        DECISION_TRACE_ENFORCEMENT_VERSION,
        DECISION_TRACE_ENFORCEMENT_HASH,
    )
    if existing is None:
        conn.execute(
            f"INSERT INTO {REGISTRY_TABLE}(component, schema_version, ddl_hash, applied_at) "  # nosec B608
            "VALUES (?, ?, ?, ?)",
            (
                DECISION_TRACE_ENFORCEMENT_COMPONENT,
                *expected,
                _now(),
            ),
        )
    elif tuple(str(value) for value in existing) != expected:
        raise CognitiveStateSchemaError(
            "decision-trace enforcement marker conflicts with the canonical contract"
        )


def decision_trace_enforcement_enabled(conn: sqlite3.Connection) -> bool:
    """Return whether the canonical DecisionTrace enforcement marker is active."""

    if REGISTRY_TABLE not in _table_names(conn):
        return False
    row = conn.execute(
        f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
        (DECISION_TRACE_ENFORCEMENT_COMPONENT,),
    ).fetchone()
    return row is not None and tuple(str(value) for value in row) == (
        DECISION_TRACE_ENFORCEMENT_VERSION,
        DECISION_TRACE_ENFORCEMENT_HASH,
    )


def write_prediction_enforcement_marker(conn: sqlite3.Connection) -> None:
    """Activate strict PredictionRecord enforcement in an explicit transaction."""

    from core.cognitive.prediction_schema_marker import write_prediction_marker

    write_prediction_marker(
        conn,
        applied_at=_now(),
        error_type=CognitiveStateSchemaError,
    )


def prediction_enforcement_enabled(conn: sqlite3.Connection) -> bool:
    """Return whether strict PredictionRecord enforcement is active."""

    from core.cognitive.prediction_schema_marker import prediction_marker_enabled

    return prediction_marker_enabled(
        conn,
        table_names=_table_names,
    )


def _execute_canonical_ddl(conn: sqlite3.Connection) -> None:
    """Execute the schema statement-by-statement so DDL stays transactional."""

    statement = ""
    for line in CANONICAL_DDL.splitlines():
        if line.strip().upper().startswith("PRAGMA FOREIGN_KEYS"):
            continue
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise CognitiveStateSchemaError("canonical DDL contains an incomplete statement")


def inspect_cognitive_state_schema(conn: sqlite3.Connection) -> CognitiveStateSchemaState:
    tables = _table_names(conn)
    anchors = set(tables) & set(CANONICAL_TABLES)
    if not anchors:
        return CognitiveStateSchemaState(
            classification="absent",
            schema_version=STATE_SCHEMA_VERSION,
            ddl_hash="",
            canonical_ddl_hash=CANONICAL_DDL_HASH,
            registry_version="",
            registry_ddl_hash="",
            tables=tables,
            migration_required=False,
            errors=(),
        )
    ddl_hash = _schema_signature(conn)
    registry_version, registry_hash = _registry_row(conn)
    canonical_tables_present = set(CANONICAL_TABLES) <= set(tables)
    legacy_canonical_tables_present = set(LEGACY_CANONICAL_TABLES) <= set(tables)
    if (
        canonical_tables_present
        and ddl_hash == CANONICAL_DDL_HASH
        and registry_version == STATE_SCHEMA_VERSION
        and registry_hash == CANONICAL_DDL_HASH
    ):
        classification = "canonical"
        migration_required = False
        errors: tuple[str, ...] = ()
    elif (
        canonical_tables_present
        and ddl_hash == LEGACY_CANONICAL_V4_DDL_HASH
        and registry_version == LEGACY_CANONICAL_V4_SCHEMA_VERSION
        and registry_hash == LEGACY_CANONICAL_V4_DDL_HASH
    ):
        classification = "canonical_v4_stage_receipt_upgrade_required"
        migration_required = True
        errors = ()
    elif (
        canonical_tables_present
        and ddl_hash == LEGACY_CANONICAL_V3_DDL_HASH
        and registry_version == LEGACY_CANONICAL_V3_SCHEMA_VERSION
        and registry_hash == LEGACY_CANONICAL_V3_DDL_HASH
    ):
        classification = "canonical_v3_training_governance_upgrade_required"
        migration_required = True
        errors = ()
    elif (
        legacy_canonical_tables_present
        and ddl_hash == LEGACY_CANONICAL_V2_DDL_HASH
        and registry_version == LEGACY_CANONICAL_V2_SCHEMA_VERSION
        and registry_hash == LEGACY_CANONICAL_V2_DDL_HASH
    ):
        classification = "canonical_v2_feedback_attribution_upgrade_required"
        migration_required = True
        errors = ()
    elif (
        legacy_canonical_tables_present
        and ddl_hash == LEGACY_CANONICAL_V1_DDL_HASH
        and registry_version == LEGACY_CANONICAL_V1_SCHEMA_VERSION
        and registry_hash == LEGACY_CANONICAL_V1_DDL_HASH
    ):
        classification = "canonical_v1_decision_trace_upgrade_required"
        migration_required = True
        errors = ()
    elif {"runtime_flow_registry", "runtime_flow_events"} & set(
        tables
    ) and "cognitive_state_revisions" not in tables:
        classification = "legacy_runtime_v1_or_v2"
        migration_required = True
        errors = ()
    else:
        classification = "unknown_or_partial"
        migration_required = True
        errors = ("unknown or partially initialized cognitive state schema",)
    return CognitiveStateSchemaState(
        classification=classification,
        schema_version=STATE_SCHEMA_VERSION,
        ddl_hash=ddl_hash,
        canonical_ddl_hash=CANONICAL_DDL_HASH,
        registry_version=registry_version,
        registry_ddl_hash=registry_hash,
        tables=tables,
        migration_required=migration_required,
        errors=errors,
    )


def initialize_cognitive_state_schema(target: Path | sqlite3.Connection) -> None:
    """Provision a fresh schema, or validate an already canonical one."""

    if isinstance(target, sqlite3.Connection):
        owns_connection = False
        conn = target
    else:
        owns_connection = True
        db_path = Path(target)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        state = inspect_cognitive_state_schema(conn)
        if state.classification == "absent":
            _execute_canonical_ddl(conn)
            from core.cognitive.search_state_headers import initialize_state_search_headers

            initialize_state_search_headers(conn)
            _write_registry_row(conn)
            write_decision_trace_enforcement_marker(conn)
            write_prediction_enforcement_marker(conn)
            conn.commit()
            verified = inspect_cognitive_state_schema(conn)
            if not verified.ok:
                raise CognitiveStateSchemaError("fresh cognitive state schema verification failed")
            return
        if not state.ok:
            raise CognitiveStateSchemaError(
                "cognitive state migration required; run "
                "scripts/reconcile_cognitive_state_store.py before opening writers: "
                f"classification={state.classification}"
            )
    finally:
        if owns_connection:
            conn.close()


def upgrade_canonical_v1_for_decision_trace_in_transaction(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Losslessly rebuild the canonical v1 tables under the v2 DDL.

    The caller owns a transaction with ``foreign_keys=OFF`` and must write the
    decision-trace activation marker plus historical inventory before commit.
    """

    if not conn.in_transaction:
        raise CognitiveStateSchemaError(
            "decision-trace schema upgrade requires a caller-owned transaction"
        )
    before = inspect_cognitive_state_schema(conn)
    if before.classification != "canonical_v1_decision_trace_upgrade_required":
        raise CognitiveStateSchemaError(
            "decision-trace schema upgrade source is not exact canonical v1"
        )
    source_counts = {
        table: _table_row_count(conn, table)
        for table in LEGACY_CANONICAL_TABLES
    }
    legacy_names: dict[str, str] = {}
    for table in LEGACY_CANONICAL_TABLES:
        legacy = f"__decision_trace_v1__{table}"
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')  # nosec B608
        legacy_names[table] = legacy
    _drop_historical_indexes_and_triggers(conn, tuple(legacy_names.values()))
    _execute_canonical_ddl(conn)

    copy_order = (
        "runtime_flow_registry",
        "runtime_flow_events",
        "runtime_flow_receipts",
        "cognitive_data_events",
        "cognitive_data_consumptions",
        "cognitive_data_consumer_heads",
        "cognitive_data_reconciliations",
        "cognitive_state_revisions",
        "cognitive_state_heads",
        "cognitive_state_outbox",
        "cognitive_state_effect_receipts",
        "cognitive_state_migration_quarantine",
    )
    for table in copy_order:
        legacy = legacy_names[table]
        source_columns = tuple(
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{legacy}")').fetchall()
        )
        target_columns = tuple(
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        )
        if source_columns != target_columns:
            raise CognitiveStateSchemaError(f"decision-trace schema upgrade column drift: {table}")
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
        WHERE component NOT IN (?, ?)
        """,  # nosec B608
        (SCHEMA_COMPONENT, DECISION_TRACE_ENFORCEMENT_COMPONENT),
    )
    _write_registry_row(conn)
    for legacy in reversed(tuple(legacy_names.values())):
        conn.execute(f'DROP TABLE "{legacy}"')  # nosec B608
    for table, count in source_counts.items():
        current = _table_row_count(conn, table)
        expected = count if table != REGISTRY_TABLE else max(1, count)
        if table != REGISTRY_TABLE and current != expected:
            raise CognitiveStateSchemaError(
                f"decision-trace schema upgrade row-count drift: {table}"
            )
    after = inspect_cognitive_state_schema(conn)
    if not after.ok:
        raise CognitiveStateSchemaError(
            "decision-trace schema upgrade did not produce canonical v2"
        )
    return source_counts


def upgrade_canonical_v4_for_stage_receipt_in_transaction(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Losslessly rebuild exact canonical v4 tables under the v5 DDL."""

    from core.cognitive.stage_receipt_schema_upgrade import (
        upgrade_stage_receipt_schema,
    )

    return upgrade_stage_receipt_schema(
        conn,
        error_type=CognitiveStateSchemaError,
        inspect_schema=inspect_cognitive_state_schema,
        table_row_count=_table_row_count,
        drop_historical_objects=_drop_historical_indexes_and_triggers,
        execute_canonical_ddl=_execute_canonical_ddl,
        write_registry_row=_write_registry_row,
    )


def upgrade_canonical_v3_for_training_governance_in_transaction(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Losslessly rebuild exact canonical v3 tables under the v4 DDL."""

    if not conn.in_transaction:
        raise CognitiveStateSchemaError(
            "training-governance schema upgrade requires a caller-owned transaction"
        )
    before = inspect_cognitive_state_schema(conn)
    if before.classification != "canonical_v3_training_governance_upgrade_required":
        raise CognitiveStateSchemaError(
            "training-governance schema upgrade source is not exact canonical v3"
        )
    source_counts = {
        table: _table_row_count(conn, table)
        for table in CANONICAL_TABLES
    }
    legacy_names: dict[str, str] = {}
    for table in CANONICAL_TABLES:
        legacy = f"__training_v3__{table}"
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')  # nosec B608
        legacy_names[table] = legacy
    _drop_historical_indexes_and_triggers(conn, tuple(legacy_names.values()))
    _execute_canonical_ddl(conn)

    for table in tuple(item for item in CANONICAL_TABLES if item != REGISTRY_TABLE):
        legacy = legacy_names[table]
        source_columns = tuple(
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{legacy}")').fetchall()
        )
        target_columns = tuple(
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        )
        if source_columns != target_columns:
            raise CognitiveStateSchemaError(
                f"training-governance schema upgrade column drift: {table}"
            )
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
    _write_registry_row(conn)
    for legacy in reversed(tuple(legacy_names.values())):
        conn.execute(f'DROP TABLE "{legacy}"')  # nosec B608
    for table, count in source_counts.items():
        current = _table_row_count(conn, table)
        if current != count:
            raise CognitiveStateSchemaError(
                f"training-governance schema upgrade row-count drift: {table}"
            )
    after = inspect_cognitive_state_schema(conn)
    if not after.ok:
        raise CognitiveStateSchemaError(
            "training-governance schema upgrade did not produce canonical v4"
        )
    return source_counts


def upgrade_canonical_v2_for_feedback_attribution_in_transaction(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Losslessly rebuild exact canonical v2 tables under the v3 DDL."""

    if not conn.in_transaction:
        raise CognitiveStateSchemaError(
            "feedback-attribution schema upgrade requires a caller-owned transaction"
        )
    before = inspect_cognitive_state_schema(conn)
    if before.classification != "canonical_v2_feedback_attribution_upgrade_required":
        raise CognitiveStateSchemaError(
            "feedback-attribution schema upgrade source is not exact canonical v2"
        )
    source_counts = {
        table: _table_row_count(conn, table)
        for table in LEGACY_CANONICAL_TABLES
    }
    legacy_names: dict[str, str] = {}
    for table in LEGACY_CANONICAL_TABLES:
        legacy = f"__feedback_v2__{table}"
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')  # nosec B608
        legacy_names[table] = legacy
    _drop_historical_indexes_and_triggers(conn, tuple(legacy_names.values()))
    _execute_canonical_ddl(conn)

    copy_order = tuple(table for table in LEGACY_CANONICAL_TABLES if table != REGISTRY_TABLE)
    for table in copy_order:
        legacy = legacy_names[table]
        source_columns = tuple(
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{legacy}")').fetchall()
        )
        target_columns = tuple(
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        )
        if source_columns != target_columns:
            raise CognitiveStateSchemaError(
                f"feedback-attribution schema upgrade column drift: {table}"
            )
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
    _write_registry_row(conn)
    for legacy in reversed(tuple(legacy_names.values())):
        conn.execute(f'DROP TABLE "{legacy}"')  # nosec B608
    for table, count in source_counts.items():
        current = _table_row_count(conn, table)
        if current != count:
            raise CognitiveStateSchemaError(
                f"feedback-attribution schema upgrade row-count drift: {table}"
            )
    after = inspect_cognitive_state_schema(conn)
    if not after.ok:
        raise CognitiveStateSchemaError(
            "feedback-attribution schema upgrade did not produce canonical v3"
        )
    return source_counts


def validate_cognitive_state_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    state = inspect_cognitive_state_schema(conn)
    if not state.ok:
        raise CognitiveStateSchemaError(
            "cognitive state schema is not canonical: " f"classification={state.classification}"
        )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _iter_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    batch_size: int = 500,
) -> Iterator[dict[str, Any]]:
    cursor = conn.execute(f'SELECT * FROM "{table}"')  # nosec B608 - internal table names only
    names = tuple(str(item[0]) for item in cursor.description or ())
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            yield dict(zip(names, row))


def _safe_json(value: Any, default: Any) -> str:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = default
    return canonical_json(parsed)


def _historical_candidate_counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    counts = {
        "typed_candidates": 0,
        "quarantined_semantic_events": 0,
        "orphan_consumptions": 0,
    }
    if not _table_exists(conn, table):
        return counts
    event_ids: set[str] = set()
    for event in _iter_rows(conn, table):
        event_ids.add(str(event.get("event_id") or ""))
        if str(event.get("data_type") or "") not in COGNITIVE_OBJECT_TYPES:
            continue
        try:
            _historical_revision(event)
            counts["typed_candidates"] += 1
        except ValueError:
            counts["quarantined_semantic_events"] += 1
    consumption_table = table.replace("cognitive_data_events", "cognitive_data_consumptions")
    if _table_exists(conn, consumption_table):
        counts["orphan_consumptions"] = sum(
            1
            for row in _iter_rows(conn, consumption_table)
            if str(row.get("event_id") or "") not in event_ids
        )
    return counts


def _quarantine(
    conn: sqlite3.Connection,
    *,
    source_table: str,
    source_key: str,
    reason_code: str,
    payload: Mapping[str, Any],
) -> str:
    redacted = redact_persistence_value(dict(payload))
    if not isinstance(redacted.value, Mapping):
        raise CognitiveStateSchemaError("migration quarantine payload is invalid")
    payload_json = canonical_json(redacted.value)
    payload_hash = sha256_json(redacted.value)
    identity = {
        "source_table": source_table,
        "source_key": source_key,
        "reason_code": reason_code,
        "payload_hash": payload_hash,
    }
    quarantine_id = "cogquarantine-" + sha256_json(identity).split(":", 1)[1][:32]
    conn.execute(
        """
        INSERT INTO cognitive_state_migration_quarantine (
            quarantine_id, source_table, source_key, reason_code,
            field_manifest, payload_json, payload_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quarantine_id,
            source_table,
            source_key,
            reason_code,
            canonical_json(sorted(str(key) for key in payload)),
            payload_json,
            payload_hash,
            _now(),
        ),
    )
    return quarantine_id


def _drop_historical_indexes_and_triggers(
    conn: sqlite3.Connection,
    legacy_tables: tuple[str, ...],
) -> None:
    placeholders = ",".join("?" for _ in legacy_tables)
    if not placeholders:
        return
    rows = conn.execute(
        f"SELECT type, name FROM sqlite_master "  # nosec B608 - bound placeholder list
        f"WHERE type IN ('index', 'trigger') AND tbl_name IN ({placeholders}) "
        "AND name NOT LIKE 'sqlite_autoindex_%'",
        legacy_tables,
    ).fetchall()
    for object_type, name in rows:
        if object_type == "index":
            conn.execute(f'DROP INDEX "{str(name)}"')  # nosec B608 - sqlite-owned name
        else:
            conn.execute(f'DROP TRIGGER "{str(name)}"')  # nosec B608 - sqlite-owned name


def _ensure_runtime_flow(
    conn: sqlite3.Connection,
    flow_id: str,
    *,
    source: str = "legacy_migration",
) -> None:
    if conn.execute(
        "SELECT 1 FROM runtime_flow_registry WHERE flow_id=?",
        (flow_id,),
    ).fetchone():
        return
    now = _now()
    conn.execute(
        """
        INSERT INTO runtime_flow_registry (
            flow_id, data_type, topic, producer_refs, consumer_refs,
            pending_budget, dead_letter_budget, max_lag_seconds,
            registered_at, updated_at, required, min_observations,
            observation_mode, not_applicable_reason, freshness_required,
            receipt_grace_seconds
        ) VALUES (?, 'legacy_runtime_event', ?, ?, '[]', 0, 0, 86400,
                  ?, ?, 1, 1, 'continuous', '', 1, 0)
        """,
        (flow_id, flow_id, canonical_json([source]), now, now),
    )


def _copy_runtime_registry(conn: sqlite3.Connection, legacy: str) -> int:
    if not _table_exists(conn, legacy):
        return 0
    copied = 0
    for row in _iter_rows(conn, legacy):
        flow_id = str(row.get("flow_id") or "")
        if not flow_id:
            _quarantine(
                conn,
                source_table="runtime_flow_registry",
                source_key="missing-flow-id",
                reason_code="runtime_registry_missing_flow_id",
                payload=row,
            )
            continue
        mode = str(row.get("observation_mode") or "continuous")
        if mode not in {"continuous", "on_event", "not_applicable"}:
            mode = "continuous"
        conn.execute(
            """
            INSERT INTO runtime_flow_registry (
                flow_id, data_type, topic, producer_refs, consumer_refs,
                pending_budget, dead_letter_budget, max_lag_seconds,
                registered_at, updated_at, required, min_observations,
                observation_mode, not_applicable_reason, freshness_required,
                receipt_grace_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                flow_id,
                str(row.get("data_type") or "legacy_runtime_event"),
                str(row.get("topic") or flow_id),
                _safe_json(row.get("producer_refs"), []),
                _safe_json(row.get("consumer_refs"), []),
                max(0, int(row.get("pending_budget") or 0)),
                max(0, int(row.get("dead_letter_budget") or 0)),
                max(0, int(row.get("max_lag_seconds") or 86400)),
                str(row.get("registered_at") or _now()),
                str(row.get("updated_at") or _now()),
                1 if bool(row.get("required", 1)) else 0,
                max(0, int(row.get("min_observations") or 1)),
                mode,
                str(row.get("not_applicable_reason") or ""),
                1 if bool(row.get("freshness_required", 1)) else 0,
                max(0, int(row.get("receipt_grace_seconds") or 0)),
            ),
        )
        copied += 1
    return copied


def _copy_runtime_events_and_receipts(
    conn: sqlite3.Connection,
    legacy_events: str,
    legacy_receipts: str,
) -> tuple[int, int]:
    copied_events = 0
    copied_receipts = 0
    terminal_rows: list[dict[str, Any]] = []
    if _table_exists(conn, legacy_events):
        for row in _iter_rows(conn, legacy_events):
            direction = str(row.get("direction") or "")
            if direction != "produced":
                terminal_rows.append(row)
                continue
            flow_id = str(row.get("flow_id") or "legacy")
            _ensure_runtime_flow(conn, flow_id, source=str(row.get("source") or "legacy"))
            conn.execute(
                """
                INSERT INTO runtime_flow_events (
                    event_id, flow_id, direction, topic, source, item_id,
                    created_at, metadata, generation_id, intended_consumers,
                    idempotency_key
                ) VALUES (?, ?, 'produced', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row.get("event_id") or f"legacy-event-{uuid.uuid4().hex}"),
                    flow_id,
                    str(row.get("topic") or flow_id),
                    str(row.get("source") or "legacy"),
                    str(row.get("item_id") or ""),
                    str(row.get("created_at") or _now()),
                    _safe_json(row.get("metadata"), {}),
                    str(row.get("generation_id") or "legacy-unknown"),
                    _safe_json(row.get("intended_consumers"), []),
                    str(row.get("idempotency_key") or ""),
                ),
            )
            copied_events += 1
    if _table_exists(conn, legacy_receipts):
        terminal_rows.extend(_iter_rows(conn, legacy_receipts))
    for row in terminal_rows:
        raw_status = str(row.get("status") or row.get("direction") or "consumed")
        status = raw_status if raw_status in {"consumed", "dead_letter", "skipped"} else "consumed"
        flow_id = str(row.get("flow_id") or "legacy")
        item_id = str(row.get("item_id") or "")
        _ensure_runtime_flow(conn, flow_id, source=str(row.get("source") or "legacy"))
        production_event_id = str(row.get("production_event_id") or "")
        if not production_event_id:
            produced = conn.execute(
                """
                SELECT event_id FROM runtime_flow_events
                WHERE flow_id=? AND item_id=? ORDER BY created_at DESC LIMIT 1
                """,
                (flow_id, item_id),
            ).fetchone()
            production_event_id = str(produced[0]) if produced else ""
        receipt_id = str(row.get("receipt_id") or row.get("event_id") or uuid.uuid4().hex)
        idempotency_key = str(
            row.get("idempotency_key")
            or f"migration:{flow_id}:{production_event_id}:{receipt_id}:{status}"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO runtime_flow_receipts (
                receipt_id, production_event_id, flow_id, consumer_id,
                status, item_id, generation_id, idempotency_key,
                created_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                production_event_id,
                flow_id,
                str(row.get("consumer_id") or row.get("source") or "legacy_consumer"),
                status,
                item_id,
                str(row.get("generation_id") or "legacy-unknown"),
                idempotency_key,
                str(row.get("created_at") or _now()),
                _safe_json(row.get("metadata"), {}),
            ),
        )
        copied_receipts += 1
    return copied_events, copied_receipts


def _canonical_event_row(
    event: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any],
) -> tuple[Any, ...]:
    lifecycle = str(event.get("lifecycle_status") or "produced")
    if lifecycle not in {
        "produced",
        "normalized",
        "deduped",
        "rejected",
        "expired",
        "superseded",
        "dead_letter",
    }:
        lifecycle = "produced"
    confidence = float(event.get("confidence") or 0.0)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("legacy event confidence is outside the canonical range")
    intended_json = _safe_json(event.get("intended_consumers"), [])
    evidence_json = _safe_json(event.get("evidence_refs"), [])
    intended = json.loads(intended_json)
    evidence = json.loads(evidence_json)
    if (
        not isinstance(intended, list)
        or not intended
        or not all(str(item).strip() for item in intended)
    ):
        raise ValueError("legacy event intended consumers are incomplete")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(str(item).strip() for item in evidence)
    ):
        raise ValueError("legacy event evidence refs are incomplete")
    redacted = redact_persistence_value(dict(metadata))
    return (
        str(event.get("event_id") or ""),
        str(event.get("source_id") or ""),
        str(event.get("asset_id") or ""),
        str(event.get("source_kind") or ""),
        str(event.get("source_uri") or ""),
        str(event.get("content_hash") or ""),
        str(event.get("canonical_subject") or ""),
        str(event.get("data_type") or ""),
        str(event.get("producer") or ""),
        intended_json,
        str(event.get("privacy_level") or "private"),
        confidence,
        evidence_json,
        str(event.get("dedupe_key") or ""),
        lifecycle,
        str(event.get("retention_policy") or "default"),
        canonical_json(redacted.value),
        str(event.get("created_at") or _now()),
        str(event.get("updated_at") or event.get("recorded_at") or _now()),
    )


def _insert_migrated_event(conn: sqlite3.Connection, values: tuple[Any, ...]) -> None:
    if any(values[index] == "" for index in (0, 3, 4, 5, 6, 7, 8, 13)):
        raise ValueError("legacy event required identity is incomplete")
    conn.execute(
        """
        INSERT INTO cognitive_data_events (
            event_id, source_id, asset_id, source_kind, source_uri,
            content_hash, canonical_subject, data_type, producer,
            intended_consumers, privacy_level, confidence, evidence_refs,
            dedupe_key, lifecycle_status, retention_policy, metadata,
            created_at, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )


def _insert_migrated_revision(
    conn: sqlite3.Connection,
    revision: CognitiveStateRevision,
) -> None:
    if revision.supersedes_revision_id:
        parent = conn.execute(
            """
            SELECT object_type, object_id, revision_no
            FROM cognitive_state_revisions WHERE revision_id=?
            """,
            (revision.supersedes_revision_id,),
        ).fetchone()
        if parent is None or tuple(parent[:2]) != (
            revision.object_type,
            revision.object_id,
        ):
            raise ValueError("legacy semantic revision supersedes a missing head")
        existing_child = conn.execute(
            "SELECT 1 FROM cognitive_state_revisions WHERE supersedes_revision_id=?",
            (revision.supersedes_revision_id,),
        ).fetchone()
        if existing_child is not None:
            raise ValueError("legacy semantic revision creates a competing branch")
        revision_no = int(parent[2]) + 1
    else:
        existing_root = conn.execute(
            """
            SELECT 1 FROM cognitive_state_revisions
            WHERE object_type=? AND object_id=?
            """,
            (revision.object_type, revision.object_id),
        ).fetchone()
        if existing_root is not None:
            raise ValueError("legacy semantic revision lacks explicit lineage")
        revision_no = 1
    if revision.correction_of_revision_id:
        corrected = conn.execute(
            """
            SELECT object_type, object_id FROM cognitive_state_revisions
            WHERE revision_id=?
            """,
            (revision.correction_of_revision_id,),
        ).fetchone()
        if corrected is None or tuple(corrected) != (
            revision.object_type,
            revision.object_id,
        ):
            raise ValueError("legacy correction target is not in the same object chain")
    conn.execute(
        """
        INSERT INTO cognitive_state_revisions (
            revision_id, object_type, object_id, schema_version, revision_no,
            source_event_id, source_revision_id, source_content_hash,
            scope_type, scope_id, evidence_refs, evidence_hash,
            payload_json, payload_hash, supersedes_revision_id,
            correction_of_revision_id, admission_state, redaction_policy, redaction_counts,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''),
                  NULLIF(?, ''), 'historical_candidate', ?, ?, ?)
        """,
        (
            revision.revision_id,
            revision.object_type,
            revision.object_id,
            revision.schema_version,
            revision_no,
            revision.source_event_id,
            revision.source_revision_id,
            revision.source_content_hash,
            revision.scope_type,
            revision.scope_id,
            canonical_json(list(revision.evidence_refs)),
            revision.evidence_hash,
            canonical_json(revision.payload),
            revision.payload_hash,
            revision.supersedes_revision_id,
            revision.correction_of_revision_id,
            revision.redaction_policy,
            canonical_json(dict(revision.redaction_counts)),
            revision.created_at,
        ),
    )


def _copy_cognitive_events(
    conn: sqlite3.Connection,
    legacy: str,
) -> tuple[set[str], int, int]:
    preserved: set[str] = set()
    typed = 0
    quarantined = 0
    if not _table_exists(conn, legacy):
        return preserved, typed, quarantined
    semantic_candidates: list[tuple[dict[str, Any], CognitiveStateRevision, tuple[Any, ...]]] = []
    for event in _iter_rows(conn, legacy):
        event_id = str(event.get("event_id") or "")
        if str(event.get("data_type") or "") in COGNITIVE_OBJECT_TYPES:
            try:
                revision = _historical_revision(event)
                typed_metadata = {
                    "revision_ids": [revision.revision_id],
                    "migration_status": "typed_candidate",
                }
                semantic_candidates.append(
                    (
                        event,
                        revision,
                        _canonical_event_row(event, metadata=typed_metadata),
                    )
                )
            except (TypeError, ValueError, sqlite3.Error):
                _quarantine(
                    conn,
                    source_table="cognitive_data_events",
                    source_key=event_id or "missing-event-id",
                    reason_code="semantic_event_incomplete",
                    payload=event,
                )
                quarantined += 1
            continue
        try:
            metadata_raw = json.loads(str(event.get("metadata") or "{}"))
            legacy_metadata: Mapping[str, Any] = (
                dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
            )
            _insert_migrated_event(
                conn,
                _canonical_event_row(event, metadata=legacy_metadata),
            )
            preserved.add(event_id)
        except (TypeError, ValueError, sqlite3.Error):
            _quarantine(
                conn,
                source_table="cognitive_data_events",
                source_key=event_id or "missing-event-id",
                reason_code="event_contract_incomplete",
                payload=event,
            )
            quarantined += 1
    pending = semantic_candidates
    while pending:
        deferred: list[tuple[dict[str, Any], CognitiveStateRevision, tuple[Any, ...]]] = []
        progressed = False
        for event, revision, event_values in pending:
            dependencies = tuple(
                value
                for value in (
                    revision.supersedes_revision_id,
                    revision.correction_of_revision_id,
                )
                if value
            )
            if dependencies and any(
                conn.execute(
                    "SELECT 1 FROM cognitive_state_revisions WHERE revision_id=?",
                    (dependency,),
                ).fetchone()
                is None
                for dependency in dependencies
            ):
                deferred.append((event, revision, event_values))
                continue
            conn.execute("SAVEPOINT migrate_semantic_event")
            try:
                _insert_migrated_event(conn, event_values)
                _insert_migrated_revision(conn, revision)
                conn.execute("RELEASE SAVEPOINT migrate_semantic_event")
                preserved.add(str(event.get("event_id") or ""))
                typed += 1
                progressed = True
            except (TypeError, ValueError, sqlite3.Error):
                conn.execute("ROLLBACK TO SAVEPOINT migrate_semantic_event")
                conn.execute("RELEASE SAVEPOINT migrate_semantic_event")
                _quarantine(
                    conn,
                    source_table="cognitive_data_events",
                    source_key=str(event.get("event_id") or "missing-event-id"),
                    reason_code="semantic_event_lineage_invalid",
                    payload=event,
                )
                quarantined += 1
        if not deferred:
            break
        if progressed:
            pending = deferred
            continue
        for event, _revision, _event_values in deferred:
            _quarantine(
                conn,
                source_table="cognitive_data_events",
                source_key=str(event.get("event_id") or "missing-event-id"),
                reason_code="semantic_event_lineage_unresolved",
                payload=event,
            )
            quarantined += 1
        break
    return preserved, typed, quarantined


def _copy_cognitive_consumptions(
    conn: sqlite3.Connection,
    legacy: str,
    preserved_events: set[str],
) -> tuple[int, int]:
    if not _table_exists(conn, legacy):
        return 0, 0
    rows = list(_iter_rows(conn, legacy))
    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row.get("event_id") or ""), str(row.get("consumer_id") or ""))
        by_pair.setdefault(key, []).append(row)
    copied = 0
    quarantined = 0
    for (event_id, consumer_id), pair_rows in by_pair.items():
        intended_row = conn.execute(
            "SELECT intended_consumers FROM cognitive_data_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        intended = set(json.loads(str(intended_row[0]))) if intended_row else set()
        if event_id not in preserved_events or consumer_id not in intended:
            for row in pair_rows:
                _quarantine(
                    conn,
                    source_table="cognitive_data_consumptions",
                    source_key=str(row.get("consumption_id") or uuid.uuid4().hex),
                    reason_code=(
                        "orphan_consumption"
                        if event_id not in preserved_events
                        else "unintended_consumer"
                    ),
                    payload=row,
                )
                quarantined += 1
            continue
        active_candidate = len(pair_rows) == 1 and not bool(pair_rows[0].get("action_changed"))
        for row in pair_rows:
            raw_status = str(row.get("status") or "consumed")
            status = {
                "consumed": "committed",
                "skipped": "intentional_skip",
                "rejected": "rejected",
                "dead_letter": "dead_letter",
                "expired": "expired",
                "superseded": "superseded",
            }.get(raw_status, "committed")
            consumption_id = str(row.get("consumption_id") or f"legacy-{uuid.uuid4().hex}")
            receipt_state = "active" if active_candidate else "historical_incomplete"
            metadata_raw = redact_persistence_value(
                {
                    "legacy_metadata": json.loads(_safe_json(row.get("metadata"), {})),
                    "legacy_action_changed": bool(row.get("action_changed")),
                }
            ).value
            conn.execute(
                """
                INSERT INTO cognitive_data_consumptions (
                    consumption_id, event_id, consumer_id, outcome, status,
                    target_effect_id, before_hash, after_hash,
                    effect_evidence_refs, action_changed, metadata,
                    idempotency_key, supersedes_consumption_id,
                    correction_of_consumption_id, receipt_state, created_at
                ) VALUES (?, ?, ?, ?, ?, '', '', '', '[]', 0, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    consumption_id,
                    event_id,
                    consumer_id,
                    str(row.get("outcome") or ""),
                    status,
                    canonical_json(metadata_raw),
                    f"migration:{consumption_id}",
                    receipt_state,
                    str(row.get("created_at") or _now()),
                ),
            )
            if active_candidate:
                conn.execute(
                    "INSERT INTO cognitive_data_consumer_heads VALUES (?, ?, ?, ?)",
                    (event_id, consumer_id, consumption_id, str(row.get("created_at") or _now())),
                )
            copied += 1
    return copied, quarantined


def _copy_reconciliations(
    conn: sqlite3.Connection,
    legacy: str,
    preserved_events: set[str],
) -> tuple[int, int]:
    if not _table_exists(conn, legacy):
        return 0, 0
    copied = 0
    quarantined = 0
    for row in _iter_rows(conn, legacy):
        event_id = str(row.get("event_id") or "")
        related_event_id = str(row.get("related_event_id") or "")
        key = str(row.get("reconciliation_id") or uuid.uuid4().hex)
        relation_type = str(row.get("relation_type") or "")
        if (
            event_id not in preserved_events
            or related_event_id not in preserved_events
            or relation_type not in {"duplicate", "derived", "reinforcement"}
        ):
            _quarantine(
                conn,
                source_table="cognitive_data_reconciliations",
                source_key=key,
                reason_code="reconciliation_proof_incomplete",
                payload=row,
            )
            quarantined += 1
            continue
        refs: list[str] = []
        for candidate in (event_id, related_event_id):
            raw = conn.execute(
                "SELECT evidence_refs FROM cognitive_data_events WHERE event_id=?",
                (candidate,),
            ).fetchone()
            refs.extend(str(value) for value in json.loads(str(raw[0])))
        conn.execute(
            """
            INSERT INTO cognitive_data_reconciliations (
                reconciliation_id, event_id, related_event_id, relation_type,
                dedupe_key, reason, source_revision_refs, proof_hash,
                proof_status, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'historical_heuristic', ?, ?)
            """,
            (
                key,
                event_id,
                related_event_id,
                relation_type,
                str(row.get("dedupe_key") or "legacy"),
                str(row.get("reason") or "legacy heuristic reconciliation"),
                canonical_json(sorted(set(refs))),
                sha256_json(dict(row)),
                _safe_json(row.get("metadata"), {}),
                str(row.get("created_at") or _now()),
            ),
        )
        copied += 1
    return copied, quarantined


def reconcile_cognitive_state_schema(
    conn: sqlite3.Connection,
    *,
    apply: bool = False,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Inspect or transactionally migrate a recognized runtime/v2/v3 ledger."""

    before = inspect_cognitive_state_schema(conn)
    event_table = "cognitive_data_events"
    candidates = _historical_candidate_counts(conn, event_table)
    report: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "applied": False,
        "before": before.as_dict(),
        "after": before.as_dict(),
        "candidate_counts": candidates,
    }
    if before.classification == "canonical":
        report["action"] = "already_canonical"
        return report
    if before.classification == "absent":
        report["action"] = "create_fresh_schema"
        if not apply:
            return report
    elif before.classification == "canonical_v4_stage_receipt_upgrade_required":
        report["action"] = "upgrade_stage_receipt_schema"
        if not apply:
            return report
    elif before.classification == "canonical_v3_training_governance_upgrade_required":
        report["action"] = "upgrade_training_governance_schema"
        if not apply:
            return report
    elif before.classification == "canonical_v2_feedback_attribution_upgrade_required":
        report["action"] = "upgrade_feedback_attribution_schema"
        if not apply:
            return report
    elif before.classification != "legacy_runtime_v1_or_v2":
        raise CognitiveStateSchemaError(
            f"unknown cognitive state schema cannot be migrated: {before.classification}"
        )
    elif not apply:
        report["action"] = "migrate_with_quarantine"
        return report

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if before.classification == "absent":
            _execute_canonical_ddl(conn)
            _write_registry_row(conn)
            if failpoint:
                failpoint("after_schema")
            copied_counts: dict[str, int] = {}
        elif before.classification == "canonical_v4_stage_receipt_upgrade_required":
            copied_counts = upgrade_canonical_v4_for_stage_receipt_in_transaction(conn)
            if failpoint:
                failpoint("after_copy")
        elif before.classification == "canonical_v3_training_governance_upgrade_required":
            copied_counts = upgrade_canonical_v3_for_training_governance_in_transaction(conn)
            if failpoint:
                failpoint("after_copy")
        elif before.classification == "canonical_v2_feedback_attribution_upgrade_required":
            copied_counts = upgrade_canonical_v2_for_feedback_attribution_in_transaction(conn)
            if failpoint:
                failpoint("after_copy")
        else:
            source_tables = tuple(table for table in CANONICAL_TABLES if _table_exists(conn, table))
            legacy_names: dict[str, str] = {}
            for table in source_tables:
                legacy = f"__legacy__{table}"
                conn.execute(f'ALTER TABLE "{table}" RENAME TO "{legacy}"')  # nosec B608
                legacy_names[table] = legacy
            _drop_historical_indexes_and_triggers(conn, tuple(legacy_names.values()))
            if failpoint:
                failpoint("after_rename")
            _execute_canonical_ddl(conn)
            if failpoint:
                failpoint("after_schema")
            copied_registry = _copy_runtime_registry(
                conn,
                legacy_names.get("runtime_flow_registry", ""),
            )
            copied_runtime_events, copied_runtime_receipts = _copy_runtime_events_and_receipts(
                conn,
                legacy_names.get("runtime_flow_events", ""),
                legacy_names.get("runtime_flow_receipts", ""),
            )
            preserved, typed, quarantined_events = _copy_cognitive_events(
                conn,
                legacy_names.get("cognitive_data_events", ""),
            )
            copied_consumptions, quarantined_consumptions = _copy_cognitive_consumptions(
                conn,
                legacy_names.get("cognitive_data_consumptions", ""),
                preserved,
            )
            copied_relations, quarantined_relations = _copy_reconciliations(
                conn,
                legacy_names.get("cognitive_data_reconciliations", ""),
                preserved,
            )
            if failpoint:
                failpoint("after_copy")
            for legacy in reversed(tuple(legacy_names.values())):
                conn.execute(f'DROP TABLE "{legacy}"')  # nosec B608 - internally generated
            _write_registry_row(conn)
            copied_counts = {
                "runtime_registry": copied_registry,
                "runtime_events": copied_runtime_events,
                "runtime_receipts": copied_runtime_receipts,
                "cognitive_events": len(preserved),
                "typed_revisions": typed,
                "cognitive_consumptions": copied_consumptions,
                "reconciliations": copied_relations,
                "quarantined": (
                    quarantined_events + quarantined_consumptions + quarantined_relations
                ),
            }
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise CognitiveStateSchemaError(
                f"post-migration foreign key verification failed: {len(foreign_key_errors)}"
            )
        after = inspect_cognitive_state_schema(conn)
        if not after.ok:
            raise CognitiveStateSchemaError("post-migration schema verification failed")
        if failpoint:
            failpoint("before_commit")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    report.update(
        {
            "applied": True,
            "action": "created" if before.classification == "absent" else "migrated",
            "after": after.as_dict(),
            "copied_counts": copied_counts,
        }
    )
    return report
