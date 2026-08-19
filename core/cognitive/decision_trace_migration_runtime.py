"""Exclusive runtime and SQLite snapshot primitives for decision migration."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence
from urllib.parse import quote

from core.ops.exclusive_file_lock import exclusive_file_lock


def _exclusive_migration_runtime_lock(database_dir: Path) -> Any:
    root = Path(database_dir)

    @contextmanager
    def hold() -> Iterator[None]:
        """Hold both the migration lock and the daemon lifetime lock."""

        with exclusive_file_lock(
            root / ".decision_trace_history_migration.lock",
            unavailable_message="decision-trace migration lock is already held",
        ):
            # The daemon uses the same OS lock on daemon.pid for its lifetime.
            # Holding it closes the stop-check/start race during the migration.
            with exclusive_file_lock(
                root / "daemon.pid",
                unavailable_message="Mnemos daemon started before migration lock",
            ):
                yield

    return hold()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(Path(path).resolve(strict=True)), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _database_integrity(path: Path) -> str:
    with _connect_read_only(path) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def _sqlite_snapshot_hash(path: Path) -> str:
    with _connect_read_only(path) as conn:
        return _sqlite_connection_snapshot_hash(conn)


def _sqlite_connection_snapshot_hash(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for statement in conn.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _safe_identifier(value: str, allowed: Sequence[str]) -> str:
    if value not in set(allowed):
        raise ValueError(f"unsupported SQLite identifier: {value}")
    return value
