"""Shared internal state for the model-call-ledger implementation.

This is deliberately not part of the public ledger interface.  It centralizes
the local SQLite lifecycle and the two runtime-mode guards so the schema,
lifecycle, deletion and reporting implementations operate on the same owner.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .contracts import ModelCallLedgerInvariantError


@dataclass
class LedgerState:
    """Mutable construction state shared only by internal ledger modules."""

    db_path: Path
    config: Any | None = None
    runtime_schema_validated: bool = False
    reconciliation_only: bool = False

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open one local ledger connection and always release its handle."""
        conn = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        else:
            if conn.in_transaction:
                conn.commit()
        finally:
            conn.close()

    def require_runtime_write_ready(self) -> None:
        if self.reconciliation_only or not self.runtime_schema_validated:
            raise ModelCallLedgerInvariantError(
                "public model-call mutation requires a validated runtime ledger"
            )


def table_names(conn: sqlite3.Connection) -> set[str]:
    """Return ordinary table names without assigning ownership to extras."""
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def require_delete_journal_mode_for_private_scrub(conn: sqlite3.Connection) -> None:
    """Require a local SQLite state that can safely release redacted cells."""
    try:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0] or 0) != 0:
            raise ModelCallLedgerInvariantError(
                "private model-call scrub cannot checkpoint an active WAL reader"
            )
        row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.Error as exc:
        raise ModelCallLedgerInvariantError(
            "private model-call scrub requires exclusive SQLite journal_mode=DELETE"
        ) from exc
    if row is None or str(row[0] or "").lower() != "delete":
        raise ModelCallLedgerInvariantError(
            "private model-call scrub requires exclusive SQLite journal_mode=DELETE"
        )


def require_secure_delete_for_private_scrub(conn: sqlite3.Connection) -> None:
    """Enable SQLite's local cell cleanup before a redaction/delete operation."""
    conn.execute("PRAGMA secure_delete=ON")
    row = conn.execute("PRAGMA secure_delete").fetchone()
    if row is None or int(row[0] or 0) != 1:
        raise ModelCallLedgerInvariantError(
            "private model-call scrub requires SQLite secure_delete=ON"
        )
