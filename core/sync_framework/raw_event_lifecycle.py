"""Destructive subject redaction and scheduled retention lifecycle for Raw."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import math
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from core.config import ConfigProvider
    from core.db_utils import SqlitePool

from core.db_utils import render_sql
from core.sync_framework.raw_event_identity import (
    DEFAULT_FRESHNESS_HALF_LIFE_DAYS,
    DEFAULT_RECALC_DAYS,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SURVIVAL_PRUNE_THRESHOLD,
    _NATIVE_RAW_CONTRACT_LEDGER,
    _compress_text,
    _initial_confidence,
    _json_dumps,
    _parse_datetime,
    _utcnow,
)
from core.sync_framework.raw_subject_deletion import (
    RAW_SUBJECT_DELETION_SCHEMA_VERSION,
    RAW_SUBJECT_DELETION_TABLE,
    subject_deletion_receipt_id,
    subject_deletion_visibility_predicate,
    subject_scope_hash,
)


class RawEventLifecycleMixin:
    """Own irreversible Raw deletion and scheduled retention transitions."""

    if TYPE_CHECKING:
        config: ConfigProvider
        _pool: SqlitePool

        def _resolve_logical_event_id(
            self,
            event_id: str,
            *,
            conn: sqlite3.Connection | None = None,
        ) -> str: ...

    def delete_subject_scope(
        self,
        *,
        request_id: str,
        scope_kind: str,
        scope_value: str,
    ) -> Dict[str, Any]:
        """Irreversibly redact canonical Raw for one confirmed ownership scope.

        The append-only source hash and provenance edges remain so downstream
        owners can receive their own deletion commands, but every recoverable
        Raw body, structured payload, source path, access query, and immutable
        revision snapshot is replaced in the same transaction.  A receipt is
        the only durable record of that operation and contains no subject
        literal or deleted content.
        """

        normalized_kind = str(scope_kind or "").strip().lower()
        raw_value = str(scope_value or "").strip()
        supported_scopes = {"all", "agent", "session", "project", "path", "raw_event_id"}
        if normalized_kind not in supported_scopes:
            return {
                "status": "unsupported_scope",
                "target_count": 0,
                "supported_scopes": sorted(supported_scopes),
            }
        if not str(request_id or "").strip() or not raw_value:
            raise ValueError("raw subject deletion requires request_id and scope_value")

        normalized_value = (
            raw_value.lower() if normalized_kind in {"agent", "project"} else raw_value
        )
        scope_hash = subject_scope_hash(normalized_kind, normalized_value)
        conn = self._pool.get_conn()
        if conn.in_transaction:
            raise RuntimeError("raw_subject_deletion_transaction_already_active")
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            render_sql(
                """
            SELECT COUNT(*), COALESCE(SUM(dependent_consumer_count), 0)
            FROM {subject_deletion_table}
            WHERE scope_kind=? AND scope_value_hash=? AND status='applied'
            """,
                identifiers={"subject_deletion_table": RAW_SUBJECT_DELETION_TABLE},
            ),
            (normalized_kind, scope_hash),
        ).fetchone()
        if prior and int(prior[0] or 0):
            access_log_after_count = int(
                conn.execute(
                    render_sql(
                        """
                    SELECT COUNT(*)
                    FROM raw_access_log AS access_log
                    JOIN {subject_deletion_table} AS subject_delete
                      ON subject_delete.event_id=access_log.event_id
                    WHERE subject_delete.scope_kind=?
                      AND subject_delete.scope_value_hash=?
                      AND subject_delete.status='applied'
                    """,
                        identifiers={"subject_deletion_table": RAW_SUBJECT_DELETION_TABLE},
                    ),
                    (normalized_kind, scope_hash),
                ).fetchone()[0]
                or 0
            )
            result = {
                "status": "existing",
                "target_count": int(prior[0] or 0),
                "receipt_count": int(prior[0] or 0),
                "pending_dependent_consumers": int(prior[1] or 0),
                "access_log_deleted": 0,
                "access_log_after_count": access_log_after_count,
                "consumer_access_log_verified": access_log_after_count == 0,
            }
            conn.rollback()
            return result

        target_query = render_sql(
            """
            SELECT
                t.event_id, t.current_revision_id, t.content_hash,
                t.full_content_hash, t.source_agent, t.session_id,
                t.source_path, t.metadata_json
            FROM raw_turns AS t
            WHERE NOT EXISTS (
                SELECT 1
                FROM {subject_deletion_table} AS subject_delete
                WHERE subject_delete.event_id=t.event_id
                  AND subject_delete.status='applied'
            )
        """,
            identifiers={"subject_deletion_table": RAW_SUBJECT_DELETION_TABLE},
        )
        params: list[Any] = []
        if normalized_kind == "agent":
            target_query += " AND lower(t.source_agent)=?"
            params.append(normalized_value)
        elif normalized_kind == "session":
            target_query += " AND t.session_id=?"
            params.append(normalized_value)
        elif normalized_kind == "path":
            target_query += " AND t.source_path=?"
            params.append(normalized_value)
        elif normalized_kind == "raw_event_id":
            logical_event_id = self._resolve_logical_event_id(
                normalized_value,
                conn=conn,
            )
            target_query += " AND t.event_id=?"
            params.append(logical_event_id)

        rows = conn.execute(target_query, tuple(params)).fetchall()
        targets: list[tuple[Any, ...]] = []
        for row in rows:
            if normalized_kind == "project":
                try:
                    metadata = json.loads(str(row[7] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    # A malformed scope header is not enough evidence to
                    # delete a different project's Raw data.
                    continue
                if str(metadata.get("project") or "").strip().lower() != normalized_value:
                    continue
            targets.append(tuple(row))

        if not targets:
            conn.rollback()
            return {
                "status": "no_targets",
                "target_count": 0,
                "receipt_count": 0,
                "pending_dependent_consumers": 0,
                "access_log_deleted": 0,
                "access_log_after_count": 0,
                "consumer_access_log_verified": True,
            }

        try:
            conn.execute("PRAGMA secure_delete = ON")
            secure_delete_row = conn.execute("PRAGMA secure_delete").fetchone()
        except sqlite3.Error:
            conn.rollback()
            return {
                "status": "blocked",
                "target_count": 0,
                "error": "sqlite_secure_delete_unavailable",
            }
        if not secure_delete_row or int(secure_delete_row[0] or 0) < 1:
            conn.rollback()
            return {
                "status": "blocked",
                "target_count": 0,
                "error": "sqlite_secure_delete_disabled",
            }

        now = _utcnow()
        empty_blob = _compress_text("")
        receipt_count = 0
        revision_count = 0
        access_log_deleted = 0
        native_contract_deleted = 0
        pending_dependents = 0
        gap_conditions: list[tuple[str, str]] = []
        try:
            for (
                event_id,
                current_revision_id,
                source_content_hash,
                full_content_hash,
                source_agent,
                session_id,
                _source_path,
                _metadata_json,
            ) in targets:
                event_id = str(event_id)
                receipt_id = subject_deletion_receipt_id(
                    request_id=str(request_id),
                    event_id=event_id,
                    scope_hash=scope_hash,
                )
                revisions = conn.execute(
                    """
                    SELECT revision_id, content_hash, full_content_hash
                    FROM raw_turn_revisions
                    WHERE logical_event_id=?
                    ORDER BY revision_number
                    """,
                    (event_id,),
                ).fetchall()
                dependent_count_row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT consumer_type || ':' || consumer_id)
                    FROM raw_provenance_edges AS edge
                    JOIN raw_turn_revisions AS revision
                      ON revision.revision_id=edge.source_revision_id
                    WHERE revision.logical_event_id=?
                    """,
                    (event_id,),
                ).fetchone()
                dependent_count = int(dependent_count_row[0] or 0) if dependent_count_row else 0
                redaction_material = json.dumps(
                    {
                        "schema_version": RAW_SUBJECT_DELETION_SCHEMA_VERSION,
                        "receipt_id": receipt_id,
                        "event_id": event_id,
                        "source_content_hash": str(source_content_hash or ""),
                        "scope_value_hash": scope_hash,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                redaction_hash = (
                    "sha256:" + hashlib.sha256(redaction_material.encode("utf-8")).hexdigest()
                )

                for revision_id, revision_content_hash, revision_full_content_hash in revisions:
                    tombstone_snapshot = {
                        "event_id": event_id,
                        "revision_id": str(revision_id),
                        "content_hash": str(revision_content_hash or ""),
                        "full_content_hash": str(revision_full_content_hash or ""),
                        "tombstone": {
                            "schema_version": RAW_SUBJECT_DELETION_SCHEMA_VERSION,
                            "receipt_id": receipt_id,
                            "redaction_hash": redaction_hash,
                        },
                        "user_content": "",
                        "assistant_content": "",
                        "reasoning": "",
                        "tool_calls": [],
                        "tool_results": [],
                        "attachments": [],
                        "raw_event_refs": [],
                        "source_files": [],
                        "metadata": {},
                    }
                    conn.execute(
                        "UPDATE raw_turn_revisions SET snapshot_blob=? WHERE revision_id=?",
                        (_compress_text(_json_dumps(tombstone_snapshot)), str(revision_id)),
                    )
                revision_count += len(revisions)

                redacted_metadata = json.dumps(
                    {
                        "subject_deletion_receipt": receipt_id,
                        "schema_version": RAW_SUBJECT_DELETION_SCHEMA_VERSION,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                conn.execute(
                    """
                    UPDATE raw_turns
                    SET source_agent='deleted',
                        session_id='deleted',
                        turn_number=0,
                        model_tag=NULL,
                        conversation_at=NULL,
                        captured_at=?,
                        origin='subject_deletion',
                        source_path=NULL,
                        source_files_json='[]',
                        completeness_status='subject_deleted',
                        completeness_json='{}',
                        metadata_json=?,
                        tool_calls_json='[]',
                        tool_results_json='[]',
                        attachments_json='[]',
                        raw_event_refs_json='[]',
                        reasoning_blob=?,
                        user_content_blob=?,
                        assistant_content_blob=?,
                        raw_bytes=0,
                        quality_rank=0,
                        updated_at=?
                    WHERE event_id=?
                    """,
                    (now, redacted_metadata, empty_blob, empty_blob, empty_blob, now, event_id),
                )
                conn.execute(
                    """
                    UPDATE raw_metrics
                    SET search_count=0,
                        result_count=0,
                        hit_count=0,
                        view_count=0,
                        reference_count=0,
                        last_accessed_at=NULL,
                        last_survival_recalc_at=NULL,
                        next_survival_recalc_at=NULL,
                        freshness_score=0,
                        confidence=0,
                        survival_score=0,
                        pinned=0,
                        retention_state='subject_deleted',
                        updated_at=?
                    WHERE event_id=?
                    """,
                    (now, event_id),
                )
                access_log_deleted += int(
                    conn.execute(
                        "DELETE FROM raw_access_log WHERE event_id=?", (event_id,)
                    ).rowcount
                    or 0
                )
                native_contract_deleted += int(
                    conn.execute(
                        "DELETE FROM raw_native_contract_observations WHERE logical_event_id=?",
                        (event_id,),
                    ).rowcount
                    or 0
                )
                conn.execute(
                    render_sql(
                        """
                    INSERT INTO {subject_deletion_table} (
                        receipt_id, schema_version, request_id, scope_kind,
                        scope_value_hash, event_id, current_revision_id,
                        source_content_hash, redaction_hash, revision_count,
                        dependent_consumer_count, status, created_at, applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)
                    """,
                        identifiers={"subject_deletion_table": RAW_SUBJECT_DELETION_TABLE},
                    ),
                    (
                        receipt_id,
                        RAW_SUBJECT_DELETION_SCHEMA_VERSION,
                        str(request_id),
                        normalized_kind,
                        scope_hash,
                        event_id,
                        str(current_revision_id or ""),
                        str(source_content_hash or full_content_hash or ""),
                        redaction_hash,
                        len(revisions),
                        dependent_count,
                        now,
                        now,
                    ),
                )
                receipt_count += 1
                pending_dependents += dependent_count
                gap_conditions.append((str(source_agent or ""), str(session_id or "")))

            if normalized_kind == "all":
                conn.execute("DELETE FROM raw_provenance_gaps")
            else:
                for source_agent, session_id in sorted(set(gap_conditions)):
                    if source_agent and session_id:
                        conn.execute(
                            "DELETE FROM raw_provenance_gaps WHERE source_agent=? AND session_id=?",
                            (source_agent, session_id),
                        )
        except (sqlite3.Error, OSError, TypeError, ValueError):
            conn.rollback()
            return {
                "status": "blocked",
                "target_count": 0,
                "error": "raw_subject_redaction_failed",
            }

        target_event_ids = tuple(str(target[0]) for target in targets)
        try:
            access_log_after_count = int(
                conn.execute(
                    render_sql(
                        "SELECT COUNT(*) FROM raw_access_log " "WHERE event_id IN ({event_ids})",
                        placeholder_counts={"event_ids": len(target_event_ids)},
                    ),
                    target_event_ids,
                ).fetchone()[0]
                or 0
            )
        except sqlite3.Error:
            conn.rollback()
            return {
                "status": "blocked",
                "target_count": 0,
                "error": "raw_subject_redaction_verification_failed",
            }
        if access_log_after_count != 0:
            conn.rollback()
            return {
                "status": "blocked",
                "target_count": 0,
                "error": "raw_subject_access_log_residual",
                "access_log_after_count": access_log_after_count,
            }
        conn.commit()

        return {
            "status": "applied",
            "target_count": len(targets),
            "receipt_count": receipt_count,
            "revision_count": revision_count,
            "access_log_deleted": access_log_deleted,
            "access_log_after_count": access_log_after_count,
            "consumer_access_log_verified": access_log_after_count == 0,
            "native_contract_observations_deleted": native_contract_deleted,
            "pending_dependent_consumers": pending_dependents,
            "secure_delete": True,
        }

    def refresh_survival_scores(
        self,
        *,
        now: Optional[datetime] = None,
        force: bool = False,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Atomically refresh one bounded Raw retention generation."""
        conn = self._pool.get_conn()
        if conn.in_transaction:
            raise RuntimeError("raw_lifecycle_transaction_already_active")
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = self._refresh_survival_scores_in_transaction(
                conn=conn,
                now=now,
                force=force,
                limit=limit,
            )
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise

    def _refresh_survival_scores_in_transaction(
        self,
        *,
        conn: sqlite3.Connection,
        now: Optional[datetime] = None,
        force: bool = False,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Weekly lifecycle refresh for raw retention metrics.

        Normal search/access paths only append counters.  This method is the
        scheduled batch job that applies decay and marks low-value hot raw
        projections as eligible for deletion.
        """
        now = now or datetime.now()
        now_iso = now.isoformat()
        from core.kia.policy import get_shadowed_value

        recalc_days = int(self.config.get("raw_event_store.recalc_days", DEFAULT_RECALC_DAYS))
        retention_days = int(
            get_shadowed_value(
                "raw_event_store.retention_days",
                self.config.get("raw_event_store.retention_days", DEFAULT_RETENTION_DAYS),
            )
        )
        prune_threshold = float(
            get_shadowed_value(
                "raw_event_store.survival_prune_threshold",
                self.config.get(
                    "raw_event_store.survival_prune_threshold",
                    DEFAULT_SURVIVAL_PRUNE_THRESHOLD,
                ),
            )
        )
        half_life_days = max(
            1.0,
            float(
                self.config.get(
                    "raw_event_store.freshness_half_life_days",
                    DEFAULT_FRESHNESS_HALF_LIFE_DAYS,
                )
            ),
        )

        query = """
            SELECT
                m.event_id, m.search_count, m.result_count, m.hit_count,
                m.view_count, m.reference_count, m.confidence, m.pinned,
                t.completeness_status, t.conversation_at, t.captured_at
            FROM raw_metrics m
            JOIN raw_turns t ON t.event_id = m.event_id
            WHERE NOT EXISTS (
                SELECT 1
                FROM raw_event_identity_aliases a
                WHERE a.alias_event_id=m.event_id
            )
        """
        query += _NATIVE_RAW_CONTRACT_LEDGER.current_event_visibility_predicate("m.event_id")
        query += subject_deletion_visibility_predicate("m.event_id")
        params: list[Any] = []
        if not force:
            query += """
                AND (m.next_survival_recalc_at IS NULL
                   OR m.next_survival_recalc_at <= ?
                )
            """
            params.append(now_iso)
        query += " ORDER BY COALESCE(m.next_survival_recalc_at, '') ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))

        rows = conn.execute(query, params).fetchall()
        updated = 0
        eligible_delete = 0
        active = 0
        next_recalc = (now + timedelta(days=recalc_days)).isoformat()
        for row in rows:
            (
                event_id,
                search_count,
                result_count,
                hit_count,
                _view_count,
                reference_count,
                confidence,
                pinned,
                completeness_status,
                conversation_at,
                captured_at,
            ) = row
            anchor = _parse_datetime(conversation_at or captured_at, now)
            age_days = max(0.0, (now - anchor).total_seconds() / 86400)
            freshness_score = math.pow(0.5, age_days / half_life_days)
            confidence = float(confidence or _initial_confidence(completeness_status))

            activity_units = (
                float(search_count or 0) * 0.2
                + float(result_count or 0) * 0.5
                + float(hit_count or 0) * 3.0
                + float(reference_count or 0) * 5.0
            )
            activity_score = min(60.0, math.log1p(activity_units) * 18.0)
            survival_score = min(
                100.0,
                (100.0 if pinned else 0.0)
                + confidence * 20.0
                + freshness_score * 20.0
                + activity_score,
            )

            retention_state = "active"
            if (
                not pinned
                and age_days >= retention_days
                and int(hit_count or 0) == 0
                and int(reference_count or 0) == 0
                and survival_score < prune_threshold
            ):
                retention_state = "eligible_delete"
                eligible_delete += 1
            else:
                active += 1

            conn.execute(
                """
                UPDATE raw_metrics
                SET freshness_score = ?,
                    survival_score = ?,
                    retention_state = ?,
                    last_survival_recalc_at = ?,
                    next_survival_recalc_at = ?,
                    updated_at = ?
                WHERE event_id = ?
                """,
                (
                    round(freshness_score, 4),
                    round(survival_score, 2),
                    retention_state,
                    now_iso,
                    next_recalc,
                    now_iso,
                    event_id,
                ),
            )
            updated += 1

        conn.execute(
            """
            INSERT INTO raw_lifecycle_state (key, value, updated_at)
            VALUES ('last_survival_refresh', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (
                json.dumps(
                    {
                        "updated": updated,
                        "eligible_delete": eligible_delete,
                        "active": active,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now_iso,
            ),
        )
        return {
            "updated": updated,
            "eligible_delete": eligible_delete,
            "active": active,
            "next_recalc_at": next_recalc,
        }

    def purge_eligible_delete(self, *, limit: Optional[int] = None) -> Dict[str, int]:
        """Atomically purge one exact eligible Raw target set."""
        conn = self._pool.get_conn()
        if conn.in_transaction:
            raise RuntimeError("raw_lifecycle_transaction_already_active")
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = self._purge_eligible_delete_in_transaction(
                conn=conn,
                limit=limit,
            )
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise

    def _purge_eligible_delete_in_transaction(
        self,
        *,
        conn: sqlite3.Connection,
        limit: Optional[int] = None,
    ) -> Dict[str, int]:
        """Physically delete raw turns already marked eligible for deletion."""
        query = """
            SELECT m.event_id
            FROM raw_metrics m
            WHERE m.retention_state = 'eligible_delete'
              AND NOT EXISTS (
                  SELECT 1
                  FROM raw_event_identity_aliases a
                  WHERE a.alias_event_id=m.event_id
                     OR a.canonical_event_id=m.event_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM raw_turn_revisions r
                  JOIN raw_provenance_edges e
                    ON e.source_revision_id = r.revision_id
                  WHERE r.logical_event_id = m.event_id
              )
        """
        query += _NATIVE_RAW_CONTRACT_LEDGER.current_event_visibility_predicate("m.event_id")
        query += subject_deletion_visibility_predicate("m.event_id")
        query += " ORDER BY updated_at ASC"
        params: list[Any] = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))

        event_ids = [str(row[0]) for row in conn.execute(query, params).fetchall()]
        if not event_ids:
            return {
                "purged": 0,
                "raw_turns_deleted": 0,
                "raw_metrics_deleted": 0,
                "raw_access_logs_deleted": 0,
                "raw_native_contract_observations_deleted": 0,
                "raw_revisions_deleted": 0,
            }

        access_deleted = conn.execute(
            render_sql(
                "DELETE FROM raw_access_log WHERE event_id IN ({event_ids})",
                placeholder_counts={"event_ids": len(event_ids)},
            ),
            event_ids,
        ).rowcount
        metrics_deleted = conn.execute(
            render_sql(
                "DELETE FROM raw_metrics WHERE event_id IN ({event_ids})",
                placeholder_counts={"event_ids": len(event_ids)},
            ),
            event_ids,
        ).rowcount
        native_contract_observations_deleted = conn.execute(
            render_sql(
                "DELETE FROM raw_native_contract_observations "
                "WHERE logical_event_id IN ({event_ids})",
                placeholder_counts={"event_ids": len(event_ids)},
            ),
            event_ids,
        ).rowcount
        revisions_deleted = conn.execute(
            render_sql(
                "DELETE FROM raw_turn_revisions " "WHERE logical_event_id IN ({event_ids})",
                placeholder_counts={"event_ids": len(event_ids)},
            ),
            event_ids,
        ).rowcount
        turns_deleted = conn.execute(
            render_sql(
                "DELETE FROM raw_turns WHERE event_id IN ({event_ids})",
                placeholder_counts={"event_ids": len(event_ids)},
            ),
            event_ids,
        ).rowcount
        conn.execute(
            """
            INSERT INTO raw_lifecycle_state (key, value, updated_at)
            VALUES ('last_physical_purge', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (
                json.dumps(
                    {
                        "purged": turns_deleted,
                        "raw_metrics_deleted": metrics_deleted,
                        "raw_access_logs_deleted": access_deleted,
                        "raw_native_contract_observations_deleted": native_contract_observations_deleted,
                        "raw_revisions_deleted": revisions_deleted,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                _utcnow(),
            ),
        )
        return {
            "purged": int(turns_deleted or 0),
            "raw_turns_deleted": int(turns_deleted or 0),
            "raw_metrics_deleted": int(metrics_deleted or 0),
            "raw_access_logs_deleted": int(access_deleted or 0),
            "raw_native_contract_observations_deleted": int(
                native_contract_observations_deleted or 0
            ),
            "raw_revisions_deleted": int(revisions_deleted or 0),
        }
