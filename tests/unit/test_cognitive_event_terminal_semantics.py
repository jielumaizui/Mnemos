from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.producer_consumer_ledger import ProducerConsumerLedger


def _ledger(tmp_path: Path) -> ProducerConsumerLedger:
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    return ProducerConsumerLedger(SimpleNamespace(database_dir=tmp_path), initialize=False)


def _event() -> CognitiveDataEvent:
    return CognitiveDataEvent(
        event_id="cde-terminal-contract",
        source_kind="raw_capture",
        source_uri="raw://terminal-contract",
        content_hash="sha256:terminal-contract",
        canonical_subject="terminal-contract",
        data_type="conversation_turn",
        producer="capture_service",
        intended_consumers=("distill", "persona"),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=("raw-event#0:10",),
        dedupe_key="terminal-contract",
        created_at="2026-07-16T00:00:00+00:00",
    )


def test_consumption_requires_an_existing_event_and_intended_consumer(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    with pytest.raises(ValueError, match="cognitive data event does not exist"):
        ledger.record_data_consumed("missing", consumer_id="distill")

    ledger.record_data_event(_event())
    with pytest.raises(ValueError, match="consumer is not intended"):
        ledger.record_data_consumed(_event().event_id, consumer_id="wiki")


def test_one_consumer_cannot_mark_the_whole_event_consumed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    event = _event()
    ledger.record_data_event(event)

    ledger.record_data_consumed(event.event_id, consumer_id="distill")

    snapshot = ledger.cognitive_data_snapshot()
    assert snapshot["events"][0]["aggregate_status"] == "partially_consumed"
    assert snapshot["counts"]["aggregate_consumed_with_missing_consumer"] == 0
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT lifecycle_status FROM cognitive_data_events WHERE event_id=?",
            (event.event_id,),
        ).fetchone()[0] == "produced"


def test_conflicting_terminal_receipt_requires_explicit_supersession(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    event = _event()
    ledger.record_data_event(event)
    first = ledger.record_data_consumed(
        event.event_id,
        consumer_id="distill",
        status="committed",
        outcome="first",
        idempotency_key="distill:first",
    )

    with pytest.raises(ValueError, match="terminal receipt conflict"):
        ledger.record_data_consumed(
            event.event_id,
            consumer_id="distill",
            status="rejected",
            outcome="conflict",
            idempotency_key="distill:conflict",
        )

    correction = ledger.record_data_consumed(
        event.event_id,
        consumer_id="distill",
        status="rejected",
        outcome="corrected",
        idempotency_key="distill:corrected",
        supersedes_consumption_id=first,
        correction_of_consumption_id=first,
    )
    assert correction != first
    assert ledger.cognitive_data_snapshot()["events"][0]["aggregate_status"] == (
        "partially_consumed"
    )


def test_action_changed_is_derived_from_reciprocal_effect_evidence(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    event = _event()
    ledger.record_data_event(event)

    with pytest.raises(ValueError, match="action_changed is derived"):
        ledger.record_data_consumed(
            event.event_id,
            consumer_id="distill",
            action_changed=True,
        )

    ledger.record_data_consumed(
        event.event_id,
        consumer_id="distill",
        target_effect_id="distill-effect-1",
        before_hash="sha256:before",
        after_hash="sha256:after",
        effect_evidence_refs=("receipt://distill-effect-1",),
    )

    counts = ledger.cognitive_data_snapshot()["counts"]
    assert counts["action_changed_consumptions"] == 1
    assert counts["mutable_action_evidence"] == 0


def test_semantic_event_cannot_bypass_the_state_unit_of_work(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    semantic = _event()
    semantic = CognitiveDataEvent(
        **{
            **semantic.as_dict(),
            "intended_consumers": semantic.intended_consumers,
            "evidence_refs": semantic.evidence_refs,
            "data_type": "cognition_episode",
        }
    )

    with pytest.raises(ValueError, match="cognitive state unit of work is required"):
        ledger.record_data_event(semantic)
