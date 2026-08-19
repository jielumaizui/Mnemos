"""Canonical owner and atomic UnitOfWork for typed cognitive state.

The store shares ``producer_consumer_ledger.db`` with flow envelopes, but it is
the only owner of semantic revisions.  Every semantic commit uses one SQLite
connection and one ``BEGIN IMMEDIATE`` transaction for revision rows, the
transport envelope and local outbox commands.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence, cast

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_access,
    cognitive_access_hash,
    validate_cognitive_access_envelope,
)
from core.cognitive.state_effect_receipts import CognitiveStateEffectReceiptMixin
from core.cognitive.feedback_command_attempt_store import FeedbackCommandAttemptMixin
from core.cognitive.state_lifecycle import CognitiveStateLifecycleMixin
from core.cognitive.state_contract import (
    COGNITIVE_OBJECT_TYPES,
    CognitiveHeadPrecondition,
    CognitiveStateCommitReceipt,
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
)
from core.cognitive.state_schema import validate_cognitive_state_schema
from core.cognitive.state_types import (
    COGNITIVE_TOMBSTONE_COMMAND_TYPE,
    CognitiveStateConflict,
)
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.cognitive_event_ledger import (
    cognitive_data_snapshot_in_connection,
    insert_data_event_in_connection,
)
from core.privacy.ownership_freeze import cognitive_write_is_frozen


def _validated_bound_search_access(candidate: sqlite3.Row) -> dict[str, Any]:
    """Validate one small ACL header against its immutable binding projection."""

    access = validate_cognitive_access_envelope(
        json.loads(str(candidate["access_control_json"] or "")),
        expected_scope_type=str(candidate["scope_type"]),
        expected_scope_id=str(candidate["scope_id"]),
    )
    binding_access = validate_cognitive_access_envelope(
        json.loads(str(candidate["binding_access_control_json"] or "")),
        expected_scope_type=str(candidate["binding_scope_type"]),
        expected_scope_id=str(candidate["binding_scope_id"]),
    )
    access_hash = cognitive_access_hash(access)
    if access_hash != str(candidate["access_control_hash"]):
        raise ValueError("typed search ACL header hash mismatch")
    if (
        access != binding_access
        or access_hash != cognitive_access_hash(binding_access)
        or access_hash != str(candidate["binding_access_control_hash"])
    ):
        raise ValueError("typed search ACL header revision binding mismatch")
    if not (
        str(candidate["revision_payload_hash"])
        == str(candidate["binding_payload_hash"])
        == str(candidate["canonical_payload_hash"])
    ):
        raise ValueError("typed search revision payload binding mismatch")
    header_identity = (
        str(candidate["header_object_type"]),
        str(candidate["header_object_id"]),
        str(candidate["scope_type"]),
        str(candidate["scope_id"]),
    )
    binding_identity = (
        str(candidate["binding_object_type"]),
        str(candidate["binding_object_id"]),
        str(candidate["binding_scope_type"]),
        str(candidate["binding_scope_id"]),
    )
    canonical_identity = (
        str(candidate["canonical_object_type"]),
        str(candidate["canonical_object_id"]),
        str(candidate["canonical_scope_type"]),
        str(candidate["canonical_scope_id"]),
    )
    if header_identity != binding_identity or binding_identity != canonical_identity:
        raise ValueError("typed search header revision identity mismatch")
    return access


class CognitiveStateStore(
    CognitiveStateLifecycleMixin,
    FeedbackCommandAttemptMixin,
    CognitiveStateEffectReceiptMixin,
):
    """Read/write facade for the canonical semantic state database."""

    def __init__(self, db_path_or_config: Path | str | Any):
        self.config: Any | None = None
        if isinstance(db_path_or_config, (Path, str)):
            candidate = Path(db_path_or_config)
            if candidate.suffix == ".db":
                self.db_path = candidate
            else:
                self.db_path = candidate / "producer_consumer_ledger.db"
        else:
            self.config = db_path_or_config
            database_dir = getattr(db_path_or_config, "database_dir", None)
            if database_dir is None:
                raise ValueError("config.database_dir is required for cognitive state store")
            self.db_path = Path(database_dir) / "producer_consumer_ledger.db"

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        if read_only:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                timeout=30,
            )
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        validate_cognitive_state_schema(conn)
        return conn

    def unit_of_work(self) -> "CognitiveStateUnitOfWork":
        return CognitiveStateUnitOfWork(self)

    def _assert_writes_not_frozen(
        self,
        revisions: Sequence[CognitiveStateRevision],
        commands: Sequence[LocalConsumerCommand],
    ) -> None:
        """Reject state writes whose derived object scope is under freeze."""

        if self.config is None or _is_tombstone_control_commit(revisions, commands):
            return

        for revision in revisions:
            access_control = validate_cognitive_access_envelope(
                revision.payload["access_control"],
                expected_scope_type=revision.scope_type,
                expected_scope_id=revision.scope_id,
            )
            scope = access_control["scope"]
            if cognitive_write_is_frozen(
                self.config,
                session_id=str(scope["session_id"]),
                project=str(scope["project"]),
                agent=str(access_control["owner"]["agent"]),
                source_event_ids=(revision.source_event_id, revision.source_revision_id),
            ):
                raise PermissionError(
                    "cognitive state write is blocked by a matching frozen data ownership scope"
                )

    def current_revision(
        self,
        object_type: str,
        object_id: str,
    ) -> CognitiveStateRevision | None:
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT r.*
                FROM cognitive_state_heads AS h
                JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
                WHERE h.object_type=? AND h.object_id=?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cognitive_state_outbox AS tombstone
                      WHERE tombstone.command_type=?
                        AND EXISTS (
                            SELECT 1
                            FROM json_each(
                                tombstone.payload_json,
                                '$.target_revision_ids'
                            ) AS target
                            WHERE target.value=r.revision_id
                        )
                  )
                """,
                (object_type, object_id, COGNITIVE_TOMBSTONE_COMMAND_TYPE),
            ).fetchone()
        return _revision_from_row(row) if row is not None else None

    def current_revisions(
        self,
        *,
        object_type: str = "",
        object_id: str = "",
        scope_type: str = "",
        scope_id: str = "",
    ) -> tuple[CognitiveStateRevision, ...]:
        """Read the typed current-state projection for internal owner workflows.

        Prompt-facing and externally visible retrieval must use
        :meth:`authorized_current_revisions`, which authorizes a compact ACL
        projection before it fetches a revision body.
        """

        if object_type and object_type not in COGNITIVE_OBJECT_TYPES:
            raise ValueError(f"unsupported cognitive object type: {object_type}")
        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT r.*
                FROM cognitive_state_heads AS h
                JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
                WHERE (?='' OR r.object_type=?)
                  AND (?='' OR r.object_id=?)
                  AND (?='' OR r.scope_type=?)
                  AND (?='' OR r.scope_id=?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cognitive_state_outbox AS tombstone
                      WHERE tombstone.command_type=?
                        AND EXISTS (
                            SELECT 1
                            FROM json_each(
                                tombstone.payload_json,
                                '$.target_revision_ids'
                            ) AS target
                            WHERE target.value=r.revision_id
                        )
                  )
                ORDER BY r.object_type, r.object_id
                """,
                (
                    object_type,
                    object_type,
                    object_id,
                    object_id,
                    scope_type,
                    scope_type,
                    scope_id,
                    scope_id,
                    COGNITIVE_TOMBSTONE_COMMAND_TYPE,
                ),
            ).fetchall()
        return tuple(_revision_from_row(row) for row in rows)

    def authorized_current_revisions(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
        object_type: str = "",
        object_id: str = "",
        scope_type: str = "",
        scope_id: str = "",
    ) -> tuple[tuple[CognitiveStateRevision, ...], dict[str, Any]]:
        """Return only revisions admitted by the object ACL before body fetch.

        The first query selects immutable identity fields plus the compact
        ``access_control`` object.  It intentionally does not return
        ``payload_json``.  A second query fetches complete revision bodies only
        for candidates already authorized by a server principal and explicit
        request purpose.
        """

        if object_type and object_type not in COGNITIVE_OBJECT_TYPES:
            raise ValueError(f"unsupported cognitive object type: {object_type}")
        if principal is None:
            return (), {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"principal_required": 1},
            }
        if not str(purpose or "").strip():
            return (), {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"purpose_required": 1},
            }

        denied_by_reason: dict[str, int] = {}
        authorized_ids: list[str] = []
        with self._connect(read_only=True) as conn:
            from core.cognitive.search_state_headers import require_state_search_headers

            require_state_search_headers(conn)
            candidates = conn.execute(
                """
                SELECT search.revision_id,
                       search.object_type AS header_object_type,
                       search.object_id AS header_object_id,
                       search.scope_type, search.scope_id,
                       search.access_control AS access_control_json,
                       search.access_control_hash,
                       search.revision_payload_hash,
                       binding.access_control AS binding_access_control_json,
                       binding.access_control_hash AS binding_access_control_hash,
                       binding.revision_payload_hash AS binding_payload_hash,
                       binding.object_type AS binding_object_type,
                       binding.object_id AS binding_object_id,
                       binding.scope_type AS binding_scope_type,
                       binding.scope_id AS binding_scope_id,
                       revision.payload_hash AS canonical_payload_hash,
                       revision.object_type AS canonical_object_type,
                       revision.object_id AS canonical_object_id,
                       revision.scope_type AS canonical_scope_type,
                       revision.scope_id AS canonical_scope_id
                FROM cognitive_state_heads AS h
                JOIN typed_search_state_headers AS search
                  ON search.revision_id=h.revision_id
                JOIN typed_search_state_revision_bindings AS binding
                  ON binding.revision_id=search.revision_id
                JOIN cognitive_state_revisions AS revision
                  ON revision.revision_id=search.revision_id
                WHERE (?='' OR search.object_type=?)
                  AND (?='' OR search.object_id=?)
                  AND (?='' OR search.scope_type=?)
                  AND (?='' OR search.scope_id=?)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cognitive_state_outbox AS tombstone
                      WHERE tombstone.command_type=?
                        AND EXISTS (
                            SELECT 1
                            FROM json_each(
                                tombstone.payload_json,
                                '$.target_revision_ids'
                            ) AS target
                            WHERE target.value=search.revision_id
                        )
                  )
                ORDER BY search.object_type, search.object_id
                """,
                (
                    object_type,
                    object_type,
                    object_id,
                    object_id,
                    scope_type,
                    scope_type,
                    scope_id,
                    scope_id,
                    COGNITIVE_TOMBSTONE_COMMAND_TYPE,
                ),
            ).fetchall()
            for candidate in candidates:
                try:
                    access = _validated_bound_search_access(candidate)
                except (TypeError, ValueError, json.JSONDecodeError):
                    reason = "acl_unknown"
                else:
                    decision = authorize_cognitive_access(
                        access,
                        principal=principal,
                        narrowing=narrowing,
                        purpose=purpose,
                    )
                    reason = decision.reason
                if reason == "authorized":
                    authorized_ids.append(str(candidate["revision_id"]))
                else:
                    denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1

            if not authorized_ids:
                return (), {
                    "candidate_count": len(candidates),
                    "authorized_count": 0,
                    "denied_by_reason": denied_by_reason,
                }
            placeholders = ",".join("?" for _ in authorized_ids)
            rows = conn.execute(
                "SELECT * FROM cognitive_state_revisions "
                f"WHERE revision_id IN ({placeholders}) "  # nosec B608 - placeholders are generated
                "ORDER BY object_type, object_id",
                tuple(authorized_ids),
            ).fetchall()
        return (
            tuple(_revision_from_row(row) for row in rows),
            {
                "candidate_count": len(candidates),
                "authorized_count": len(rows),
                "denied_by_reason": denied_by_reason,
            },
        )

    def authorized_current_revisions_by_purpose(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purposes_by_type: Mapping[str, str],
    ) -> tuple[tuple[CognitiveStateRevision, ...], dict[str, Any]]:
        """Authorize all current headers once using each type's fixed read purpose."""

        if principal is None:
            return (), {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"principal_required": 1},
            }
        normalized_purposes = {
            str(object_type): str(purpose).strip()
            for object_type, purpose in purposes_by_type.items()
            if str(object_type) in COGNITIVE_OBJECT_TYPES and str(purpose).strip()
        }
        if not normalized_purposes:
            return (), {
                "candidate_count": 0,
                "authorized_count": 0,
                "denied_by_reason": {"purpose_required": 1},
            }

        denied_by_reason: dict[str, int] = {}
        authorized_ids: list[str] = []
        with self._connect(read_only=True) as conn:
            from core.cognitive.search_state_headers import require_state_search_headers

            require_state_search_headers(conn)
            candidates = conn.execute(
                """
                SELECT search.revision_id,
                       search.object_type AS header_object_type,
                       search.object_id AS header_object_id,
                       search.scope_type, search.scope_id,
                       search.access_control AS access_control_json,
                       search.access_control_hash,
                       search.revision_payload_hash,
                       binding.access_control AS binding_access_control_json,
                       binding.access_control_hash AS binding_access_control_hash,
                       binding.revision_payload_hash AS binding_payload_hash,
                       binding.object_type AS binding_object_type,
                       binding.object_id AS binding_object_id,
                       binding.scope_type AS binding_scope_type,
                       binding.scope_id AS binding_scope_id,
                       revision.payload_hash AS canonical_payload_hash,
                       revision.object_type AS canonical_object_type,
                       revision.object_id AS canonical_object_id,
                       revision.scope_type AS canonical_scope_type,
                       revision.scope_id AS canonical_scope_id
                FROM cognitive_state_heads AS h
                JOIN typed_search_state_headers AS search
                  ON search.revision_id=h.revision_id
                JOIN typed_search_state_revision_bindings AS binding
                  ON binding.revision_id=search.revision_id
                JOIN cognitive_state_revisions AS revision
                  ON revision.revision_id=search.revision_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM cognitive_state_outbox AS tombstone
                    WHERE tombstone.command_type=?
                      AND EXISTS (
                          SELECT 1
                          FROM json_each(
                              tombstone.payload_json,
                              '$.target_revision_ids'
                          ) AS target
                          WHERE target.value=search.revision_id
                      )
                )
                ORDER BY search.object_type, search.object_id
                """,
                (COGNITIVE_TOMBSTONE_COMMAND_TYPE,),
            ).fetchall()
            for candidate in candidates:
                purpose = normalized_purposes.get(str(candidate["header_object_type"]))
                if purpose is None:
                    reason = "purpose_not_registered"
                else:
                    try:
                        access = _validated_bound_search_access(candidate)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        reason = "acl_unknown"
                    else:
                        decision = authorize_cognitive_access(
                            access,
                            principal=principal,
                            narrowing=narrowing,
                            purpose=purpose,
                        )
                        reason = decision.reason
                        if decision.allowed:
                            authorized_ids.append(str(candidate["revision_id"]))
                            continue
                denied_by_reason[reason] = denied_by_reason.get(reason, 0) + 1

            rows: list[sqlite3.Row] = []
            for offset in range(0, len(authorized_ids), 500):
                batch = authorized_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(
                    conn.execute(
                        "SELECT * FROM cognitive_state_revisions "
                        f"WHERE revision_id IN ({placeholders}) "  # nosec B608 - placeholders are generated
                        "ORDER BY object_type, object_id",
                        tuple(batch),
                    ).fetchall()
                )
        return (
            tuple(_revision_from_row(row) for row in rows),
            {
                "candidate_count": len(candidates),
                "authorized_count": len(rows),
                "denied_by_reason": denied_by_reason,
            },
        )

    def revision(self, revision_id: str) -> CognitiveStateRevision | None:
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT r.*
                FROM cognitive_state_revisions AS r
                WHERE r.revision_id=?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cognitive_state_outbox AS tombstone
                      WHERE tombstone.command_type=?
                        AND EXISTS (
                            SELECT 1
                            FROM json_each(
                                tombstone.payload_json,
                                '$.target_revision_ids'
                            ) AS target
                            WHERE target.value=r.revision_id
                        )
                  )
                """,
                (str(revision_id or ""), COGNITIVE_TOMBSTONE_COMMAND_TYPE),
            ).fetchone()
        return _revision_from_row(row) if row is not None else None

    def revision_chain(
        self,
        object_type: str,
        object_id: str,
    ) -> tuple[CognitiveStateRevision, ...]:
        """Return one object's immutable revisions in canonical revision order."""

        normalized_type = str(object_type or "").strip()
        normalized_id = str(object_id or "").strip()
        if not normalized_type or not normalized_id:
            return ()
        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM cognitive_state_revisions
                WHERE object_type=? AND object_id=?
                ORDER BY revision_no, revision_id
                """,
                (normalized_type, normalized_id),
            ).fetchall()
        return tuple(_revision_from_row(row) for row in rows)

    def authorized_revision(
        self,
        revision_id: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        purpose: str,
    ) -> tuple[CognitiveStateRevision | None, str]:
        """Authorize one immutable revision before hydrating its payload body."""

        normalized_revision = str(revision_id or "").strip()
        if not normalized_revision:
            return None, "revision_id_required"
        if principal is None:
            return None, "principal_required"
        if not str(purpose or "").strip():
            return None, "purpose_required"
        with self._connect(read_only=True) as conn:
            from core.cognitive.search_state_headers import require_state_search_headers

            require_state_search_headers(conn)
            candidate = conn.execute(
                """
                SELECT search.revision_id,
                       search.object_type AS header_object_type,
                       search.object_id AS header_object_id,
                       search.scope_type, search.scope_id,
                       search.access_control AS access_control_json,
                       search.access_control_hash,
                       search.revision_payload_hash,
                       binding.access_control AS binding_access_control_json,
                       binding.access_control_hash AS binding_access_control_hash,
                       binding.revision_payload_hash AS binding_payload_hash,
                       binding.object_type AS binding_object_type,
                       binding.object_id AS binding_object_id,
                       binding.scope_type AS binding_scope_type,
                       binding.scope_id AS binding_scope_id,
                       revision.payload_hash AS canonical_payload_hash,
                       revision.object_type AS canonical_object_type,
                       revision.object_id AS canonical_object_id,
                       revision.scope_type AS canonical_scope_type,
                       revision.scope_id AS canonical_scope_id
                FROM typed_search_state_headers AS search
                JOIN typed_search_state_revision_bindings AS binding
                  ON binding.revision_id=search.revision_id
                JOIN cognitive_state_revisions AS revision
                  ON revision.revision_id=search.revision_id
                WHERE search.revision_id=?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cognitive_state_outbox AS tombstone
                      WHERE tombstone.command_type=?
                        AND EXISTS (
                            SELECT 1
                            FROM json_each(
                                tombstone.payload_json,
                                '$.target_revision_ids'
                            ) AS target
                            WHERE target.value=search.revision_id
                        )
                  )
                """,
                (normalized_revision, COGNITIVE_TOMBSTONE_COMMAND_TYPE),
            ).fetchone()
            if candidate is None:
                return None, "not_found"
            try:
                access = _validated_bound_search_access(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None, "acl_unknown"
            authorization = authorize_cognitive_access(
                access,
                principal=principal,
                narrowing=(
                    narrowing
                    if narrowing is not None
                    else AccessNarrowing(
                        session_id=str(access["scope"]["session_id"]),
                        project=str(access["scope"]["project"]),
                    )
                ),
                purpose=purpose,
            )
            if not authorization.allowed:
                return None, authorization.reason
            row = conn.execute(
                "SELECT * FROM cognitive_state_revisions WHERE revision_id=?",
                (normalized_revision,),
            ).fetchone()
        return (
            (_revision_from_row(row) if row is not None else None),
            "authorized" if row is not None else "not_found",
        )

    @staticmethod
    def revision_state_hash(
        values: Sequence[tuple[str, str, str]] | tuple[str, str, str],
    ) -> str:
        triples: tuple[tuple[str, str, str], ...]
        if len(values) == 3 and all(isinstance(item, str) for item in values):
            triples = (cast(tuple[str, str, str], values),)
        else:
            triples = tuple(cast(Sequence[tuple[str, str, str]], values))
        payload = [
            {"object_type": item[0], "object_id": item[1], "revision_id": item[2]}
            for item in sorted(triples)
        ]
        return sha256_json(payload)

    def integrity_report(self) -> dict[str, Any]:
        with self._connect(read_only=True) as conn:
            placeholders = ",".join("?" for _ in COGNITIVE_OBJECT_TYPES)
            object_types = tuple(sorted(COGNITIVE_OBJECT_TYPES))
            semantic_without_envelope = int(conn.execute("""
                    SELECT COUNT(*)
                    FROM cognitive_state_revisions AS r
                    LEFT JOIN cognitive_data_events AS e ON e.event_id=r.source_event_id
                    WHERE e.event_id IS NULL
                    """).fetchone()[0])
            envelope_without_revision = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM cognitive_data_events AS e
                    LEFT JOIN cognitive_state_revisions AS r ON r.source_event_id=e.event_id
                    WHERE e.data_type IN ({placeholders}) AND r.revision_id IS NULL
                    """,  # nosec B608 - placeholders are generated, values remain bound
                    object_types,
                ).fetchone()[0]
            )
            outbox_without_source = int(conn.execute("""
                    SELECT COUNT(*)
                    FROM cognitive_state_outbox AS o
                    LEFT JOIN cognitive_state_revisions AS r ON r.revision_id=o.revision_id
                    LEFT JOIN cognitive_data_events AS e ON e.event_id=o.event_id
                    WHERE r.revision_id IS NULL OR e.event_id IS NULL
                    """).fetchone()[0])
            inactive_revision_in_active_head = int(conn.execute("""
                    SELECT COUNT(*)
                    FROM cognitive_state_heads AS h
                    JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
                    WHERE r.admission_state != 'active'
                       OR EXISTS (
                           SELECT 1
                           FROM cognitive_state_migration_quarantine AS quarantine
                           WHERE quarantine.source_table='cognitive_state_revisions'
                             AND quarantine.source_key=r.revision_id
                       )
                    """).fetchone()[0])
            effect_without_command = int(conn.execute("""
                    SELECT COUNT(*)
                    FROM cognitive_state_effect_receipts AS r
                    LEFT JOIN cognitive_state_outbox AS o ON o.command_id=r.command_id
                    WHERE o.command_id IS NULL
                    """).fetchone()[0])
            mutable_action_evidence = int(conn.execute("""
                    SELECT COUNT(*) FROM cognitive_data_consumptions
                    WHERE action_changed=1 AND (
                        target_effect_id='' OR before_hash='' OR after_hash=''
                        OR before_hash=after_hash
                        OR json_array_length(effect_evidence_refs)=0
                    )
                    """).fetchone()[0])
            revision_hash_mismatch = sum(
                1
                for evidence_json, evidence_hash, payload_json, payload_hash in conn.execute("""
                    SELECT evidence_refs, evidence_hash, payload_json, payload_hash
                    FROM cognitive_state_revisions
                    """).fetchall()
                if sha256_json(json.loads(str(evidence_json))) != str(evidence_hash)
                or sha256_json(json.loads(str(payload_json))) != str(payload_hash)
            )
            outbox_hash_mismatch = sum(
                1
                for payload_json, payload_hash in conn.execute(
                    "SELECT payload_json, payload_hash FROM cognitive_state_outbox"
                ).fetchall()
                if sha256_json(json.loads(str(payload_json))) != str(payload_hash)
            )
            effect_reciprocity_gap = int(conn.execute("""
                    SELECT COUNT(*)
                    FROM cognitive_state_effect_receipts AS r
                    JOIN cognitive_state_outbox AS o ON o.command_id=r.command_id
                    JOIN cognitive_data_consumptions AS c
                      ON c.consumption_id=r.consumption_id
                    WHERE r.revision_id != o.revision_id
                       OR r.event_id != o.event_id
                       OR r.consumer_id != o.consumer_id
                       OR c.event_id != r.event_id
                       OR c.consumer_id != r.consumer_id
                       OR c.status != r.status
                       OR c.target_effect_id != r.target_effect_id
                       OR c.before_hash != r.before_hash
                       OR c.after_hash != r.after_hash
                    """).fetchone()[0])
            effect_evidence_gap = sum(
                1
                for receipt_id, receipt_refs_json, consumption_refs_json in conn.execute("""
                    SELECT r.receipt_id, r.evidence_refs, c.effect_evidence_refs
                    FROM cognitive_state_effect_receipts AS r
                    JOIN cognitive_data_consumptions AS c
                      ON c.consumption_id=r.consumption_id
                    """).fetchall()
                if not set(json.loads(str(receipt_refs_json))).issubset(
                    set(json.loads(str(consumption_refs_json)))
                )
                or f"cognitive-effect-receipt:{receipt_id}"
                not in set(json.loads(str(consumption_refs_json)))
            )
            cognitive_snapshot = cognitive_data_snapshot_in_connection(conn)
            consumed_with_missing = int(
                cognitive_snapshot["counts"]["aggregate_consumed_with_missing_consumer"]
            )
            consumed_without_event = int(
                cognitive_snapshot["counts"]["consumed_without_data_event"]
            )
        rebuilt = self.rebuild_current_state()
        multiple_current = len(rebuilt["ambiguous"])
        partial_facade = (
            semantic_without_envelope
            + envelope_without_revision
            + outbox_without_source
            + effect_without_command
            + effect_reciprocity_gap
            + effect_evidence_gap
            + revision_hash_mismatch
            + outbox_hash_mismatch
            + inactive_revision_in_active_head
        )
        return {
            "canonical_state_owner_count": 1,
            "metadata_only_cognition": envelope_without_revision,
            "consumed_without_event": consumed_without_event,
            "aggregate_consumed_with_missing_consumer": consumed_with_missing,
            "multiple_current_revision": multiple_current,
            "mutable_action_evidence": mutable_action_evidence,
            "semantic_revision_without_envelope": semantic_without_envelope,
            "envelope_without_semantic_revision": envelope_without_revision,
            "partial_facade_commit": partial_facade,
            "outbox_without_source_commit": outbox_without_source,
            "inactive_revision_in_active_head": inactive_revision_in_active_head,
            "effect_receipt_without_command": effect_without_command,
            "effect_receipt_reciprocity_gap": effect_reciprocity_gap,
            "effect_receipt_evidence_gap": effect_evidence_gap,
            "revision_hash_mismatch": revision_hash_mismatch,
            "outbox_hash_mismatch": outbox_hash_mismatch,
            "current_state_hash_mismatch": int(not rebuilt["projection_hash_matches"]),
            "current_state_hash": rebuilt["state_hash"],
            "projection_state_hash": rebuilt["projection_hash"],
        }


