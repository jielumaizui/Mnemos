"""Explicit schema owner for the mutable Capture queue database.

Only installation/bootstrap and the reconciliation command call
``CaptureQueueSchema.initialize``.  Runtime producers and diagnostics merely
require this schema; they never provision or migrate it as a side effect.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from core.ops.durable_io import (
    DurableIOError,
    anchored_sqlite_write_connection,
    inspect_path_kind,
    physical_scope_signature,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite


SCHEMA_VERSION = "mnemos.capture_queue_schema.v2"


class CaptureQueueSchemaMigrationRequired(RuntimeError):
    """Raised when Capture needs explicit bootstrap or reconciliation."""


class CaptureQueueSchema:
    """Single DDL and schema-state owner for ``capture_queue.db``."""

    _REQUIRED_TABLES = frozenset(
        {
            "capture_events",
            "capture_idempotency_receipts",
            "capture_raw_failures",
            "source_backoff",
            "session_end_events",
            "capture_distillation_handoffs",
            "capture_schema_meta",
            "capture_maintenance_receipts",
        }
    )
    _REQUIRED_EVENT_COLUMNS = frozenset(
        {
            "id",
            "dedupe_key",
            "source_agent",
            "session_id",
            "turn_id",
            "turn_number",
            "payload_json",
            "content_hash",
            "raw_revision_id",
            "replay_generation",
            "status",
            "retry_count",
            "created_at",
            "processed_at",
            "error",
            "working_dir",
            "deferred_until",
        }
    )

    @classmethod
    def inspect(cls, db_path: Path | str) -> dict[str, Any]:
        """Read schema state without provisioning a path or SQLite sidecars."""
        path = Path(db_path).expanduser()
        try:
            path_kind = inspect_path_kind(path)
        except DurableIOError:
            return {
                "status": "unreadable",
                "db_path": str(path),
                "error": "capture_queue_path_unavailable",
            }
        if path_kind == "missing":
            return {"status": "uninitialized", "db_path": str(path), "missing": ["database"]}
        if path_kind != "file":
            return {
                "status": "unreadable",
                "db_path": str(path),
                "error": "capture_queue_path_not_regular",
            }
        wal_path = Path(f"{path}-wal")
        try:
            wal_signature = physical_scope_signature(
                (wal_path.absolute(),),
                hash_max_bytes=0,
            )
            entries = wal_signature.get("entries")
            if not isinstance(entries, list) or len(entries) != 1:
                raise DurableIOError("capture_queue_wal_signature_invalid")
            wal_entry = entries[0]
            if not isinstance(wal_entry, dict):
                raise DurableIOError("capture_queue_wal_signature_invalid")
            if wal_entry.get("present") is False:
                live_wal = False
            elif wal_entry.get("kind") != "file":
                return {
                    "status": "unreadable",
                    "db_path": str(path),
                    "error": "capture_queue_wal_not_regular",
                }
            else:
                live_wal = int(wal_entry.get("size") or 0) > 0
        except (DurableIOError, OSError, TypeError, ValueError):
            return {
                "status": "unreadable",
                "db_path": str(path),
                "error": "capture_queue_wal_unavailable",
            }
        try:
            conn = connect_readonly_sqlite(
                path,
                immutable=not live_wal,
            )
        except (OSError, sqlite3.Error) as exc:
            return {"status": "unreadable", "db_path": str(path), "error": str(exc)}
        try:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = sorted(cls._REQUIRED_TABLES - tables)
            event_columns: set[str] = set()
            if "capture_events" in tables:
                event_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(capture_events)")
                }
                missing.extend(sorted(cls._REQUIRED_EVENT_COLUMNS - event_columns))
            version = ""
            if "capture_schema_meta" in tables:
                row = conn.execute(
                    "SELECT value FROM capture_schema_meta WHERE key='schema_version'"
                ).fetchone()
                version = str(row[0]) if row else ""
            status = "current" if not missing and version == SCHEMA_VERSION else "migration_required"
            return {
                "status": status,
                "db_path": str(path),
                "schema_version": version,
                "expected_schema_version": SCHEMA_VERSION,
                "missing": sorted(set(missing)),
            }
        except sqlite3.Error as exc:
            return {"status": "unreadable", "db_path": str(path), "error": str(exc)}
        finally:
            conn.close()

    @classmethod
    def require_current(cls, db_path: Path | str) -> None:
        """Fail before a producer opens or changes an unprovisioned database."""
        result = cls.inspect(db_path)
        if result.get("status") != "current":
            raise CaptureQueueSchemaMigrationRequired(
                "capture queue schema is not current; first review "
                "python3 scripts/reconcile_capture_queue_schema.py "
                "--backup-dir <dir> --json, then run --apply with "
                "--expected-plan-hash <sha256>"
            )

    @classmethod
    def initialize(cls, db_path: Path | str) -> dict[str, Any]:
        """Create or migrate schema under an explicit bootstrap owner."""
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with anchored_sqlite_write_connection(path, create=True) as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("BEGIN IMMEDIATE")
                cls._create_or_upgrade(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        result = cls.inspect(path)
        if result.get("status") != "current":
            raise CaptureQueueSchemaMigrationRequired(
                f"capture queue schema bootstrap did not converge: {result}"
            )
        return result

    @classmethod
    def _create_or_upgrade(cls, conn: sqlite3.Connection) -> None:
        cls._execute_schema_statements(
            conn,
            """
            CREATE TABLE IF NOT EXISTS capture_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT UNIQUE,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                turn_number INTEGER,
                payload_json TEXT,
                content_hash TEXT,
                raw_revision_id TEXT NOT NULL DEFAULT '',
                replay_generation INTEGER NOT NULL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                created_at TEXT,
                processed_at TEXT,
                error TEXT,
                working_dir TEXT,
                deferred_until TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dedupe_key ON capture_events(dedupe_key);
            CREATE INDEX IF NOT EXISTS idx_source_status ON capture_events(source_agent, status);
            CREATE INDEX IF NOT EXISTS idx_session_turn ON capture_events(session_id, turn_number);
            CREATE INDEX IF NOT EXISTS idx_status ON capture_events(status);

            CREATE TABLE IF NOT EXISTS capture_idempotency_receipts (
                idempotency_key TEXT PRIMARY KEY,
                source_agent TEXT NOT NULL,
                raw_revision_id TEXT NOT NULL,
                replay_generation INTEGER NOT NULL,
                capture_event_id INTEGER,
                identity_kind TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_agent, raw_revision_id, replay_generation)
            );
            CREATE INDEX IF NOT EXISTS idx_capture_idempotency_event
                ON capture_idempotency_receipts(capture_event_id);

            CREATE TABLE IF NOT EXISTS capture_raw_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_number INTEGER,
                content_hash TEXT NOT NULL,
                error TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_capture_raw_failures_session
                ON capture_raw_failures(source_agent, session_id, turn_number);

            CREATE TABLE IF NOT EXISTS source_backoff (
                source_agent TEXT PRIMARY KEY,
                error_count INTEGER DEFAULT 0,
                last_retry_at TEXT
            );

            CREATE TABLE IF NOT EXISTS session_end_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                receipt_id TEXT,
                status TEXT NOT NULL DEFAULT 'handoff_pending',
                error TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT,
                UNIQUE(source_agent, session_id)
            );

            CREATE TABLE IF NOT EXISTS capture_schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capture_maintenance_receipts (
                receipt_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                candidates_json TEXT NOT NULL,
                applied_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        cls.ensure_handoff_schema(conn)
        cls._add_pre_v2_columns(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_status_deferred "
            "ON capture_events(status, deferred_until)"
        )
        cls._backfill_pre_v2_receipts(conn)
        conn.execute(
            """
            INSERT INTO capture_schema_meta(key, value, updated_at)
            VALUES ('schema_version', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (SCHEMA_VERSION,),
        )

    @staticmethod
    def _execute_schema_statements(conn: sqlite3.Connection, script: str) -> None:
        """Execute fixed DDL without ``executescript``'s implicit commit."""
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(statement)

    @staticmethod
    def ensure_handoff_schema(conn: sqlite3.Connection) -> None:
        """Provision Capture's outbox tables from the one schema owner."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capture_distillation_handoffs (
                receipt_id TEXT PRIMARY KEY,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                input_revision TEXT NOT NULL,
                status TEXT NOT NULL,
                event_ids_json TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                meta_json TEXT NOT NULL,
                downstream_receipt_id TEXT DEFAULT '',
                downstream_task_id TEXT DEFAULT '',
                terminal_reason TEXT DEFAULT '',
                error TEXT DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_agent, session_id, input_revision)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_capture_handoff_status
            ON capture_distillation_handoffs(status, updated_at)
            """
        )

    @staticmethod
    def _add_pre_v2_columns(conn: sqlite3.Connection) -> None:
        event_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(capture_events)")}
        for name, ddl in {
            "deferred_until": "TEXT",
            "raw_revision_id": "TEXT NOT NULL DEFAULT ''",
            "replay_generation": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in event_columns:
                conn.execute(f"ALTER TABLE capture_events ADD COLUMN {name} {ddl}")
        end_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(session_end_events)")}
        for name, ddl in {
            "receipt_id": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'handoff_pending'",
            "error": "TEXT DEFAULT ''",
            "updated_at": "TEXT",
        }.items():
            if name not in end_columns:
                conn.execute(f"ALTER TABLE session_end_events ADD COLUMN {name} {ddl}")

    @staticmethod
    def _backfill_pre_v2_receipts(conn: sqlite3.Connection) -> None:
        """Preserve pre-v2 queue keys without claiming canonical Raw identity."""
        conn.execute(
            """
            INSERT OR IGNORE INTO capture_idempotency_receipts (
                idempotency_key, source_agent, raw_revision_id,
                replay_generation, capture_event_id, identity_kind, created_at
            )
            SELECT dedupe_key, source_agent,
                   CASE WHEN raw_revision_id='' THEN 'legacy:' || dedupe_key
                        ELSE raw_revision_id END,
                   replay_generation, id,
                   CASE WHEN raw_revision_id='' THEN 'legacy_capture_key'
                        ELSE 'canonical_raw_revision' END,
                   COALESCE(created_at, datetime('now'))
            FROM capture_events
            WHERE dedupe_key IS NOT NULL AND dedupe_key != ''
            """
        )
