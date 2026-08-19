"""Subject attribution, redaction deletion, retention and spend tombstones."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

from core.db_utils import render_sql
from core.runtime_paths import RuntimePaths

from .contracts import (
    _RETIRED_PROMPT_STORAGE_TABLES,
    _STALE_DISPATCH_GRACE_SECONDS,
    _SUBJECT_SCOPE_KINDS,
    ModelCallLedgerInvariantError,
    ModelCallSubjectFrozen,
)
from .normalization import (
    _CANONICAL_RUN_ID_PREFIX,
    _LEGACY_CANONICAL_RUN_ID_PREFIX,
    _hash_text,
    _is_canonical_run_id,
    _is_canonical_timestamp,
    _is_prior_canonical_run_id,
    _nonnegative_finite_float,
    _readonly_sqlite_connection,
    _spend_day_from_timestamp,
    _utc_now,
)
from .schema_validation import LedgerSchemaValidation
from .state import (
    LedgerState,
    require_delete_journal_mode_for_private_scrub,
    require_secure_delete_for_private_scrub,
)


class LedgerSubjectsRetention(LedgerSchemaValidation):
    """Internal owner of subject gates and budget-preserving local deletion."""

    def __init__(self, state: LedgerState):
        super().__init__(state)

    @property
    def db_path(self) -> Path:
        return self._state.db_path

    def _require_runtime_write_ready(self) -> None:
        self._state.require_runtime_write_ready()

    @staticmethod
    def _subject_binding(
        subject_scope: tuple[str, str] | None,
    ) -> tuple[str, str] | None:
        if subject_scope is None:
            return None
        try:
            scope_kind, scope_value = subject_scope
        except (TypeError, ValueError) as exc:
            raise ModelCallLedgerInvariantError("subject scope must contain kind and value") from exc
        normalized_kind = str(scope_kind or "").strip()
        normalized_value = str(scope_value or "").strip()
        if normalized_kind not in _SUBJECT_SCOPE_KINDS or not normalized_value:
            raise ModelCallLedgerInvariantError("subject scope requires an exact non-all subject")
        return normalized_kind, _hash_text(f"{normalized_kind}:{normalized_value}")

    @classmethod
    def _subject_bindings(
        cls,
        subject_scopes: Iterable[tuple[str, str]] | None,
    ) -> tuple[tuple[str, str], ...]:
        if subject_scopes is None:
            return ()
        bindings = {
            binding
            for binding in (cls._subject_binding(scope) for scope in subject_scopes)
            if binding is not None
        }
        if not bindings:
            raise ModelCallLedgerInvariantError("at least one exact entry subject is required")
        return tuple(sorted(bindings))

    @staticmethod
    def _all_subject_binding() -> tuple[str, str]:
        return "all", _hash_text("all:all")

    @staticmethod
    def _run_id_digest(run_id: str) -> str:
        """Key permanent run tombstones without retaining caller-controlled text."""
        normalized = str(run_id or "")
        if _is_canonical_run_id(normalized):
            return normalized.removeprefix(_CANONICAL_RUN_ID_PREFIX)
        if _is_prior_canonical_run_id(normalized):
            return normalized.removeprefix(_LEGACY_CANONICAL_RUN_ID_PREFIX)
        return _hash_text(f"model-call-run-id:{normalized}")

    @staticmethod
    def _run_binding(conn: sqlite3.Connection, run_id: str) -> tuple[str, str]:
        row = conn.execute(
            "SELECT scope_kind, subject_hash FROM model_call_run_subjects WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ModelCallLedgerInvariantError(
                "model-call run lacks immutable subject attribution; run reconciliation first"
            )
        return str(row["scope_kind"]), str(row["subject_hash"])

    @classmethod
    def _assert_subjects_not_frozen(
        cls,
        conn: sqlite3.Connection,
        bindings: Iterable[tuple[str, str]],
    ) -> None:
        for scope_kind, subject_hash in bindings:
            if cls._subject_is_frozen(conn, (scope_kind, subject_hash)):
                raise ModelCallSubjectFrozen("model-call subject is frozen before provider dispatch")

    @classmethod
    def _subject_is_frozen(
        cls,
        conn: sqlite3.Connection,
        binding: tuple[str, str],
    ) -> bool:
        all_kind, all_hash = cls._all_subject_binding()
        row = conn.execute(
            """
            SELECT 1 FROM model_call_frozen_subjects
            WHERE (scope_kind=? AND subject_hash=?)
               OR (scope_kind=? AND subject_hash=?)
            LIMIT 1
            """,
            (binding[0], binding[1], all_kind, all_hash),
        ).fetchone()
        return row is not None

    @staticmethod
    def _entry_effective_cost(row: sqlite3.Row) -> float:
        state = str(row["lifecycle_state"])
        if state in {"released", "legacy_observed"} or (
            state == "reserved" and not bool(row["request_dispatched"])
        ):
            # A reservation that never crossed the dispatch barrier cannot be
            # charged into a deletion/retention tombstone.  It held temporary
            # live capacity, but preserving it after cleanup would invent a
            # provider cost and permanently consume future budget.
            return 0.0
        if state in {"settled", "incurred_overrun"} and row["actual_cost"] is not None:
            return _nonnegative_finite_float(row["actual_cost"], label="persisted actual cost")
        return _nonnegative_finite_float(row["reserved_cost"], label="persisted reserved cost")

    @classmethod
    def _daily_tombstoned_cost(cls, conn: sqlite3.Connection, day: str) -> float:
        row = conn.execute(
            "SELECT effective_cost FROM model_call_daily_spend_tombstones WHERE spend_day=?",
            (day,),
        ).fetchone()
        return _nonnegative_finite_float(
            row[0] if row else 0.0,
            label="persisted daily tombstone cost",
        )

    @classmethod
    def _stale_inflight_entry_ids(
        cls,
        conn: sqlite3.Connection,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Return dispatched reservations that exceeded the bounded settle window.

        A live provider response may legitimately be in flight, so only an old
        dispatched reservation is stale.  Malformed
        timestamps are handled by runtime schema validation; this helper never
        turns a parse error into a false fresh status.
        """
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(
            seconds=_STALE_DISPATCH_GRACE_SECONDS
        )
        stale_entry_ids = []
        for row in conn.execute(
            "SELECT entry_id, dispatched_at FROM model_call_entries "
            "WHERE lifecycle_state='reserved' AND request_dispatched=1 "
            "ORDER BY entry_id"
        ).fetchall():
            dispatched_at = str(row["dispatched_at"] or "")
            if not _is_canonical_timestamp(dispatched_at):
                continue
            if datetime.fromisoformat(dispatched_at) <= cutoff:
                stale_entry_ids.append(str(row["entry_id"]))
        return tuple(stale_entry_ids)

    @classmethod
    def _stale_inflight_entry_count(
        cls,
        conn: sqlite3.Connection,
        *,
        now: datetime | None = None,
    ) -> int:
        return len(cls._stale_inflight_entry_ids(conn, now=now))

    @classmethod
    def _run_tombstoned_cost(cls, conn: sqlite3.Connection, run_id: str) -> float:
        row = conn.execute(
            "SELECT effective_cost FROM model_call_run_spend_tombstones WHERE run_id_digest=?",
            (cls._run_id_digest(run_id),),
        ).fetchone()
        return _nonnegative_finite_float(
            row[0] if row else 0.0,
            label="persisted run tombstone cost",
        )

    @classmethod
    def _run_tombstone_exists(cls, conn: sqlite3.Connection, run_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM model_call_run_spend_tombstones WHERE run_id_digest=? LIMIT 1",
            (cls._run_id_digest(run_id),),
        ).fetchone() is not None

    def freeze_subject_scope(self, scope_kind: str, scope_value: str) -> Dict[str, Any]:
        """Persist the provider-dispatch barrier for a frozen ownership scope."""
        self._require_runtime_write_ready()
        normalized_kind = str(scope_kind or "").strip()
        normalized_value = str(scope_value or "").strip()
        binding: tuple[str, str] | None
        if normalized_kind == "all" and normalized_value == "all":
            binding = self._all_subject_binding()
        else:
            binding = self._subject_binding((normalized_kind, normalized_value))
        if binding is None:
            raise ModelCallLedgerInvariantError("freeze requires an exact subject or all")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_runtime_data_integrity(conn, operation="subject freeze")
            conn.execute(
                "INSERT OR IGNORE INTO model_call_frozen_subjects("
                "scope_kind, subject_hash, frozen_at) VALUES (?, ?, ?)",
                (binding[0], binding[1], _utc_now()),
            )
            conn.commit()
        return {"status": "frozen", "scope_kind": normalized_kind}

    @classmethod
    def _record_deleted_spend_tombstones(
        cls, conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]
    ) -> None:
        daily_totals: Dict[str, tuple[float, int]] = {}
        run_totals: Dict[str, tuple[float, int]] = {}
        for row in rows:
            day = _spend_day_from_timestamp(row["created_at"])
            cost = cls._entry_effective_cost(row)
            current_cost, current_count = daily_totals.get(day, (0.0, 0))
            daily_totals[day] = (current_cost + cost, current_count + 1)
            run_id = str(row["run_id"] or "")
            if run_id:
                run_id_digest = cls._run_id_digest(run_id)
                current_cost, current_count = run_totals.get(run_id_digest, (0.0, 0))
                run_totals[run_id_digest] = (current_cost + cost, current_count + 1)
        for day, (cost, count) in daily_totals.items():
            conn.execute(
                """
                INSERT INTO model_call_daily_spend_tombstones(
                    spend_day, effective_cost, deleted_entry_count, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(spend_day) DO UPDATE SET
                    effective_cost=model_call_daily_spend_tombstones.effective_cost
                        + excluded.effective_cost,
                    deleted_entry_count=model_call_daily_spend_tombstones.deleted_entry_count
                        + excluded.deleted_entry_count,
                    updated_at=excluded.updated_at
                """,
                (day, cost, count, _utc_now()),
            )
        for run_id_digest, (cost, count) in run_totals.items():
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
                    updated_at=excluded.updated_at
                """,
                (run_id_digest, cost, count, _utc_now()),
            )

    def cleanup_older_than(self, days: int, dry_run: bool = False) -> int:
        """Delete aged, settled-safe entries without weakening live call accounting."""
        self._require_runtime_write_ready()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))).isoformat()
        with self._connect() as conn:
            # A retention delete releases old provider metadata.  Do not leave
            # it in a WAL or SQLite free page merely because it was not prompt
            # text at the time this schema was written.  A dry run is strictly
            # read-only and deliberately does not alter the caller's journal.
            if not dry_run:
                require_delete_journal_mode_for_private_scrub(conn)
                require_secure_delete_for_private_scrub(conn)
            conn.execute("BEGIN IMMEDIATE")
            self._require_current_runtime_data_integrity(conn, operation="retention cleanup")
            rows = conn.execute(
                "SELECT * FROM model_call_entries WHERE created_at < ?", (cutoff,)
            ).fetchall()
            inflight_count = sum(
                1
                for row in rows
                if str(row["lifecycle_state"]) == "reserved" and bool(row["request_dispatched"])
            )
            if inflight_count:
                conn.rollback()
                raise ModelCallLedgerInvariantError(
                    "retention cannot delete dispatched model-call reservations before settlement"
                )
            deleted = len(rows)
            if dry_run or not rows:
                conn.rollback()
                return deleted
            self._record_deleted_spend_tombstones(conn, rows)
            conn.execute("DELETE FROM model_call_entries WHERE created_at < ?", (cutoff,))
            affected_run_ids = sorted({str(row["run_id"]) for row in rows if row["run_id"]})
            if affected_run_ids:
                conn.execute(
                    render_sql(
                        "DELETE FROM model_call_runs WHERE run_id IN ({run_ids}) "
                        "AND NOT EXISTS (SELECT 1 FROM model_call_entries e "
                        "WHERE e.run_id=model_call_runs.run_id)",
                        placeholder_counts={"run_ids": len(affected_run_ids)},
                    ),
                    affected_run_ids,
                )
            conn.execute(
                "DELETE FROM model_call_daily_spend_tombstones WHERE spend_day < ?",
                (cutoff[:10],),
            )
            conn.commit()
        return deleted

    def delete_subject_scope(
        self,
        scope_kind: str,
        scope_value: str,
        *,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Delete exact entry-level subject evidence without raw identifiers.

        A batch entry may belong to more than one asset.  Deleting any one
        subject removes that entire non-reversible cost row (conservative
        over-deletion) and writes a day-level spend tombstone so privacy cannot
        be used to reset the provider cost cap.  In-flight entries block rather
        than race a post-dispatch provider response.
        """
        normalized_kind = str(scope_kind or "").strip()
        normalized_value = str(scope_value or "").strip()
        if (
            not normalized_value
            or (normalized_kind != "all" and normalized_kind not in _SUBJECT_SCOPE_KINDS)
            or (normalized_kind == "all" and normalized_value != "all")
        ):
            raise ModelCallLedgerInvariantError("subject scope kind and value are required")
        if not self.db_path.is_file():
            return {
                "status": "absent",
                "matched_run_count": 0,
                "deleted_entry_count": 0,
                "deleted_run_count": 0,
            }
        self._require_runtime_write_ready()
        subject_binding = (
            self._all_subject_binding()
            if normalized_kind == "all"
            else self._subject_binding((normalized_kind, normalized_value))
        )
        if subject_binding is None:
            raise ModelCallLedgerInvariantError("delete subject attribution is invalid")
        subject_hash = subject_binding[1]
        with self._connect() as conn:
            runtime_gaps = self._runtime_schema_gaps(conn)
            if runtime_gaps:
                return {
                    "status": "blocked",
                    "matched_run_count": 0,
                    "deleted_entry_count": 0,
                    "deleted_run_count": 0,
                    "schema_gap_count": len(runtime_gaps),
                    "error": "model_call_ledger_schema_invalid",
                }
            # This API is the user-facing privacy erasure path.  Its apply
            # mode must physically scrub released entry/run cells; dry-run
            # keeps both data and SQLite journal configuration unchanged.
            if not dry_run:
                require_delete_journal_mode_for_private_scrub(conn)
                require_secure_delete_for_private_scrub(conn)
            conn.execute("BEGIN IMMEDIATE")
            current_data_gaps = self._runtime_data_gaps(conn)
            if current_data_gaps:
                conn.rollback()
                return {
                    "status": "blocked",
                    "matched_run_count": 0,
                    "deleted_entry_count": 0,
                    "deleted_run_count": 0,
                    "schema_gap_count": len(current_data_gaps),
                    "error": "model_call_ledger_schema_invalid",
                }
            if not self._subject_is_frozen(conn, subject_binding):
                conn.rollback()
                return {
                    "status": "blocked",
                    "matched_run_count": 0,
                    "deleted_entry_count": 0,
                    "deleted_run_count": 0,
                    "error": "model_call_subject_not_frozen",
                }
            if normalized_kind == "all":
                rows = conn.execute("SELECT * FROM model_call_entries ORDER BY entry_id").fetchall()
                run_ids = {
                    str(row[0])
                    for row in conn.execute("SELECT run_id FROM model_call_runs").fetchall()
                }
            else:
                unattributed = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM model_call_entries e WHERE NOT EXISTS ("
                        "SELECT 1 FROM model_call_entry_subjects s WHERE s.entry_id=e.entry_id)"
                    ).fetchone()[0]
                    or 0
                )
                if unattributed:
                    conn.rollback()
                    return {
                        "status": "blocked",
                        "matched_run_count": 0,
                        "deleted_entry_count": 0,
                        "deleted_run_count": 0,
                        "error": "unattributed_billable_entry_count",
                        "unattributed_billable_entry_count": unattributed,
                    }
                rows = conn.execute(
                    """
                    SELECT DISTINCT e.* FROM model_call_entries e
                    JOIN model_call_entry_subjects s ON s.entry_id=e.entry_id
                    WHERE s.scope_kind=? AND s.subject_hash=?
                    ORDER BY e.entry_id
                    """,
                    (normalized_kind, subject_hash),
                ).fetchall()
                run_ids = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT run_id FROM model_call_run_subjects "
                        "WHERE scope_kind=? AND subject_hash=?",
                        (normalized_kind, subject_hash),
                    ).fetchall()
                }
                run_ids.update(str(row["run_id"]) for row in rows)
            inflight_count = sum(
                1
                for row in rows
                if str(row["lifecycle_state"]) == "reserved" and bool(row["request_dispatched"])
            )
            if inflight_count:
                conn.rollback()
                return {
                    "status": "blocked",
                    "matched_run_count": len(run_ids),
                    "deleted_entry_count": 0,
                    "deleted_run_count": 0,
                    "inflight_entry_count": inflight_count,
                    "error": "inflight_model_call_entries",
                }
            matched_entries = len(rows)
            matched_runs = len(run_ids)
            deleted_runs = 0
            if dry_run:
                conn.rollback()
            else:
                self._record_deleted_spend_tombstones(conn, rows)
                if rows:
                    entry_ids = [str(row["entry_id"]) for row in rows]
                    conn.execute(
                        render_sql(
                            "DELETE FROM model_call_entries "
                            "WHERE entry_id IN ({entry_ids})",
                            placeholder_counts={"entry_ids": len(entry_ids)},
                        ),
                        entry_ids,
                    )
                if normalized_kind == "all":
                    deleted_runs = conn.execute("DELETE FROM model_call_runs").rowcount
                elif run_ids:
                    deleted_runs = conn.execute(
                        render_sql(
                            "DELETE FROM model_call_runs WHERE run_id IN ({run_ids}) "
                            "AND NOT EXISTS (SELECT 1 FROM model_call_entries e "
                            "WHERE e.run_id=model_call_runs.run_id)",
                            placeholder_counts={"run_ids": len(run_ids)},
                        ),
                        sorted(run_ids),
                    ).rowcount
                conn.commit()
        return {
            "status": "dry_run" if dry_run else "applied",
            "matched_run_count": matched_runs,
            "deleted_entry_count": matched_entries,
            "deleted_run_count": 0 if dry_run else deleted_runs,
        }

    @classmethod
    def _retired_prompt_storage(cls, config: Any | None) -> tuple[int, int]:
        """Return ``(path_count, row_count)`` for retired split prompt stores.

        This runs read-only from health/privacy paths.  A remaining table is
        not silently tolerated: its rows are historic billable calls that have
        not yet been reconciled into the canonical ledger.
        """
        paths = RuntimePaths.from_config(config)
        database_dir = paths.database_dir
        path_count = 0
        row_count = 0
        candidate_paths = (
            database_dir / "wiki_state.db",
            database_dir / "prompt_calls.db",
            database_dir / "sync_log.db",
            paths.model_call_ledger_db,
        )
        seen_paths: set[Path] = set()
        for path in candidate_paths:
            resolved_path = path.expanduser().resolve()
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)
            if not path.is_file():
                continue
            try:
                uri = path.resolve().as_uri() + "?mode=ro"
                with _readonly_sqlite_connection(uri) as conn:
                    retired_tables = [
                        str(row[0])
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name IN ('prompt_calls', 'prompt_call_log', 'prompt_call_stats')"
                        ).fetchall()
                    ]
                    if not retired_tables:
                        continue
                    path_count += 1
                    for table in retired_tables:
                        # ``prompt_call_stats`` carries no individual billable
                        # request rows, but it is still a retired storage owner
                        # and must keep the path-mismatch signal visible until
                        # reconciliation removes it.
                        if table == "prompt_call_stats":
                            continue
                        if table not in _RETIRED_PROMPT_STORAGE_TABLES:
                            raise ValueError("unexpected_retired_prompt_table")
                        row = conn.execute(
                            render_sql(
                                "SELECT COUNT(*) FROM {table}",
                                identifiers={"table": table},
                            )
                        ).fetchone()
                        row_count += int(row[0] or 0) if row else 0
            except (sqlite3.Error, OSError, ValueError):
                # Treat an unreadable retired owner as present.  The migration
                # tool must be used to classify it; health must not erase the
                # signal merely because it cannot inspect it today.
                path_count += 1
        return path_count, row_count