def _is_tombstone_control_commit(
    revisions: Sequence[CognitiveStateRevision],
    commands: Sequence[LocalConsumerCommand],
) -> bool:
    """Allow only the narrow deletion-control transaction through an all-freeze.

    The control record contains no user cognitive body and is required to make
    an ``all`` delete executable after its mandatory freeze. Ordinary state
    writers cannot opt into this bypass: every revision and command must match
    the canonical typed tombstone contract.
    """

    if not revisions or not commands:
        return False
    revision_ids = {revision.revision_id for revision in revisions}
    if any(
        command.command_type != COGNITIVE_TOMBSTONE_COMMAND_TYPE
        or command.revision_id not in revision_ids
        for command in commands
    ):
        return False
    for revision in revisions:
        if (
            revision.object_type != "cognitive_update_receipt"
            or revision.scope_type != "deletion_request"
            or not revision.source_revision_id.startswith("data-delete:")
            or not str(revision.payload.get("target_command_ref") or "").startswith("tombstone:")
        ):
            return False
        try:
            access_control = validate_cognitive_access_envelope(
                revision.payload["access_control"],
                expected_scope_type=revision.scope_type,
                expected_scope_id=revision.scope_id,
            )
        except (KeyError, TypeError, ValueError):
            return False
        if (
            access_control["owner"]["principal_id"] != "system:data-ownership"
            or access_control["owner"]["agent"] != "system"
            or access_control["scope"]["resolution"] != "restricted_unknown"
            or access_control["visibility"] != "restricted"
        ):
            return False
    return True


