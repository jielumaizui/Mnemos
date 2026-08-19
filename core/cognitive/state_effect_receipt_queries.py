"""Read-only paging and receipt revalidation for cognitive-state effects."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, cast, Mapping, Sequence, TYPE_CHECKING

from core.cognitive.feedback_owner_identity import is_canonical_feedback_owner
from core.cognitive.state_contract import CognitiveStateRevision, canonical_json


class StateEffectReceiptQueryMixin:
    """Expose command/receipt reads and independent terminal revalidation."""

    if TYPE_CHECKING:
        db_path: Path

        def _connect(self, *, read_only: bool = False) -> sqlite3.Connection: ...

        def revision(self, revision_id: str) -> CognitiveStateRevision | None: ...

        def current_revision(
            self,
            object_type: str,
            object_id: str,
        ) -> CognitiveStateRevision | None: ...

        def _validate_feedback_receipt_derivation(
            self,
            command: Mapping[str, Any],
            *,
            status: str,
            target_effect_id: str,
            before_hash: str,
            after_hash: str,
            evidence_refs: tuple[str, ...],
            outcome: str,
            terminal_reason_code: str,
        ) -> None: ...

        def _validate_training_admission_intake_receipt_derivation(
            self,
            command: Mapping[str, Any],
            *,
            status: str,
            target_effect_id: str,
            before_hash: str,
            after_hash: str,
            evidence_refs: tuple[str, ...],
            outcome: str,
            terminal_reason_code: str,
        ) -> None: ...

    def _bind_feedback_owner_capability(
        self,
        owner: Any,
    ) -> None:
        """Create, bind, and never return one canonical owner identity."""

        if not is_canonical_feedback_owner(owner) or getattr(owner, "state", None) is not self:
            raise PermissionError("feedback capability requires canonical owner type")
        identities = getattr(
            self,
            "_CognitiveStateEffectReceiptMixin__feedback_terminal_identities",
            None,
        )
        if identities is None:
            identities = {}
            setattr(
                self,
                "_CognitiveStateEffectReceiptMixin__feedback_terminal_identities",
                identities,
            )
        owner_identity = id(owner)
        existing = identities.get(owner_identity)
        if existing is not None:
            return
        capability = object()
        identities[owner_identity] = capability
        setattr(
            owner,
            "_FeedbackAttributionStore__feedback_failure_capability",
            capability,
        )

    def _feedback_terminal_capability_matches(
        self,
        owner_identity: int,
        candidate: object,
    ) -> bool:
        identities = getattr(
            self,
            "_CognitiveStateEffectReceiptMixin__feedback_terminal_identities",
            None,
        )
        return bool(identities is not None and identities.get(int(owner_identity)) is candidate)

    def pending_commands(self, consumer_id: str = "") -> list[dict[str, Any]]:
        with self._connect(read_only=True) as conn:
            sql = """
                SELECT o.*
                FROM cognitive_state_outbox AS o
                LEFT JOIN cognitive_state_effect_receipts AS r
                  ON r.command_id=o.command_id
                WHERE r.command_id IS NULL
            """
            params: tuple[Any, ...] = ()
            if consumer_id:
                sql += " AND o.consumer_id=?"
                params = (consumer_id,)
            sql += " ORDER BY o.created_at, o.command_id"
            rows = conn.execute(sql, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(str(item.pop("payload_json")))
            result.append(item)
        return result

    def pending_commands_page(
        self,
        *,
        after_created_at: str = "",
        after_command_id: str = "",
        command_types: tuple[str, ...] = (),
        revision_object_type: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one stable keyset page of unclosed commands without writes."""

        normalized_limit = int(limit)
        if normalized_limit <= 0:
            raise ValueError("pending command page limit must be positive")
        cursor_time = str(after_created_at or "")
        cursor_id = str(after_command_id or "")
        normalized_types = tuple(str(value or "").strip() for value in command_types)
        if normalized_types and (
            any(not value for value in normalized_types)
            or normalized_types != tuple(sorted(set(normalized_types)))
        ):
            raise ValueError("pending command type filter must be sorted and unique")
        normalized_object_type = str(revision_object_type or "").strip()
        with self._connect(read_only=True) as conn:
            sql = """
                SELECT o.*
                FROM cognitive_state_outbox AS o
                JOIN cognitive_state_revisions AS v
                  ON v.revision_id=o.revision_id
                LEFT JOIN cognitive_state_effect_receipts AS r
                  ON r.command_id=o.command_id
                WHERE r.command_id IS NULL
            """
            params: list[Any] = []
            if normalized_types:
                placeholders = ",".join("?" for _value in normalized_types)
                sql += f" AND o.command_type IN ({placeholders})"
                params.extend(normalized_types)
            if normalized_object_type:
                sql += " AND v.object_type=?"
                params.append(normalized_object_type)
            if cursor_time or cursor_id:
                if not cursor_time or not cursor_id:
                    raise ValueError("pending command keyset cursor is incomplete")
                sql += """
                    AND (
                        o.created_at > ?
                        OR (o.created_at = ? AND o.command_id > ?)
                    )
                """
                params.extend((cursor_time, cursor_time, cursor_id))
            sql += " ORDER BY o.created_at, o.command_id LIMIT ?"
            params.append(normalized_limit)
            rows = conn.execute(sql, tuple(params)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(str(item.pop("payload_json")))
            result.append(item)
        return result

    def command(self, command_id: str) -> dict[str, Any] | None:
        """Return one immutable outbox command without changing its state."""

        normalized_id = str(command_id or "").strip()
        if not normalized_id:
            return None
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM cognitive_state_outbox WHERE command_id=?",
                (normalized_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(str(item.pop("payload_json")))
        return item

    def commands_for_revision(self, revision_id: str) -> tuple[dict[str, Any], ...]:
        """Return all immutable outbox commands for one exact revision."""

        normalized = str(revision_id or "").strip()
        if not normalized:
            return ()
        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM cognitive_state_outbox
                WHERE revision_id=? ORDER BY consumer_id, command_id
                """,
                (normalized,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(str(item.pop("payload_json")))
            result.append(item)
        return tuple(result)

    def effect_receipts_for_revision(
        self,
        revision_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Return reciprocal effect receipts derived from one exact revision."""

        normalized_revision = str(revision_id or "").strip()
        if not normalized_revision:
            return ()
        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT r.*, o.command_type, o.payload_json AS command_payload_json,
                       c.outcome AS consumption_outcome,
                       c.metadata AS consumption_metadata
                FROM cognitive_state_effect_receipts AS r
                JOIN cognitive_state_outbox AS o ON o.command_id=r.command_id
                JOIN cognitive_data_consumptions AS c
                  ON c.consumption_id=r.consumption_id
                WHERE r.revision_id=?
                ORDER BY r.consumer_id, r.command_id
                """,
                (normalized_revision,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["evidence_refs"] = json.loads(str(item["evidence_refs"]))
            item["command_payload"] = json.loads(str(item.pop("command_payload_json")))
            consumption_metadata = json.loads(str(item["consumption_metadata"]))
            item["reason_code"] = str(consumption_metadata.get("terminal_reason_code") or "")
            item["retry_exhausted"] = bool(consumption_metadata.get("retry_exhausted"))
            result.append(item)
        return tuple(result)

    def effect_receipt(self, command_id: str) -> dict[str, Any] | None:
        """Return one immutable canonical effect receipt by command identity."""

        command = self.command(command_id)
        if command is None:
            return None
        for receipt in self.effect_receipts_for_revision(str(command["revision_id"])):
            if receipt["command_id"] == command_id:
                return receipt
        return None

    def validate_feedback_effect_receipt(self, command_id: str) -> None:
        """Independently re-prove one stored feedback receipt and domain effect."""

        command = self.command(command_id)
        receipt = self.effect_receipt(command_id)
        if command is None or receipt is None:
            raise ValueError("feedback command lacks its terminal receipt")
        if (
            receipt["revision_id"] != command["revision_id"]
            or receipt["event_id"] != command["event_id"]
            or receipt["consumer_id"] != command["consumer_id"]
        ):
            raise ValueError("feedback receipt command binding mismatch")
        command_row = {
            "command_id": command["command_id"],
            "revision_id": command["revision_id"],
            "consumer_id": command["consumer_id"],
            "command_type": command["command_type"],
            "payload_json": canonical_json(command["payload"]),
            "payload_hash": command["payload_hash"],
        }
        self._validate_feedback_receipt_derivation(
            command_row,
            status=str(receipt["status"]),
            target_effect_id=str(receipt["target_effect_id"]),
            before_hash=str(receipt["before_hash"]),
            after_hash=str(receipt["after_hash"]),
            evidence_refs=tuple(receipt["evidence_refs"]),
            outcome=str(receipt["consumption_outcome"] or ""),
            terminal_reason_code=str(receipt["reason_code"] or ""),
        )

    def validate_training_admission_intake_receipt(
        self,
        command_id: str,
    ) -> None:
        """Independently re-prove one terminal governed-admission intake."""

        from core.cognitive.training_contract import (
            TRAINING_ADMISSION_COMMAND,
            TRAINING_ADMISSION_CONSUMER,
        )

        command = self.command(command_id)
        receipt = self.effect_receipt(command_id)
        if command is None or receipt is None:
            raise ValueError("training admission intake lacks its terminal receipt")
        if (
            command["consumer_id"] != TRAINING_ADMISSION_CONSUMER
            or command["command_type"] != TRAINING_ADMISSION_COMMAND
            or receipt["revision_id"] != command["revision_id"]
            or receipt["event_id"] != command["event_id"]
            or receipt["consumer_id"] != command["consumer_id"]
        ):
            raise ValueError("training admission intake receipt command mismatch")
        command_row = {
            "command_id": command["command_id"],
            "revision_id": command["revision_id"],
            "consumer_id": command["consumer_id"],
            "command_type": command["command_type"],
            "payload_json": canonical_json(command["payload"]),
            "payload_hash": command["payload_hash"],
        }
        self._validate_training_admission_intake_receipt_derivation(
            command_row,
            status=str(receipt["status"]),
            target_effect_id=str(receipt["target_effect_id"]),
            before_hash=str(receipt["before_hash"]),
            after_hash=str(receipt["after_hash"]),
            evidence_refs=tuple(receipt["evidence_refs"]),
            outcome=str(receipt["consumption_outcome"] or ""),
            terminal_reason_code=str(receipt["reason_code"] or ""),
        )

    def close_ineligible_feedback_commands(
        self,
        command_ids: Sequence[str],
        *,
        registered_targets: Sequence[str],
        registry_hash: str,
        created_at: str,
    ) -> tuple[dict[str, str], ...]:
        """Close one replay page of validated no-effect feedback commands."""

        from core.cognitive.feedback_command_closure import (
            close_ineligible_feedback_commands,
        )

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            closed = close_ineligible_feedback_commands(
                conn,
                command_ids=command_ids,
                registered_targets=registered_targets,
                registry_hash=registry_hash,
                created_at=created_at,
            )
            conn.commit()
            return cast(tuple[dict[str, str], ...], closed)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
