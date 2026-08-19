"""Atomic no-effect closure for feedback commands superseded by correction."""

from __future__ import annotations

import sqlite3
import json
from typing import Sequence

from core.cognitive.state_contract import (
    CognitiveStateRevision,
    canonical_json,
    sha256_json,
)
from core.ops.cognitive_event_ledger import insert_data_consumption_in_connection


SUPERSEDED_REASON_CODE = "feedback_correction_superseded_before_effect"
INELIGIBLE_REASON_CODE = "feedback_target_ineligible"


def close_ineligible_feedback_commands(
    conn: sqlite3.Connection,
    *,
    command_ids: Sequence[str],
    registered_targets: Sequence[str],
    registry_hash: str,
    created_at: str,
) -> tuple[dict[str, str], ...]:
    """Atomically close one validated page of canonical no-effect commands."""

    normalized = tuple(str(value or "").strip() for value in command_ids)
    targets = tuple(str(value or "").strip() for value in registered_targets)
    if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
        raise ValueError("feedback ineligible command ids must be unique and non-empty")
    if (
        not normalized
        or any(not value for value in targets)
        or not str(registry_hash).startswith("sha256:")
    ):
        raise ValueError("feedback ineligible closure requires registered targets")
    closed: list[dict[str, str]] = []
    for command_id in normalized:
        row = conn.execute(
            """
            SELECT o.revision_id, o.event_id, o.consumer_id, o.command_type,
                   o.payload_json, r.object_type, r.object_id, r.payload_hash,
                   r.payload_json AS revision_payload_json, h.revision_id AS head_id
            FROM cognitive_state_outbox AS o
            JOIN cognitive_state_revisions AS r ON r.revision_id=o.revision_id
            LEFT JOIN cognitive_state_heads AS h
              ON h.object_type=r.object_type AND h.object_id=r.object_id
            WHERE o.command_id=?
            """,
            (command_id,),
        ).fetchone()
        if row is None:
            raise ValueError("feedback ineligible command is missing")
        if conn.execute(
            "SELECT 1 FROM cognitive_state_effect_receipts WHERE command_id=?",
            (command_id,),
        ).fetchone() is not None:
            raise ValueError("feedback ineligible command already has a receipt")
        command_payload = json.loads(str(row["payload_json"]))
        revision_payload = json.loads(str(row["revision_payload_json"]))
        target_id = str(row["consumer_id"])
        if (
            str(row["object_type"]) != "feedback_attribution_record"
            or str(row["command_type"]) != "evaluate_feedback_target"
            or target_id not in targets
            or str(row["head_id"] or "") != str(row["revision_id"])
            or command_payload.get("schema_version")
            != "mnemos.feedback_target_command.v1"
            or command_payload.get("target_id") != target_id
            or command_payload.get("attribution_revision_id")
            != str(row["revision_id"])
            or command_payload.get("attribution_payload_hash")
            != str(row["payload_hash"])
            or tuple(command_payload.get("required_target_ids") or ()) != targets
            or command_payload.get("input_set_hash")
            != revision_payload.get("input_set_hash")
        ):
            raise ValueError("feedback ineligible command contract mismatch")
        dispositions = [
            item
            for item in revision_payload.get("target_dispositions", [])
            if item.get("target_id") == target_id
        ]
        if len(dispositions) != 1:
            raise ValueError("feedback ineligible target disposition is unavailable")
        disposition = dispositions[0]
        if (
            disposition.get("eligible") is not False
            or command_payload.get("eligible") is not False
            or disposition.get("exclusion_reason")
            != command_payload.get("exclusion_reason")
            or disposition.get("command_ref", {}).get("command_key")
            != command_payload.get("command_key")
        ):
            raise ValueError("feedback command is not a bound ineligible target")
        revision_id = str(row["revision_id"])
        unchanged_hash = sha256_json(
            {
                "attribution_revision_id": revision_id,
                "target_id": target_id,
                "state": "unchanged",
            }
        )
        target_effect_id = (
            "feedback-skip:"
            + target_id
            + ":"
            + revision_id.removeprefix("cogrev-")
        )
        evidence_refs = (
            f"feedback-command:{command_id}",
            f"feedback-attribution:{revision_id}",
            "feedback-target-registry:" + str(registry_hash),
        )
        identity = {
            "command_id": command_id,
            "status": "intentional_skip",
            "target_effect_id": target_effect_id,
            "before_hash": unchanged_hash,
            "after_hash": unchanged_hash,
            "evidence_refs": list(evidence_refs),
            "terminal_reason_code": INELIGIBLE_REASON_CODE,
            "retry_exhausted": False,
        }
        receipt_id = "cogeffect-" + sha256_json(identity).split(":", 1)[1][:32]
        reciprocal_ref = f"cognitive-effect-receipt:{receipt_id}"
        consumption_id, _ = insert_data_consumption_in_connection(
            conn,
            str(row["event_id"]),
            consumer_id=target_id,
            status="intentional_skip",
            outcome=str(disposition["exclusion_reason"]),
            idempotency_key=f"cognitive-effect:{receipt_id}",
            target_effect_id=target_effect_id,
            before_hash=unchanged_hash,
            after_hash=unchanged_hash,
            effect_evidence_refs=(*evidence_refs, reciprocal_ref),
            metadata={
                "command_id": command_id,
                "effect_receipt_id": receipt_id,
                "terminal_reason_code": INELIGIBLE_REASON_CODE,
                "retry_exhausted": False,
            },
            created_at=created_at,
        )
        conn.execute(
            """
            INSERT INTO cognitive_state_effect_receipts (
                receipt_id, command_id, revision_id, event_id, consumer_id,
                consumption_id, status, target_effect_id, before_hash,
                after_hash, evidence_refs, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'intentional_skip', ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                command_id,
                revision_id,
                str(row["event_id"]),
                target_id,
                consumption_id,
                target_effect_id,
                unchanged_hash,
                unchanged_hash,
                canonical_json(list(evidence_refs)),
                created_at,
            ),
        )
        closed.append(
            {
                "command_id": command_id,
                "target_id": target_id,
                "attribution_revision_id": revision_id,
                "effect_receipt_id": receipt_id,
                "target_effect_id": target_effect_id,
                "before_hash": unchanged_hash,
                "after_hash": unchanged_hash,
            }
        )
    return tuple(closed)


def close_superseded_feedback_commands(
    conn: sqlite3.Connection,
    *,
    command_ids: Sequence[str],
    revisions: Sequence[CognitiveStateRevision],
    created_at: str,
) -> tuple[str, ...]:
    """Close exact pending commands inside their correction transaction."""

    normalized = tuple(str(value or "").strip() for value in command_ids)
    if any(not value for value in normalized) or len(set(normalized)) != len(normalized):
        raise ValueError("feedback command supersession ids must be unique and non-empty")
    corrections = tuple(
        revision
        for revision in revisions
        if revision.object_type == "feedback_attribution_record"
        and revision.correction_of_revision_id
        and revision.supersedes_revision_id
        == revision.correction_of_revision_id
    )
    if len(corrections) != 1:
        raise ValueError("feedback command supersession requires one correction revision")
    correction = corrections[0]
    ancestor_ids: set[str] = set()
    cursor = correction.supersedes_revision_id
    while cursor:
        if cursor in ancestor_ids:
            raise ValueError("feedback correction attribution lineage contains a cycle")
        ancestor_ids.add(cursor)
        row = conn.execute(
            """
            SELECT COALESCE(supersedes_revision_id, '')
            FROM cognitive_state_revisions WHERE revision_id=?
            """,
            (cursor,),
        ).fetchone()
        if row is None:
            raise ValueError("feedback correction attribution ancestor is missing")
        cursor = str(row[0] or "")
    receipt_ids: list[str] = []
    for command_id in normalized:
        command = conn.execute(
            """
            SELECT o.revision_id, o.event_id, o.consumer_id, o.command_type,
                   o.payload_hash, r.object_type, r.object_id
            FROM cognitive_state_outbox AS o
            JOIN cognitive_state_revisions AS r ON r.revision_id=o.revision_id
            WHERE o.command_id=?
            """,
            (command_id,),
        ).fetchone()
        if command is None:
            raise ValueError("feedback command supersession source is missing")
        prior_revision_id = str(command["revision_id"])
        if (
            prior_revision_id not in ancestor_ids
            or str(command["object_type"]) != "feedback_attribution_record"
            or str(command["object_id"]) != correction.object_id
            or str(command["command_type"]) != "evaluate_feedback_target"
        ):
            raise ValueError("feedback command supersession lineage mismatch")
        existing = conn.execute(
            "SELECT receipt_id FROM cognitive_state_effect_receipts WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if existing is not None:
            raise ValueError("feedback command supersession raced with a target effect")
        target_id = str(command["consumer_id"])
        unchanged_hash = str(command["payload_hash"])
        target_effect_id = "feedback-command-superseded:" + command_id
        evidence_refs = (
            f"feedback-command:{command_id}",
            f"feedback-attribution:{prior_revision_id}",
            f"feedback-correction:{correction.revision_id}",
            f"no-effect-oracle:{command_id}:{unchanged_hash}",
        )
        identity = {
            "command_id": command_id,
            "status": "rejected",
            "target_effect_id": target_effect_id,
            "before_hash": unchanged_hash,
            "after_hash": unchanged_hash,
            "evidence_refs": list(evidence_refs),
            "terminal_reason_code": SUPERSEDED_REASON_CODE,
            "retry_exhausted": False,
        }
        receipt_id = "cogeffect-" + str(sha256_json(identity)).split(":", 1)[1][:32]
        reciprocal_ref = f"cognitive-effect-receipt:{receipt_id}"
        consumption_id, _ = insert_data_consumption_in_connection(
            conn,
            str(command["event_id"]),
            consumer_id=target_id,
            status="rejected",
            outcome="superseded_before_effect",
            idempotency_key=f"cognitive-effect:{receipt_id}",
            target_effect_id=target_effect_id,
            before_hash=unchanged_hash,
            after_hash=unchanged_hash,
            effect_evidence_refs=(*evidence_refs, reciprocal_ref),
            metadata={
                "command_id": command_id,
                "effect_receipt_id": receipt_id,
                "terminal_reason_code": SUPERSEDED_REASON_CODE,
                "retry_exhausted": False,
            },
            created_at=created_at,
        )
        conn.execute(
            """
            INSERT INTO cognitive_state_effect_receipts (
                receipt_id, command_id, revision_id, event_id, consumer_id,
                consumption_id, status, target_effect_id, before_hash,
                after_hash, evidence_refs, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'rejected', ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                command_id,
                prior_revision_id,
                str(command["event_id"]),
                target_id,
                consumption_id,
                target_effect_id,
                unchanged_hash,
                unchanged_hash,
                canonical_json(list(evidence_refs)),
                created_at,
            ),
        )
        receipt_ids.append(receipt_id)
    return tuple(receipt_ids)