class CognitiveStateUnitOfWork:
    """One-connection transaction coordinator for semantic state commits."""

    def __init__(self, store: CognitiveStateStore):
        self.store = store

    def commit(
        self,
        *,
        revisions: Sequence[CognitiveStateRevision],
        event: CognitiveDataEvent,
        commands: Sequence[LocalConsumerCommand],
        expected_heads: Sequence[CognitiveHeadPrecondition] = (),
        superseded_feedback_command_ids: Sequence[str] = (),
        failpoint: Callable[[str], None] | None = None,
    ) -> CognitiveStateCommitReceipt:
        normalized_revisions = tuple(revisions)
        normalized_commands = tuple(commands)
        normalized_heads = tuple(expected_heads)
        normalized_superseded_commands = tuple(superseded_feedback_command_ids)
        if not normalized_revisions:
            raise ValueError("at least one cognitive state revision is required")
        revision_ids = {revision.revision_id for revision in normalized_revisions}
        if len(revision_ids) != len(normalized_revisions):
            raise ValueError("duplicate cognitive state revision id")
        for revision in normalized_revisions:
            if revision.admission_state != "active":
                raise ValueError("runtime unit of work accepts active revisions only")
            if revision.source_event_id != event.event_id:
                raise ValueError("revision source_event_id must match the envelope")
            if revision.source_content_hash != event.content_hash:
                raise ValueError("revision source_content_hash must match the envelope")
        if event.data_type not in COGNITIVE_OBJECT_TYPES:
            raise ValueError("semantic envelope data_type must be a cognitive object type")
        if event.data_type == "decision_trace":
            decision_revisions = tuple(
                revision
                for revision in normalized_revisions
                if revision.object_type == "decision_trace"
            )
            if len(decision_revisions) != 1:
                raise ValueError("decision transaction requires exactly one DecisionTrace revision")
            decision_state = str(decision_revisions[0].payload.get("decision_state") or "")
            if decision_state == "approved" and not normalized_commands:
                raise ValueError("approved decision requires a material-action command")
            if decision_state == "rejected" and normalized_commands:
                raise ValueError("rejected decision cannot emit a material-action command")
            if decision_state not in {"approved", "rejected"}:
                raise ValueError("unsupported DecisionTrace decision_state")
        for command in normalized_commands:
            if command.revision_id not in revision_ids:
                raise ValueError("outbox command references a revision outside the unit of work")
        command_consumers = {command.consumer_id for command in normalized_commands}
        if command_consumers != set(event.intended_consumers):
            raise ValueError("outbox consumers must exactly match intended consumers")
        head_keys: set[tuple[str, str]] = set()
        for head in normalized_heads:
            if not isinstance(head, CognitiveHeadPrecondition):
                raise ValueError("expected_heads must contain typed head preconditions")
            key = (head.object_type, head.object_id)
            if key in head_keys:
                raise ValueError("expected head preconditions must be unique by object")
            head_keys.add(key)
        self.store._assert_writes_not_frozen(normalized_revisions, normalized_commands)
        transaction_hash = sha256_json(
            {
                "event_id": event.event_id,
                "revision_ids": sorted(revision_ids),
                "command_ids": sorted(command.command_id for command in normalized_commands),
                "superseded_feedback_command_ids": sorted(
                    str(value) for value in normalized_superseded_commands
                ),
                "expected_heads": [
                    {
                        "object_type": head.object_type,
                        "object_id": head.object_id,
                        "revision_id": head.revision_id,
                    }
                    for head in sorted(
                        normalized_heads,
                        key=lambda item: (item.object_type, item.object_id),
                    )
                ],
            }
        )
        conn = self.store._connect()
        inserted_any = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            if event.data_type == "decision_trace":
                conflicting_events = conn.execute(
                    """
                    SELECT event_id FROM cognitive_data_events
                    WHERE dedupe_key=? AND event_id!=?
                    ORDER BY event_id
                    """,
                    (event.dedupe_key, event.event_id),
                ).fetchall()
                if conflicting_events:
                    raise CognitiveStateConflict(
                        "decision idempotency key is already bound to different semantics"
                    )
            self._assert_expected_heads(conn, normalized_heads)
            _call_failpoint(failpoint, "after_head_preconditions")
            for revision in normalized_revisions:
                inserted_any = self._insert_revision(conn, revision) or inserted_any
                _call_failpoint(
                    failpoint,
                    f"after_{revision.object_type}_revision",
                )
            _call_failpoint(failpoint, "after_revision")
            if normalized_superseded_commands:
                from core.cognitive.feedback_command_closure import (
                    close_superseded_feedback_commands,
                )

                close_superseded_feedback_commands(
                    conn,
                    command_ids=normalized_superseded_commands,
                    revisions=normalized_revisions,
                    created_at=event.created_at,
                )
                inserted_any = True
                _call_failpoint(failpoint, "after_feedback_command_supersession")
            _, inserted_event = insert_data_event_in_connection(
                conn,
                event,
                lifecycle_status="produced",
                allow_semantic=True,
            )
            inserted_any = inserted_event or inserted_any
            _call_failpoint(failpoint, "after_event")
            outbox_ids: list[str] = []
            for command in normalized_commands:
                inserted_any = self._insert_outbox(conn, event.event_id, command) or inserted_any
                outbox_ids.append(command.command_id)
                _call_failpoint(failpoint, "after_outbox")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return CognitiveStateCommitReceipt(
            status="committed" if inserted_any else "existing",
            event_id=event.event_id,
            revision_ids=tuple(revision.revision_id for revision in normalized_revisions),
            outbox_ids=tuple(outbox_ids),
            transaction_hash=transaction_hash,
        )

    @staticmethod
    def _assert_expected_heads(
        conn: sqlite3.Connection,
        expected_heads: Sequence[CognitiveHeadPrecondition],
    ) -> None:
        for head in expected_heads:
            current = conn.execute(
                """
                SELECT h.revision_id
                FROM cognitive_state_heads AS h
                JOIN cognitive_state_revisions AS r
                  ON r.revision_id=h.revision_id
                WHERE h.object_type=? AND h.object_id=?
                  AND r.admission_state='active'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cognitive_state_outbox AS tombstone
                      WHERE tombstone.command_type=?
                        AND EXISTS (
                            SELECT 1
                            FROM json_each(
                                tombstone.payload_json,
                                '$.target_revision_ids'
                            ) AS target
                            WHERE target.value=h.revision_id
                        )
                  )
                """,
                (
                    head.object_type,
                    head.object_id,
                    COGNITIVE_TOMBSTONE_COMMAND_TYPE,
                ),
            ).fetchone()
            if current is None or str(current["revision_id"]) != head.revision_id:
                raise CognitiveStateConflict(
                    "cognitive state head precondition failed; rebuild the snapshot"
                )

    @staticmethod
    def _insert_revision(
        conn: sqlite3.Connection,
        revision: CognitiveStateRevision,
    ) -> bool:
        existing = conn.execute(
            "SELECT * FROM cognitive_state_revisions WHERE revision_id=?",
            (revision.revision_id,),
        ).fetchone()
        if existing is not None:
            if _revision_identity_from_row(existing) != _revision_identity(revision):
                raise CognitiveStateConflict("immutable cognitive revision conflict")
            projection = conn.execute(
                """
                SELECT h.access_control_hash AS header_access_hash,
                       h.revision_payload_hash AS header_payload_hash,
                       h.object_type AS header_object_type,
                       h.object_id AS header_object_id,
                       h.scope_type AS header_scope_type,
                       h.scope_id AS header_scope_id,
                       b.access_control AS binding_access_control,
                       b.access_control_hash AS binding_access_hash,
                       b.revision_payload_hash AS binding_payload_hash,
                       b.object_type AS binding_object_type,
                       b.object_id AS binding_object_id,
                       b.scope_type AS binding_scope_type,
                       b.scope_id AS binding_scope_id
                FROM typed_search_state_headers AS h
                JOIN typed_search_state_revision_bindings AS b
                  ON b.revision_id=h.revision_id
                WHERE h.revision_id=?
                """,
                (revision.revision_id,),
            ).fetchone()
            if projection is None:
                raise CognitiveStateConflict(
                    "immutable cognitive revision lacks bound search ACL projection"
                )
            from core.cognitive.access_control import cognitive_access_hash

            binding_access = validate_cognitive_access_envelope(
                json.loads(str(projection["binding_access_control"] or "")),
                expected_scope_type=revision.scope_type,
                expected_scope_id=revision.scope_id,
            )
            expected_access = validate_cognitive_access_envelope(
                revision.payload["access_control"],
                expected_scope_type=revision.scope_type,
                expected_scope_id=revision.scope_id,
            )
            expected_access_hash = cognitive_access_hash(revision.payload["access_control"])
            if not (
                binding_access == expected_access
                and cognitive_access_hash(binding_access) == expected_access_hash
                and str(projection["header_access_hash"])
                == str(projection["binding_access_hash"])
                == expected_access_hash
            ):
                raise CognitiveStateConflict("immutable cognitive search ACL header conflict")
            if not (
                str(projection["header_payload_hash"])
                == str(projection["binding_payload_hash"])
                == revision.payload_hash
            ):
                raise CognitiveStateConflict(
                    "immutable cognitive search revision payload binding conflict"
                )
            expected_identity = (
                revision.object_type,
                revision.object_id,
                revision.scope_type,
                revision.scope_id,
            )
            header_identity = tuple(
                str(projection[name])
                for name in (
                    "header_object_type",
                    "header_object_id",
                    "header_scope_type",
                    "header_scope_id",
                )
            )
            binding_identity = tuple(
                str(projection[name])
                for name in (
                    "binding_object_type",
                    "binding_object_id",
                    "binding_scope_type",
                    "binding_scope_id",
                )
            )
            if header_identity != expected_identity or binding_identity != expected_identity:
                raise CognitiveStateConflict(
                    "immutable cognitive search revision identity binding conflict"
                )
            return False
        current = conn.execute(
            """
            SELECT h.revision_id, r.revision_no
            FROM cognitive_state_heads AS h
            JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
            WHERE h.object_type=? AND h.object_id=?
            """,
            (revision.object_type, revision.object_id),
        ).fetchone()
        if current is None:
            if revision.supersedes_revision_id:
                raise CognitiveStateConflict("first revision cannot supersede a missing head")
            revision_no = 1
        else:
            current_id = str(current["revision_id"])
            if revision.supersedes_revision_id != current_id:
                raise CognitiveStateConflict("revision does not supersede the current head")
            revision_no = int(current["revision_no"]) + 1
        if revision.correction_of_revision_id:
            correction = conn.execute(
                """
                SELECT object_type, object_id FROM cognitive_state_revisions
                WHERE revision_id=?
                """,
                (revision.correction_of_revision_id,),
            ).fetchone()
            if correction is None or tuple(correction) != (
                revision.object_type,
                revision.object_id,
            ):
                raise CognitiveStateConflict("correction target is not in the same object chain")
        conn.execute(
            """
            INSERT INTO cognitive_state_revisions (
                revision_id, object_type, object_id, schema_version, revision_no,
                source_event_id, source_revision_id, source_content_hash,
                scope_type, scope_id, evidence_refs, evidence_hash,
                payload_json, payload_hash, supersedes_revision_id,
                correction_of_revision_id, admission_state, redaction_policy,
                redaction_counts,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''),
                      NULLIF(?, ''), ?, ?, ?, ?)
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
                revision.admission_state,
                revision.redaction_policy,
                canonical_json(dict(revision.redaction_counts)),
                revision.created_at,
            ),
        )
        from core.cognitive.search_state_headers import insert_state_search_header

        insert_state_search_header(
            conn,
            revision_id=revision.revision_id,
            object_type=revision.object_type,
            object_id=revision.object_id,
            scope_type=revision.scope_type,
            scope_id=revision.scope_id,
            payload=revision.payload,
            revision_payload_hash=revision.payload_hash,
            created_at=revision.created_at,
        )
        conn.execute(
            """
            INSERT INTO cognitive_state_heads(object_type, object_id, revision_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(object_type, object_id) DO UPDATE SET
                revision_id=excluded.revision_id,
                updated_at=excluded.updated_at
            """,
            (
                revision.object_type,
                revision.object_id,
                revision.revision_id,
                revision.created_at,
            ),
        )
        return True

    @staticmethod
    def _insert_outbox(
        conn: sqlite3.Connection,
        event_id: str,
        command: LocalConsumerCommand,
    ) -> bool:
        existing = conn.execute(
            """
            SELECT revision_id, event_id, consumer_id, command_type,
                   payload_json, payload_hash
            FROM cognitive_state_outbox WHERE command_id=?
            """,
            (command.command_id,),
        ).fetchone()
        expected = (
            command.revision_id,
            event_id,
            command.consumer_id,
            command.command_type,
            canonical_json(command.payload),
            command.payload_hash,
        )
        if existing is not None:
            if tuple(existing) != expected:
                raise CognitiveStateConflict("immutable cognitive outbox conflict")
            return False
        conn.execute(
            """
            INSERT INTO cognitive_state_outbox (
                command_id, revision_id, event_id, consumer_id, command_type,
                payload_json, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (command.command_id, *expected, command.created_at),
        )
        return True


def _call_failpoint(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


def _revision_identity(revision: CognitiveStateRevision) -> tuple[Any, ...]:
    # ``created_at`` is observation metadata, not part of ``revision_id``.
    # Exact concurrent replays preserve the first timestamp just like event
    # envelopes and outbox commands, whose immutable comparisons also omit it.
    return (
        revision.object_type,
        revision.object_id,
        revision.schema_version,
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
        revision.admission_state,
        revision.redaction_policy,
        canonical_json(dict(revision.redaction_counts)),
    )


def _revision_identity_from_row(row: sqlite3.Row) -> tuple[Any, ...]:
    return (
        str(row["object_type"]),
        str(row["object_id"]),
        str(row["schema_version"]),
        str(row["source_event_id"]),
        str(row["source_revision_id"]),
        str(row["source_content_hash"]),
        str(row["scope_type"]),
        str(row["scope_id"]),
        str(row["evidence_refs"]),
        str(row["evidence_hash"]),
        str(row["payload_json"]),
        str(row["payload_hash"]),
        str(row["supersedes_revision_id"] or ""),
        str(row["correction_of_revision_id"] or ""),
        str(row["admission_state"]),
        str(row["redaction_policy"]),
        str(row["redaction_counts"]),
    )


def _revision_from_row(row: sqlite3.Row) -> CognitiveStateRevision:
    payload = json.loads(str(row["payload_json"]))
    redaction_counts = json.loads(str(row["redaction_counts"]))
    return CognitiveStateRevision(
        revision_id=str(row["revision_id"]),
        object_type=str(row["object_type"]),
        object_id=str(row["object_id"]),
        schema_version=str(row["schema_version"]),
        source_event_id=str(row["source_event_id"]),
        source_revision_id=str(row["source_revision_id"]),
        source_content_hash=str(row["source_content_hash"]),
        scope_type=str(row["scope_type"]),
        scope_id=str(row["scope_id"]),
        evidence_refs=tuple(str(value) for value in json.loads(str(row["evidence_refs"]))),
        payload=MappingProxyType(payload),
        payload_hash=str(row["payload_hash"]),
        evidence_hash=str(row["evidence_hash"]),
        supersedes_revision_id=str(row["supersedes_revision_id"] or ""),
        correction_of_revision_id=str(row["correction_of_revision_id"] or ""),
        created_at=str(row["created_at"]),
        admission_state=str(row["admission_state"]),
        redaction_policy=str(row["redaction_policy"]),
        redaction_counts=tuple(
            sorted((str(key), int(value)) for key, value in redaction_counts.items())
        ),
    )
