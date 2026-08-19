"""Append-only execution attempts for feedback target commands."""

from __future__ import annotations

import sqlite3
from typing import Any, TYPE_CHECKING

from core.cognitive.state_contract import CognitiveStateRevision, now_utc, sha256_json


ATTEMPT_SCHEMA_VERSION = "mnemos.feedback_command_attempt.v2"


class FeedbackCommandAttemptMixin:
    """Record target execution before it runs without inventing failure state."""

    if TYPE_CHECKING:
        def _connect(self, *, read_only: bool = False) -> sqlite3.Connection: ...

        def _feedback_command_context(
            self,
            command_id: str,
            *,
            attribution_revision_id: str = "",
        ) -> tuple[dict[str, Any], CognitiveStateRevision]: ...

    def feedback_command_attempt(
        self,
        command_id: str,
    ) -> dict[str, Any] | None:
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM cognitive_feedback_command_attempts "
                "WHERE command_id=?",
                (str(command_id or ""),),
            ).fetchone()
        return None if row is None else dict(row)

    def _start_feedback_command_attempt(
        self,
        command_id: str,
        *,
        created_at: str = "",
    ) -> dict[str, Any]:
        existing = self.feedback_command_attempt(command_id)
        if existing is not None:
            self._validate_feedback_command_attempt(command_id)
            return existing
        command, attribution = self._feedback_command_context(command_id)
        timestamp = created_at or now_utc()
        identity = {
            "schema_version": ATTEMPT_SCHEMA_VERSION,
            "command_id": str(command["command_id"]),
            "target_id": str(command["consumer_id"]),
            "command_type": str(command["command_type"]),
            "command_payload_hash": str(command["payload_hash"]),
            "attribution_payload_hash": attribution.payload_hash,
            "created_at": timestamp,
        }
        proof_hash = sha256_json(identity)
        attempt_id = "feedback-attempt-start-" + proof_hash.split(":", 1)[1][:32]
        values = (
            attempt_id,
            str(command["command_id"]),
            str(command["consumer_id"]),
            str(command["command_type"]),
            str(command["payload_hash"]),
            attribution.payload_hash,
            proof_hash,
            timestamp,
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO cognitive_feedback_command_attempts VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            existing = self.feedback_command_attempt(command_id)
            if existing is None:
                raise
            return existing
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return dict(zip(_ATTEMPT_COLUMNS, values))

    def _validate_feedback_command_attempt(
        self,
        command_id: str,
    ) -> dict[str, Any]:
        command, attribution = self._feedback_command_context(command_id)
        started = self.feedback_command_attempt(command_id)
        if started is None:
            raise ValueError("feedback command attempt lineage is incomplete")
        start_identity = {
            "schema_version": ATTEMPT_SCHEMA_VERSION,
            "command_id": str(command["command_id"]),
            "target_id": str(command["consumer_id"]),
            "command_type": str(command["command_type"]),
            "command_payload_hash": str(command["payload_hash"]),
            "attribution_payload_hash": attribution.payload_hash,
            "created_at": str(started["created_at"]),
        }
        start_proof = sha256_json(start_identity)
        expected_start_id = (
            "feedback-attempt-start-" + start_proof.split(":", 1)[1][:32]
        )
        if (
            str(started["attempt_id"]) != expected_start_id
            or str(started["proof_hash"]) != start_proof
        ):
            raise ValueError("feedback started attempt proof mismatch")
        return started


_ATTEMPT_COLUMNS = (
    "attempt_id",
    "command_id",
    "target_id",
    "command_type",
    "command_payload_hash",
    "attribution_payload_hash",
    "proof_hash",
    "created_at",
)
