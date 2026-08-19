from __future__ import annotations

from types import SimpleNamespace

from core.ops.cognitive_data_contract import (
    CognitiveDataEvent,
    now_utc,
    stable_dedupe_key,
    stable_event_id,
)
from core.ops.producer_consumer_ledger import ProducerConsumerLedger
from core.ops.runtime_flow_health import (
    audit_runtime_producer_consumer_closure,
)


def _event(
    suffix: str,
    *,
    source_kind: str,
    source_uri: str,
    content_hash: str,
    producer: str,
    consumers: tuple[str, ...],
) -> CognitiveDataEvent:
    subject = "shared-user-feedback"
    return CognitiveDataEvent(
        event_id=stable_event_id(source_kind, subject, content_hash, suffix),
        source_id=f"{producer}:{suffix}",
        asset_id="asset-shared-user-feedback",
        source_kind=source_kind,
        source_uri=source_uri,
        content_hash=content_hash,
        canonical_subject=subject,
        data_type="feedback_signal",
        producer=producer,
        intended_consumers=consumers,
        privacy_level="local",
        confidence=0.9,
        evidence_refs=(f"evidence:{suffix}",),
        dedupe_key=stable_dedupe_key(source_kind, subject, content_hash),
        created_at=now_utc(),
        retention_policy="feedback_retention",
    )


def test_duplicate_capture_consume_reconciliation(tmp_path) -> None:
    cfg = SimpleNamespace(database_dir=tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    first = _event(
        "first",
        source_kind="raw_capture",
        source_uri="codex://session/shared",
        content_hash="hash-a",
        producer="capture_service",
        consumers=("persona", "distill"),
    )
    duplicate = _event(
        "duplicate",
        source_kind="sync_turn",
        source_uri="codex://session/shared",
        content_hash="hash-a",
        producer="sync_engine",
        consumers=("persona", "distill"),
    )
    derived = _event(
        "derived",
        source_kind="raw_capture",
        source_uri="codex://session/shared",
        content_hash="hash-b",
        producer="capture_service",
        consumers=("persona", "distill"),
    )
    reinforcement = _event(
        "reinforcement",
        source_kind="reflection",
        source_uri="reflection://shared",
        content_hash="hash-c",
        producer="reflection_store",
        consumers=("persona", "policy"),
    )

    for event in (first, duplicate, derived, reinforcement):
        ledger.record_data_event(event)
    first_persona_receipt = ledger.record_data_consumed(
        first.event_id,
        consumer_id="persona",
        action_changed=True,
        outcome="profile_signal_updated",
        target_effect_id="profile-signal-1",
        before_hash="sha256:profile-before",
        after_hash="sha256:profile-after",
        effect_evidence_refs=("receipt://profile-signal-1",),
    )
    ledger.record_data_consumed(
        first.event_id,
        consumer_id="persona",
        action_changed=False,
        outcome="profile_signal_seen_again",
        status="committed",
        supersedes_consumption_id=first_persona_receipt,
        correction_of_consumption_id=first_persona_receipt,
    )
    for event in (first, duplicate, derived, reinforcement):
        for consumer_id in event.intended_consumers:
            if event.event_id == first.event_id and consumer_id == "persona":
                continue
            ledger.record_data_consumed(
                event.event_id,
                consumer_id=consumer_id,
                outcome="verified_terminal_consumption",
            )
    ledger.record_data_reconciliation(
        event_id=duplicate.event_id,
        related_event_id=first.event_id,
        relation_type="duplicate",
        dedupe_key=duplicate.dedupe_key,
        reason="same admitted source bytes",
        source_revision_refs=("evidence:first", "evidence:duplicate"),
        proof_hash="sha256:duplicate-proof",
    )
    ledger.record_data_reconciliation(
        event_id=derived.event_id,
        related_event_id=first.event_id,
        relation_type="derived",
        dedupe_key=derived.dedupe_key,
        reason="same source revision with changed interpretation",
        source_revision_refs=("evidence:first", "evidence:derived"),
        proof_hash="sha256:derived-proof",
    )
    ledger.record_data_reconciliation(
        event_id=reinforcement.event_id,
        related_event_id=first.event_id,
        relation_type="reinforcement",
        dedupe_key=reinforcement.dedupe_key,
        reason="independent source supports the same subject",
        source_revision_refs=("evidence:first", "evidence:reinforcement"),
        proof_hash="sha256:reinforcement-proof",
    )

    snapshot = ledger.cognitive_data_snapshot()
    counts = snapshot["counts"]
    errors = audit_runtime_producer_consumer_closure(
        cfg,
        strict=True,
        matrix_path=None,
    )

    assert errors == []
    assert snapshot["status"] == "ok"
    assert counts["duplicate_relations"] >= 1
    assert counts["derived_relations"] >= 1
    assert counts["reinforcement_relations"] >= 1
    assert counts["consumptions"] == 9
    assert counts["action_changed_consumptions"] == 1
    assert counts["missing_intended_consumptions"] == 0
    assert counts["duplicate_without_reconciliation"] == 0
    assert counts["unexplained_divergence"] == 0
