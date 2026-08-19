"""Envelope and consumer-receipt operations for the shared cognitive ledger.

This module deliberately knows nothing about Belief/Decision/Episode domain
state.  It owns immutable transport envelopes, append-only pair receipts and
explicit reconciliation proof; the state store calls the same connection-level
functions from its UnitOfWork.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence
import uuid

from core.cognitive.state_contract import COGNITIVE_OBJECT_TYPES, canonical_json
from core.ops.cognitive_data_contract import (
    COGNITIVE_DATA_EVENT_SCHEMA_VERSION,
    COGNITIVE_DATA_INTERFACES,
    DATA_INTERFACE_REGISTRY_SCHEMA_VERSION,
    RECONCILIATION_TYPES,
    CognitiveDataEvent,
    classify_reconciliation,
    data_interface_registry_payload,
    is_registered_consumer,
    is_registered_producer,
)

EVENT_LIFECYCLE_STATUSES = frozenset(
    {"produced", "normalized", "deduped", "rejected", "expired", "superseded", "dead_letter"}
)
CONSUMER_TERMINAL_STATUSES = frozenset(
    {
        "committed",
        "failed_terminal",
        "intentional_skip",
        "rejected",
        "revoked",
        "dead_letter",
        "expired",
        "superseded",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return canonical_json(value)


def _hash(value: Any) -> str:
    raw = _json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _semantic_metadata_is_reference_only(metadata: Mapping[str, Any]) -> bool:
    allowed = {
        "revision_ids",
        "transaction_hash",
        "contract_version",
        "migration_status",
        "access_control_hash",
    }
    if not set(str(key) for key in metadata) <= allowed:
        return False
    access_hash = metadata.get("access_control_hash")
    return access_hash in (None, "") or (
        isinstance(access_hash, str)
        and access_hash.startswith("sha256:")
        and len(access_hash) == len("sha256:") + 64
    )


def insert_data_event_in_connection(
    conn: sqlite3.Connection,
    event: CognitiveDataEvent,
    *,
    lifecycle_status: str = "produced",
    allow_semantic: bool = False,
) -> tuple[str, bool]:
    """Insert an immutable envelope on a caller-owned transaction."""

    errors = event.validate()
    if errors:
        raise ValueError("; ".join(errors))
    if lifecycle_status not in EVENT_LIFECYCLE_STATUSES:
        raise ValueError(f"unsupported cognitive data lifecycle status: {lifecycle_status}")
    if event.data_type in COGNITIVE_OBJECT_TYPES:
        if not allow_semantic:
            raise ValueError("cognitive state unit of work is required for semantic events")
        if not _semantic_metadata_is_reference_only(event.metadata):
            raise ValueError("semantic envelope metadata must contain references only")
    event_data = event.as_dict()
    immutable_values = (
        event.source_id,
        event.asset_id,
        event.source_kind,
        event.source_uri,
        event.content_hash,
        event.canonical_subject,
        event.data_type,
        event.producer,
        _json(event_data["intended_consumers"]),
        event.privacy_level,
        float(event.confidence),
        _json(event_data["evidence_refs"]),
        event.dedupe_key,
        lifecycle_status,
        event.retention_policy,
        _json(event_data["metadata"]),
    )
    existing = conn.execute(
        """
        SELECT source_id, asset_id, source_kind, source_uri, content_hash,
               canonical_subject, data_type, producer, intended_consumers,
               privacy_level, confidence, evidence_refs, dedupe_key,
               lifecycle_status, retention_policy, metadata
        FROM cognitive_data_events WHERE event_id=?
        """,
        (event.event_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != immutable_values:
            raise ValueError(f"immutable cognitive event conflict for event_id={event.event_id}")
        return event.event_id, False
    conn.execute(
        """
        INSERT INTO cognitive_data_events (
            event_id, source_id, asset_id, source_kind, source_uri,
            content_hash, canonical_subject, data_type, producer,
            intended_consumers, privacy_level, confidence, evidence_refs,
            dedupe_key, lifecycle_status, retention_policy, metadata,
            created_at, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.source_id,
            event.asset_id,
            event.source_kind,
            event.source_uri,
            event.content_hash,
            event.canonical_subject,
            event.data_type,
            event.producer,
            _json(event_data["intended_consumers"]),
            event.privacy_level,
            float(event.confidence),
            _json(event_data["evidence_refs"]),
            event.dedupe_key,
            lifecycle_status,
            event.retention_policy,
            _json(event_data["metadata"]),
            event.created_at,
            _now(),
        ),
    )
    return event.event_id, True


