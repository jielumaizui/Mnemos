"""Fresh-schema creation and explicitly authorized retired-schema repair."""

from __future__ import annotations

import sqlite3
from typing import Any

from core.db_utils import render_sql

from .contracts import (
    _RUNTIME_REQUIRED_COLUMNS,
    _UNRECOVERABLE_TOMBSTONE_DISPOSITION,
    _UNRECOVERABLE_TOMBSTONE_DISPOSITION_KEY,
    ModelCallLedgerInvariantError,
)
from .normalization import (
    _canonical_entry_id,
    _canonical_run_id,
    _canonical_spend_day,
    _canonical_timestamp,
    _hash_text,
    _is_canonical_entry_id,
    _is_canonical_run_id,
    _normalize_cache_status,
    _normalize_metered_usage_receipt,
    _normalize_model_label,
    _normalize_operation,
    _normalize_price_version,
    _normalize_provider_label,
    _nonnegative_finite_float,
    _nonnegative_int,
    _opaque_metadata_reference,
    _utc_now,
)
from .schema_validation import LedgerSchemaValidation


class LedgerSchemaReconciliation(LedgerSchemaValidation):
    """Internal schema mutation implementation; runtime callers never invoke it."""

    @staticmethod
    def _run_id_digest(run_id: str) -> str:
        normalized = str(run_id or "")
        if _is_canonical_run_id(normalized):
            return normalized.removeprefix("mclrun:v2:")
        if normalized.startswith("mclrun:") and len(normalized.removeprefix("mclrun:")) == 64:
            return normalized.removeprefix("mclrun:")
        return _hash_text(f"model-call-run-id:{normalized}")

    @staticmethod
    def _create_subject_schema(conn: sqlite3.Connection) -> None:
        """Create privacy tables only from bootstrap or explicit reconciliation."""
        # A run owns the shared budget.  Per-entry attribution below is the
        # deletion authority because one provider batch may contain many assets.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_call_run_subjects (
                run_id TEXT PRIMARY KEY,
                scope_kind TEXT NOT NULL,
                subject_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES model_call_runs(run_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_call_run_subjects_scope "
            "ON model_call_run_subjects(scope_kind, subject_hash)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_call_entry_subjects (
                entry_id TEXT NOT NULL,
                scope_kind TEXT NOT NULL,
                subject_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(entry_id, scope_kind, subject_hash),
                FOREIGN KEY(entry_id) REFERENCES model_call_entries(entry_id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_call_entry_subjects_scope "
            "ON model_call_entry_subjects(scope_kind, subject_hash, entry_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_call_frozen_subjects (
                scope_kind TEXT NOT NULL,
                subject_hash TEXT NOT NULL,
                frozen_at TEXT NOT NULL,
                PRIMARY KEY(scope_kind, subject_hash)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_call_daily_spend_tombstones (
                spend_day TEXT PRIMARY KEY,
                effective_cost REAL NOT NULL,
                deleted_entry_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_call_run_spend_tombstones (
                run_id_digest TEXT PRIMARY KEY,
                effective_cost REAL NOT NULL,
                deleted_entry_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _require_delete_journal_mode_for_private_scrub(conn: sqlite3.Connection) -> None:
        """Eliminate a stale WAL before releasing any privacy-sensitive cell.

        Reconciliation runs only while the daemon is stopped.  If another
        reader keeps a WAL checkpoint from completing, fail before modifying
        the source so the old raw row/table remains visible as a blocker.
        """
        try:
            # SQLite can retain a harmless-but-locking empty WAL after the
            # last writer closes.  Checkpoint it first; a live reader reports
            # busy and we fail before releasing any private cell.
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

    @staticmethod
    def _require_secure_delete_for_private_scrub(conn: sqlite3.Connection) -> None:
        """Require SQLite to overwrite released cells before a privacy delete."""
        conn.execute("PRAGMA secure_delete=ON")
        row = conn.execute("PRAGMA secure_delete").fetchone()
        if row is None or int(row[0] or 0) != 1:
            raise ModelCallLedgerInvariantError(
                "private model-call scrub requires SQLite secure_delete=ON"
            )

    @staticmethod
    def _record_unrecoverable_run_tombstone_disposition(
        conn: sqlite3.Connection,
        *,
        known_row_count: int,
    ) -> None:
        """Leave a durable, non-content warning when early cascade history is unknowable."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_call_ledger_reconciliation_dispositions (
                disposition_key TEXT NOT NULL PRIMARY KEY,
                disposition TEXT NOT NULL,
                known_row_count INTEGER NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        normalized_count = _nonnegative_int(
            known_row_count, label="unrecoverable tombstone disposition row count"
        )
        existing = conn.execute(
            "SELECT disposition, known_row_count FROM "
            "model_call_ledger_reconciliation_dispositions WHERE disposition_key=?",
            (_UNRECOVERABLE_TOMBSTONE_DISPOSITION_KEY,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["disposition"] or "")
                != _UNRECOVERABLE_TOMBSTONE_DISPOSITION
                or _nonnegative_int(
                    existing["known_row_count"],
                    label="persisted unrecoverable tombstone disposition row count",
                )
                != normalized_count
            ):
                raise ModelCallLedgerInvariantError(
                    "reconciliation disposition receipt is immutable and conflicts with "
                    "the observed legacy tombstone history"
                )
            return
        conn.execute(
            """
            INSERT INTO model_call_ledger_reconciliation_dispositions(
                disposition_key, disposition, known_row_count, recorded_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                _UNRECOVERABLE_TOMBSTONE_DISPOSITION_KEY,
                _UNRECOVERABLE_TOMBSTONE_DISPOSITION,
                normalized_count,
                _utc_now(),
            ),
        )

    @classmethod
    def _rebuild_run_spend_tombstones_without_fk(
        cls,
        conn: sqlite3.Connection,
        *,
        discard_unrecoverable_cascade_history: bool,
    ) -> bool:
        """Replace known early tombstones with non-reversible retained budget facts.

        Early Phase-2 rows used a raw ``run_id`` and, at first, a cascading FK.
        Either shape is unsafe after deletion: the former retained caller text;
        the latter disappeared and allowed budget reset.  The explicitly
        backup-gated reconciler converts only those known shapes to a one-way
        run-id digest with no FK.  Arbitrary drift remains fail-closed.
        """
        if "model_call_run_spend_tombstones" not in cls._table_names(conn):
            return False
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(model_call_run_spend_tombstones)").fetchall()
        }
        new_columns = _RUNTIME_REQUIRED_COLUMNS["model_call_run_spend_tombstones"]
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(model_call_run_spend_tombstones)"
        ).fetchall()
        if columns == new_columns and not foreign_keys:
            return False
        old_columns = {"run_id", "effective_cost", "deleted_entry_count", "updated_at"}
        if columns != old_columns:
            raise ModelCallLedgerInvariantError(
                "cannot rebuild unsupported model-call run spend tombstone schema"
            )
        # Dropping the old raw-ID table must scrub its released cells.  The
        # caller has already moved journal mode out of WAL before opening this
        # transaction; retain an explicit local guard so this private helper
        # cannot be used as a future bypass.
        conn.execute("PRAGMA secure_delete=ON")
        secure_delete = conn.execute("PRAGMA secure_delete").fetchone()
        if secure_delete is None or int(secure_delete[0] or 0) != 1:
            raise ModelCallLedgerInvariantError(
                "legacy run tombstone privacy reconciliation requires SQLite secure_delete=ON"
            )
        had_cascading_fk = bool(foreign_keys)
        if had_cascading_fk and not discard_unrecoverable_cascade_history:
            raise ModelCallLedgerInvariantError(
                "legacy cascading run tombstones may have already lost budget history; "
                "explicit unrecoverable-history disposition is required"
            )
        rows = conn.execute(
            "SELECT run_id, effective_cost, deleted_entry_count, updated_at "
            "FROM model_call_run_spend_tombstones"
        ).fetchall()
        replacement = "model_call_run_spend_tombstones_reconciled"
        conn.execute(
            f"CREATE TABLE {replacement} ("  # nosec B608
            "run_id_digest TEXT PRIMARY KEY, effective_cost REAL NOT NULL, "
            "deleted_entry_count INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        for row in rows:
            run_id_digest = cls._run_id_digest(str(row["run_id"] or ""))
            conn.execute(
                f"INSERT INTO {replacement}("  # nosec B608
                "run_id_digest, effective_cost, deleted_entry_count, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(run_id_digest) DO UPDATE SET "
                f"effective_cost={replacement}.effective_cost + excluded.effective_cost, "  # nosec B608
                f"deleted_entry_count={replacement}.deleted_entry_count + excluded.deleted_entry_count, "  # nosec B608
                f"updated_at=excluded.updated_at",
                (
                    run_id_digest,
                    _nonnegative_finite_float(row["effective_cost"], label="legacy tombstone cost"),
                    _nonnegative_int(row["deleted_entry_count"], label="legacy tombstone entry count"),
                    str(row["updated_at"] or _utc_now()),
                ),
            )
        conn.execute("DROP TABLE model_call_run_spend_tombstones")
        conn.execute(
            f"ALTER TABLE {replacement} RENAME TO model_call_run_spend_tombstones"  # nosec B608
        )
        if had_cascading_fk:
            cls._record_unrecoverable_run_tombstone_disposition(
                conn,
                known_row_count=len(rows),
            )
        return had_cascading_fk

    @classmethod
    def _rekey_model_call_run_ids_to_opaque(cls, conn: sqlite3.Connection) -> int:
        """Replace retired caller-controlled run labels without losing tombstones.

        v1's digest was the run-tombstone key.  A v2 rekey changes that
        digest, so move each active run's tombstone fact atomically before
        rewriting the parent/child keys.  Tombstones for already-deleted v1
        runs retain their v1 digest and are still rejected by ``start_run``;
        no raw identifier is retained in either case.
        """
        rows = conn.execute("SELECT run_id FROM model_call_runs ORDER BY run_id").fetchall()
        replacements = [
            (str(row[0]), _canonical_run_id(row[0]))
            for row in rows
            if not _is_canonical_run_id(row[0])
        ]
        if not replacements:
            return 0
        canonical_ids = [canonical for _, canonical in replacements]
        if len(set(canonical_ids)) != len(canonical_ids):
            raise ModelCallLedgerInvariantError("legacy run-id rekey produced a collision")
        # Updating a row alone may leave the caller-controlled old key in a
        # free SQLite cell.  Refuse to rekey unless this connection confirms
        # secure deletion before it releases any retired b-tree content.
        conn.execute("PRAGMA secure_delete=ON")
        secure_delete = conn.execute("PRAGMA secure_delete").fetchone()
        if secure_delete is None or int(secure_delete[0] or 0) != 1:
            raise ModelCallLedgerInvariantError(
                "opaque run-id reconciliation requires SQLite secure_delete=ON"
            )
        conn.execute("PRAGMA defer_foreign_keys=ON")
        for raw_run_id, canonical_run_id in replacements:
            old_digest = cls._run_id_digest(raw_run_id)
            new_digest = cls._run_id_digest(canonical_run_id)
            if old_digest != new_digest:
                tombstone = conn.execute(
                    "SELECT effective_cost, deleted_entry_count, updated_at "
                    "FROM model_call_run_spend_tombstones WHERE run_id_digest=?",
                    (old_digest,),
                ).fetchone()
                if tombstone is not None:
                    conn.execute(
                        "DELETE FROM model_call_run_spend_tombstones WHERE run_id_digest=?",
                        (old_digest,),
                    )
                    conn.execute(
                        """
                        INSERT INTO model_call_run_spend_tombstones(
                            run_id_digest, effective_cost, deleted_entry_count, updated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(run_id_digest) DO UPDATE SET
                            effective_cost=model_call_run_spend_tombstones.effective_cost
                                + excluded.effective_cost,
                            deleted_entry_count=model_call_run_spend_tombstones.deleted_entry_count
                                + excluded.deleted_entry_count,
                            updated_at=MAX(model_call_run_spend_tombstones.updated_at,
                                excluded.updated_at)
                        """,
                        (
                            new_digest,
                            _nonnegative_finite_float(
                                tombstone["effective_cost"],
                                label="legacy run tombstone cost",
                            ),
                            _nonnegative_int(
                                tombstone["deleted_entry_count"],
                                label="legacy run tombstone count",
                            ),
                            _canonical_timestamp(tombstone["updated_at"]),
                        ),
                    )
            conn.execute(
                "UPDATE model_call_entries SET run_id=? WHERE run_id=?",
                (canonical_run_id, raw_run_id),
            )
            conn.execute(
                "UPDATE model_call_run_subjects SET run_id=? WHERE run_id=?",
                (canonical_run_id, raw_run_id),
            )
            conn.execute(
                "UPDATE model_call_runs SET run_id=? WHERE run_id=?",
                (canonical_run_id, raw_run_id),
            )
        return len(replacements)

    @classmethod
    def _rekey_model_call_entry_ids_to_opaque(cls, conn: sqlite3.Connection) -> int:
        """Rekey retired entry IDs and their attribution FKs under one transaction."""
        rows = conn.execute(
            "SELECT entry_id FROM model_call_entries ORDER BY entry_id"
        ).fetchall()
        replacements = [
            (str(row[0]), _canonical_entry_id(row[0]))
            for row in rows
            if not _is_canonical_entry_id(row[0])
        ]
        if not replacements:
            return 0
        canonical_ids = [canonical for _, canonical in replacements]
        if len(set(canonical_ids)) != len(canonical_ids):
            raise ModelCallLedgerInvariantError("legacy entry-id rekey produced a collision")
        cls._require_secure_delete_for_private_scrub(conn)
        conn.execute("PRAGMA defer_foreign_keys=ON")
        for raw_entry_id, canonical_entry_id in replacements:
            conn.execute(
                "UPDATE model_call_entry_subjects SET entry_id=? WHERE entry_id=?",
                (canonical_entry_id, raw_entry_id),
            )
            conn.execute(
                "UPDATE model_call_entries SET entry_id=? WHERE entry_id=?",
                (canonical_entry_id, raw_entry_id),
            )
        return len(replacements)

    @classmethod
    def _metadata_reconciliation_updates(
        cls,
        conn: sqlite3.Connection,
        *,
        has_metered_usage_receipt: bool,
    ) -> list[tuple[str, str, str, str, str, str, str, str, str]]:
        """Return exact non-content metadata replacements for an old ledger.

        The historical schema never promised that its descriptive columns were
        identifiers.  Reconciliation must not leave an arbitrary provider
        label, request id, or cache text in the canonical ledger merely
        because prompt storage has already been removed.  This helper returns
        only one-way references or reviewed literals; callers enable secure
        deletion before applying any returned replacement.
        """
        metered_expression = (
            "metered_usage_receipt"
            if has_metered_usage_receipt
            else "'' AS metered_usage_receipt"
        )
        rows = conn.execute(
            render_sql(
                "SELECT entry_id, operation, provider, model, cache_status, "
                "provider_usage_id, request_id, {metered_expression}, "
                "price_version FROM model_call_entries ORDER BY entry_id",
                fixed_fragments={
                    "metered_expression": (
                        metered_expression,
                        {"metered_usage_receipt", "'' AS metered_usage_receipt"},
                    )
                },
            )
        ).fetchall()
        updates: list[tuple[str, str, str, str, str, str, str, str, str]] = []
        for row in rows:
            replacement = (
                _normalize_operation(row["operation"], historical=True),
                _normalize_provider_label(row["provider"], historical=True),
                _normalize_model_label(row["model"], historical=True),
                _normalize_cache_status(row["cache_status"], historical=True),
                _opaque_metadata_reference(
                    "provider_usage", row["provider_usage_id"], preserve_canonical=True
                ),
                _opaque_metadata_reference(
                    "request", row["request_id"], preserve_canonical=True
                ),
                _normalize_metered_usage_receipt(
                    row["metered_usage_receipt"], preserve_canonical=True
                ),
                _normalize_price_version(row["price_version"], historical=True),
            )
            current = (
                str(row["operation"] or ""),
                str(row["provider"] or ""),
                str(row["model"] or ""),
                str(row["cache_status"] or ""),
                str(row["provider_usage_id"] or ""),
                str(row["request_id"] or ""),
                str(row["metered_usage_receipt"] or ""),
                str(row["price_version"] or ""),
            )
            if replacement != current:
                updates.append((*replacement, str(row["entry_id"])))
        return updates

    @classmethod
    def _timestamp_reconciliation_updates(
        cls, conn: sqlite3.Connection
    ) -> list[tuple[str, tuple[Any, ...]]]:
        """Build private timestamp/day repairs for every canonical owner table.

        Old ledgers may contain arbitrary text in timestamp-shaped columns.
        Return parameterized operations only; the caller establishes DELETE
        journaling and secure-delete before executing them so the old bytes
        cannot remain in a WAL or SQLite free page.
        """
        tables = cls._table_names(conn)
        updates: list[tuple[str, tuple[Any, ...]]] = []

        def _update_timestamp_column(
            table: str,
            key_columns: tuple[str, ...],
            timestamp_columns: tuple[str, ...],
        ) -> None:
            if table not in tables:
                return
            available_columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_xinfo({table})").fetchall()  # nosec B608
            }
            # A supported raw-id v1 tombstone table is converted before this
            # helper runs for real.  During the pre-transaction scrub check it
            # has no ``run_id_digest`` yet, so leave it to the conversion
            # rather than issuing a query against a non-existent column.
            if not set((*key_columns, *timestamp_columns)).issubset(available_columns):
                return
            selected = (*key_columns, *timestamp_columns)
            rows = conn.execute(
                render_sql(
                    "SELECT {columns} FROM {table}",
                    identifiers={"table": table},
                    identifier_lists={"columns": selected},
                )
            ).fetchall()
            for row in rows:
                replacement = tuple(
                    None
                    if row[column] is None
                    else _canonical_timestamp(row[column])
                    for column in timestamp_columns
                )
                current = tuple(row[column] for column in timestamp_columns)
                if replacement == current:
                    continue
                assignments = ", ".join(f"{column}=?" for column in timestamp_columns)
                where = " AND ".join(f"{column}=?" for column in key_columns)
                updates.append(
                    (
                        f"UPDATE {table} SET {assignments} WHERE {where}",  # nosec B608
                        (*replacement, *(row[column] for column in key_columns)),
                    )
                )

        _update_timestamp_column("model_call_runs", ("run_id",), ("created_at",))
        _update_timestamp_column(
            "model_call_entries",
            ("entry_id",),
            ("created_at", "dispatched_at", "settled_at"),
        )
        _update_timestamp_column(
            "model_call_run_subjects", ("run_id",), ("created_at",)
        )
        _update_timestamp_column(
            "model_call_entry_subjects",
            ("entry_id", "scope_kind", "subject_hash"),
            ("created_at",),
        )
        _update_timestamp_column(
            "model_call_frozen_subjects",
            ("scope_kind", "subject_hash"),
            ("frozen_at",),
        )
        _update_timestamp_column(
            "model_call_run_spend_tombstones", ("run_id_digest",), ("updated_at",)
        )

        daily_table = "model_call_daily_spend_tombstones"
        if daily_table in tables:
            rows = conn.execute(
                "SELECT spend_day, effective_cost, deleted_entry_count, updated_at "
                "FROM model_call_daily_spend_tombstones"
            ).fetchall()
            normalized_rows: dict[str, tuple[float, int, str]] = {}
            changed = False
            for row in rows:
                normalized_day = _canonical_spend_day(row["spend_day"])
                normalized_updated_at = _canonical_timestamp(row["updated_at"])
                current_cost, current_count, current_updated_at = normalized_rows.get(
                    normalized_day, (0.0, 0, normalized_updated_at)
                )
                normalized_rows[normalized_day] = (
                    current_cost
                    + _nonnegative_finite_float(
                        row["effective_cost"], label="legacy daily tombstone cost"
                    ),
                    current_count
                    + _nonnegative_int(
                        row["deleted_entry_count"], label="legacy daily tombstone count"
                    ),
                    max(current_updated_at, normalized_updated_at),
                )
                if (
                    normalized_day != str(row["spend_day"] or "")
                    or normalized_updated_at != str(row["updated_at"] or "")
                ):
                    changed = True
            if changed:
                updates.append(("DELETE FROM model_call_daily_spend_tombstones", ()))
                updates.extend(
                    (
                        "INSERT INTO model_call_daily_spend_tombstones("
                        "spend_day, effective_cost, deleted_entry_count, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (day, cost, count, updated_at),
                    )
                    for day, (cost, count, updated_at) in sorted(normalized_rows.items())
                )
        return updates

    @staticmethod
    def _create_base_indexes(conn: sqlite3.Connection) -> None:
        """Create the canonical lookup indexes during bootstrap/reconciliation."""
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_call_entries_run "
            "ON model_call_entries(run_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_call_entries_created "
            "ON model_call_entries(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_call_entries_state "
            "ON model_call_entries(lifecycle_state, created_at)"
        )

    def _bootstrap_schema(self) -> None:
        """Create a complete schema only for a brand-new ledger path."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_call_runs (
                    run_id TEXT PRIMARY KEY,
                    cost_budget REAL,
                    created_at TEXT NOT NULL,
                    schema_version TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_call_entries (
                    entry_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    reserved_input_tokens INTEGER NOT NULL,
                    reserved_output_tokens INTEGER NOT NULL,
                    reserved_cost REAL NOT NULL,
                    actual_input_tokens INTEGER,
                    actual_output_tokens INTEGER,
                    actual_total_tokens INTEGER,
                    actual_cost REAL,
                    refund_cost REAL NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    provider_usage_id TEXT NOT NULL DEFAULT '',
                    metered_usage_receipt TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT '',
                    price_version TEXT NOT NULL,
                    input_price REAL NOT NULL,
                    output_price REAL NOT NULL,
                    cache_status TEXT NOT NULL DEFAULT 'miss',
                    retry_attempt INTEGER NOT NULL DEFAULT 0,
                    lifecycle_state TEXT NOT NULL,
                    request_dispatched INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    legacy_fingerprint TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT,
                    settled_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES model_call_runs(run_id)
                )
                """
            )
            self._create_base_indexes(conn)
            self._create_subject_schema(conn)
