"""Tombstone planning and canonical-head rebuild lifecycle."""

from __future__ import annotations

from collections import defaultdict
import json
import sqlite3
from typing import Any, Sequence, TYPE_CHECKING

from core.cognitive.access_control import cognitive_access_hash, make_cognitive_access_envelope
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    now_utc,
    sha256_json,
)
from core.cognitive.state_types import (
    COGNITIVE_TOMBSTONE_COMMAND_TYPE,
    COGNITIVE_TOMBSTONE_SCHEMA_VERSION,
    CognitiveStateConflict,
    CognitiveTombstonePlan,
)
from core.ops.cognitive_data_contract import CognitiveDataEvent


class CognitiveStateLifecycleMixin:
    """Append-only lifecycle operations for cognitive revisions."""

    if TYPE_CHECKING:
        def _connect(self, *, read_only: bool = False) -> sqlite3.Connection: ...

        def unit_of_work(self) -> Any: ...

        def revision_state_hash(
            self,
            values: Sequence[tuple[str, str, str]] | tuple[str, str, str],
        ) -> str: ...

    def plan_subject_tombstone(
        self,
        *,
        request_id: str,
        scope_kind: str,
        scope_value: str,
        snapshot_ref: str,
    ) -> CognitiveTombstonePlan:
        """Write an immutable deletion plan through the canonical state outbox.

        The plan is deliberately a ``cognitive_update_receipt`` revision rather
        than a second deletion ledger.  As soon as its typed consumer commands
        commit, all target current revisions are excluded from every state read
        seam; consumers must still return independently evidenced effect
        receipts before the plan can become verified.
        """

        normalized_request = str(request_id or "").strip()
        normalized_kind = str(scope_kind or "").strip()
        normalized_value = str(scope_value or "").strip()
        normalized_snapshot = str(snapshot_ref or "").strip()
        if not normalized_request:
            raise ValueError("tombstone request_id is required")
        if not normalized_kind or not normalized_value:
            raise ValueError("tombstone scope is required")
        if not normalized_snapshot:
            raise ValueError("tombstone snapshot_ref is required")
        subject_hash = sha256_json(
            {"scope_kind": normalized_kind, "scope_value": normalized_value}
        )
        snapshot_hash = sha256_json(normalized_snapshot)
        existing = self._existing_tombstone_plan(
            request_id=normalized_request,
            subject_hash=subject_hash,
            snapshot_hash=snapshot_hash,
        )
        if existing is not None:
            return existing
        query, parameters = self._tombstone_target_query(
            normalized_kind,
            normalized_value,
        )
        if query is None:
            return CognitiveTombstonePlan(
                status="unsupported_scope",
                request_id=normalized_request,
                subject_hash=subject_hash,
                target_revision_ids=(),
                control_revision_id="",
                command_ids=(),
                required_consumers=(),
                before_hash="",
                tombstone_hash="",
            )
        with self._connect(read_only=True) as conn:
            target_rows = conn.execute(query, parameters).fetchall()
            target_ids = tuple(sorted(str(row["revision_id"]) for row in target_rows))
            if not target_ids:
                return CognitiveTombstonePlan(
                    status="no_targets",
                    request_id=normalized_request,
                    subject_hash=subject_hash,
                    target_revision_ids=(),
                    control_revision_id="",
                    command_ids=(),
                    required_consumers=(),
                    before_hash="",
                    tombstone_hash="",
                )
            placeholders = ",".join("?" for _ in target_ids)
            consumer_rows = conn.execute(
                f"""
                SELECT DISTINCT consumer_id
                FROM cognitive_state_outbox
                WHERE revision_id IN ({placeholders})
                ORDER BY consumer_id
                """,  # nosec B608 - placeholders derive solely from target_ids
                target_ids,
            ).fetchall()
        consumers = tuple(str(row["consumer_id"]) for row in consumer_rows)
        if not consumers:
            return CognitiveTombstonePlan(
                status="consumer_contract_missing",
                request_id=normalized_request,
                subject_hash=subject_hash,
                target_revision_ids=target_ids,
                control_revision_id="",
                command_ids=(),
                required_consumers=(),
                before_hash="",
                tombstone_hash="",
            )

        before_hash = sha256_json(
            [
                {
                    "revision_id": str(row["revision_id"]),
                    "payload_hash": str(row["payload_hash"]),
                }
                for row in sorted(target_rows, key=lambda row: str(row["revision_id"]))
            ]
        )
        tombstone_hash = sha256_json(
            {
                "schema_version": COGNITIVE_TOMBSTONE_SCHEMA_VERSION,
                "request_id": normalized_request,
                "subject_hash": subject_hash,
                "snapshot_hash": snapshot_hash,
                "target_revision_ids": list(target_ids),
                "before_hash": before_hash,
                "required_consumers": list(consumers),
            }
        )
        control_scope_id = "delete:" + tombstone_hash.removeprefix("sha256:")[:32]
        access_control = make_cognitive_access_envelope(
            owner_principal_id="system:data-ownership",
            owner_agent="system",
            scope_type="deletion_request",
            scope_id=control_scope_id,
            purposes=("cognitive_state_delete",),
            consent_provenance_refs=(),
            sensitivity="restricted",
            retention_policy="deletion_audit",
            source_acl_lineage=(subject_hash,),
            visibility="restricted",
            scope_resolution="restricted_unknown",
            consent_status="restricted_unknown",
        )
        event_id = "cde-tombstone-" + tombstone_hash.removeprefix("sha256:")[:24]
        created_at = now_utc()
        evidence_refs = (
            "data-delete-request:" + normalized_request,
            "snapshot-ref:" + snapshot_hash,
        )
        revision = CognitiveStateRevision.create(
            object_type="cognitive_update_receipt",
            object_id="tombstone:" + tombstone_hash.removeprefix("sha256:")[:32],
            source_event_id=event_id,
            source_revision_id="data-delete:" + normalized_request,
            source_content_hash=tombstone_hash,
            scope_type="deletion_request",
            scope_id=control_scope_id,
            evidence_refs=evidence_refs,
            payload={
                "input_refs": list(target_ids),
                "attribution": {
                    "action": "subject_tombstone",
                    "subject_hash": subject_hash,
                    "target_count": len(target_ids),
                },
                "target_command_ref": "tombstone:" + normalized_request,
                "before_hash": before_hash,
                "after_hash": tombstone_hash,
                "effect_receipt_ref": "pending",
                "access_control": access_control,
            },
            created_at=created_at,
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id="data-ownership:" + subject_hash.removeprefix("sha256:")[:32],
            asset_id=subject_hash,
            source_kind="data_ownership_delete",
            source_uri="mnemos://data-ownership/tombstone/"
            + subject_hash.removeprefix("sha256:")[:32],
            content_hash=tombstone_hash,
            canonical_subject="cognitive_tombstone:" + subject_hash.removeprefix("sha256:")[:32],
            data_type="cognitive_update_receipt",
            producer="cognitive_state_store",
            intended_consumers=consumers,
            privacy_level="restricted",
            confidence=1.0,
            evidence_refs=evidence_refs,
            dedupe_key="cognitive-tombstone:" + tombstone_hash.removeprefix("sha256:")[:32],
            created_at=created_at,
            retention_policy="deletion_audit",
            metadata={
                "revision_ids": [revision.revision_id],
                "contract_version": COGNITIVE_TOMBSTONE_SCHEMA_VERSION,
                "access_control_hash": cognitive_access_hash(access_control),
            },
        )
        commands = tuple(
            LocalConsumerCommand.create(
                revision_id=revision.revision_id,
                consumer_id=consumer,
                command_type=COGNITIVE_TOMBSTONE_COMMAND_TYPE,
                payload={
                    "schema_version": COGNITIVE_TOMBSTONE_SCHEMA_VERSION,
                    "request_id": normalized_request,
                    "subject_hash": subject_hash,
                    "snapshot_hash": snapshot_hash,
                    "target_revision_ids": list(target_ids),
                    "before_hash": before_hash,
                    "tombstone_hash": tombstone_hash,
                    "required_consumers": list(consumers),
                    "access_control_hash": cognitive_access_hash(access_control),
                },
                created_at=created_at,
            )
            for consumer in consumers
        )
        committed = self.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=commands,
        )
        return CognitiveTombstonePlan(
            status=committed.status,
            request_id=normalized_request,
            subject_hash=subject_hash,
            target_revision_ids=target_ids,
            control_revision_id=revision.revision_id,
            command_ids=committed.outbox_ids,
            required_consumers=consumers,
            before_hash=before_hash,
            tombstone_hash=tombstone_hash,
        )

    def _existing_tombstone_plan(
        self,
        *,
        request_id: str,
        subject_hash: str,
        snapshot_hash: str,
    ) -> CognitiveTombstonePlan | None:
        """Load one exact immutable plan or fail closed on a conflicting replay."""

        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT revision_id, command_id, consumer_id, payload_json
                FROM cognitive_state_outbox
                WHERE command_type=?
                  AND json_extract(payload_json, '$.request_id')=?
                ORDER BY consumer_id, command_id
                """,
                (COGNITIVE_TOMBSTONE_COMMAND_TYPE, request_id),
            ).fetchall()
        if not rows:
            return None
        try:
            payloads = [json.loads(str(row["payload_json"])) for row in rows]
            first = payloads[0]
            target_ids = tuple(sorted(str(value) for value in first["target_revision_ids"]))
            required_consumers = tuple(sorted(str(value) for value in first["required_consumers"]))
            command_consumers = tuple(sorted(str(row["consumer_id"]) for row in rows))
            control_ids = {str(row["revision_id"]) for row in rows}
            immutable_payloads = {
                canonical_json(
                    {
                        "subject_hash": payload["subject_hash"],
                        "snapshot_hash": payload["snapshot_hash"],
                        "target_revision_ids": sorted(str(value) for value in payload["target_revision_ids"]),
                        "before_hash": payload["before_hash"],
                        "tombstone_hash": payload["tombstone_hash"],
                        "required_consumers": sorted(str(value) for value in payload["required_consumers"]),
                    }
                )
                for payload in payloads
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CognitiveStateConflict("existing tombstone plan is malformed") from exc
        if (
            len(control_ids) != 1
            or len(immutable_payloads) != 1
            or command_consumers != required_consumers
        ):
            raise CognitiveStateConflict("existing tombstone plan has an invalid consumer contract")
        if str(first["subject_hash"]) != subject_hash:
            raise CognitiveStateConflict("tombstone request_id is already bound to a different subject")
        if str(first["snapshot_hash"]) != snapshot_hash:
            raise CognitiveStateConflict("tombstone request_id is already bound to a different snapshot")
        return CognitiveTombstonePlan(
            status="existing",
            request_id=request_id,
            subject_hash=subject_hash,
            target_revision_ids=target_ids,
            control_revision_id=next(iter(control_ids)),
            command_ids=tuple(str(row["command_id"]) for row in rows),
            required_consumers=required_consumers,
            before_hash=str(first["before_hash"]),
            tombstone_hash=str(first["tombstone_hash"]),
        )

    @staticmethod
    def _tombstone_target_query(
        scope_kind: str,
        scope_value: str,
    ) -> tuple[str | None, tuple[Any, ...]]:
        """Return a compact, non-body target query for one ownership scope."""
        predicates = {
            "all": ("1=1", ()),
            "session": (
                "json_extract(r.payload_json, '$.access_control.scope.session_id')=?",
                (scope_value,),
            ),
            "project": (
                "lower(json_extract(r.payload_json, '$.access_control.scope.project'))=?",
                (scope_value.lower(),),
            ),
            "agent": (
                "lower(json_extract(r.payload_json, '$.access_control.owner.agent'))=?",
                (scope_value.lower(),),
            ),
            "raw_event_id": (
                "(r.source_event_id=? OR r.source_revision_id=?)",
                (scope_value, scope_value),
            ),
        }
        predicate = predicates.get(scope_kind)
        if predicate is None:
            return None, ()
        condition, parameters = predicate
        query = f"""
            SELECT r.revision_id, r.payload_hash
            FROM cognitive_state_heads AS h
            JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
            WHERE r.admission_state='active'
              AND r.scope_type!='deletion_request'
              AND {condition}
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
            ORDER BY r.revision_id
        """  # nosec B608 - condition comes from the fixed predicate catalog
        return query, (*parameters, COGNITIVE_TOMBSTONE_COMMAND_TYPE)

    def tombstone_status(self, request_id: str) -> dict[str, Any]:
        """Return an independently auditable terminal-state summary for a plan."""
        normalized_request = str(request_id or "").strip()
        if not normalized_request:
            raise ValueError("tombstone request_id is required")
        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT o.command_id, o.consumer_id, o.payload_json,
                       r.status, r.target_effect_id, r.before_hash,
                       r.after_hash, r.evidence_refs
                FROM cognitive_state_outbox AS o
                LEFT JOIN cognitive_state_effect_receipts AS r
                  ON r.command_id=o.command_id
                WHERE o.command_type=?
                  AND json_extract(o.payload_json, '$.request_id')=?
                ORDER BY o.consumer_id, o.command_id
                """,
                (COGNITIVE_TOMBSTONE_COMMAND_TYPE, normalized_request),
            ).fetchall()
        if not rows:
            return {
                "status": "not_found",
                "request_id": normalized_request,
                "required_count": 0,
                "terminal_count": 0,
                "verified": False,
            }
        terminal_count = 0
        verified_count = 0
        consumers: list[str] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
                expected_target = (
                    f"tombstone:{row['consumer_id']}:{normalized_request}"
                )
                expected_command_ref = f"tombstone-command:{row['command_id']}"
                required = tuple(sorted(str(value) for value in payload["required_consumers"]))
                target_ids = tuple(sorted(str(value) for value in payload["target_revision_ids"]))
                payload_is_valid = (
                    str(payload["request_id"]) == normalized_request
                    and bool(target_ids)
                    and required == tuple(sorted(str(value["consumer_id"]) for value in rows))
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                payload = {}
                expected_target = ""
                expected_command_ref = ""
                payload_is_valid = False
            status = str(row["status"] or "")
            try:
                evidence_refs = tuple(json.loads(str(row["evidence_refs"] or "[]")))
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence_refs = ()
            consumers.append(str(row["consumer_id"]))
            if status == "committed":
                terminal_count += 1
            if (
                payload_is_valid
                and
                status == "committed"
                and str(row["target_effect_id"] or "") == expected_target
                and str(row["before_hash"] or "") == str(payload.get("before_hash") or "")
                and str(row["after_hash"] or "") == str(payload.get("tombstone_hash") or "")
                and expected_command_ref in evidence_refs
                and any(str(ref).startswith("tombstone-oracle:") for ref in evidence_refs)
            ):
                verified_count += 1
        verified = verified_count == len(rows)
        return {
            "status": "verified" if verified else "pending",
            "request_id": normalized_request,
            "required_count": len(rows),
            "terminal_count": terminal_count,
            "verified_count": verified_count,
            "required_consumers": consumers,
            "verified": verified,
        }

    def rebuild_current_state(self) -> dict[str, Any]:
        """Rebuild current heads from immutable revisions without trusting projection rows."""

        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT object_type, object_id, revision_id, revision_no,
                       COALESCE(supersedes_revision_id, '') AS supersedes_revision_id
                FROM cognitive_state_revisions AS r
                WHERE admission_state='active'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cognitive_state_migration_quarantine AS quarantine
                      WHERE quarantine.source_table='cognitive_state_revisions'
                        AND quarantine.source_key=r.revision_id
                  )
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
                ORDER BY object_type, object_id, revision_no, revision_id
                """,
                (COGNITIVE_TOMBSTONE_COMMAND_TYPE,),
            ).fetchall()
            projection_rows = conn.execute(
                """
                SELECT h.object_type, h.object_id, h.revision_id
                FROM cognitive_state_heads AS h
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
                          WHERE target.value=h.revision_id
                      )
                )
                ORDER BY object_type, object_id
                """,
                (COGNITIVE_TOMBSTONE_COMMAND_TYPE,),
            ).fetchall()
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[(str(row["object_type"]), str(row["object_id"]))].append(row)
        heads: list[dict[str, str]] = []
        ambiguous: list[dict[str, Any]] = []
        for (object_type, object_id), revisions in sorted(grouped.items()):
            superseded = {
                str(row["supersedes_revision_id"])
                for row in revisions
                if str(row["supersedes_revision_id"])
            }
            leaves = [row for row in revisions if str(row["revision_id"]) not in superseded]
            if len(leaves) != 1:
                ambiguous.append(
                    {
                        "object_type": object_type,
                        "object_id": object_id,
                        "leaf_revision_ids": sorted(str(row["revision_id"]) for row in leaves),
                    }
                )
                continue
            heads.append(
                {
                    "object_type": object_type,
                    "object_id": object_id,
                    "revision_id": str(leaves[0]["revision_id"]),
                }
            )
        projection = [
            {
                "object_type": str(row["object_type"]),
                "object_id": str(row["object_id"]),
                "revision_id": str(row["revision_id"]),
            }
            for row in projection_rows
        ]
        state_hash = self.revision_state_hash(
            tuple((item["object_type"], item["object_id"], item["revision_id"]) for item in heads)
        )
        projection_hash = self.revision_state_hash(
            tuple(
                (item["object_type"], item["object_id"], item["revision_id"])
                for item in projection
            )
        )
        return {
            "heads": heads,
            "ambiguous": ambiguous,
            "state_hash": state_hash,
            "projection_hash": projection_hash,
            "projection_hash_matches": not ambiguous and state_hash == projection_hash,
        }
