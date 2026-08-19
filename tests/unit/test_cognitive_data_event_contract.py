from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.ops.cognitive_data_contract import (
    COGNITIVE_DATA_IDENTITY_RULES,
    COGNITIVE_DATA_INTERFACES,
    COGNITIVE_DATA_EVENT_SCHEMA_VERSION,
    CognitiveDataEvent,
    is_registered_consumer,
    is_registered_producer,
    now_utc,
    stable_dedupe_key,
    stable_event_id,
    validate_data_interface_registry,
)
from core.ops.producer_consumer_ledger import ProducerConsumerLedger


def _event(
    suffix: str, *, content_hash: str = "hash-a", producer: str = "capture_service"
) -> CognitiveDataEvent:
    source_kind = "raw_capture" if producer == "capture_service" else "document_processor"
    canonical_subject = "session-1"
    return CognitiveDataEvent(
        event_id=stable_event_id(source_kind, canonical_subject, content_hash, suffix),
        source_id=f"source-{suffix}",
        asset_id="asset-session-1",
        source_kind=source_kind,
        source_uri=f"{source_kind}://session-1",
        content_hash=content_hash,
        canonical_subject=canonical_subject,
        data_type="conversation_turn",
        producer=producer,
        intended_consumers=("distill", "persona"),
        privacy_level="local",
        confidence=0.91,
        evidence_refs=(f"evidence:{suffix}",),
        dedupe_key=stable_dedupe_key(source_kind, canonical_subject, content_hash),
        created_at=now_utc(),
    )


def test_cognitive_data_event_requires_contract_fields() -> None:
    event = _event("valid")

    assert event.schema_version == COGNITIVE_DATA_EVENT_SCHEMA_VERSION
    assert event.validate() == []
    assert event.as_dict()["intended_consumers"] == ["distill", "persona"]

    invalid = CognitiveDataEvent(
        event_id="",
        source_kind="",
        source_uri="",
        content_hash="",
        canonical_subject="",
        data_type="",
        producer="",
        intended_consumers=(),
        privacy_level="local",
        confidence=1.2,
        evidence_refs=(),
        dedupe_key="",
        created_at="",
    )

    errors = invalid.validate()
    assert "event_id is required" in errors
    assert "intended_consumers must be non-empty" in errors
    assert "confidence must be between 0 and 1" in errors


def test_data_interface_registry_covers_known_producers() -> None:
    assert validate_data_interface_registry() == []
    cognition = next(
        item for item in COGNITIVE_DATA_INTERFACES if item.interface_id == "cognition_asset_commit"
    )
    assert cognition.producer == "cognition_asset_store"
    assert cognition.privacy_class == "private"


def test_typed_cognitive_identity_rules_cover_phase3_runtime_producers() -> None:
    decision = replace(
        _event("decision"),
        source_kind="material_decision",
        data_type="decision_trace",
        producer="decision_trace_store",
        intended_consumers=(
            "material-action:knowledge_delivery:4294fdfd7ac39048",
        ),
    )
    calibration = replace(
        _event("calibration"),
        source_kind="observation_calibration",
        data_type="calibration_record",
        producer="observation_calibrator",
        intended_consumers=("observation_index", "wiki_projection"),
    )
    episode = replace(
        _event("episode"),
        source_kind="distillation_extraction",
        data_type="cognition_episode",
        producer="cognitive_state_store",
        intended_consumers=("wiki", "knowledge_graph", "cognitive_graph"),
    )

    assert {rule.rule_id for rule in COGNITIVE_DATA_IDENTITY_RULES} >= {
        "decision_trace_store_producer",
        "observation_calibrator_producer",
        "material_action_consumer",
        "observation_index_consumer",
        "calibration_wiki_projection_consumer",
        "cognition_episode_knowledge_graph_consumer",
    }
    assert is_registered_producer(decision)
    assert is_registered_producer(calibration)
    assert is_registered_consumer(
        decision,
        "material-action:knowledge_delivery:4294fdfd7ac39048",
    )
    assert is_registered_consumer(calibration, "observation_index")
    assert is_registered_consumer(calibration, "wiki_projection")
    assert is_registered_consumer(episode, "knowledge_graph")


@pytest.mark.parametrize(
    "consumer_id",
    (
        "material-action:knowledge_delivery:not-a-hash",
        "material-action::4294fdfd7ac39048",
        "material-action:knowledge_delivery:4294fdfd7ac390480",
        "material-action:knowledge_delivery:4294FDFD7AC39048",
    ),
)
def test_material_action_identity_rule_rejects_malformed_consumers(
    consumer_id: str,
) -> None:
    decision = replace(
        _event("decision-malformed"),
        source_kind="material_decision",
        data_type="decision_trace",
        producer="decision_trace_store",
        intended_consumers=(consumer_id,),
    )

    assert not is_registered_consumer(decision, consumer_id)