def _normalize_terminal_status(status: str) -> str:
    normalized = str(status or "committed")
    if normalized == "consumed":
        return "committed"
    if normalized == "skipped":
        return "intentional_skip"
    if normalized not in CONSUMER_TERMINAL_STATUSES:
        raise ValueError(f"unsupported cognitive data terminal status: {normalized}")
    return normalized


def insert_data_consumption_in_connection(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    consumer_id: str,
    action_changed: bool = False,
    outcome: str = "",
    status: str = "committed",
    metadata: Mapping[str, Any] | None = None,
    created_at: str | None = None,
    idempotency_key: str | None = None,
    target_effect_id: str = "",
    before_hash: str = "",
    after_hash: str = "",
    effect_evidence_refs: Sequence[str] = (),
    supersedes_consumption_id: str = "",
    correction_of_consumption_id: str = "",
    receipt_state: str = "active",
) -> tuple[str, bool]:
    """Append one consumer-pair terminal fact and advance its rebuildable head."""

    if not event_id:
        raise ValueError("event_id is required")
    if not consumer_id:
        raise ValueError("consumer_id is required")
    event_row = conn.execute(
        "SELECT intended_consumers FROM cognitive_data_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if event_row is None:
        raise ValueError("cognitive data event does not exist")
    intended = tuple(str(value) for value in json.loads(str(event_row[0])))
    if consumer_id not in intended:
        raise ValueError("consumer is not intended for cognitive data event")
    normalized_status = _normalize_terminal_status(status)
    normalized_effect_refs = tuple(str(value).strip() for value in effect_evidence_refs)
    if any(not value for value in normalized_effect_refs):
        raise ValueError("effect_evidence_refs contains a blank value")
    derived_action_changed = bool(
        target_effect_id
        and before_hash
        and after_hash
        and before_hash != after_hash
        and normalized_effect_refs
    )
    if action_changed and not derived_action_changed:
        raise ValueError("action_changed is derived from reciprocal effect evidence")
    if receipt_state not in {"active", "historical_incomplete", "quarantined"}:
        raise ValueError("unsupported cognitive consumption receipt_state")
    immutable_payload = {
        "event_id": event_id,
        "consumer_id": consumer_id,
        "outcome": str(outcome or ""),
        "status": normalized_status,
        "target_effect_id": str(target_effect_id or ""),
        "before_hash": str(before_hash or ""),
        "after_hash": str(after_hash or ""),
        "effect_evidence_refs": list(normalized_effect_refs),
        "action_changed": 1 if derived_action_changed else 0,
        "metadata": dict(metadata or {}),
        "supersedes_consumption_id": str(supersedes_consumption_id or ""),
        "correction_of_consumption_id": str(correction_of_consumption_id or ""),
        "receipt_state": receipt_state,
    }
    resolved_key = str(idempotency_key or _hash(immutable_payload))
    existing = conn.execute(
        """
        SELECT consumption_id, event_id, consumer_id, outcome, status,
               target_effect_id, before_hash, after_hash, effect_evidence_refs,
               action_changed, metadata, COALESCE(supersedes_consumption_id, ''),
               COALESCE(correction_of_consumption_id, ''), receipt_state
        FROM cognitive_data_consumptions WHERE idempotency_key=?
        """,
        (resolved_key,),
    ).fetchone()
    expected = (
        event_id,
        consumer_id,
        str(outcome or ""),
        normalized_status,
        str(target_effect_id or ""),
        str(before_hash or ""),
        str(after_hash or ""),
        _json(list(normalized_effect_refs)),
        1 if derived_action_changed else 0,
        _json(dict(metadata or {})),
        str(supersedes_consumption_id or ""),
        str(correction_of_consumption_id or ""),
        receipt_state,
    )
    if existing is not None:
        if tuple(existing[1:]) != expected:
            raise ValueError("immutable cognitive consumption idempotency conflict")
        return str(existing[0]), False
    current = conn.execute(
        """
        SELECT consumption_id FROM cognitive_data_consumer_heads
        WHERE event_id=? AND consumer_id=?
        """,
        (event_id, consumer_id),
    ).fetchone()
    if current is not None:
        current_id = str(current[0])
        if str(supersedes_consumption_id or "") != current_id:
            raise ValueError("terminal receipt conflict requires explicit supersession")
    elif supersedes_consumption_id:
        raise ValueError("superseded cognitive consumption is not the current head")
    if correction_of_consumption_id:
        corrected = conn.execute(
            """
            SELECT event_id, consumer_id FROM cognitive_data_consumptions
            WHERE consumption_id=?
            """,
            (correction_of_consumption_id,),
        ).fetchone()
        if corrected is None or tuple(corrected) != (event_id, consumer_id):
            raise ValueError("correction target is not in the same consumer pair")
    consumption_id = "cogconsume-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO cognitive_data_consumptions (
            consumption_id, event_id, consumer_id, outcome, status,
            target_effect_id, before_hash, after_hash, effect_evidence_refs,
            action_changed, metadata, idempotency_key,
            supersedes_consumption_id, correction_of_consumption_id,
            receipt_state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), ?, ?)
        """,
        (
            consumption_id,
            *expected[:10],
            resolved_key,
            expected[10],
            expected[11],
            receipt_state,
            created_at or _now(),
        ),
    )
    if receipt_state == "active":
        conn.execute(
            """
            INSERT INTO cognitive_data_consumer_heads(
                event_id, consumer_id, consumption_id, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id, consumer_id) DO UPDATE SET
                consumption_id=excluded.consumption_id,
                updated_at=excluded.updated_at
            """,
            (event_id, consumer_id, consumption_id, _now()),
        )
    return consumption_id, True


def insert_reconciliation_in_connection(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    related_event_id: str,
    relation_type: str,
    dedupe_key: str,
    reason: str,
    source_revision_refs: Sequence[str],
    proof_hash: str,
    proof_status: str = "verified",
    metadata: Mapping[str, Any] | None = None,
) -> tuple[str, bool]:
    if relation_type not in RECONCILIATION_TYPES:
        raise ValueError(f"unsupported cognitive data relation: {relation_type}")
    refs = tuple(str(value).strip() for value in source_revision_refs)
    if not refs or any(not value for value in refs):
        raise ValueError("source_revision_refs must be non-empty")
    if not proof_hash:
        raise ValueError("proof_hash is required")
    if proof_status not in {"verified", "historical_heuristic", "quarantined"}:
        raise ValueError("unsupported reconciliation proof_status")
    existing = conn.execute(
        """
        SELECT reconciliation_id, dedupe_key, reason, source_revision_refs,
               proof_hash, proof_status, metadata
        FROM cognitive_data_reconciliations
        WHERE event_id=? AND related_event_id=? AND relation_type=?
        """,
        (event_id, related_event_id, relation_type),
    ).fetchone()
    expected = (
        dedupe_key,
        reason,
        _json(list(refs)),
        proof_hash,
        proof_status,
        _json(dict(metadata or {})),
    )
    if existing is not None:
        if tuple(existing[1:]) != expected:
            raise ValueError("immutable cognitive reconciliation conflict")
        return str(existing[0]), False
    reconciliation_id = "cogreconcile-" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO cognitive_data_reconciliations (
            reconciliation_id, event_id, related_event_id, relation_type,
            dedupe_key, reason, source_revision_refs, proof_hash,
            proof_status, metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reconciliation_id,
            event_id,
            related_event_id,
            relation_type,
            *expected,
            _now(),
        ),
    )
    return reconciliation_id, True


def cognitive_data_snapshot_in_connection(conn: sqlite3.Connection) -> dict[str, Any]:
    event_rows = conn.execute(
        """
        SELECT event_id, source_kind, source_uri, content_hash,
               canonical_subject, data_type, producer, intended_consumers,
               dedupe_key, lifecycle_status
        FROM cognitive_data_events ORDER BY created_at, event_id
        """
    ).fetchall()
    consumption_rows = conn.execute(
        """
        SELECT c.event_id, c.consumer_id, c.action_changed, c.outcome,
               c.status, c.receipt_state, c.target_effect_id,
               c.before_hash, c.after_hash, c.effect_evidence_refs,
               CASE WHEN h.consumption_id=c.consumption_id THEN 1 ELSE 0 END,
               c.metadata, COALESCE(c.supersedes_consumption_id, ''),
               COALESCE(c.correction_of_consumption_id, '')
        FROM cognitive_data_consumptions AS c
        LEFT JOIN cognitive_data_consumer_heads AS h
          ON h.event_id=c.event_id AND h.consumer_id=c.consumer_id
         AND h.consumption_id=c.consumption_id
        ORDER BY c.created_at, c.consumption_id
        """
    ).fetchall()
    relation_rows = conn.execute(
        """
        SELECT event_id, related_event_id, relation_type, proof_status
        FROM cognitive_data_reconciliations
        """
    ).fetchall()
    events: list[dict[str, Any]] = [
        {
            "event_id": str(row[0]),
            "source_kind": str(row[1]),
            "source_uri": str(row[2]),
            "content_hash": str(row[3]),
            "canonical_subject": str(row[4]),
            "data_type": str(row[5]),
            "producer": str(row[6]),
            "intended_consumers": [str(value) for value in json.loads(str(row[7]))],
            "dedupe_key": str(row[8]),
            "lifecycle_status": str(row[9]),
        }
        for row in event_rows
    ]
    event_ids = {event["event_id"] for event in events}
    current_receipts: dict[tuple[str, str], str] = {}
    for row in consumption_rows:
        if not int(row[10]) or str(row[5]) != "active":
            continue
        try:
            current_metadata = json.loads(str(row[11] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            current_metadata = {}
        reopened = (
            str(row[4]) == "revoked"
            and isinstance(current_metadata, Mapping)
            and current_metadata.get("reopen_required") is True
            and bool(str(row[12]))
            and str(row[12]) == str(row[13])
        )
        if not reopened:
            current_receipts[(str(row[0]), str(row[1]))] = str(row[4])
    intended_pairs = {
        (event["event_id"], consumer)
        for event in events
        for consumer in event["intended_consumers"]
    }
    terminal_pairs = set(current_receipts)
    missing_pairs = intended_pairs - terminal_pairs
    extra_pairs = terminal_pairs - intended_pairs
    for event in events:
        event_pairs: set[tuple[str, str]] = {
            (str(event["event_id"]), str(consumer))
            for consumer in event["intended_consumers"]
        }
        present = event_pairs & terminal_pairs
        if not present:
            aggregate = "produced"
        elif present != event_pairs:
            aggregate = "partially_consumed"
        elif all(
            current_receipts[pair] in {"committed", "intentional_skip"}
            for pair in event_pairs
        ):
            aggregate = "consumed"
        else:
            aggregate = "terminal_with_failures"
        event["aggregate_status"] = aggregate

    relation_counts = {relation: 0 for relation in sorted(RECONCILIATION_TYPES)}
    historical_relation_count = 0
    for row in relation_rows:
        if str(row[3]) == "verified":
            relation_counts[str(row[2])] += 1
        else:
            historical_relation_count += 1
    heuristic_relation_candidates = 0
    for index, event in enumerate(events):
        for other in events[index + 1 :]:
            candidate = classify_reconciliation(event, other)
            if candidate:
                heuristic_relation_candidates += 1

    events_by_id = {str(event["event_id"]): event for event in events}
    unregistered_producers = sorted(
        {
            str(event["producer"])
            for event in events
            if not is_registered_producer(event)
        }
    )
    unregistered_consumer_ids: set[str] = set()
    for row in consumption_rows:
        source_event = events_by_id.get(str(row[0]))
        consumer_id = str(row[1])
        if source_event is None or not is_registered_consumer(
            source_event,
            consumer_id,
        ):
            unregistered_consumer_ids.add(consumer_id)
    unregistered_consumers = sorted(unregistered_consumer_ids)
    consumed_without_event = [
        str(row[0]) for row in consumption_rows if str(row[0]) not in event_ids
    ]
    mutable_action_evidence = sum(
        1
        for row in consumption_rows
        if int(row[2])
        and not (
            str(row[6])
            and str(row[7])
            and str(row[8])
            and str(row[7]) != str(row[8])
            and json.loads(str(row[9]))
        )
    )
    aggregate_consumed_with_missing = sum(
        1
        for event in events
        if event["aggregate_status"] == "consumed"
        and any(pair[0] == event["event_id"] for pair in missing_pairs)
    )
    counts = {
        "interfaces_registered": len(COGNITIVE_DATA_INTERFACES),
        "events": len(events),
        "consumptions": len(consumption_rows),
        "consumed_events": sum(1 for event in events if event["aggregate_status"] == "consumed"),
        "intended_consumptions": len(intended_pairs),
        "terminal_consumptions": len(intended_pairs & terminal_pairs),
        "missing_intended_consumptions": len(missing_pairs),
        "extra_consumptions": len(extra_pairs),
        "action_changed_consumptions": sum(1 for row in consumption_rows if int(row[2])),
        "mutable_action_evidence": mutable_action_evidence,
        "aggregate_consumed_with_missing_consumer": aggregate_consumed_with_missing,
        "multiple_terminal_heads": 0,
        "duplicate_relations": relation_counts.get("duplicate", 0),
        "derived_relations": relation_counts.get("derived", 0),
        "reinforcement_relations": relation_counts.get("reinforcement", 0),
        "historical_heuristic_relations": historical_relation_count,
        # Heuristic subject/source/hash similarity is discovery telemetry only;
        # it cannot prove a canonical reconciliation relation.
        "heuristic_reconciliation_candidates": heuristic_relation_candidates,
        "duplicate_without_reconciliation": 0,
        "unexplained_divergence": 0,
        "consumed_without_data_event": len(consumed_without_event),
        "unregistered_producers": len(unregistered_producers),
        "unregistered_consumers": len(unregistered_consumers),
    }
    degraded_keys = (
        "duplicate_without_reconciliation",
        "unexplained_divergence",
        "consumed_without_data_event",
        "unregistered_producers",
        "unregistered_consumers",
        "missing_intended_consumptions",
        "extra_consumptions",
        "mutable_action_evidence",
        "aggregate_consumed_with_missing_consumer",
        "multiple_terminal_heads",
    )
    return {
        "schema_version": COGNITIVE_DATA_EVENT_SCHEMA_VERSION,
        "registry_schema_version": DATA_INTERFACE_REGISTRY_SCHEMA_VERSION,
        "status": "degraded" if any(counts[key] for key in degraded_keys) else "ok",
        "counts": counts,
        "events": events,
        "unregistered_producers": unregistered_producers,
        "unregistered_consumers": unregistered_consumers,
        "consumed_without_data_event_ids": consumed_without_event[:20],
        "missing_intended_consumers": [
            {"event_id": event_id, "consumer_id": consumer_id}
            for event_id, consumer_id in sorted(missing_pairs)[:20]
        ],
        "extra_consumers": [
            {"event_id": event_id, "consumer_id": consumer_id}
            for event_id, consumer_id in sorted(extra_pairs)[:20]
        ],
        "relation_counts": relation_counts,
        "registry": data_interface_registry_payload(),
    }
