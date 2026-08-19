# -*- coding: utf-8 -*-
"""Durable cursors for lossless continuous AgentSource Raw capture.

The scheduler cursor only rotates a discovered source denominator.  The
per-session cursor is separate and represents the next native turn that has
not yet been confirmed in canonical Raw.  Keeping those responsibilities
separate prevents a bounded batch from ever being mistaken for completion.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterable, Iterator
from uuid import uuid4

CURSOR_FILE_NAME = "agent_sync_cursors.db"


class AgentSyncCursorError(RuntimeError):
    """Raised when the durable cursor ledger cannot safely be used."""


@dataclass(frozen=True)
class SessionRawCursor:
    """Raw-commit high-water state for one canonical source session."""

    source_name: str
    canonical_session_id: str
    next_turn_number: int | None
    last_raw_commit_at: str = ""


@dataclass(frozen=True)
class SourceDenominatorProgress:
    """Persistent reconciliation evidence for one discovered source denominator."""

    source_name: str
    generation_id: str
    roster_hash: str
    session_count: int
    observed_session_count: int
    observed_turn_count: int
    complete: bool
    completed_at: str = ""


@dataclass(frozen=True)
class SourceCaptureFingerprintState:
    """Content-free identity of one exact native-turn-to-Raw generation."""

    source_name: str
    generation_id: str
    roster_hash: str
    generation_eligible: bool
    expected_turn_count: int
    receipt_count: int
    exact_receipt_count: int
    pending_turn_count: int
    orphan_receipt_count: int
    denominator_session_set_hash: str
    expected_turn_fingerprint_set_hash: str
    receipt_binding_set_hash: str

    @property
    def complete(self) -> bool:
        """Whether every expected turn has one exact, non-orphan Raw receipt."""
        return bool(
            self.generation_eligible
            and self.pending_turn_count == 0
            and self.orphan_receipt_count == 0
            and self.exact_receipt_count == self.expected_turn_count
            and self.receipt_count == self.expected_turn_count
        )

    def to_cursor_fields(self) -> dict[str, object]:
        """Project the exact proof into the content-free runtime cursor."""
        return {
            "capture_generation_id": self.generation_id,
            "capture_roster_hash": self.roster_hash,
            "capture_generation_eligible": self.generation_eligible,
            "capture_expected_turn_count": self.expected_turn_count,
            "capture_receipt_count": self.receipt_count,
            "capture_exact_receipt_count": self.exact_receipt_count,
            "capture_pending_turn_count": self.pending_turn_count,
            "capture_orphan_receipt_count": self.orphan_receipt_count,
            "capture_denominator_session_set_hash": (self.denominator_session_set_hash),
            "capture_expected_turn_fingerprint_set_hash": (self.expected_turn_fingerprint_set_hash),
            "capture_receipt_binding_set_hash": self.receipt_binding_set_hash,
        }


@dataclass(frozen=True)
class SourceReconciliationReset:
    """Auditable removal counts for one derived source reconciliation state.

    This deliberately covers only scheduler/cursor evidence.  It never
    deletes canonical Raw rows or immutable revisions; callers must rebuild
    the state by parsing the current native denominator and obtaining fresh
    Raw receipts.
    """

    source_name: str
    session_raw_cursor_count: int
    reconciliation_cursor_count: int
    denominator_state_count: int
    denominator_session_count: int
    capture_generation_count: int
    capture_expected_turn_count: int
    capture_raw_receipt_count: int


def cursor_store_path(database_dir: Path) -> Path:
    """Return the daemon-owned cursor ledger path."""
    return Path(database_dir) / CURSOR_FILE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strict_int(value: object, label: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise AgentSyncCursorError(f"cursor ledger returned invalid {label}")


def _valid_sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _row_set_hash(rows: Iterable[tuple[object, ...]]) -> str:
    """Hash sorted typed rows without delimiter ambiguity or native content."""
    rendered = json.dumps(
        [list(row) for row in sorted(rows)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(rendered.encode("utf-8")).hexdigest()


CURSOR_SCHEMA_VERSION = "mnemos.agent_sync_cursor.v5"
FINGERPRINTLESS_CURSOR_SCHEMA_VERSION = "mnemos.agent_sync_cursor.v4"
PREVIOUS_CURSOR_SCHEMA_VERSION = "mnemos.agent_sync_cursor.v3"
SNAPSHOTLESS_CURSOR_SCHEMA_VERSION = "mnemos.agent_sync_cursor.v2"
LEGACY_CURSOR_SCHEMA_VERSION = "mnemos.agent_sync_cursor.v1"

_REQUIRED_TABLES = frozenset(
    {
        "cursor_schema",
        "session_raw_cursors",
        "source_reconciliation_cursors",
        "source_denominator_state",
        "source_denominator_sessions",
        "source_capture_generations",
        "source_capture_expected_turns",
        "source_capture_raw_receipts",
    }
)


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()  # nosec B608
    }


def _validate_cursor_schema_v5(connection: sqlite3.Connection) -> None:
    """Reject partial evidence ledgers instead of silently repairing them."""
    table_names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not _REQUIRED_TABLES.issubset(table_names):
        raise AgentSyncCursorError("incomplete agent sync cursor schema; reconciliation required")
    required_columns = {
        "source_capture_generations": {
            "source_name",
            "generation_id",
            "roster_hash",
            "native_source_snapshot_hash",
            "snapshot_binding_eligible",
            "started_at",
        },
        "source_capture_expected_turns": {
            "source_name",
            "generation_id",
            "canonical_session_id",
            "turn_number",
            "turn_fingerprint",
            "observed_at",
        },
        "source_capture_raw_receipts": {
            "source_name",
            "generation_id",
            "canonical_session_id",
            "turn_number",
            "raw_revision_id",
            "turn_fingerprint",
            "recorded_at",
        },
        "source_denominator_sessions": {
            "source_name",
            "canonical_session_id",
            "roster_hash",
            "turn_count",
            "disposition",
            "disposition_reason",
            "artifact_evidence_hash",
            "observed_at",
        },
    }
    for table_name, expected in required_columns.items():
        if not expected.issubset(_table_columns(connection, table_name)):
            raise AgentSyncCursorError(
                "invalid agent sync capture evidence schema; reconciliation required"
            )


def _initialize_cursor_schema_v5(connection: sqlite3.Connection) -> None:
    """Create a new v5 ledger. Existing state uses explicit migration."""
    script = """
        CREATE TABLE session_raw_cursors (
            source_name TEXT NOT NULL,
            canonical_session_id TEXT NOT NULL,
            next_turn_number INTEGER NOT NULL,
            last_raw_commit_at TEXT NOT NULL,
            PRIMARY KEY (source_name, canonical_session_id)
        );
        CREATE TABLE source_reconciliation_cursors (
            source_name TEXT PRIMARY KEY,
            after_canonical_session_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE source_denominator_state (
            source_name TEXT PRIMARY KEY,
            roster_hash TEXT NOT NULL,
            session_count INTEGER NOT NULL,
            observed_session_count INTEGER NOT NULL DEFAULT 0,
            observed_turn_count INTEGER NOT NULL DEFAULT 0,
            complete INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE source_denominator_sessions (
            source_name TEXT NOT NULL,
            canonical_session_id TEXT NOT NULL,
            roster_hash TEXT NOT NULL,
            turn_count INTEGER NOT NULL,
            disposition TEXT NOT NULL,
            disposition_reason TEXT NOT NULL,
            artifact_evidence_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source_name, canonical_session_id)
        );
        CREATE INDEX idx_denominator_sessions_source_roster
            ON source_denominator_sessions(source_name, roster_hash);
        CREATE TABLE source_capture_generations (
            source_name TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL UNIQUE,
            roster_hash TEXT NOT NULL,
            native_source_snapshot_hash TEXT NOT NULL DEFAULT '',
            snapshot_binding_eligible INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL
        );
        CREATE TABLE source_capture_expected_turns (
            source_name TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            canonical_session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            turn_fingerprint TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (source_name, generation_id, canonical_session_id, turn_number)
        );
        CREATE INDEX idx_capture_expected_turns_source_generation
            ON source_capture_expected_turns(source_name, generation_id, canonical_session_id);
        CREATE TABLE source_capture_raw_receipts (
            source_name TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            canonical_session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            raw_revision_id TEXT NOT NULL,
            turn_fingerprint TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (source_name, generation_id, canonical_session_id, turn_number)
        );
        CREATE INDEX idx_capture_raw_receipts_source_generation
            ON source_capture_raw_receipts(source_name, generation_id, canonical_session_id);
        """
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute(
        "INSERT INTO cursor_schema (key, value) VALUES ('schema_version', ?)",
        (CURSOR_SCHEMA_VERSION,),
    )


def migrate_historical_cursor_schema(connection: sqlite3.Connection) -> None:
    """Migrate v1-v4 without inventing snapshot, disposition, or turn proof."""
    row = connection.execute(
        "SELECT value FROM cursor_schema WHERE key='schema_version'"
    ).fetchone()
    before_version = str(row[0]) if row is not None else ""
    if before_version not in {
        LEGACY_CURSOR_SCHEMA_VERSION,
        SNAPSHOTLESS_CURSOR_SCHEMA_VERSION,
        PREVIOUS_CURSOR_SCHEMA_VERSION,
        FINGERPRINTLESS_CURSOR_SCHEMA_VERSION,
    }:
        raise AgentSyncCursorError(
            "v1, v2, v3, or v4 agent sync cursor schema is required for migration"
        )
    legacy_tables = {
        "session_raw_cursors",
        "source_reconciliation_cursors",
        "source_denominator_state",
        "source_denominator_sessions",
    }
    table_names = {
        str(item[0])
        for item in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not legacy_tables.issubset(table_names):
        raise AgentSyncCursorError("legacy agent sync cursor schema is incomplete")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if before_version == LEGACY_CURSOR_SCHEMA_VERSION:
            for statement in (
                """
                CREATE TABLE source_capture_generations (
                    source_name TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL UNIQUE,
                    roster_hash TEXT NOT NULL,
                    native_source_snapshot_hash TEXT NOT NULL DEFAULT '',
                    snapshot_binding_eligible INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE source_capture_expected_turns (
                    source_name TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    canonical_session_id TEXT NOT NULL,
                    turn_number INTEGER NOT NULL,
                    turn_fingerprint TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (source_name, generation_id, canonical_session_id, turn_number)
                )
                """,
                """
                CREATE INDEX idx_capture_expected_turns_source_generation
                    ON source_capture_expected_turns(source_name, generation_id, canonical_session_id)
                """,
                """
                CREATE TABLE source_capture_raw_receipts (
                    source_name TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    canonical_session_id TEXT NOT NULL,
                    turn_number INTEGER NOT NULL,
                    raw_revision_id TEXT NOT NULL,
                    turn_fingerprint TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (source_name, generation_id, canonical_session_id, turn_number)
                )
                """,
                """
                CREATE INDEX idx_capture_raw_receipts_source_generation
                    ON source_capture_raw_receipts(source_name, generation_id, canonical_session_id)
                """,
            ):
                connection.execute(statement)
        elif before_version == SNAPSHOTLESS_CURSOR_SCHEMA_VERSION:
            connection.execute("""
                ALTER TABLE source_capture_generations
                ADD COLUMN native_source_snapshot_hash TEXT NOT NULL DEFAULT ''
                """)
            connection.execute("""
                ALTER TABLE source_capture_generations
                ADD COLUMN snapshot_binding_eligible INTEGER NOT NULL DEFAULT 0
                """)
        if before_version != FINGERPRINTLESS_CURSOR_SCHEMA_VERSION:
            connection.execute("""
                ALTER TABLE source_denominator_sessions
                ADD COLUMN disposition TEXT NOT NULL DEFAULT 'legacy_unverified'
                """)
            connection.execute("""
                ALTER TABLE source_denominator_sessions
                ADD COLUMN disposition_reason TEXT NOT NULL DEFAULT ''
                """)
            connection.execute("""
                ALTER TABLE source_denominator_sessions
                ADD COLUMN artifact_evidence_hash TEXT NOT NULL DEFAULT ''
                """)
        if before_version != LEGACY_CURSOR_SCHEMA_VERSION:
            connection.execute("""
                ALTER TABLE source_capture_expected_turns
                ADD COLUMN turn_fingerprint TEXT NOT NULL DEFAULT ''
                """)
            connection.execute("""
                ALTER TABLE source_capture_raw_receipts
                ADD COLUMN turn_fingerprint TEXT NOT NULL DEFAULT ''
                """)
        connection.execute("""
            UPDATE source_capture_generations
            SET snapshot_binding_eligible=0,
                native_source_snapshot_hash=''
            """)
        connection.execute(
            "UPDATE cursor_schema SET value=? WHERE key='schema_version'",
            (CURSOR_SCHEMA_VERSION,),
        )
        _validate_cursor_schema_v5(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


class AgentSyncCursorStore:
    """Small SQLite ledger with atomic, monotonic cursor advancement."""

    def __init__(self, database_dir: Path):
        self.path = cursor_store_path(Path(database_dir))

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._ensure_schema(connection)
        except BaseException:
            connection.close()
            raise
        try:
            for artifact in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            ):
                try:
                    artifact.lstat()
                except FileNotFoundError:
                    continue
                os.chmod(artifact, 0o600)
        except OSError as exc:
            connection.close()
            raise AgentSyncCursorError("cannot secure agent sync cursor ledger") from exc
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back one operation and always close its descriptor."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        """Initialize only an empty v4 ledger; require explicit older migration."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS cursor_schema (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """)
            row = connection.execute(
                "SELECT value FROM cursor_schema WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                other_tables = connection.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT IN ('cursor_schema', 'sqlite_sequence')
                    """).fetchall()
                if other_tables:
                    raise AgentSyncCursorError(
                        "unversioned agent sync cursor schema; reconciliation required"
                    )
                _initialize_cursor_schema_v5(connection)
                _validate_cursor_schema_v5(connection)
            elif str(row[0]) in {
                LEGACY_CURSOR_SCHEMA_VERSION,
                SNAPSHOTLESS_CURSOR_SCHEMA_VERSION,
                PREVIOUS_CURSOR_SCHEMA_VERSION,
                FINGERPRINTLESS_CURSOR_SCHEMA_VERSION,
            }:
                raise AgentSyncCursorError(
                    "legacy agent sync cursor schema; explicit reconciliation required"
                )
            elif str(row[0]) != CURSOR_SCHEMA_VERSION:
                raise AgentSyncCursorError(
                    "unknown agent sync cursor schema; reconciliation required"
                )
            else:
                _validate_cursor_schema_v5(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def get_session_raw_cursor(
        self,
        source_name: str,
        canonical_session_id: str,
    ) -> SessionRawCursor:
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT next_turn_number, last_raw_commit_at
                    FROM session_raw_cursors
                    WHERE source_name=? AND canonical_session_id=?
                    """,
                    (source_name, canonical_session_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot read agent session Raw cursor") from exc
        if row is None:
            return SessionRawCursor(source_name, canonical_session_id, None)
        return SessionRawCursor(
            source_name=source_name,
            canonical_session_id=canonical_session_id,
            next_turn_number=int(row[0]),
            last_raw_commit_at=str(row[1] or ""),
        )

    def advance_session_raw_cursor(
        self,
        source_name: str,
        canonical_session_id: str,
        *,
        next_turn_number: int,
    ) -> SessionRawCursor:
        """Advance only after the caller has a canonical Raw commit receipt.

        The SQL guard makes concurrent tail/reconciliation attempts monotonic:
        a stale worker cannot move a cursor backwards or overwrite a newer
        high-water mark.
        """
        if next_turn_number < 0:
            raise AgentSyncCursorError("next turn number must be non-negative")
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO session_raw_cursors (
                        source_name, canonical_session_id, next_turn_number, last_raw_commit_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(source_name, canonical_session_id) DO UPDATE SET
                        next_turn_number=excluded.next_turn_number,
                        last_raw_commit_at=excluded.last_raw_commit_at
                    WHERE excluded.next_turn_number > session_raw_cursors.next_turn_number
                    """,
                    (source_name, canonical_session_id, next_turn_number, _now()),
                )
                row = connection.execute(
                    """
                    SELECT next_turn_number, last_raw_commit_at
                    FROM session_raw_cursors
                    WHERE source_name=? AND canonical_session_id=?
                    """,
                    (source_name, canonical_session_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot advance agent session Raw cursor") from exc
        if row is None:  # pragma: no cover - SQLite UPSERT guarantee
            raise AgentSyncCursorError("cursor advance was not persisted")
        return SessionRawCursor(
            source_name=source_name,
            canonical_session_id=canonical_session_id,
            next_turn_number=int(row[0]),
            last_raw_commit_at=str(row[1] or ""),
        )

    def reconciliation_after(self, source_name: str) -> str:
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT after_canonical_session_id
                    FROM source_reconciliation_cursors
                    WHERE source_name=?
                    """,
                    (source_name,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot read source reconciliation cursor") from exc
        return str(row[0]) if row else ""

    def advance_reconciliation_after(
        self,
        source_name: str,
        canonical_session_id: str,
    ) -> None:
        """Persist the round-robin scheduling position for a source denominator."""
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO source_reconciliation_cursors (
                        source_name, after_canonical_session_id, updated_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(source_name) DO UPDATE SET
                        after_canonical_session_id=excluded.after_canonical_session_id,
                        updated_at=excluded.updated_at
                    """,
                    (source_name, canonical_session_id, _now()),
                )
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot advance source reconciliation cursor") from exc

    def select_reconciliation_session_ids(
        self,
        source_name: str,
        canonical_session_ids: Iterable[str],
        *,
        limit: int,
    ) -> list[str]:
        """Return a stable round-robin slice of the complete discovered denominator."""
        if limit <= 0:
            return []
        session_ids = sorted({str(value) for value in canonical_session_ids if str(value)})
        if not session_ids:
            return []
        after = self.reconciliation_after(source_name)
        start = next(
            (index for index, session_id in enumerate(session_ids) if session_id > after),
            0,
        )
        count = min(limit, len(session_ids))
        return [session_ids[(start + offset) % len(session_ids)] for offset in range(count)]

    @staticmethod
    def _roster_hash(canonical_session_ids: Iterable[str]) -> str:
        rendered = "\0".join(sorted({str(value) for value in canonical_session_ids if str(value)}))
        return sha256(rendered.encode("utf-8")).hexdigest()

    @staticmethod
    def _progress_from_row(
        source_name: str,
        row: tuple[object, ...],
        generation_id: str,
    ) -> SourceDenominatorProgress:
        return SourceDenominatorProgress(
            source_name=source_name,
            generation_id=generation_id,
            roster_hash=str(row[0]),
            session_count=_strict_int(row[1], "session count"),
            observed_session_count=_strict_int(row[2], "observed session count"),
            observed_turn_count=_strict_int(row[3], "observed turn count"),
            complete=bool(row[4]),
            completed_at=str(row[5] or ""),
        )

    def begin_source_denominator(
        self,
        source_name: str,
        canonical_session_ids: Iterable[str],
    ) -> SourceDenominatorProgress:
        """Start or resume a roster-bound reconciliation generation.

        A changed canonical session roster invalidates the old aggregate.  The
        next full rotation must observe every member again before reporting an
        exact turn denominator.
        """
        session_ids = sorted({str(value) for value in canonical_session_ids if str(value)})
        roster_hash = self._roster_hash(session_ids)
        now = _now()
        generation_id = ""
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT roster_hash, session_count, observed_session_count,
                           observed_turn_count, complete, completed_at
                    FROM source_denominator_state WHERE source_name=?
                    """,
                    (source_name,),
                ).fetchone()
                if row is None or str(row[0]) != roster_hash:
                    # A new roster starts a new proof generation.  Retaining
                    # old high-water marks would skip current native turns and
                    # leave receipt evidence deceptively incomplete.
                    connection.execute(
                        "DELETE FROM session_raw_cursors WHERE source_name=?",
                        (source_name,),
                    )
                    connection.execute(
                        "DELETE FROM source_reconciliation_cursors WHERE source_name=?",
                        (source_name,),
                    )
                    connection.execute(
                        "DELETE FROM source_denominator_sessions WHERE source_name=?",
                        (source_name,),
                    )
                    connection.execute(
                        "DELETE FROM source_capture_expected_turns WHERE source_name=?",
                        (source_name,),
                    )
                    connection.execute(
                        "DELETE FROM source_capture_raw_receipts WHERE source_name=?",
                        (source_name,),
                    )
                    connection.execute(
                        "DELETE FROM source_capture_generations WHERE source_name=?",
                        (source_name,),
                    )
                    connection.execute(
                        """
                        INSERT INTO source_denominator_state (
                            source_name, roster_hash, session_count,
                            observed_session_count, observed_turn_count,
                            complete, completed_at, updated_at
                        ) VALUES (?, ?, ?, 0, 0, ?, ?, ?)
                        ON CONFLICT(source_name) DO UPDATE SET
                            roster_hash=excluded.roster_hash,
                            session_count=excluded.session_count,
                            observed_session_count=excluded.observed_session_count,
                            observed_turn_count=excluded.observed_turn_count,
                            complete=excluded.complete,
                            completed_at=excluded.completed_at,
                            updated_at=excluded.updated_at
                        """,
                        (
                            source_name,
                            roster_hash,
                            len(session_ids),
                            1 if not session_ids else 0,
                            now if not session_ids else "",
                            now,
                        ),
                    )
                    generation_id = f"capture-gen-{uuid4().hex}"
                    connection.execute(
                        """
                        INSERT INTO source_capture_generations (
                            source_name, generation_id, roster_hash,
                            native_source_snapshot_hash, snapshot_binding_eligible,
                            started_at
                        ) VALUES (?, ?, ?, '', 1, ?)
                        """,
                        (source_name, generation_id, roster_hash, now),
                    )
                else:
                    generation = connection.execute(
                        """
                        SELECT generation_id, roster_hash,
                               snapshot_binding_eligible
                        FROM source_capture_generations WHERE source_name=?
                        """,
                        (source_name,),
                    ).fetchone()
                    if (
                        generation is None
                        or str(generation[1]) != roster_hash
                        or int(generation[2]) != 1
                    ):
                        raise AgentSyncCursorError(
                            "source capture generation missing; explicit reset required"
                        )
                    generation_id = str(generation[0])
                row = connection.execute(
                    """
                    SELECT roster_hash, session_count, observed_session_count,
                           observed_turn_count, complete, completed_at
                    FROM source_denominator_state WHERE source_name=?
                    """,
                    (source_name,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AgentSyncCursorError(
                "cannot initialize source denominator reconciliation"
            ) from exc
        if row is None or not generation_id:  # pragma: no cover - database contract
            raise AgentSyncCursorError("source denominator state was not persisted")
        return self._progress_from_row(source_name, row, generation_id)

    def record_denominator_session(
        self,
        source_name: str,
        canonical_session_id: str,
        *,
        turn_count: int,
        turn_numbers: Iterable[int] | None = None,
        turn_fingerprints: dict[int, str] | None = None,
        disposition: str | None = None,
        disposition_reason: str | None = None,
        artifact_evidence_hash: str = "",
    ) -> SourceDenominatorProgress:
        """Record one parsed session and its exact current turn-number domain."""
        if turn_count < 0:
            raise AgentSyncCursorError("denominator turn count must be non-negative")
        numbers = (
            list(range(turn_count))
            if turn_numbers is None
            else [int(value) for value in turn_numbers]
        )
        if (
            len(numbers) != turn_count
            or any(number < 0 for number in numbers)
            or len(set(numbers)) != len(numbers)
        ):
            raise AgentSyncCursorError("denominator turn numbers are invalid")
        fingerprints = {
            int(number): str(fingerprint or "")
            for number, fingerprint in (turn_fingerprints or {}).items()
        }
        if set(fingerprints) != set(numbers) or any(
            not _valid_sha256(value) for value in fingerprints.values()
        ):
            raise AgentSyncCursorError("denominator turn fingerprints are invalid")
        resolved_disposition = disposition or ("parsed" if turn_count else "typed_empty")
        resolved_reason = disposition_reason or (
            "native_turns_parsed" if turn_count else "valid_empty_native_session"
        )
        if resolved_disposition not in {
            "parsed",
            "typed_empty",
            "evidence_excluded",
        }:
            raise AgentSyncCursorError("denominator session disposition is invalid")
        if (resolved_disposition == "parsed") != bool(turn_count) or not resolved_reason:
            raise AgentSyncCursorError("denominator session disposition is inconsistent")
        if not (
            artifact_evidence_hash.startswith("sha256:")
            and _valid_sha256(artifact_evidence_hash.removeprefix("sha256:"))
        ):
            raise AgentSyncCursorError("denominator artifact evidence hash is invalid")
        generation_id = ""
        try:
            with self._transaction() as connection:
                state = connection.execute(
                    """
                    SELECT roster_hash, session_count FROM source_denominator_state
                    WHERE source_name=?
                    """,
                    (source_name,),
                ).fetchone()
                if state is None:
                    raise AgentSyncCursorError("source denominator generation is missing")
                roster_hash = str(state[0])
                generation = connection.execute(
                    """
                    SELECT generation_id, roster_hash
                    FROM source_capture_generations WHERE source_name=?
                    """,
                    (source_name,),
                ).fetchone()
                if generation is None or str(generation[1]) != roster_hash:
                    raise AgentSyncCursorError(
                        "source capture generation missing; explicit reset required"
                    )
                generation_id = str(generation[0])
                now = _now()
                connection.execute(
                    """
                    UPDATE source_capture_generations
                    SET native_source_snapshot_hash=''
                    WHERE source_name=?
                    """,
                    (source_name,),
                )
                connection.execute(
                    """
                    INSERT INTO source_denominator_sessions (
                        source_name, canonical_session_id, roster_hash, turn_count,
                        disposition, disposition_reason, artifact_evidence_hash,
                        observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_name, canonical_session_id) DO UPDATE SET
                        roster_hash=excluded.roster_hash,
                        turn_count=excluded.turn_count,
                        disposition=excluded.disposition,
                        disposition_reason=excluded.disposition_reason,
                        artifact_evidence_hash=excluded.artifact_evidence_hash,
                        observed_at=excluded.observed_at
                    """,
                    (
                        source_name,
                        canonical_session_id,
                        roster_hash,
                        turn_count,
                        resolved_disposition,
                        resolved_reason,
                        artifact_evidence_hash,
                        now,
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM source_capture_expected_turns
                    WHERE source_name=? AND generation_id=? AND canonical_session_id=?
                    """,
                    (source_name, generation_id, canonical_session_id),
                )
                connection.executemany(
                    """
                    INSERT INTO source_capture_expected_turns (
                        source_name, generation_id, canonical_session_id,
                        turn_number, turn_fingerprint, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            source_name,
                            generation_id,
                            canonical_session_id,
                            number,
                            fingerprints[number],
                            now,
                        )
                        for number in numbers
                    ],
                )
                connection.execute(
                    """
                    DELETE FROM source_capture_raw_receipts AS receipt
                    WHERE receipt.source_name=?
                      AND receipt.generation_id=?
                      AND receipt.canonical_session_id=?
                      AND NOT EXISTS (
                          SELECT 1
                          FROM source_capture_expected_turns AS expected
                          WHERE expected.source_name=receipt.source_name
                            AND expected.generation_id=receipt.generation_id
                            AND expected.canonical_session_id=receipt.canonical_session_id
                            AND expected.turn_number=receipt.turn_number
                      )
                    """,
                    (source_name, generation_id, canonical_session_id),
                )
                observed_count, observed_turns = connection.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(turn_count), 0)
                    FROM source_denominator_sessions
                    WHERE source_name=? AND roster_hash=?
                    """,
                    (source_name, roster_hash),
                ).fetchone()
                session_count = int(state[1])
                complete = int(observed_count) == session_count
                connection.execute(
                    """
                    UPDATE source_denominator_state
                    SET observed_session_count=?, observed_turn_count=?, complete=?,
                        completed_at=?, updated_at=?
                    WHERE source_name=?
                    """,
                    (
                        int(observed_count),
                        int(observed_turns),
                        1 if complete else 0,
                        now if complete else "",
                        now,
                        source_name,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT roster_hash, session_count, observed_session_count,
                           observed_turn_count, complete, completed_at
                    FROM source_denominator_state WHERE source_name=?
                    """,
                    (source_name,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot persist source denominator evidence") from exc
        if row is None or not generation_id:  # pragma: no cover - database contract
            raise AgentSyncCursorError("source denominator evidence was not persisted")
        return self._progress_from_row(source_name, row, generation_id)

    def record_raw_capture_receipts(
        self,
        source_name: str,
        canonical_session_id: str,
        receipts: Iterable[tuple[int, str, str]],
    ) -> None:
        """Bind selected native turn numbers to immutable Raw revision receipts.

        This is deliberately derived evidence: it proves exactly which current
        denominator turn was committed during this generation, while the Raw
        store remains the independent owner of revision identity and content.
        """
        normalized: list[tuple[int, str, str]] = []
        for turn_number, revision_id, turn_fingerprint in receipts:
            number = int(turn_number)
            revision = str(revision_id or "")
            fingerprint = str(turn_fingerprint or "")
            if number < 0 or not revision or not _valid_sha256(fingerprint):
                raise AgentSyncCursorError("invalid raw capture receipt")
            normalized.append((number, revision, fingerprint))
        if not normalized:
            return
        if len({number for number, _revision, _fingerprint in normalized}) != len(normalized):
            raise AgentSyncCursorError("duplicate raw capture receipt turn")
        try:
            with self._transaction() as connection:
                generation = connection.execute(
                    "SELECT generation_id FROM source_capture_generations WHERE source_name=?",
                    (source_name,),
                ).fetchone()
                if generation is None:
                    raise AgentSyncCursorError(
                        "source capture generation missing; explicit reset required"
                    )
                generation_id = str(generation[0])
                connection.execute(
                    """
                    UPDATE source_capture_generations
                    SET native_source_snapshot_hash=''
                    WHERE source_name=?
                    """,
                    (source_name,),
                )
                expected = {
                    int(row[0]): str(row[1] or "")
                    for row in connection.execute(
                        """
                        SELECT turn_number, turn_fingerprint
                        FROM source_capture_expected_turns
                        WHERE source_name=? AND generation_id=? AND canonical_session_id=?
                        """,
                        (source_name, generation_id, canonical_session_id),
                    ).fetchall()
                }
                supplied = {number: fingerprint for number, _revision, fingerprint in normalized}
                if (
                    not expected
                    or not set(supplied).issubset(expected)
                    or any(supplied[number] != expected[number] for number in supplied)
                ):
                    raise AgentSyncCursorError("raw capture receipt is outside current denominator")
                now = _now()
                connection.executemany(
                    """
                    INSERT INTO source_capture_raw_receipts (
                        source_name, generation_id, canonical_session_id,
                        turn_number, raw_revision_id, turn_fingerprint, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_name, generation_id, canonical_session_id, turn_number)
                    DO UPDATE SET raw_revision_id=excluded.raw_revision_id,
                                  turn_fingerprint=excluded.turn_fingerprint,
                                  recorded_at=excluded.recorded_at
                    """,
                    [
                        (
                            source_name,
                            generation_id,
                            canonical_session_id,
                            number,
                            revision,
                            fingerprint,
                            now,
                        )
                        for number, revision, fingerprint in normalized
                    ],
                )
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot persist raw capture receipts") from exc

    def pending_session_turn_numbers(
        self,
        source_name: str,
        canonical_session_id: str,
    ) -> list[int]:
        """Return current turns without an exact fingerprint-bound Raw receipt."""
        try:
            with self._transaction() as connection:
                generation = connection.execute(
                    """
                    SELECT generation_id FROM source_capture_generations
                    WHERE source_name=?
                    """,
                    (source_name,),
                ).fetchone()
                if generation is None:
                    raise AgentSyncCursorError(
                        "source capture generation missing; explicit reset required"
                    )
                rows = connection.execute(
                    """
                    SELECT expected.turn_number
                    FROM source_capture_expected_turns AS expected
                    LEFT JOIN source_capture_raw_receipts AS receipt
                      ON receipt.source_name=expected.source_name
                     AND receipt.generation_id=expected.generation_id
                     AND receipt.canonical_session_id=expected.canonical_session_id
                     AND receipt.turn_number=expected.turn_number
                    WHERE expected.source_name=?
                      AND expected.generation_id=?
                      AND expected.canonical_session_id=?
                      AND (
                          receipt.raw_revision_id IS NULL
                          OR receipt.turn_fingerprint != expected.turn_fingerprint
                      )
                    ORDER BY expected.turn_number
                    """,
                    (
                        source_name,
                        str(generation[0]),
                        canonical_session_id,
                    ),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot read pending source capture turns") from exc
        return [int(row[0]) for row in rows]

    @staticmethod
    def _capture_fingerprint_state_from_connection(
        connection: sqlite3.Connection,
        source_name: str,
    ) -> SourceCaptureFingerprintState:
        generation = connection.execute(
            """
            SELECT generation_id, roster_hash, snapshot_binding_eligible
            FROM source_capture_generations WHERE source_name=?
            """,
            (source_name,),
        ).fetchone()
        denominator = connection.execute(
            """
            SELECT roster_hash
            FROM source_denominator_state WHERE source_name=?
            """,
            (source_name,),
        ).fetchone()
        if (
            generation is None
            or denominator is None
            or not str(generation[0] or "")
            or str(generation[1] or "") != str(denominator[0] or "")
            or int(generation[2]) not in {0, 1}
        ):
            raise AgentSyncCursorError("source capture generation missing; explicit reset required")
        generation_id = str(generation[0])
        roster_hash = str(generation[1])
        if not _valid_sha256(roster_hash):
            raise AgentSyncCursorError("source capture roster hash is invalid")
        denominator_rows = [
            (
                str(row[0]),
                _strict_int(row[1], "denominator session turn count"),
                str(row[2] or ""),
                str(row[3] or ""),
                str(row[4] or ""),
            )
            for row in connection.execute(
                """
                SELECT canonical_session_id, turn_count, disposition,
                       disposition_reason, artifact_evidence_hash
                FROM source_denominator_sessions
                WHERE source_name=? AND roster_hash=?
                ORDER BY canonical_session_id
                """,
                (source_name, roster_hash),
            ).fetchall()
        ]
        expected_rows = [
            (
                str(row[0]),
                _strict_int(row[1], "expected turn number"),
                str(row[2] or ""),
            )
            for row in connection.execute(
                """
                SELECT canonical_session_id, turn_number, turn_fingerprint
                FROM source_capture_expected_turns
                WHERE source_name=? AND generation_id=?
                ORDER BY canonical_session_id, turn_number
                """,
                (source_name, generation_id),
            ).fetchall()
        ]
        receipt_rows = [
            (
                str(row[0]),
                _strict_int(row[1], "receipt turn number"),
                str(row[2] or ""),
                str(row[3] or ""),
            )
            for row in connection.execute(
                """
                SELECT canonical_session_id, turn_number, raw_revision_id,
                       turn_fingerprint
                FROM source_capture_raw_receipts
                WHERE source_name=? AND generation_id=?
                ORDER BY canonical_session_id, turn_number
                """,
                (source_name, generation_id),
            ).fetchall()
        ]
        if any(
            turn_number < 0 or not _valid_sha256(fingerprint)
            for _session_id, turn_number, fingerprint in expected_rows
        ):
            raise AgentSyncCursorError("source capture expected fingerprint evidence is invalid")
        if any(
            turn_number < 0 or not revision_id or not _valid_sha256(fingerprint)
            for _session_id, turn_number, revision_id, fingerprint in receipt_rows
        ):
            raise AgentSyncCursorError("source capture receipt fingerprint evidence is invalid")
        expected = {
            (session_id, turn_number): fingerprint
            for session_id, turn_number, fingerprint in expected_rows
        }
        receipts = {
            (session_id, turn_number): (revision_id, fingerprint)
            for session_id, turn_number, revision_id, fingerprint in receipt_rows
        }
        exact_receipt_count = sum(
            1
            for key, expected_fingerprint in expected.items()
            if key in receipts and receipts[key][1] == expected_fingerprint
        )
        orphan_receipt_count = len(set(receipts) - set(expected))
        return SourceCaptureFingerprintState(
            source_name=source_name,
            generation_id=generation_id,
            roster_hash=roster_hash,
            generation_eligible=bool(generation[2]),
            expected_turn_count=len(expected_rows),
            receipt_count=len(receipt_rows),
            exact_receipt_count=exact_receipt_count,
            pending_turn_count=len(expected_rows) - exact_receipt_count,
            orphan_receipt_count=orphan_receipt_count,
            denominator_session_set_hash=_row_set_hash(denominator_rows),
            expected_turn_fingerprint_set_hash=_row_set_hash(expected_rows),
            receipt_binding_set_hash=_row_set_hash(receipt_rows),
        )

    def source_capture_fingerprint_state(
        self,
        source_name: str,
    ) -> SourceCaptureFingerprintState:
        """Read one transactionally consistent exact capture proof."""
        try:
            with self._transaction() as connection:
                connection.execute("BEGIN")
                return self._capture_fingerprint_state_from_connection(
                    connection,
                    source_name,
                )
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot read source capture fingerprint state") from exc

    def bind_native_source_snapshot(
        self,
        source_name: str,
        native_source_snapshot_hash: str,
        *,
        expected_capture_state: SourceCaptureFingerprintState,
    ) -> None:
        """Atomically compare and bind one exact complete capture generation."""
        if not _valid_sha256(native_source_snapshot_hash):
            raise AgentSyncCursorError("native source snapshot hash is invalid")
        if expected_capture_state.source_name != source_name or not _valid_sha256(
            expected_capture_state.roster_hash
        ):
            raise AgentSyncCursorError("expected capture state is invalid")
        try:
            with self._transaction() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current_capture_state = self._capture_fingerprint_state_from_connection(
                    connection,
                    source_name,
                )
                if current_capture_state != expected_capture_state:
                    raise AgentSyncCursorError("capture state changed before snapshot binding")
                if not current_capture_state.complete:
                    raise AgentSyncCursorError(
                        "complete source capture generation is required before snapshot binding"
                    )
                updated = connection.execute(
                    """
                    UPDATE source_capture_generations
                    SET native_source_snapshot_hash=?
                    WHERE source_name=?
                      AND EXISTS (
                          SELECT 1
                          FROM source_denominator_state AS denominator
                          WHERE denominator.source_name=source_capture_generations.source_name
                            AND denominator.roster_hash=source_capture_generations.roster_hash
                            AND denominator.complete=1
                            AND source_capture_generations.snapshot_binding_eligible=1
                      )
                    """,
                    (native_source_snapshot_hash, source_name),
                )
                if updated.rowcount != 1:
                    raise AgentSyncCursorError(
                        "complete source capture generation is required before snapshot binding"
                    )
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot bind native source snapshot") from exc

    def source_denominator_progress(self, source_name: str) -> SourceDenominatorProgress:
        generation_id = ""
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT roster_hash, session_count, observed_session_count,
                           observed_turn_count, complete, completed_at
                    FROM source_denominator_state WHERE source_name=?
                    """,
                    (source_name,),
                ).fetchone()
                generation = connection.execute(
                    "SELECT generation_id FROM source_capture_generations WHERE source_name=?",
                    (source_name,),
                ).fetchone()
                if generation is not None:
                    generation_id = str(generation[0])
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot read source denominator evidence") from exc
        if row is None or not generation_id:
            raise AgentSyncCursorError("source denominator generation is missing")
        return self._progress_from_row(source_name, row, generation_id)

    def reset_source_reconciliation(self, source_name: str) -> SourceReconciliationReset:
        """Clear one source's *derived* cursor generation for an explicit rebuild.

        Parser repairs can invalidate an otherwise unchanged canonical session
        roster.  A monotonic high-water cursor must not then skip the repaired
        parser's current turns.  This explicit operation is intentionally not
        called by normal polling: production reconciliation tooling must first
        preserve a backup, then rerun the complete native-to-Raw denominator.
        """
        normalized_source = str(source_name or "").strip()
        if not normalized_source:
            raise AgentSyncCursorError("source name is required for reconciliation reset")
        try:
            with self._transaction() as connection:
                session_raw_cursor_count = connection.execute(
                    "SELECT COUNT(*) FROM session_raw_cursors WHERE source_name=?",
                    (normalized_source,),
                ).fetchone()
                reconciliation_cursor_count = connection.execute(
                    "SELECT COUNT(*) FROM source_reconciliation_cursors WHERE source_name=?",
                    (normalized_source,),
                ).fetchone()
                denominator_state_count = connection.execute(
                    "SELECT COUNT(*) FROM source_denominator_state WHERE source_name=?",
                    (normalized_source,),
                ).fetchone()
                denominator_session_count = connection.execute(
                    "SELECT COUNT(*) FROM source_denominator_sessions WHERE source_name=?",
                    (normalized_source,),
                ).fetchone()
                capture_generation_count = connection.execute(
                    "SELECT COUNT(*) FROM source_capture_generations WHERE source_name=?",
                    (normalized_source,),
                ).fetchone()
                capture_expected_turn_count = connection.execute(
                    "SELECT COUNT(*) FROM source_capture_expected_turns WHERE source_name=?",
                    (normalized_source,),
                ).fetchone()
                capture_raw_receipt_count = connection.execute(
                    "SELECT COUNT(*) FROM source_capture_raw_receipts WHERE source_name=?",
                    (normalized_source,),
                ).fetchone()
                connection.execute(
                    "DELETE FROM session_raw_cursors WHERE source_name=?",
                    (normalized_source,),
                )
                connection.execute(
                    "DELETE FROM source_reconciliation_cursors WHERE source_name=?",
                    (normalized_source,),
                )
                connection.execute(
                    "DELETE FROM source_denominator_sessions WHERE source_name=?",
                    (normalized_source,),
                )
                connection.execute(
                    "DELETE FROM source_capture_expected_turns WHERE source_name=?",
                    (normalized_source,),
                )
                connection.execute(
                    "DELETE FROM source_capture_raw_receipts WHERE source_name=?",
                    (normalized_source,),
                )
                connection.execute(
                    "DELETE FROM source_capture_generations WHERE source_name=?",
                    (normalized_source,),
                )
                connection.execute(
                    "DELETE FROM source_denominator_state WHERE source_name=?",
                    (normalized_source,),
                )
        except sqlite3.Error as exc:
            raise AgentSyncCursorError("cannot reset source reconciliation state") from exc

        return SourceReconciliationReset(
            source_name=normalized_source,
            session_raw_cursor_count=int(session_raw_cursor_count[0] or 0),
            reconciliation_cursor_count=int(reconciliation_cursor_count[0] or 0),
            denominator_state_count=int(denominator_state_count[0] or 0),
            denominator_session_count=int(denominator_session_count[0] or 0),
            capture_generation_count=int(capture_generation_count[0] or 0),
            capture_expected_turn_count=int(capture_expected_turn_count[0] or 0),
            capture_raw_receipt_count=int(capture_raw_receipt_count[0] or 0),
        )