def test_identity_rules_are_scoped_to_the_declared_event_contract() -> None:
    ordinary = _event("ordinary")
    spoofed_decision = replace(
        ordinary,
        producer="decision_trace_store",
        intended_consumers=(
            "material-action:knowledge_delivery:4294fdfd7ac39048",
        ),
    )

    assert not is_registered_producer(spoofed_decision)
    assert not is_registered_consumer(
        spoofed_decision,
        "material-action:knowledge_delivery:4294fdfd7ac39048",
    )
    assert not is_registered_consumer(ordinary, "observation_index")
    assert not is_registered_consumer(ordinary, "knowledge_graph")


def test_ledger_reconciles_duplicate_derived_and_reinforcement(tmp_path) -> None:
    ledger = ProducerConsumerLedger(SimpleNamespace(database_dir=tmp_path), initialize=True)
    first = _event("first")
    duplicate = _event("duplicate")
    derived = _event("derived", content_hash="hash-b")
    reinforcement = _event("reinforcement", content_hash="hash-c", producer="document_processor")

    for event in (first, duplicate, derived, reinforcement):
        ledger.record_data_event(event)
        for consumer_id in event.intended_consumers:
            ledger.record_data_consumed(
                event.event_id,
                consumer_id=consumer_id,
                outcome="terminal_test_consumption",
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
        reason="same source revision with a different admitted interpretation",
        source_revision_refs=("evidence:first", "evidence:derived"),
        proof_hash="sha256:derived-proof",
    )
    ledger.record_data_reconciliation(
        event_id=reinforcement.event_id,
        related_event_id=first.event_id,
        relation_type="reinforcement",
        dedupe_key=reinforcement.dedupe_key,
        reason="independent source revision supports the same subject",
        source_revision_refs=("evidence:first", "evidence:reinforcement"),
        proof_hash="sha256:reinforcement-proof",
    )

    snapshot = ledger.cognitive_data_snapshot()
    counts = snapshot["counts"]

    assert snapshot["status"] == "ok"
    assert counts["events"] == 4
    assert counts["duplicate_relations"] >= 1
    assert counts["derived_relations"] >= 1
    assert counts["reinforcement_relations"] >= 1
    assert counts["duplicate_without_reconciliation"] == 0
    assert counts["unexplained_divergence"] == 0


def test_invalid_lifecycle_rejected(tmp_path) -> None:
    ledger = ProducerConsumerLedger(SimpleNamespace(database_dir=tmp_path), initialize=True)

    with pytest.raises(ValueError, match="unsupported cognitive data lifecycle"):
        ledger.record_data_event(_event("bad"), lifecycle_status="unknown")


def test_cognitive_event_id_is_immutable_and_exact_replay_is_idempotent(tmp_path) -> None:
    ledger = ProducerConsumerLedger(SimpleNamespace(database_dir=tmp_path), initialize=True)
    event = _event("immutable")
    ledger.record_data_event(event)
    ledger.record_data_consumed(event.event_id, consumer_id="distill")

    assert ledger.record_data_event(event) == event.event_id
    with sqlite3.connect(ledger.db_path) as conn:
        lifecycle = conn.execute(
            "SELECT lifecycle_status FROM cognitive_data_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()[0]
    assert lifecycle == "produced"
    assert ledger.cognitive_data_snapshot()["events"][0]["aggregate_status"] == (
        "partially_consumed"
    )

    conflicting = replace(event, content_hash="different-content")
    with pytest.raises(ValueError, match="immutable cognitive event conflict"):
        ledger.record_data_event(conflicting)


def test_cognitive_event_replay_preserves_first_observation_timestamp(tmp_path) -> None:
    ledger = ProducerConsumerLedger(SimpleNamespace(database_dir=tmp_path), initialize=True)
    event = replace(_event("timestamp-replay"), created_at="2026-07-13T01:02:03+00:00")
    replay = replace(event, created_at="2026-07-13T01:02:09+00:00")

    assert ledger.record_data_event(event) == event.event_id
    assert ledger.record_data_event(replay) == event.event_id

    with sqlite3.connect(ledger.db_path) as conn:
        created_at = conn.execute(
            "SELECT created_at FROM cognitive_data_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()[0]
    assert created_at == event.created_at
