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
    build_runtime_producer_consumer_health,
)


def _capture_event() -> CognitiveDataEvent:
    subject = "codex-session-issue34"
    content_hash = "turn-hash-1"
    return CognitiveDataEvent(
        event_id=stable_event_id("raw_capture", subject, content_hash, "turn-1"),
        source_id="capture-service:turn-1",
        asset_id="asset-codex-session-issue34",
        source_kind="raw_capture",
        source_uri="codex://session/issue34#turn-1",
        content_hash=content_hash,
        canonical_subject=subject,
        data_type="conversation_turn",
        producer="capture_service",
        intended_consumers=("amphora", "distill", "persona"),
        privacy_level="local",
        confidence=0.98,
        evidence_refs=("capture:turn-1",),
        dedupe_key=stable_dedupe_key("raw_capture", subject, content_hash),
        created_at=now_utc(),
        retention_policy="raw_retention",
    )


def test_cognitive_data_ledger_capture_to_distill(tmp_path) -> None:
    cfg = SimpleNamespace(database_dir=tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    event = _capture_event()

    ledger.record_data_event(event)
    ledger.record_data_consumed(
        event.event_id,
        consumer_id="distill",
        action_changed=True,
        outcome="distill_task_created",
        target_effect_id="distill-task-1",
        before_hash="sha256:no-task",
        after_hash="sha256:distill-task-1",
        effect_evidence_refs=("receipt://distill-task-1",),
    )
    ledger.record_data_consumed(
        event.event_id,
        consumer_id="persona",
        outcome="persona_signal_recorded",
    )
    ledger.record_data_consumed(
        event.event_id,
        consumer_id="amphora",
        outcome="distill_task_enqueued",
    )
    ledger.register_flow(
        flow_id="capture_to_distill_data_event",
        data_type="conversation_turn",
        producer_refs=["core/sync_framework/capture_service.py"],
        consumer_refs=["core/hephaestus/distillation_engine.py"],
        pending_budget=0,
        dead_letter_budget=0,
    )
    ledger.record_produced(
        "capture_to_distill_data_event",
        source="capture_service",
        item_id=event.event_id,
        intended_consumers=["distill"],
    )
    ledger.record_consumed(
        "capture_to_distill_data_event",
        source="distill",
        item_id=event.event_id,
    )

    errors = audit_runtime_producer_consumer_closure(
        cfg,
        strict=True,
        matrix_path=None,
    )
    health = build_runtime_producer_consumer_health(cfg, matrix_path=None)

    assert errors == []
    assert health["status"] == "ok"
    assert health["flows"]["capture_to_distill_data_event"]["produced_count"] == 1
    assert health["cognitive_data"]["counts"]["events"] == 1
    assert health["cognitive_data"]["counts"]["consumed_events"] == 1
    assert health["cognitive_data"]["counts"]["action_changed_consumptions"] == 1
    assert health["cognitive_data"]["counts"]["intended_consumptions"] == 3
    assert health["cognitive_data"]["counts"]["terminal_consumptions"] == 3


def test_cognitive_data_event_requires_every_intended_consumer(tmp_path) -> None:
    cfg = SimpleNamespace(database_dir=tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    event = _capture_event()
    ledger.record_data_event(event)
    ledger.record_data_consumed(event.event_id, consumer_id="distill", outcome="queued")

    health = build_runtime_producer_consumer_health(cfg, matrix_path=None)
    errors = audit_runtime_producer_consumer_closure(cfg, strict=True, matrix_path=None)

    assert health["status"] == "degraded"
    assert health["cognitive_data"]["counts"]["missing_intended_consumptions"] == 2
    assert any("intended consumers missing terminal receipts" in error for error in errors)
