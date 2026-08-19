# -*- coding: utf-8 -*-
"""Canonical raw turn store.

This database is the default raw capture layer.  Low-value raw turns are kept
only for the configured retention window; Obsidian raw markdown is rebuilt from
the retained rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from core.config import ConfigProvider, get_config
from core.db_utils import SqlitePool, release_transient_pools, render_sql
from core.sync_framework.raw_event_identity import (
    DEFAULT_RECALC_DAYS,
    _NATIVE_RAW_CONTRACT_LEDGER,
    RawEventIdentitySchemaMigrationRequired,
    _bind_source_support_metadata,
    _compress_text,
    _event_id,
    _initial_confidence,
    _json_dumps,
    _quality_rank,
    _record_native_raw_contract_outcome,
    _revision_id,
    _utcnow,
    _validate_native_raw_contract,
    classify_completeness as classify_completeness,
    compute_logical_event_id as compute_logical_event_id,
    compute_raw_content_hash,
)
from core.sync_framework.raw_event_identity_aliases import (
    ensure_schema as ensure_event_identity_alias_schema,
    resolve_canonical_event_id,
)
from core.sync_framework.raw_session_identity_reconciliation import (
    RawSessionIdentityReconciliationError,
    receipt_allows_current_fingerprint,
)
from core.sync_framework.raw_event_reader import (
    CanonicalRawReadError as CanonicalRawReadError,
    CanonicalRawTurn as CanonicalRawTurn,
    _decompress_text as _decompress_text,
    canonical_observation_text as canonical_observation_text,
    count_current_raw_turns_readonly as count_current_raw_turns_readonly,
    decode_raw_revision_snapshot as decode_raw_revision_snapshot,
    iter_current_raw_turns_readonly as iter_current_raw_turns_readonly,
    list_current_raw_turns_readonly as list_current_raw_turns_readonly,
)
from core.sync_framework.raw_provenance_store import RawProvenanceStore
from core.sync_framework.raw_subject_deletion import (
    ensure_subject_deletion_schema,
    is_subject_deleted,
    subject_deletion_visibility_predicate,
)
from core.sync_framework.native_event_identity import resolve_native_event_identity
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_lifecycle import RawEventLifecycleMixin
from core.sync_framework.raw_current_projection_store import (
    repair_current_projection_if_invalid,
    restore_current_projection_from_revision,
)
from core.sync_framework.agent_source import TURN_STRUCTURED_METADATA_KEYS
from core.utils import secure_file

logger = logging.getLogger(__name__)
_RAW_ACCESS_INCREMENT_CONTRACT = frozenset(
    {
        "search_count = search_count + 1",
        "result_count = result_count + 1",
        "hit_count = hit_count + 1",
        "view_count = view_count + 1",
        "reference_count = reference_count + 1",
    }
)

__all__ = [
    "CanonicalRawReadError",
    "CanonicalRawTurn",
    "RawEventIdentitySchemaMigrationRequired",
    "RawEventStore",
    "canonical_observation_text",
    "classify_completeness",
    "compute_logical_event_id",
    "compute_raw_content_hash",
    "count_current_raw_turns_readonly",
    "decode_raw_revision_snapshot",
    "iter_current_raw_turns_readonly",
    "list_current_raw_turns_readonly",
]


# A Raw revision can legitimately be large because Raw is lossless.  Page by
# compressed snapshot bytes so one normal backlog page cannot retain the whole
# store in memory.  An individual oversized revision is still yielded by
# itself; rejecting or truncating it would violate the Raw contract.


class RawEventStore(RawEventLifecycleMixin):
    """SQLite-backed canonical raw turn store and lifecycle metrics."""

    def __getattribute__(self, name: str):
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or name == "close" or not callable(attr):
            return attr
        class_attr = getattr(type(self), name, None)
        if not callable(class_attr):
            return attr

        def release_after_call(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            finally:
                release_transient_pools(self, "_pool")

        return release_after_call

    def __init__(
        self,
        db_path: Optional[Path] = None,
        config: Optional[ConfigProvider] = None,
    ) -> None:
        self.config = config or get_config()
        configured = (
            self.config.get("raw_event_store.db_path") if hasattr(self.config, "get") else None
        )
        self.db_path = Path(
            db_path or configured or (self.config.database_dir / "raw_events.db")
        ).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = SqlitePool(self.db_path)
        self._provenance = RawProvenanceStore(
            get_connection=self._pool.get_conn,
            get_turn=lambda event_id, conn: self._get_turn_in_connection(
                conn,
                event_id,
            ),
            resolve_logical_event_id=lambda event_id, conn: (
                self._resolve_logical_event_id(event_id, conn=conn)
            ),
        )
        try:
            self._ensure_schema()
        except BaseException:
            self._pool.close()
            raise
        release_transient_pools(self, "_pool")
        secure_file(self.db_path)

    def close(self) -> None:
        self._pool.close()

    def _ensure_schema(self) -> None:
        conn = self._pool.get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._execute_schema_statements(conn, """
            CREATE TABLE IF NOT EXISTS raw_turns (
                event_id TEXT PRIMARY KEY,
                current_revision_id TEXT,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL,
                model_tag TEXT,
                conversation_at TEXT,
                captured_at TEXT NOT NULL,
                origin TEXT NOT NULL,
                source_path TEXT,
                source_files_json TEXT,
                content_hash TEXT NOT NULL,
                full_content_hash TEXT,
                completeness_status TEXT NOT NULL,
                completeness_json TEXT,
                metadata_json TEXT,
                tool_calls_json TEXT,
                tool_results_json TEXT,
                attachments_json TEXT,
                raw_event_refs_json TEXT,
                reasoning_blob BLOB,
                user_content_blob BLOB NOT NULL,
                assistant_content_blob BLOB NOT NULL,
                compression TEXT NOT NULL DEFAULT 'zlib',
                raw_bytes INTEGER NOT NULL DEFAULT 0,
                quality_rank INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raw_turns_source_session
                ON raw_turns(source_agent, session_id);
            CREATE INDEX IF NOT EXISTS idx_raw_turns_source_session_turn
                ON raw_turns(source_agent, session_id, turn_number);
            CREATE INDEX IF NOT EXISTS idx_raw_turns_status
                ON raw_turns(completeness_status);

            CREATE TABLE IF NOT EXISTS raw_turn_revisions (
                revision_id TEXT PRIMARY KEY,
                logical_event_id TEXT NOT NULL,
                revision_number INTEGER NOT NULL,
                supersedes_revision_id TEXT,
                content_hash TEXT NOT NULL,
                full_content_hash TEXT,
                snapshot_blob BLOB NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(logical_event_id, content_hash),
                FOREIGN KEY(logical_event_id) REFERENCES raw_turns(event_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_raw_revisions_logical_number
                ON raw_turn_revisions(logical_event_id, revision_number);

            CREATE TABLE IF NOT EXISTS raw_provenance_edges (
                edge_id TEXT PRIMARY KEY,
                source_revision_id TEXT NOT NULL,
                span_start INTEGER NOT NULL,
                span_end INTEGER NOT NULL,
                consumer_type TEXT NOT NULL,
                consumer_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(
                    source_revision_id, span_start, span_end,
                    consumer_type, consumer_id
                ),
                FOREIGN KEY(source_revision_id)
                    REFERENCES raw_turn_revisions(revision_id) ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS idx_raw_provenance_source
                ON raw_provenance_edges(source_revision_id, created_at);

            CREATE TABLE IF NOT EXISTS raw_provenance_gaps (
                gap_id TEXT PRIMARY KEY,
                consumer_type TEXT NOT NULL,
                consumer_id TEXT NOT NULL,
                source_agent TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_rebuild',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE(consumer_type, consumer_id, reason)
            );
            CREATE INDEX IF NOT EXISTS idx_raw_provenance_gap_status
                ON raw_provenance_gaps(status, consumer_type);

            CREATE TABLE IF NOT EXISTS raw_metrics (
                event_id TEXT PRIMARY KEY,
                search_count INTEGER NOT NULL DEFAULT 0,
                result_count INTEGER NOT NULL DEFAULT 0,
                hit_count INTEGER NOT NULL DEFAULT 0,
                view_count INTEGER NOT NULL DEFAULT 0,
                reference_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at TEXT,
                last_survival_recalc_at TEXT,
                next_survival_recalc_at TEXT,
                freshness_score REAL NOT NULL DEFAULT 1.0,
                confidence REAL NOT NULL DEFAULT 0.0,
                survival_score REAL NOT NULL DEFAULT 0.0,
                pinned INTEGER NOT NULL DEFAULT 0,
                retention_state TEXT NOT NULL DEFAULT 'active',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES raw_turns(event_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS raw_access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                access_type TEXT NOT NULL,
                query TEXT,
                consumer TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_raw_access_event_time
                ON raw_access_log(event_id, created_at);

            CREATE TABLE IF NOT EXISTS raw_lifecycle_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(raw_turns)").fetchall()
            }
            if "current_revision_id" not in columns:
                conn.execute("ALTER TABLE raw_turns ADD COLUMN current_revision_id TEXT")
            self._require_identity_schema(conn)
            ensure_event_identity_alias_schema(conn)
            NativeRawContractLedger.ensure_schema(conn)
            ensure_subject_deletion_schema(conn)
            self._backfill_unversioned_revisions(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _execute_schema_statements(
        conn: sqlite3.Connection,
        script: str,
    ) -> None:
        """Execute fixed DDL without ``executescript``'s implicit commit."""
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(statement)

    @staticmethod
    def _require_identity_schema(conn: sqlite3.Connection) -> None:
        """Reject the old turn-number uniqueness until a deliberate migration.

        A parser can legitimately map two native events to the same ordinal
        after an insertion or source-artifact split.  Silently rebuilding a
        live Raw table in a constructor would make ordinary capture mutate
        schema and hide a migration boundary, so the reconciler owns it.
        """
        for index in conn.execute("PRAGMA index_list(raw_turns)").fetchall():
            # seq, name, unique, origin, partial
            if len(index) < 3 or not int(index[2]):
                continue
            name = str(index[1])
            columns = tuple(
                str(row[2])
                for row in conn.execute(
                    render_sql(
                        "PRAGMA index_info({index})",
                        identifiers={"index": str(name)},
                    )
                ).fetchall()
            )
            if columns == ("source_agent", "session_id", "turn_number"):
                raise RawEventIdentitySchemaMigrationRequired(
                    "raw_turns legacy UNIQUE(source_agent, session_id, turn_number) "
                    "requires the exact-plan migration owner "
                    "scripts/reconcile_agent_source_raw_capture.py --apply "
                    "--expected-plan-hash <sha256> --backup-dir <dir> --json"
                )
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RawEventIdentitySchemaMigrationRequired(
                "raw provenance foreign-key violations require "
                "the exact-plan migration owner "
                "scripts/reconcile_agent_source_raw_capture.py --apply "
                "--expected-plan-hash <sha256> --backup-dir <dir> --json"
            )

    @staticmethod
    def _turn_snapshot(data: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = dict(data)
        for key in (
            "source_files_json",
            "completeness_json",
            "metadata_json",
            "tool_calls_json",
            "tool_results_json",
            "attachments_json",
            "raw_event_refs_json",
        ):
            out_key = key.removesuffix("_json")
            try:
                snapshot[out_key] = json.loads(snapshot.pop(key, "null") or "null")
            except json.JSONDecodeError:
                snapshot[out_key] = None
        snapshot["user_content"] = _decompress_text(snapshot.pop("user_content_blob", b""))
        snapshot["assistant_content"] = _decompress_text(
            snapshot.pop("assistant_content_blob", b"")
        )
        snapshot["reasoning"] = _decompress_text(snapshot.pop("reasoning_blob", b""))
        return snapshot

    def _backfill_unversioned_revisions(self, conn: sqlite3.Connection) -> None:
        cursor = conn.execute(
            "SELECT * FROM raw_turns WHERE current_revision_id IS NULL " "OR current_revision_id=''"
        )
        columns = [str(item[0]) for item in cursor.description]
        for raw_row in cursor.fetchall():
            data = dict(zip(columns, raw_row))
            logical_event_id = str(data["event_id"])
            revision_id = _revision_id(logical_event_id, str(data["content_hash"]))
            snapshot = self._turn_snapshot(data)
            conn.execute(
                """
                INSERT OR IGNORE INTO raw_turn_revisions (
                    revision_id, logical_event_id, revision_number,
                    supersedes_revision_id, content_hash, full_content_hash,
                    snapshot_blob, created_at
                ) VALUES (?, ?, 0, NULL, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    logical_event_id,
                    data["content_hash"],
                    data.get("full_content_hash"),
                    _compress_text(_json_dumps(snapshot)),
                    data.get("captured_at") or data.get("updated_at") or _utcnow(),
                ),
            )
            conn.execute(
                "UPDATE raw_turns SET current_revision_id=? WHERE event_id=?",
                (revision_id, logical_event_id),
            )

    def _existing_quality(self, event_id: str) -> Optional[int]:
        row = (
            self._pool.get_conn()
            .execute("SELECT quality_rank FROM raw_turns WHERE event_id = ?", (event_id,))
            .fetchone()
        )
        return int(row[0]) if row else None

    @staticmethod
    def _restore_current_projection_from_revision(
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        revision_id: str,
    ) -> bool:
        return restore_current_projection_from_revision(
            conn,
            logical_event_id=logical_event_id,
            revision_id=revision_id,
        )

    @staticmethod
    def _repair_current_projection_if_invalid(
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        revision_id: str,
    ) -> bool:
        return repair_current_projection_if_invalid(
            conn,
            logical_event_id=logical_event_id,
            revision_id=revision_id,
        )

    def upsert_turn(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        user_content: str,
        assistant_content: str,
        model_tag: str = "",
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[list[Dict[str, Any]]] = None,
        tool_results: Optional[list[Dict[str, Any]]] = None,
        reasoning: str = "",
        attachments: Optional[list[Dict[str, Any]]] = None,
        raw_event_refs: Optional[list[Dict[str, Any]]] = None,
        source_files: Optional[list[str]] = None,
        source_path: Optional[str] = None,
        completeness: Optional[Dict[str, Any]] = None,
        content_hash: Optional[str] = None,
        full_content_hash: Optional[str] = None,
        origin: str = "sync_engine",
    ) -> str:
        """Atomically insert or update one immutable Raw revision generation."""
        conn = self._pool.get_conn()
        if conn.in_transaction:
            raise RuntimeError("raw_event_store_transaction_already_active")
        conn.execute("BEGIN IMMEDIATE")
        try:
            revision_id = self._upsert_turn_in_transaction(
                conn=conn,
                source_agent=source_agent,
                session_id=session_id,
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
                model_tag=model_tag,
                timestamp=timestamp,
                metadata=metadata,
                tool_calls=tool_calls,
                tool_results=tool_results,
                reasoning=reasoning,
                attachments=attachments,
                raw_event_refs=raw_event_refs,
                source_files=source_files,
                source_path=source_path,
                completeness=completeness,
                content_hash=content_hash,
                full_content_hash=full_content_hash,
                origin=origin,
            )
            conn.commit()
            return revision_id
        except BaseException:
            conn.rollback()
            raise

    def _upsert_turn_in_transaction(
        self,
        *,
        conn: sqlite3.Connection,
        source_agent: str,
        session_id: str,
        turn_number: int,
        user_content: str,
        assistant_content: str,
        model_tag: str = "",
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[list[Dict[str, Any]]] = None,
        tool_results: Optional[list[Dict[str, Any]]] = None,
        reasoning: str = "",
        attachments: Optional[list[Dict[str, Any]]] = None,
        raw_event_refs: Optional[list[Dict[str, Any]]] = None,
        source_files: Optional[list[str]] = None,
        source_path: Optional[str] = None,
        completeness: Optional[Dict[str, Any]] = None,
        content_hash: Optional[str] = None,
        full_content_hash: Optional[str] = None,
        origin: str = "sync_engine",
    ) -> str:
        """Insert or update a raw turn without downgrading higher-quality content."""
        metadata = _bind_source_support_metadata(metadata, source_agent)
        metadata = {
            key: value
            for key, value in metadata.items()
            if key not in TURN_STRUCTURED_METADATA_KEYS
        }
        if metadata.get("identity_reconciliation_required") is True:
            version = str(metadata.get("identity_contract_version") or "")
            aliases = metadata.get("session_aliases")
            legacy_ids = metadata.get("legacy_canonical_session_ids")
            identities = [
                session_id,
                *(
                    [str(value) for value in aliases]
                    if isinstance(aliases, list)
                    else []
                ),
                *(
                    [str(value) for value in legacy_ids]
                    if isinstance(legacy_ids, list)
                    else []
                ),
            ]
            if not version or self._has_incompatible_session_identity_in_connection(
                conn,
                source_agent,
                identities,
                identity_contract_version=version,
                canonical_session_id=session_id,
                source_artifact_id=str(metadata.get("source_artifact_id") or ""),
            ):
                raise RawEventIdentitySchemaMigrationRequired(
                    "source_session_identity_reconciliation_required"
                )
        tool_calls = tool_calls or []
        tool_results = tool_results or []
        attachments = attachments or []
        raw_event_refs = raw_event_refs or []
        source_files = source_files or []
        completeness = completeness or {}
        # Identity must be resolved before the first database lookup.  A turn
        # number is only an ordering hint: parser insertion/reordering can
        # change it, whereas native event IDs and artifact offsets do not.
        identity_metadata = dict(metadata)
        if source_path:
            identity_metadata.setdefault("source_path", source_path)
        if source_files:
            identity_metadata.setdefault("source_artifact_id", source_files[0])
        identity = resolve_native_event_identity(
            metadata=identity_metadata,
            raw_event_refs=raw_event_refs,
            turn_number=turn_number,
        )
        if identity.is_explicit:
            eid = _event_id(
                source_agent,
                session_id,
                turn_number,
                native_event_id=identity.value,
            )
        elif identity.has_auditable_fallback:
            eid = _event_id(
                source_agent,
                session_id,
                turn_number,
                parser=identity.parser,
                parser_version=identity.parser_version,
                source_artifact_id=identity.source_artifact_id,
                artifact_offset=identity.artifact_offset,
            )
        else:
            eid = _event_id(source_agent, session_id, turn_number)
        metadata["logical_event_identity_kind"] = identity.kind
        if identity.value:
            metadata["logical_event_identity"] = identity.value
        if metadata.get("support_native_capture") is True:
            contract_errors = _validate_native_raw_contract(
                metadata,
                completeness,
                source_agent,
            )
            _record_native_raw_contract_outcome(metadata, contract_errors)
        status = classify_completeness(completeness, metadata)
        qrank = _quality_rank(status, origin)
        now = _utcnow()
        raw_hash = content_hash or compute_raw_content_hash(
            user_content=user_content,
            assistant_content=assistant_content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            tool_results=tool_results,
            attachments=attachments,
            raw_event_refs=raw_event_refs,
            metadata=metadata,
        )
        original_eid = eid
        eid = resolve_canonical_event_id(conn, eid)
        if is_subject_deleted(conn, eid):
            raise PermissionError(
                "raw event is subject-deleted and cannot be revived by a later capture"
            )
        if eid != original_eid:
            metadata["logical_event_identity_alias_resolved_from"] = original_eid
            metadata["logical_event_identity_alias_canonical"] = eid
        existing_row = conn.execute(
            "SELECT quality_rank, content_hash, current_revision_id "
            "FROM raw_turns WHERE event_id=?",
            (eid,),
        ).fetchone()
        preserve_existing = existing_row is not None and int(existing_row[0]) > qrank
        native_contract_observation = metadata.get("support_native_capture") is True
        revision_id = _revision_id(eid, raw_hash)
        if (
            native_contract_observation
            and existing_row is not None
            and str(existing_row[2] or "")
        ):
            self._repair_current_projection_if_invalid(
                conn,
                logical_event_id=eid,
                revision_id=str(existing_row[2]),
            )
        revision_exists = conn.execute(
            "SELECT 1 FROM raw_turn_revisions WHERE revision_id=?", (revision_id,)
        ).fetchone()
        if revision_exists:
            if (
                existing_row is not None
                and str(existing_row[2] or "") == revision_id
            ):
                self._restore_current_projection_from_revision(
                    conn,
                    logical_event_id=eid,
                    revision_id=revision_id,
                )
            if native_contract_observation:
                _NATIVE_RAW_CONTRACT_LEDGER.record(
                    conn,
                    logical_event_id=eid,
                    revision_id=revision_id,
                    metadata=metadata,
                    observed_at=now,
                )
                _NATIVE_RAW_CONTRACT_LEDGER.refresh_effective_state(
                    conn,
                    logical_event_id=eid,
                    observed_at=now,
                )
            return revision_id
        previous_revision_id = str(existing_row[2] or "") if existing_row else ""
        revision_number = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision_number), -1) + 1 "
                "FROM raw_turn_revisions WHERE logical_event_id=?",
                (eid,),
            ).fetchone()[0]
        )
        raw_bytes = (
            len((user_content or "").encode("utf-8"))
            + len((assistant_content or "").encode("utf-8"))
            + len((reasoning or "").encode("utf-8"))
        )

        conn.execute(
            """
            INSERT INTO raw_turns (
                event_id, source_agent, session_id, turn_number, model_tag,
                conversation_at, captured_at, origin, source_path, source_files_json,
                content_hash, full_content_hash, completeness_status,
                completeness_json, metadata_json, tool_calls_json,
                tool_results_json, attachments_json, raw_event_refs_json,
                reasoning_blob, user_content_blob, assistant_content_blob,
                compression, raw_bytes, quality_rank, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'zlib', ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                turn_number=excluded.turn_number,
                model_tag=excluded.model_tag,
                conversation_at=excluded.conversation_at,
                captured_at=excluded.captured_at,
                origin=excluded.origin,
                source_path=excluded.source_path,
                source_files_json=excluded.source_files_json,
                content_hash=excluded.content_hash,
                full_content_hash=excluded.full_content_hash,
                completeness_status=excluded.completeness_status,
                completeness_json=excluded.completeness_json,
                metadata_json=excluded.metadata_json,
                tool_calls_json=excluded.tool_calls_json,
                tool_results_json=excluded.tool_results_json,
                attachments_json=excluded.attachments_json,
                raw_event_refs_json=excluded.raw_event_refs_json,
                reasoning_blob=excluded.reasoning_blob,
                user_content_blob=excluded.user_content_blob,
                assistant_content_blob=excluded.assistant_content_blob,
                raw_bytes=excluded.raw_bytes,
                quality_rank=excluded.quality_rank,
                updated_at=excluded.updated_at
            WHERE excluded.quality_rank >= raw_turns.quality_rank
            """,
            (
                eid,
                source_agent,
                session_id,
                int(turn_number),
                model_tag,
                timestamp,
                now,
                origin,
                source_path,
                _json_dumps(source_files),
                raw_hash,
                full_content_hash,
                status,
                _json_dumps(completeness),
                _json_dumps(metadata),
                _json_dumps(tool_calls),
                _json_dumps(tool_results),
                _json_dumps(attachments),
                _json_dumps(raw_event_refs),
                _compress_text(reasoning or ""),
                _compress_text(user_content or ""),
                _compress_text(assistant_content or ""),
                raw_bytes,
                qrank,
                now,
            ),
        )
        snapshot = {
            "event_id": eid,
            "source_agent": source_agent,
            "session_id": session_id,
            "turn_number": int(turn_number),
            "model_tag": model_tag,
            "conversation_at": timestamp,
            "captured_at": now,
            "origin": origin,
            "source_path": source_path,
            "source_files": source_files,
            "content_hash": raw_hash,
            "full_content_hash": full_content_hash,
            "completeness_status": status,
            "completeness": completeness,
            "metadata": metadata,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "attachments": attachments,
            "raw_event_refs": raw_event_refs,
            "reasoning": reasoning or "",
            "user_content": user_content or "",
            "assistant_content": assistant_content or "",
            "compression": "zlib",
            "raw_bytes": raw_bytes,
            "quality_rank": qrank,
            "updated_at": now,
        }
        conn.execute(
            """
            INSERT INTO raw_turn_revisions (
                revision_id, logical_event_id, revision_number,
                supersedes_revision_id, content_hash, full_content_hash,
                snapshot_blob, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                eid,
                revision_number,
                previous_revision_id or None,
                raw_hash,
                full_content_hash,
                _compress_text(_json_dumps(snapshot)),
                now,
            ),
        )
        if not preserve_existing:
            conn.execute(
                "UPDATE raw_turns SET current_revision_id=? WHERE event_id=?",
                (revision_id, eid),
            )
        next_recalc = (datetime.now() + timedelta(days=DEFAULT_RECALC_DAYS)).isoformat()
        conn.execute(
            """
            INSERT OR IGNORE INTO raw_metrics (
                event_id, confidence, survival_score, next_survival_recalc_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (eid, _initial_confidence(status), _initial_confidence(status), next_recalc, now),
        )
        if native_contract_observation:
            _NATIVE_RAW_CONTRACT_LEDGER.record(
                conn,
                logical_event_id=eid,
                revision_id=revision_id,
                metadata=metadata,
                observed_at=now,
            )
            _NATIVE_RAW_CONTRACT_LEDGER.refresh_effective_state(
                conn,
                logical_event_id=eid,
                observed_at=now,
            )
        return revision_id

    def get_turn(self, event_id: str) -> Optional[Dict[str, Any]]:
        conn = self._pool.get_conn()
        return self._get_turn_in_connection(conn, event_id)

    def _get_turn_in_connection(
        self,
        conn: sqlite3.Connection,
        event_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Materialize one Raw turn without reacquiring the transaction owner."""
        revision = conn.execute(
            """
            SELECT logical_event_id, revision_number, supersedes_revision_id,
                   snapshot_blob
            FROM raw_turn_revisions WHERE revision_id=?
            """,
            (event_id,),
        ).fetchone()
        if revision:
            if is_subject_deleted(conn, str(revision[0])):
                return None
            data = decode_raw_revision_snapshot(revision[3])
            data["logical_event_id"] = str(revision[0])
            data["revision_id"] = event_id
            data["revision_number"] = int(revision[1])
            data["supersedes_revision_id"] = str(revision[2] or "")
            data["event_id"] = event_id
            return _NATIVE_RAW_CONTRACT_LEDGER.decorate_current_revision(
                conn,
                logical_event_id=str(revision[0]),
                revision_id=event_id,
                data=data,
            )
        event_id = resolve_canonical_event_id(conn, event_id)
        if is_subject_deleted(conn, event_id):
            return None
        query = "SELECT * FROM raw_turns WHERE event_id = ?"
        query += _NATIVE_RAW_CONTRACT_LEDGER.current_event_visibility_predicate(
            "raw_turns.event_id"
        )
        row = conn.execute(query, (event_id,)).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.execute("SELECT * FROM raw_turns LIMIT 0").description]
        data = dict(zip(cols, row))
        data["user_content"] = _decompress_text(data.pop("user_content_blob"))
        data["assistant_content"] = _decompress_text(data.pop("assistant_content_blob"))
        data["reasoning"] = _decompress_text(data.pop("reasoning_blob"))
        for key in (
            "source_files_json",
            "completeness_json",
            "metadata_json",
            "tool_calls_json",
            "tool_results_json",
            "attachments_json",
            "raw_event_refs_json",
        ):
            out_key = key.removesuffix("_json")
            try:
                data[out_key] = json.loads(data.pop(key) or "null")
            except json.JSONDecodeError:
                data[out_key] = None
        return data

    def list_revisions(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        native_event_id: str = "",
        parser: str = "",
        parser_version: str = "",
        source_artifact_id: str = "",
        artifact_offset: str = "",
    ) -> list[Dict[str, Any]]:
        logical_event_id = _event_id(
            source_agent,
            session_id,
            turn_number,
            native_event_id=native_event_id,
            parser=parser,
            parser_version=parser_version,
            source_artifact_id=source_artifact_id,
            artifact_offset=artifact_offset,
        )
        conn = self._pool.get_conn()
        logical_event_id = resolve_canonical_event_id(conn, logical_event_id)
        if is_subject_deleted(conn, logical_event_id):
            return []
        rows = conn.execute(
            """
            SELECT revision_id, logical_event_id, revision_number,
                   supersedes_revision_id, content_hash, full_content_hash, created_at
            FROM raw_turn_revisions
            WHERE logical_event_id=?
               OR logical_event_id IN (
                   SELECT alias_event_id
                   FROM raw_event_identity_aliases
                   WHERE canonical_event_id=?
               )
            ORDER BY created_at, logical_event_id, revision_number
            """,
            (logical_event_id, logical_event_id),
        ).fetchall()
        keys = (
            "revision_id",
            "logical_event_id",
            "revision_number",
            "supersedes_revision_id",
            "content_hash",
            "full_content_hash",
            "created_at",
        )
        return [dict(zip(keys, row)) for row in rows]

    def list_native_contract_observations(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        native_event_id: str = "",
        parser: str = "",
        parser_version: str = "",
        source_artifact_id: str = "",
        artifact_offset: str = "",
    ) -> list[Dict[str, Any]]:
        """Return append-only native contract verdicts for one logical Raw turn."""
        logical_event_id = _event_id(
            source_agent,
            session_id,
            turn_number,
            native_event_id=native_event_id,
            parser=parser,
            parser_version=parser_version,
            source_artifact_id=source_artifact_id,
            artifact_offset=artifact_offset,
        )
        conn = self._pool.get_conn()
        logical_event_id = resolve_canonical_event_id(conn, logical_event_id)
        if is_subject_deleted(conn, logical_event_id):
            return []
        return _NATIVE_RAW_CONTRACT_LEDGER.list_for_event(
            conn,
            logical_event_id=logical_event_id,
        )

    def record_provenance_edge(
        self,
        *,
        source_revision_id: str,
        span_start: int,
        span_end: int,
        consumer_type: str,
        consumer_id: str,
    ) -> str:
        return self._provenance.record_edge(
            source_revision_id=source_revision_id,
            span_start=span_start,
            span_end=span_end,
            consumer_type=consumer_type,
            consumer_id=consumer_id,
        )

    def record_intentional_no_observation(
        self,
        *,
        source_revision_id: str,
        reason: str,
    ) -> str:
        """Persist one typed Observation terminal receipt for Raw input."""
        return self._provenance.record_intentional_no_observation(
            source_revision_id=source_revision_id,
            reason=reason,
        )

    def list_provenance_edges(self, revision_id: str) -> list[Dict[str, Any]]:
        return self._provenance.list_edges(revision_id)

    def record_provenance_gap(
        self,
        *,
        consumer_type: str,
        consumer_id: str,
        reason: str,
        source_agent: str = "",
        session_id: str = "",
    ) -> str:
        return self._provenance.record_gap(
            consumer_type=consumer_type,
            consumer_id=consumer_id,
            reason=reason,
            source_agent=source_agent,
            session_id=session_id,
        )

    def resolve_provenance_gaps(self, *, consumer_type: str, consumer_id: str) -> int:
        return self._provenance.resolve_gaps(
            consumer_type=consumer_type,
            consumer_id=consumer_id,
        )

    def provenance_gap_counts(self) -> Dict[str, int]:
        return self._provenance.gap_counts()

    def list_current_headers(
        self,
        *,
        session_id: str = "",
        source_agent: str = "",
        days: Optional[int] = None,
        include_eligible_delete: bool = False,
    ) -> list[Dict[str, Any]]:
        """Return metadata-only current revision headers for auth-before-body search."""
        query = """
            SELECT
                COALESCE(t.current_revision_id, t.event_id), t.event_id,
                t.source_agent, t.session_id, t.turn_number,
                t.conversation_at, t.captured_at, t.content_hash,
                t.full_content_hash, t.completeness_status, t.metadata_json,
                COALESCE(m.retention_state, 'active')
            FROM raw_turns t
            LEFT JOIN raw_metrics m ON m.event_id=t.event_id
            WHERE NOT EXISTS (
                SELECT 1
                FROM raw_event_identity_aliases a
                WHERE a.alias_event_id=t.event_id
            )
        """
        query += _NATIVE_RAW_CONTRACT_LEDGER.current_event_visibility_predicate("t.event_id")
        query += subject_deletion_visibility_predicate("t.event_id")
        params: list[Any] = []
        if session_id:
            query += " AND t.session_id=?"
            params.append(session_id)
        if source_agent:
            query += " AND t.source_agent=?"
            params.append(source_agent)
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=max(0, int(days)))).isoformat()
            query += " AND COALESCE(t.conversation_at, t.captured_at) >= ?"
            params.append(cutoff)
        if not include_eligible_delete:
            query += " AND COALESCE(m.retention_state, 'active') != 'eligible_delete'"
        query += " ORDER BY COALESCE(t.conversation_at, t.captured_at) DESC, t.turn_number DESC"

        rows = self._pool.get_conn().execute(query, params).fetchall()
        headers: list[Dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row[10] or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            headers.append(
                {
                    "uid": f"raw-revision:{row[0]}",
                    "revision_id": str(row[0]),
                    "logical_event_id": str(row[1]),
                    "source_agent": str(row[2]),
                    "source": str(row[2]),
                    "session_id": str(row[3]),
                    "turn_number": int(row[4]),
                    "conversation_at": str(row[5] or ""),
                    "captured_at": str(row[6] or ""),
                    "content_hash": str(row[7]),
                    "full_content_hash": str(row[8] or ""),
                    "completeness_status": str(row[9] or ""),
                    "project": str(metadata.get("project") or ""),
                    "support_manifest_hash": str(metadata.get("support_manifest_hash") or ""),
                    "scope": "private",
                    "acl_schema_version": 1,
                    "acl_metadata_complete": True,
                    "acl_reconciliation_status": "canonical_raw_index",
                    "retention_state": str(row[11] or "active"),
                }
            )
        return headers

    def get_revision_header(self, revision_id: str) -> Optional[Dict[str, Any]]:
        """Return metadata-only identity for any immutable revision, including superseded ones."""
        row = (
            self._pool.get_conn()
            .execute(
                """
            SELECT
                r.revision_id, t.event_id, t.source_agent, t.session_id,
                t.turn_number, t.conversation_at, t.captured_at,
                r.content_hash, r.full_content_hash, t.completeness_status,
                t.metadata_json, COALESCE(m.retention_state, 'active')
            FROM raw_turn_revisions r
            JOIN raw_turns t ON t.event_id=r.logical_event_id
            LEFT JOIN raw_metrics m ON m.event_id=t.event_id
            WHERE r.revision_id=?
            """,
                (revision_id,),
            )
            .fetchone()
        )
        if not row:
            return None
        if is_subject_deleted(self._pool.get_conn(), str(row[1])):
            return None
        try:
            metadata = json.loads(row[10] or "{}")
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return {
            "uid": f"raw-revision:{row[0]}",
            "revision_id": str(row[0]),
            "logical_event_id": str(row[1]),
            "source_agent": str(row[2]),
            "source": str(row[2]),
            "session_id": str(row[3]),
            "turn_number": int(row[4]),
            "conversation_at": str(row[5] or ""),
            "captured_at": str(row[6] or ""),
            "content_hash": str(row[7]),
            "full_content_hash": str(row[8] or ""),
            "completeness_status": str(row[9] or ""),
            "project": str(metadata.get("project") or ""),
            "support_manifest_hash": str(metadata.get("support_manifest_hash") or ""),
            "scope": "private",
            "acl_schema_version": 1,
            "acl_metadata_complete": True,
            "acl_reconciliation_status": "canonical_raw_index",
            "retention_state": str(row[11] or "active"),
        }

    def has_incompatible_session_identity(
        self,
        source_agent: str,
        session_ids: list[str],
        *,
        identity_contract_version: str,
        canonical_session_id: str = "",
        source_artifact_id: str = "",
    ) -> bool:
        """Detect historical session rows not bound to the requested identity contract."""
        return self._has_incompatible_session_identity_in_connection(
            self._pool.get_conn(),
            source_agent,
            session_ids,
            identity_contract_version=identity_contract_version,
            canonical_session_id=canonical_session_id,
            source_artifact_id=source_artifact_id,
        )

    def _has_incompatible_session_identity_in_connection(
        self,
        conn: sqlite3.Connection,
        source_agent: str,
        session_ids: list[str],
        *,
        identity_contract_version: str,
        canonical_session_id: str = "",
        source_artifact_id: str = "",
    ) -> bool:
        """Evaluate an identity contract on the caller-owned SQLite snapshot."""
        identities = list(
            dict.fromkeys(str(value) for value in session_ids if str(value))
        )
        if not source_agent or not identities:
            return False
        rows = conn.execute(
            "SELECT metadata_json FROM raw_turns "
            "WHERE source_agent=? "
            "AND session_id IN (SELECT value FROM json_each(?))",
            (
                str(source_agent),
                json.dumps(identities, ensure_ascii=False, separators=(",", ":")),
            ),
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row[0] or "{}")
            except (json.JSONDecodeError, TypeError):
                return True
            if metadata.get("identity_contract_version") != identity_contract_version:
                try:
                    return not receipt_allows_current_fingerprint(
                        conn,
                        source_agent=source_agent,
                        identity_contract_version=identity_contract_version,
                        canonical_session_id=(
                            canonical_session_id
                            or (identities[0] if identities else "")
                        ),
                        legacy_session_ids=identities,
                        source_artifact_id=source_artifact_id,
                    )
                except (
                    RawSessionIdentityReconciliationError,
                    sqlite3.Error,
                    TypeError,
                    ValueError,
                ):
                    return True
        return False

    def get_current_event_header(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Return the current metadata-only header for one logical Raw event.

        Logical event IDs are stable public identities; authorization resolves
        their current immutable revision before materializing any body.
        """

        conn = self._pool.get_conn()
        logical_event_id = resolve_canonical_event_id(conn, str(event_id))
        query = "SELECT COALESCE(current_revision_id, event_id) FROM raw_turns " "WHERE event_id=?"
        query += _NATIVE_RAW_CONTRACT_LEDGER.current_event_visibility_predicate(
            "raw_turns.event_id"
        )
        query += subject_deletion_visibility_predicate("raw_turns.event_id")
        row = conn.execute(query, (logical_event_id,)).fetchone()
        if not row:
            return None
        return self.get_revision_header(str(row[0]))

    def record_access(
        self,
        event_id: str,
        access_type: str,
        *,
        query: Optional[str] = None,
        consumer: Optional[str] = None,
    ) -> None:
        """Record access counters. A user view is not a hit."""
        allowed = {"search", "result", "hit", "view", "reference"}
        if access_type not in allowed:
            raise ValueError(f"invalid access_type: {access_type}")
        now = _utcnow()
        increments = {
            "search": "search_count = search_count + 1",
            "result": "result_count = result_count + 1",
            "hit": "hit_count = hit_count + 1",
            "view": "view_count = view_count + 1",
            "reference": "reference_count = reference_count + 1",
        }
        conn = self._pool.get_conn()
        if conn.in_transaction:
            raise RuntimeError("raw_access_transaction_already_active")
        conn.execute("BEGIN IMMEDIATE")
        try:
            logical_event_id = self._resolve_logical_event_id(
                event_id,
                conn=conn,
            )
            if is_subject_deleted(conn, logical_event_id):
                raise PermissionError("raw event is subject-deleted")
            conn.execute(
                """
                INSERT INTO raw_access_log (
                    event_id, access_type, query, consumer, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (logical_event_id, access_type, query, consumer, now),
            )
            conn.execute(
                render_sql(
                    """
                UPDATE raw_metrics
                SET {increment},
                    last_accessed_at = ?,
                    updated_at = ?
                WHERE event_id = ?
                """,
                    fixed_fragments={
                        "increment": (
                            increments[access_type],
                            _RAW_ACCESS_INCREMENT_CONTRACT,
                        )
                    },
                ),
                (now, now, logical_event_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _resolve_logical_event_id(
        self,
        event_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Resolve either an immutable revision or logical alias to the metrics key."""
        conn = conn or self._pool.get_conn()
        row = conn.execute(
            "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
            (event_id,),
        ).fetchone()
        logical_event_id = str(row[0]) if row else event_id
        return resolve_canonical_event_id(conn, logical_event_id)

    def get_logical_event_id(self, event_id: str) -> str:
        """Return the canonical logical alias for a known immutable revision."""
        return self._resolve_logical_event_id(event_id)

    def find_event_id(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        native_event_id: str = "",
        parser: str = "",
        parser_version: str = "",
        source_artifact_id: str = "",
        artifact_offset: str = "",
    ) -> Optional[str]:
        """Find a current revision without guessing among colliding turn numbers."""
        explicit_identity = bool(
            native_event_id
            or (parser and parser_version and source_artifact_id and artifact_offset)
        )
        conn = self._pool.get_conn()
        if explicit_identity:
            logical_event_id = _event_id(
                source_agent,
                session_id,
                turn_number,
                native_event_id=native_event_id,
                parser=parser,
                parser_version=parser_version,
                source_artifact_id=source_artifact_id,
                artifact_offset=artifact_offset,
            )
            logical_event_id = resolve_canonical_event_id(conn, logical_event_id)
            query = (
                "SELECT COALESCE(current_revision_id, event_id) FROM raw_turns " "WHERE event_id=?"
            )
            query += _NATIVE_RAW_CONTRACT_LEDGER.current_event_visibility_predicate(
                "raw_turns.event_id"
            )
            query += subject_deletion_visibility_predicate("raw_turns.event_id")
            row = conn.execute(query, (logical_event_id,)).fetchone()
            return str(row[0]) if row else None
        query = """
            SELECT COALESCE(current_revision_id, event_id) FROM raw_turns
            WHERE source_agent = ? AND session_id = ? AND turn_number = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM raw_event_identity_aliases a
                  WHERE a.alias_event_id=raw_turns.event_id
              )
        """
        query += _NATIVE_RAW_CONTRACT_LEDGER.current_event_visibility_predicate(
            "raw_turns.event_id"
        )
        query += subject_deletion_visibility_predicate("raw_turns.event_id")
        query += " ORDER BY updated_at DESC LIMIT 2"
        rows = conn.execute(
            query,
            (source_agent, session_id, int(turn_number)),
        ).fetchall()
        # A bare turn number becomes ambiguous after native parser renumbering.
        return str(rows[0][0]) if len(rows) == 1 else None

    def record_turn_access(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        access_type: str,
        query: Optional[str] = None,
        consumer: Optional[str] = None,
    ) -> bool:
        """Record access by source/session/turn if the canonical event exists."""
        event_id = self.find_event_id(
            source_agent=source_agent,
            session_id=session_id,
            turn_number=turn_number,
        )
        if not event_id:
            return False
        self.record_access(event_id, access_type, query=query, consumer=consumer)
        return True

    def get_metrics(self, event_id: str) -> Optional[Dict[str, Any]]:
        conn = self._pool.get_conn()
        logical_event_id = self._resolve_logical_event_id(event_id)
        if is_subject_deleted(conn, logical_event_id):
            return None
        row = conn.execute(
            "SELECT * FROM raw_metrics WHERE event_id = ?", (logical_event_id,)
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in conn.execute("SELECT * FROM raw_metrics LIMIT 0").description]
        return dict(zip(cols, row))
