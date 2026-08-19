"""Explicit migration helper for legacy cognitive-profile access tables."""

from __future__ import annotations

import sqlite3
from typing import Callable


def ensure_cognitive_profile_access_schema(
    conn: sqlite3.Connection,
    *,
    statement_callback: Callable[[str], None] | None = None,
) -> None:
    """Install ACL columns without inventing permissions for historic rows."""

    statement_number = 0

    def execute_ddl(sql: str) -> None:
        nonlocal statement_number
        conn.execute(sql)
        statement_number += 1
        if statement_callback is not None:
            statement_callback(f"profile_access_statement:{statement_number}")

    for table_name in ("profile_signals", "profile_assertions", "profile_usage_log"):
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")}
        if "access_control" not in columns:
            execute_ddl(
                f"ALTER TABLE {table_name} "
                "ADD COLUMN access_control TEXT NOT NULL DEFAULT ''"  # nosec B608
            )
    usage_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(profile_usage_log)")}
    if "profile_revision_ids" not in usage_columns:
        execute_ddl(
            "ALTER TABLE profile_usage_log "
            "ADD COLUMN profile_revision_ids TEXT NOT NULL DEFAULT '[]'"
        )
    if "scope_snapshot" not in usage_columns:
        execute_ddl(
            "ALTER TABLE profile_usage_log " "ADD COLUMN scope_snapshot TEXT NOT NULL DEFAULT ''"
        )
    if "read_purpose" not in usage_columns:
        execute_ddl(
            "ALTER TABLE profile_usage_log " "ADD COLUMN read_purpose TEXT NOT NULL DEFAULT ''"
        )
    typed_usage_columns = {
        "matched_assertion_revisions": "TEXT NOT NULL DEFAULT '{}'",
        "read_authorization_token": "TEXT NOT NULL DEFAULT ''",
        "request_id": "TEXT NOT NULL DEFAULT ''",
        "decision_id": "TEXT NOT NULL DEFAULT ''",
        "baseline_hash": "TEXT NOT NULL DEFAULT ''",
        "persona_enabled_hash": "TEXT NOT NULL DEFAULT ''",
        "expected_delta": "TEXT NOT NULL DEFAULT '{}'",
        "actual_target_delta": "TEXT NOT NULL DEFAULT '{}'",
        "target_receipt": "TEXT NOT NULL DEFAULT '{}'",
        "target_receipt_hash": "TEXT NOT NULL DEFAULT ''",
        "terminal_status": "TEXT NOT NULL DEFAULT ''",
        "idempotency_key": "TEXT NOT NULL DEFAULT ''",
    }
    for column_name, column_sql in typed_usage_columns.items():
        if column_name not in usage_columns:
            execute_ddl(
                f"ALTER TABLE profile_usage_log ADD COLUMN {column_name} {column_sql}"  # nosec B608
            )
    execute_ddl("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_usage_idempotency
        ON profile_usage_log(idempotency_key)
        WHERE idempotency_key != ''
        """)
    execute_ddl("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_usage_target_receipt
        ON profile_usage_log(target_receipt_hash)
        WHERE target_receipt_hash != ''
        """)
    execute_ddl("""
        CREATE TABLE IF NOT EXISTS profile_read_authorizations (
            token_id TEXT PRIMARY KEY,
            consumer TEXT NOT NULL,
            read_purpose TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            principal_agent TEXT NOT NULL,
            scope_snapshot TEXT NOT NULL,
            authorized_assertion_revisions TEXT NOT NULL,
            assertion_access_hashes TEXT NOT NULL,
            access_control TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('issued', 'consumed')),
            consumed_command_id TEXT NOT NULL DEFAULT '',
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """)
    execute_ddl("""
        CREATE INDEX IF NOT EXISTS idx_profile_read_authorizations_status
        ON profile_read_authorizations(status, expires_at)
        """)
    execute_ddl("""
        CREATE TABLE IF NOT EXISTS profile_usage_outbox (
            command_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            intent_json TEXT NOT NULL,
            target_receipt_hash TEXT NOT NULL UNIQUE,
            access_control TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'committed')),
            usage_id INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (usage_id) REFERENCES profile_usage_log(id)
        )
        """)
    outbox_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(profile_usage_outbox)")
    }
    if "access_control" not in outbox_columns:
        execute_ddl(
            "ALTER TABLE profile_usage_outbox " "ADD COLUMN access_control TEXT NOT NULL DEFAULT ''"
        )
    execute_ddl("""
        CREATE INDEX IF NOT EXISTS idx_profile_usage_outbox_status
        ON profile_usage_outbox(status, created_at)
        """)
