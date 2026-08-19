from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.cognitive.state_schema import reconcile_cognitive_state_schema
from core.ops.cognitive_pipeline_receipts import _read_only_ledger
from core.ops.durable_io import DurableIOError
from core.ops.producer_consumer_ledger import (
    DEFAULT_MATRIX,
    ProducerConsumerLedger,
    _matrix_flows,
)
from core.ops.runtime_flow_health import (
    audit_runtime_producer_consumer_closure,
    build_runtime_producer_consumer_health,
)


def _config(tmp_path):
    return SimpleNamespace(database_dir=tmp_path)


def _tree_digest(path):
    digest = hashlib.sha256()
    for candidate in sorted(path.rglob("*")):
        digest.update(str(candidate.relative_to(path)).encode())
        if candidate.is_file():
            digest.update(candidate.read_bytes())
    return digest.hexdigest()


def test_runtime_flow_matrix_reader_never_follows_a_leaf_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "matrix.json"
    target.write_text('{"flows": []}', encoding="utf-8")
    alias = tmp_path / "matrix-alias.json"
    alias.symlink_to(target)

    with pytest.raises(OSError):
        _matrix_flows(alias)


def test_adaptive_flow_matrix_bootstrap_is_explicit_and_empty_is_unobserved(tmp_path):
    ledger = ProducerConsumerLedger(_config(tmp_path), initialize=True)
    assert ledger.register_adaptive_flows(DEFAULT_MATRIX) >= 14

    health = build_runtime_producer_consumer_health(_config(tmp_path), matrix_path=DEFAULT_MATRIX)

    assert health["schema_version"] == "mnemos.runtime_producer_consumer.v4"
    assert health["status"] == "degraded"
    assert health["counts"]["registered_flows"] >= 14
    assert health["counts"]["unobserved_flows"] > 0
    assert health["counts"]["producer_only"] == 0
    assert health["flows"]["raw_quality_to_distill_gate"]["topic"] == "raw_quality_to_distill_gate"
    assert health["flows"]["raw_quality_to_distill_gate"]["observation_state"] == "unobserved"
    assert health["flows"]["raw_quality_to_distill_gate"]["freshness_ok"] is False
    assert health["flows"]["migration_plan_to_ledger"]["observation_state"] == "inactive"
    assert (
        health["flows"]["kg_confidence_to_relation_display"]["receipt_grace_seconds"]
        == 60
    )
    assert (
        health["flows"]["module_toggle_to_activation_contract"]["observation_state"]
        == "not_applicable"
    )
    assert ledger.snapshot()["counts"]["registered_flows"] == health["counts"]["registered_flows"]


def test_health_is_read_only_and_missing_ledger_is_blocked(tmp_path):
    before = _tree_digest(tmp_path)

    health = build_runtime_producer_consumer_health(_config(tmp_path), matrix_path=DEFAULT_MATRIX)

    assert health["status"] == "blocked"
    assert health["observation_state"] == "blocked"
    assert health["error"] == "runtime producer/consumer ledger is unavailable"
    assert _tree_digest(tmp_path) == before
    assert not (tmp_path / "producer_consumer_ledger.db").exists()


def test_ledger_constructor_does_not_provision_by_default(tmp_path):
    with pytest.raises(FileNotFoundError):
        ProducerConsumerLedger(_config(tmp_path))

    assert not (tmp_path / "producer_consumer_ledger.db").exists()


def test_runtime_ledger_accepts_explicit_mapping_database_scope(tmp_path):
    ledger = ProducerConsumerLedger(
        {"database_dir": tmp_path},
        initialize=True,
    )

    assert ledger.database_dir == tmp_path
    assert ledger.db_path.is_file()


def test_uninspectable_runtime_ledger_never_becomes_missing(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "producer_consumer_ledger.db"
    original_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(
        DurableIOError,
        match="durable_path_inspection_failed",
    ):
        ProducerConsumerLedger(_config(tmp_path))
    with pytest.raises(
        DurableIOError,
        match="durable_path_inspection_failed",
    ):
        _read_only_ledger(_config(tmp_path))


def test_reregister_flow_updates_parent_without_deleting_runtime_evidence(
    tmp_path,
    monkeypatch,
):
    clock = {"value": "2026-07-16T09:00:00+00:00"}
    monkeypatch.setattr(
        "core.ops.producer_consumer_ledger._now_utc",
        lambda: clock["value"],
    )
    ledger = ProducerConsumerLedger(_config(tmp_path), initialize=True)
    ledger.register_flow(
        flow_id="restart_safe_flow",
        data_type="old_type",
        producer_refs=["old_producer"],
        consumer_refs=["consumer"],
        pending_budget=1,
    )
    production_event_id = ledger.record_produced(
        "restart_safe_flow",
        source="old_producer",
        item_id="item-1",
        intended_consumers=["consumer"],
    )
    receipt_id = ledger.record_consumed(
        "restart_safe_flow",
        source="consumer",
        item_id="item-1",
        production_event_id=production_event_id,
    )
    with sqlite3.connect(ledger.db_path) as conn:
        events_before = conn.execute(
            "SELECT * FROM runtime_flow_events ORDER BY event_id"
        ).fetchall()
        receipts_before = conn.execute(
            "SELECT * FROM runtime_flow_receipts ORDER BY receipt_id"
        ).fetchall()

    clock["value"] = "2026-07-16T09:01:00+00:00"
    ledger.register_flow(
        flow_id="restart_safe_flow",
        data_type="new_type",
        producer_refs=["new_producer"],
        consumer_refs=["consumer", "audit_consumer"],
        pending_budget=2,
    )

    with sqlite3.connect(ledger.db_path) as conn:
        registry = conn.execute(
            """
            SELECT data_type, producer_refs, consumer_refs, pending_budget,
                   registered_at, updated_at
            FROM runtime_flow_registry
            WHERE flow_id='restart_safe_flow'
            """
        ).fetchone()
        events_after = conn.execute(
            "SELECT * FROM runtime_flow_events ORDER BY event_id"
        ).fetchall()
        receipts_after = conn.execute(
            "SELECT * FROM runtime_flow_receipts ORDER BY receipt_id"
        ).fetchall()
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()

    assert production_event_id
    assert receipt_id
    assert registry == (
        "new_type",
        '["new_producer"]',
        '["consumer", "audit_consumer"]',
        2,
        "2026-07-16T09:00:00+00:00",
        "2026-07-16T09:01:00+00:00",
    )
    assert events_after == events_before
    assert receipts_after == receipts_before
    assert foreign_key_errors == []


def test_health_does_not_mutate_a_populated_ledger(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="read_only",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
    )
    production_event_id = ledger.record_produced(
        "read_only",
        source="producer",
        item_id="item-1",
        intended_consumers=["consumer"],
    )
    ledger.record_consumed(
        "read_only",
        source="consumer",
        item_id="item-1",
        production_event_id=production_event_id,
    )
    before = _tree_digest(tmp_path)

    health = build_runtime_producer_consumer_health(cfg, matrix_path=DEFAULT_MATRIX)

    assert health["status"] == "ok"
    assert _tree_digest(tmp_path) == before


def test_required_on_event_flow_without_evidence_is_unobserved(tmp_path):
    ledger = ProducerConsumerLedger(_config(tmp_path), initialize=True)
    ledger.register_flow(
        flow_id="required_event_flow",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
        required=True,
        observation_mode="on_event",
    )

    flow = ledger.snapshot()["flows"]["required_event_flow"]

    assert flow["observation_state"] == "unobserved"
    assert flow["status"] == "degraded"


def test_distill_receipt_resolves_exact_task_generation_and_skips_missing_cognitive_event(
    tmp_path,
):
    from types import SimpleNamespace

    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_prejudgment,
    )

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "same-session",
        SimpleNamespace(task_id="task-1", input_revision="revision-1"),
    )
    record_capture_worker_handoff(
        cfg,
        "same-session",
        SimpleNamespace(task_id="task-2", input_revision="revision-2"),
    )
    with sqlite3.connect(ledger.db_path) as conn:
        event_rows = conn.execute(
            """
            SELECT event_id, metadata
            FROM runtime_flow_events
            WHERE flow_id='raw_quality_to_distill_gate'
            ORDER BY created_at
            """
        ).fetchall()
    assert len(event_rows) == 2

    record_distillation_prejudgment(
        cfg,
        session_id="same-session",
        meta={
            "_amphora_task_id": "task-1",
            "input_revision": "revision-1",
            "cognitive_sync_event_ids": ["cde-missing"],
        },
        verdict="MAYBE",
    )

    with sqlite3.connect(ledger.db_path) as conn:
        receipts = conn.execute(
            "SELECT production_event_id, status, metadata FROM runtime_flow_receipts"
        ).fetchall()
        orphan = conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions WHERE event_id='cde-missing'"
        ).fetchone()[0]
    # The prejudgment is a nonterminal stage event bound to the exact task-1
    # generation; it must not close the flow or write cognitive consumptions.
    assert len(receipts) == 1
    assert receipts[0][0] == event_rows[0][0]
    assert receipts[0][1] == "in_progress"
    stage_metadata = json.loads(receipts[0][2])
    assert stage_metadata["transition"] == "value_prejudgment_completed"
    assert stage_metadata["verdict"] == "MAYBE"
    assert orphan == 0


def test_distill_prejudgment_records_nonterminal_stage_without_closing(tmp_path):
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_prejudgment,
    )

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-without-cognitive-event",
        SimpleNamespace(task_id="task-no-cognitive", input_revision="revision-no-cognitive"),
    )

    record_distillation_prejudgment(
        cfg,
        session_id="session-without-cognitive-event",
        meta={
            "_amphora_task_id": "task-no-cognitive",
            "input_revision": "revision-no-cognitive",
        },
        verdict="MAYBE",
    )

    flow = ledger.snapshot()["flows"]["raw_quality_to_distill_gate"]
    assert flow["produced_count"] == 1
    assert flow["terminal_consumer_count"] == 0
    assert flow["pending_count"] == 1


def test_typed_terminal_after_prejudgment_is_not_shadowed(tmp_path):
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_prejudgment,
        record_distillation_terminal,
    )
    from core.pipeline_receipts import DistillationWriteReceipt

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "shadow-session",
        SimpleNamespace(task_id="shadow-task", input_revision="shadow-revision"),
    )
    record_distillation_prejudgment(
        cfg,
        session_id="shadow-session",
        meta={"_amphora_task_id": "shadow-task", "input_revision": "shadow-revision"},
        verdict="MAYBE",
    )

    evidence = record_distillation_terminal(
        cfg,
        task={
            "task_id": "shadow-task",
            "session_id": "shadow-session",
            "input_revision": "shadow-revision",
            "meta": {},
        },
        receipt=DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="verified deterministic skip",
        ),
    )

    flow = ledger.snapshot()["flows"]["raw_quality_to_distill_gate"]
    assert evidence["matched"] is True
    assert flow["terminal_consumer_count"] == 1
    assert flow["pending_count"] == 0
    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            "SELECT status, metadata FROM runtime_flow_receipts ORDER BY created_at, receipt_id"
        ).fetchall()
    assert [row[0] for row in rows] == ["in_progress", "consumed"]
    terminal_metadata = json.loads(rows[1][1])
    assert terminal_metadata["transition"] == "distillation_terminal_receipt_verified"
    assert terminal_metadata["receipt_status"] == "intentional_skip"


def test_failed_terminal_after_prejudgment_closes_generation_as_dead_letter(tmp_path):
    from core.ops.cognitive_data_contract import CognitiveDataEvent
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_failed_terminal,
        record_distillation_prejudgment,
    )

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    event_id = "cde-failed-terminal"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id="raw-event-failed",
            asset_id="raw-event-failed",
            source_kind="sync_engine",
            source_uri="sync://agent/session-failed/turn/1",
            content_hash="content-hash-failed",
            canonical_subject="agent:session-failed:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-event-failed",),
            dedupe_key="sync-engine:session-failed:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    record_capture_worker_handoff(
        cfg,
        "session-failed",
        SimpleNamespace(task_id="failed-task", input_revision="failed-revision"),
    )
    record_distillation_prejudgment(
        cfg,
        session_id="session-failed",
        meta={
            "_amphora_task_id": "failed-task",
            "input_revision": "failed-revision",
            "cognitive_sync_event_ids": [event_id],
        },
        verdict="MAYBE",
    )

    evidence = record_distillation_failed_terminal(
        cfg,
        task={
            "task_id": "failed-task",
            "session_id": "session-failed",
            "input_revision": "failed-revision",
            "meta": {"cognitive_sync_event_ids": [event_id]},
        },
        reason="retry_exhausted",
    )

    flow = ledger.snapshot()["flows"]["raw_quality_to_distill_gate"]
    assert evidence["matched"] is True
    assert evidence["reason"] == "recorded"
    assert flow["terminal_consumer_count"] == 1
    assert flow["pending_count"] == 0
    assert flow["dead_letter_count"] == 1
    with sqlite3.connect(ledger.db_path) as conn:
        runtime_rows = conn.execute(
            "SELECT status, metadata FROM runtime_flow_receipts ORDER BY created_at, receipt_id"
        ).fetchall()
        cognitive_rows = conn.execute(
            "SELECT consumer_id, status, outcome FROM cognitive_data_consumptions "
            "WHERE event_id = ? ORDER BY created_at",
            (event_id,),
        ).fetchall()
    assert [row[0] for row in runtime_rows] == ["in_progress", "dead_letter"]
    dead_letter_metadata = json.loads(runtime_rows[1][1])
    assert dead_letter_metadata["transition"] == "distillation_failed_terminal"
    assert dead_letter_metadata["failure_reason"] == "retry_exhausted"
    assert cognitive_rows == [
        ("amphora", "committed", "distill_task_handoff_verified"),
        ("distill", "failed_terminal", "distill_task_failed_terminal"),
    ]


def test_failed_terminal_never_closes_without_amphora_cognitive_pair(
    tmp_path,
    monkeypatch,
):
    from core.ops.cognitive_data_contract import CognitiveDataEvent
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_failed_terminal,
    )

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    event_id = "cde-failed-missing-amphora-pair"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id="raw-event-missing-amphora",
            asset_id="raw-event-missing-amphora",
            source_kind="sync_engine",
            source_uri="sync://agent/session-missing-amphora/turn/1",
            content_hash="content-hash-missing-amphora",
            canonical_subject="agent:session-missing-amphora:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-event-missing-amphora",),
            dedupe_key="sync-engine:session-missing-amphora:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    record_capture_worker_handoff(
        cfg,
        "session-missing-amphora",
        SimpleNamespace(
            task_id="failed-task-missing-amphora",
            input_revision="failed-revision-missing-amphora",
        ),
    )
    monkeypatch.setattr(
        "core.ops.cognitive_pipeline_receipts.record_distillation_handoff",
        lambda *_args, **_kwargs: {
            "verified": False,
            "reason": "injected_handoff_outage",
            "cognitive_receipts": 0,
            "cognitive_deferred": 1,
        },
    )

    evidence = record_distillation_failed_terminal(
        cfg,
        task={
            "task_id": "failed-task-missing-amphora",
            "session_id": "session-missing-amphora",
            "input_revision": "failed-revision-missing-amphora",
            "retry_count": 3,
            "max_retries": 3,
            "meta": {"cognitive_sync_event_ids": [event_id]},
        },
        reason="retry_exhausted",
    )

    assert evidence["matched"] is False
    assert evidence["reason"] == "cognitive_failed_terminal_deferred"
    assert evidence["cognitive_deferred"] == 1
    with sqlite3.connect(ledger.db_path) as conn:
        runtime_terminal_count = conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='dead_letter'"
        ).fetchone()[0]
        cognitive_rows = conn.execute(
            "SELECT consumer_id, status, outcome FROM cognitive_data_consumptions "
            "WHERE event_id=? ORDER BY consumer_id",
            (event_id,),
        ).fetchall()
    assert runtime_terminal_count == 0
    assert cognitive_rows == [
        ("distill", "failed_terminal", "distill_task_failed_terminal")
    ]


def test_failed_terminal_stays_unmatched_until_cognitive_receipt_is_queryable(
    tmp_path,
    monkeypatch,
):
    from core.ops import cognitive_pipeline_receipts as receipts
    from core.ops.cognitive_data_contract import CognitiveDataEvent

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    event_id = "cde-failed-terminal-deferred"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id="raw-event-deferred",
            asset_id="raw-event-deferred",
            source_kind="sync_engine",
            source_uri="sync://agent/session-deferred/turn/1",
            content_hash="content-hash-deferred",
            canonical_subject="agent:session-deferred:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-event-deferred",),
            dedupe_key="sync-engine:session-deferred:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    receipts.record_capture_worker_handoff(
        cfg,
        "session-deferred",
        SimpleNamespace(
            task_id="failed-task-deferred",
            input_revision="failed-revision-deferred",
        ),
    )
    task = {
        "task_id": "failed-task-deferred",
        "session_id": "session-deferred",
        "input_revision": "failed-revision-deferred",
        "meta": {"cognitive_sync_event_ids": [event_id]},
    }
    real_record = receipts.record_cognitive_data_consumed
    monkeypatch.setattr(
        receipts,
        "record_cognitive_data_consumed",
        lambda *_args, **_kwargs: None,
    )

    deferred = receipts.record_distillation_failed_terminal(
        cfg,
        task=task,
        reason="retry_exhausted",
    )

    assert deferred["matched"] is False
    assert deferred["reason"] == "cognitive_failed_terminal_deferred"
    assert deferred["cognitive_deferred"] == 2
    monkeypatch.setattr(
        receipts,
        "record_cognitive_data_consumed",
        real_record,
    )
    replayed = receipts.record_distillation_failed_terminal(
        cfg,
        task=task,
        reason="retry_exhausted",
    )
    assert replayed["matched"] is True
    assert replayed["cognitive_deferred"] == 0


def test_failed_terminal_rejects_missing_explicit_cognitive_event(tmp_path):
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_failed_terminal,
        verify_distillation_failed_terminal,
    )

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "session-missing-cognitive",
        SimpleNamespace(
            task_id="failed-task-missing-cognitive",
            input_revision="failed-revision-missing-cognitive",
        ),
    )
    task = {
        "task_id": "failed-task-missing-cognitive",
        "session_id": "session-missing-cognitive",
        "input_revision": "failed-revision-missing-cognitive",
        "meta": {
            "cognitive_sync_event_ids": [
                "cde-explicitly-referenced-but-missing"
            ]
        },
    }

    recorded = record_distillation_failed_terminal(
        cfg,
        task=task,
        reason="retry_exhausted",
    )
    verified = verify_distillation_failed_terminal(
        cfg,
        task=task,
        expected_reason="retry_exhausted",
    )

    assert recorded["matched"] is False
    assert recorded["reason"] == "cognitive_failed_terminal_deferred"
    assert recorded["cognitive_deferred"] == 2
    assert verified["verified"] is False
    assert verified["reason"] == "failed_terminal_receipt_missing"
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_data_consumptions
            WHERE event_id='cde-explicitly-referenced-but-missing'
            """
        ).fetchone()[0] == 0


def test_synced_turn_does_not_publish_undurable_cognitive_event_id(
    tmp_path,
    monkeypatch,
):
    from core.ops import cognitive_pipeline_receipts as receipts

    turn = SimpleNamespace(turn_number=1, metadata={})
    monkeypatch.setattr(
        receipts,
        "record_cognitive_data_event",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        RuntimeError,
        match="cognitive_sync_event_not_durable",
    ):
        receipts.record_synced_turn(
            _config(tmp_path),
            source_name="test-agent",
            session_id="session-undurable-event",
            turn=turn,
            content_hash="content-hash",
            persona_committed=False,
        )

    assert "cognitive_sync_event_id" not in turn.metadata


def test_failed_generation_rejects_later_success_terminal_without_dual_receipts(tmp_path):
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_failed_terminal,
        record_distillation_terminal,
    )
    from core.pipeline_receipts import DistillationWriteReceipt

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "terminal-conflict-session",
        SimpleNamespace(
            task_id="terminal-conflict-task",
            input_revision="terminal-conflict-revision",
        ),
    )
    task = {
        "task_id": "terminal-conflict-task",
        "session_id": "terminal-conflict-session",
        "input_revision": "terminal-conflict-revision",
        "meta": {},
    }
    failed = record_distillation_failed_terminal(
        cfg,
        task=task,
        reason="retry_exhausted",
    )
    succeeded = record_distillation_terminal(
        cfg,
        task=task,
        receipt=DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="must not revive failed generation",
        ),
    )

    with sqlite3.connect(ledger.db_path) as conn:
        terminal_rows = conn.execute(
            "SELECT status FROM runtime_flow_receipts "
            "WHERE status IN ('consumed', 'dead_letter', 'skipped') "
            "ORDER BY created_at, receipt_id"
        ).fetchall()
    flow = ledger.snapshot()["flows"]["raw_quality_to_distill_gate"]

    assert failed["matched"] is True
    assert succeeded["matched"] is False
    assert succeeded["reason"] == "terminal_receipt_conflict"
    assert terminal_rows == [("dead_letter",)]
    assert flow["terminal_conflict_count"] == 0


def test_concurrent_opposite_terminals_are_serialized_to_one_pair_head(
    tmp_path,
    monkeypatch,
):
    cfg = _config(tmp_path)
    setup = ProducerConsumerLedger(cfg, initialize=True)
    setup.register_flow(
        flow_id="concurrent-terminal",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
    )
    production_event_id = setup.record_produced(
        "concurrent-terminal",
        source="producer",
        item_id="item-1",
        intended_consumers=["consumer"],
        generation_id="generation-1",
    )
    first_selected = threading.Event()
    release_first = threading.Event()

    class PausingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            normalized = " ".join(str(sql).split()).lower()
            if (
                "select receipt_id, status, item_id, generation_id, metadata"
                in normalized
                and "from runtime_flow_receipts" in normalized
                and not first_selected.is_set()
            ):
                first_selected.set()
                assert release_first.wait(timeout=5)
            return result

    first_ledger = ProducerConsumerLedger(cfg)
    second_ledger = ProducerConsumerLedger(cfg)

    def pausing_connect(*, validate=False):
        conn = sqlite3.connect(
            str(first_ledger.db_path),
            timeout=5,
            factory=PausingConnection,
        )
        conn.execute("PRAGMA foreign_keys = ON")
        if validate:
            from core.cognitive.state_schema import validate_cognitive_state_schema

            validate_cognitive_state_schema(conn)
        return conn

    monkeypatch.setattr(first_ledger, "_connect", pausing_connect)
    results: list[str] = []
    errors: list[BaseException] = []

    def first_worker():
        try:
            results.append(
                first_ledger.record_dead_letter(
                    "concurrent-terminal",
                    source="consumer",
                    item_id="item-1",
                    production_event_id=production_event_id,
                    generation_id="generation-1",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def second_worker():
        try:
            results.append(
                second_ledger.record_consumed(
                    "concurrent-terminal",
                    source="consumer",
                    item_id="item-1",
                    production_event_id=production_event_id,
                    generation_id="generation-1",
                )
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=first_worker)
    second = threading.Thread(target=second_worker)
    first.start()
    assert first_selected.wait(timeout=5)
    second.start()
    time.sleep(0.1)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert str(errors[0]) == "terminal_receipt_conflict"
    with sqlite3.connect(setup.db_path) as conn:
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM runtime_flow_receipts
            WHERE production_event_id=? AND consumer_id='consumer'
              AND status IN ('consumed', 'dead_letter', 'skipped')
            """,
            (production_event_id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "existing_status",
    ["dead_letter", "consumed"],
)
def test_conflicting_legacy_runtime_terminal_writes_no_cognitive_receipts(
    tmp_path,
    existing_status,
):
    """An opposite old runtime terminal must fail before cognitive mutation."""
    from core.ops.cognitive_data_contract import CognitiveDataEvent
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_failed_terminal,
        record_distillation_terminal,
    )
    from core.pipeline_receipts import DistillationWriteReceipt

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    event_id = f"cde-legacy-conflict-{existing_status}"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id=f"raw-{existing_status}",
            asset_id=f"raw-{existing_status}",
            source_kind="sync_engine",
            source_uri=f"sync://agent/{existing_status}/turn/1",
            content_hash=f"content-{existing_status}",
            canonical_subject=f"agent:{existing_status}:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=(f"raw-{existing_status}",),
            dedupe_key=f"legacy-conflict:{existing_status}",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    task = {
        "task_id": f"legacy-conflict-task-{existing_status}",
        "session_id": f"legacy-conflict-session-{existing_status}",
        "input_revision": f"legacy-conflict-revision-{existing_status}",
        "meta": {"cognitive_sync_event_ids": [event_id]},
    }
    record_capture_worker_handoff(
        cfg,
        task["session_id"],
        SimpleNamespace(
            task_id=task["task_id"],
            input_revision=task["input_revision"],
        ),
    )
    with sqlite3.connect(ledger.db_path) as conn:
        production_event_id, generation_id, item_id = conn.execute(
            """
            SELECT event_id, generation_id, item_id
            FROM runtime_flow_events
            WHERE flow_id='raw_quality_to_distill_gate'
            """
        ).fetchone()
    if existing_status == "dead_letter":
        ledger.record_dead_letter(
            "raw_quality_to_distill_gate",
            source="core/hephaestus/distillation_engine.py",
            item_id=item_id,
            production_event_id=production_event_id,
            generation_id=generation_id,
            metadata={
                "transition": "distillation_failed_terminal",
                "failure_reason": "legacy failure",
            },
        )
        evidence = record_distillation_terminal(
            cfg,
            task=task,
            receipt=DistillationWriteReceipt(
                status="intentional_skip",
                terminal_reason="must not overwrite failure",
            ),
        )
    else:
        ledger.record_consumed(
            "raw_quality_to_distill_gate",
            source="core/hephaestus/distillation_engine.py",
            item_id=item_id,
            production_event_id=production_event_id,
            generation_id=generation_id,
            metadata={
                "transition": "distillation_terminal_receipt_verified",
                "receipt_status": "intentional_skip",
            },
        )
        evidence = record_distillation_failed_terminal(
            cfg,
            task=task,
            reason="must not overwrite success",
        )

    assert evidence["matched"] is False
    assert evidence["reason"] == "terminal_receipt_conflict"
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions WHERE event_id=?",
            (event_id,),
        ).fetchone()[0] == 0


def test_snapshot_exposes_preexisting_dual_terminal_pair_as_degraded(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="legacy-terminal-conflict",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
    )
    production_event_id = ledger.record_produced(
        "legacy-terminal-conflict",
        source="producer",
        item_id="item-1",
        intended_consumers=["consumer"],
        generation_id="generation-1",
    )
    ledger.record_consumed(
        "legacy-terminal-conflict",
        source="consumer",
        item_id="item-1",
        production_event_id=production_event_id,
        generation_id="generation-1",
    )
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            """
            INSERT INTO runtime_flow_receipts(
                receipt_id, production_event_id, flow_id, consumer_id, status,
                item_id, generation_id, idempotency_key, created_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-dead-letter",
                production_event_id,
                "legacy-terminal-conflict",
                "consumer",
                "dead_letter",
                "item-1",
                "generation-1",
                "legacy-terminal-conflict:dead-letter",
                "2026-07-25T00:00:01+00:00",
                "{}",
            ),
        )
        conn.commit()

    snapshot = ledger.snapshot()
    flow = snapshot["flows"]["legacy-terminal-conflict"]

    assert flow["status"] == "degraded"
    assert flow["observation_state"] == "partial"
    assert flow["terminal_conflict_count"] == 1
    assert set(flow["terminal_conflict_pairs"][0]["statuses"]) == {
        "consumed",
        "dead_letter",
    }
    assert snapshot["counts"]["terminal_conflicts"] == 1


def test_prejudgment_stage_replay_is_idempotent(tmp_path):
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_prejudgment,
    )

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "replay-session",
        SimpleNamespace(task_id="replay-task", input_revision="replay-revision"),
    )

    for _ in range(2):
        record_distillation_prejudgment(
            cfg,
            session_id="replay-session",
            meta={"_amphora_task_id": "replay-task", "input_revision": "replay-revision"},
            verdict="YES",
        )

    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            "SELECT status FROM runtime_flow_receipts"
        ).fetchall()
    assert rows == [("in_progress",)]


def test_prejudgment_writes_no_cognitive_consumption_head(tmp_path):
    from core.ops.cognitive_data_contract import CognitiveDataEvent
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_prejudgment,
    )

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    event_id = "cde-no-prejudgment-head"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id="raw-event-head",
            asset_id="raw-event-head",
            source_kind="sync_engine",
            source_uri="sync://agent/session-head/turn/1",
            content_hash="content-hash-head",
            canonical_subject="agent:session-head:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-event-head",),
            dedupe_key="sync-engine:session-head:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    record_capture_worker_handoff(
        cfg,
        "session-head",
        SimpleNamespace(task_id="head-task", input_revision="head-revision"),
    )

    record_distillation_prejudgment(
        cfg,
        session_id="session-head",
        meta={
            "_amphora_task_id": "head-task",
            "input_revision": "head-revision",
            "cognitive_sync_event_ids": [event_id],
        },
        verdict="YES",
    )

    with sqlite3.connect(ledger.db_path) as conn:
        consumptions = conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]
    assert consumptions == 0


def test_typed_terminal_receipt_backfills_exact_runtime_generation(tmp_path):
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_terminal,
    )
    from core.pipeline_receipts import DistillationWriteReceipt

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        cfg,
        "terminal-session",
        SimpleNamespace(task_id="terminal-task", input_revision="terminal-revision"),
    )

    evidence = record_distillation_terminal(
        cfg,
        task={
            "task_id": "terminal-task",
            "session_id": "terminal-session",
            "input_revision": "terminal-revision",
            "meta": {},
        },
        receipt=DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="verified deterministic skip",
        ),
    )

    flow = ledger.snapshot()["flows"]["raw_quality_to_distill_gate"]
    assert evidence["matched"] is True
    assert flow["terminal_consumer_count"] == 1
    assert flow["pending_count"] == 0


def test_reviewed_runtime_skip_closes_without_dead_letter_debt(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    event_id = ledger.record_produced(
        "reviewed_skip_flow",
        source="producer.py",
        item_id="fixture-item",
        intended_consumers=["consumer.py"],
    )

    ledger.record_skipped(
        "reviewed_skip_flow",
        source="consumer.py",
        item_id="fixture-item",
        production_event_id=event_id,
        metadata={"terminal_reason": "reviewed_fixture_retirement"},
    )

    flow = ledger.snapshot()["flows"]["reviewed_skip_flow"]
    assert flow["terminal_consumer_count"] == 1
    assert flow["pending_count"] == 0
    assert flow["consumed_count"] == 0
    assert flow["dead_letter_count"] == 0


def test_source_span_supersession_replaces_wrong_recorder_without_mutating_history(tmp_path):
    from core.ops.cognitive_pipeline_receipts import (
        record_distillation_generation_superseded,
    )
    from core.ops.runtime_flow_telemetry import runtime_item_id

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    item_id = runtime_item_id("distill-session", "fixture")
    production_event_id = ledger.record_produced(
        "raw_quality_to_distill_gate",
        source="core/sync_framework/sync_engine.py",
        item_id=item_id,
        intended_consumers=["core/hephaestus/distillation_engine.py"],
        metadata={"task_id": "legacy-task", "input_revision": "legacy-revision"},
        generation_id="distill-task:legacy-task:legacy-revision",
    )
    wrong_receipt_id = ledger.record_consumed(
        "raw_quality_to_distill_gate",
        source="scripts/reconcile_amphora_source_spans.py",
        item_id=item_id,
        production_event_id=production_event_id,
        generation_id="distill-task:legacy-task:legacy-revision",
        metadata={
            "transition": "verified_source_span_generation_superseded",
            "replacement_task_id": "replacement-task",
        },
    )

    result = record_distillation_generation_superseded(
        cfg,
        legacy_task={
            "task_id": "legacy-task",
            "session_id": "fixture",
            "input_revision": "legacy-revision",
            "meta": {},
        },
        replacement_task_id="replacement-task",
    )

    flow = ledger.snapshot()["flows"]["raw_quality_to_distill_gate"]
    assert result["matched"] is True
    assert flow["terminal_consumer_count"] == 1
    assert flow["pending_count"] == 0
    assert flow["missing_consumers"] == []
    assert flow["extra_consumers"] == []
    assert flow["consumed_count"] == 0
    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            "SELECT receipt_id, consumer_id, status, metadata "
            "FROM runtime_flow_receipts ORDER BY created_at, receipt_id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == wrong_receipt_id
    assert rows[1][1:3] == ("core/hephaestus/distillation_engine.py", "skipped")
    assert wrong_receipt_id in json.loads(rows[1][3])["supersedes_receipt_ids"]


def test_runtime_receipt_supersession_cannot_hide_an_unrelated_extra_receipt(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    first = ledger.record_produced(
        "supersession_guard",
        source="producer.py",
        item_id="item-1",
        intended_consumers=["consumer.py"],
        generation_id="generation-1",
    )
    second = ledger.record_produced(
        "supersession_guard",
        source="producer.py",
        item_id="item-2",
        intended_consumers=["consumer.py"],
        generation_id="generation-2",
    )
    unrelated_receipt = ledger.record_consumed(
        "supersession_guard",
        source="wrong-consumer.py",
        item_id="item-1",
        production_event_id=first,
        generation_id="generation-1",
    )
    ledger.record_skipped(
        "supersession_guard",
        source="repair.py",
        consumer_id="consumer.py",
        item_id="item-2",
        production_event_id=second,
        generation_id="generation-2",
        metadata={"supersedes_receipt_ids": [unrelated_receipt]},
    )

    flow = ledger.snapshot()["flows"]["supersession_guard"]
    assert flow["extra_consumers"] == ["wrong-consumer.py"]
    assert flow["missing_consumers"] == ["consumer.py"]
    assert flow["pending_count"] == 1


def test_runtime_receipt_supersession_requires_an_explicit_reason(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    production = ledger.record_produced(
        "supersession_reason_guard",
        source="producer.py",
        item_id="item-1",
        intended_consumers=["consumer.py"],
        generation_id="generation-1",
    )
    wrong_receipt = ledger.record_consumed(
        "supersession_reason_guard",
        source="wrong-consumer.py",
        item_id="item-1",
        production_event_id=production,
        generation_id="generation-1",
    )
    ledger.record_skipped(
        "supersession_reason_guard",
        source="repair.py",
        consumer_id="consumer.py",
        item_id="item-1",
        production_event_id=production,
        generation_id="generation-1",
        metadata={"supersedes_receipt_ids": [wrong_receipt]},
    )

    flow = ledger.snapshot()["flows"]["supersession_reason_guard"]
    assert flow["extra_consumers"] == ["wrong-consumer.py"]
    assert flow["missing_consumers"] == []


def test_cognitive_reopen_requires_a_correction_of_the_current_head(tmp_path):
    from core.ops.cognitive_data_contract import CognitiveDataEvent

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    event_id = "cde-reopen-proof-guard"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id="raw-reopen-proof",
            asset_id="raw-reopen-proof",
            source_kind="sync_engine",
            source_uri="sync://agent/reopen-proof/turn/1",
            content_hash="reopen-proof-hash",
            canonical_subject="agent:reopen-proof:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("distill",),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-reopen-proof",),
            dedupe_key="sync-engine:reopen-proof",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    current = ledger.record_data_consumed(
        event_id,
        consumer_id="distill",
        outcome="incorrect terminal",
    )
    ledger.record_data_consumed(
        event_id,
        consumer_id="distill",
        outcome="unproved reopen",
        status="revoked",
        metadata={"reopen_required": True},
        supersedes_consumption_id=current,
    )

    cognitive = ledger.snapshot()["cognitive_data"]
    assert cognitive["counts"]["terminal_consumptions"] == 1
    assert cognitive["counts"]["missing_intended_consumptions"] == 0


def test_terminal_task_does_not_reconcile_cognitive_receipts_without_runtime_production(
    tmp_path,
):
    from core.ops.cognitive_data_contract import CognitiveDataEvent
    from core.ops.cognitive_pipeline_receipts import record_distillation_terminal
    from core.pipeline_receipts import DistillationWriteReceipt

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    event_id = "cde-terminal-cognitive"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id="raw-event-1",
            asset_id="raw-event-1",
            source_kind="sync_engine",
            source_uri="sync://agent/session-cognitive/turn/1",
            content_hash="content-hash-1",
            canonical_subject="agent:session-cognitive:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-event-1",),
            dedupe_key="sync-engine:session-cognitive:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )

    evidence = record_distillation_terminal(
        cfg,
        task={
            "task_id": "terminal-cognitive-task",
            "session_id": "session-cognitive",
            "input_revision": "revision-cognitive",
            "meta": {"cognitive_sync_event_ids": [event_id]},
        },
        receipt=DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="verified deterministic skip",
        ),
    )

    with sqlite3.connect(ledger.db_path) as conn:
        consumers = {
            str(row[0])
            for row in conn.execute(
                "SELECT consumer_id FROM cognitive_data_consumptions WHERE event_id = ?",
                (event_id,),
            )
        }
    assert evidence["matched"] is False
    assert evidence["reason"] == "production_missing"
    assert consumers == set()


def test_terminal_task_does_not_consume_unintended_cognitive_event(tmp_path):
    from core.ops.cognitive_data_contract import CognitiveDataEvent
    from core.ops.cognitive_pipeline_receipts import record_distillation_terminal
    from core.pipeline_receipts import DistillationWriteReceipt

    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    event_id = "cde-unintended-terminal"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id="raw-event-1",
            asset_id="raw-event-1",
            source_kind="sync_engine",
            source_uri="sync://agent/session-unintended/turn/1",
            content_hash="content-hash-1",
            canonical_subject="agent:session-unintended:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("persona",),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-event-1",),
            dedupe_key="sync-engine:session-unintended:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )

    record_distillation_terminal(
        cfg,
        task={
            "task_id": "unintended-task",
            "session_id": "session-unintended",
            "input_revision": "revision-unintended",
            "meta": {"cognitive_sync_event_ids": [event_id]},
        },
        receipt=DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="verified deterministic skip",
        ),
    )

    with sqlite3.connect(ledger.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]
    assert count == 0


def test_outdated_schema_is_blocked_without_health_time_migration(tmp_path):
    db_path = tmp_path / "producer_consumer_ledger.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE runtime_flow_registry (flow_id TEXT PRIMARY KEY)")
    before = _tree_digest(tmp_path)

    health = build_runtime_producer_consumer_health(_config(tmp_path))
    errors = audit_runtime_producer_consumer_closure(
        _config(tmp_path), strict=True, matrix_path=None
    )

    assert health["status"] == "blocked"
    assert health["error_type"] == "CognitiveStateSchemaError"
    assert errors == [
        "runtime producer/consumer ledger requires reconciliation: "
        "cognitive state schema is not canonical: classification=legacy_runtime_v1_or_v2"
    ]
    assert _tree_digest(tmp_path) == before


def test_explicit_reconciliation_migrates_legacy_terminal_rows_without_fabricating_coverage(
    tmp_path,
):
    db_path = tmp_path / "producer_consumer_ledger.db"
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE runtime_flow_registry (
                flow_id TEXT PRIMARY KEY, data_type TEXT NOT NULL, topic TEXT NOT NULL,
                producer_refs TEXT NOT NULL, consumer_refs TEXT NOT NULL,
                pending_budget INTEGER NOT NULL DEFAULT 0,
                dead_letter_budget INTEGER NOT NULL DEFAULT 0,
                max_lag_seconds INTEGER NOT NULL,
                registered_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """)
        conn.execute("""
            CREATE TABLE runtime_flow_events (
                event_id TEXT PRIMARY KEY, flow_id TEXT NOT NULL, direction TEXT NOT NULL,
                topic TEXT NOT NULL, source TEXT NOT NULL, item_id TEXT NOT NULL,
                created_at TEXT NOT NULL, metadata TEXT NOT NULL
            )
            """)
        conn.execute(
            "INSERT INTO runtime_flow_registry VALUES (?, ?, ?, ?, ?, 0, 0, 86400, ?, ?)",
            ("legacy", "event", "legacy", '["producer"]', '["consumer"]', now, now),
        )
        conn.execute(
            "INSERT INTO runtime_flow_events VALUES (?, ?, 'produced', ?, ?, ?, ?, '{}')",
            ("produced-1", "legacy", "legacy", "producer", "item-1", now),
        )
        conn.execute(
            "INSERT INTO runtime_flow_events VALUES (?, ?, 'consumed', ?, ?, ?, ?, '{}')",
            ("consumed-1", "legacy", "legacy", "consumer", "item-1", now),
        )

    with sqlite3.connect(db_path) as conn:
        report = reconcile_cognitive_state_schema(conn, apply=True)
    ledger = ProducerConsumerLedger(_config(tmp_path), initialize=False)

    with sqlite3.connect(db_path) as conn:
        directions = conn.execute(
            "SELECT direction FROM runtime_flow_events ORDER BY event_id"
        ).fetchall()
        receipts = conn.execute(
            "SELECT production_event_id, consumer_id, status FROM runtime_flow_receipts"
        ).fetchall()

    assert report["copied_counts"]["runtime_receipts"] == 1
    assert directions == [("produced",)]
    assert receipts == [("produced-1", "consumer", "consumed")]
    assert ledger.snapshot()["flows"]["legacy"]["observation_state"] == "partial"


def test_strict_audit_flags_producer_only_consumer_only_and_dead_letters(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="sample_flow",
        data_type="sample",
        producer_refs=["producer.py"],
        consumer_refs=["consumer.py"],
        pending_budget=0,
        dead_letter_budget=0,
    )

    ledger.record_produced("sample_flow", source="producer.py", item_id="p1")
    ledger.record_consumed("sample_flow", source="consumer.py", item_id="c1")
    ledger.record_dead_letter("sample_flow", source="consumer.py", item_id="d1")
    ledger.record_produced("producer_only_flow", source="producer.py", item_id="p2")
    ledger.record_consumed("consumer_only_flow", source="consumer.py", item_id="c2")

    errors = audit_runtime_producer_consumer_closure(cfg, strict=True, matrix_path=None)

    assert any(
        "sample_flow: produced 1 but consumed 1 with 1 dead letters" in error for error in errors
    )
    assert any(
        "producer_only_flow: produced 1 but no consumer event was recorded" in error
        for error in errors
    )
    assert any(
        "consumer_only_flow: consumed 1 but no producer event was recorded" in error
        for error in errors
    )


def test_consumed_flow_clears_pending_budget(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="paired_flow",
        data_type="paired",
        producer_refs=["producer.py"],
        consumer_refs=["consumer.py"],
        pending_budget=0,
        dead_letter_budget=0,
    )

    production_event_id = ledger.record_produced(
        "paired_flow",
        source="producer.py",
        item_id="p1",
        intended_consumers=["consumer.py"],
    )
    ledger.record_consumed(
        "paired_flow",
        source="consumer.py",
        item_id="p1",
        production_event_id=production_event_id,
    )

    assert audit_runtime_producer_consumer_closure(cfg, strict=True, matrix_path=None) == []
    health = build_runtime_producer_consumer_health(cfg, matrix_path=None)
    assert health["status"] == "ok"
    assert health["flows"]["paired_flow"]["observation_state"] == "observed"
    assert health["flows"]["paired_flow"]["produced_count"] == 1
    assert health["flows"]["paired_flow"]["consumed_count"] == 1
    assert health["flows"]["paired_flow"]["intended_count"] == 1
    assert health["flows"]["paired_flow"]["terminal_consumer_count"] == 1


def test_pending_productions_returns_only_missing_intended_consumer(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="projection_flow",
        data_type="relation",
        producer_refs=["producer"],
        consumer_refs=["projector"],
    )
    first = ledger.record_produced(
        "projection_flow",
        source="producer",
        item_id="item-1",
        intended_consumers=["projector"],
    )
    ledger.record_produced(
        "projection_flow",
        source="producer",
        item_id="item-2",
        intended_consumers=["projector"],
    )
    ledger.record_consumed(
        "projection_flow",
        source="projector",
        item_id="item-1",
        production_event_id=first,
    )

    pending = ledger.pending_productions("projection_flow", "projector")

    assert len(pending) == 1
    assert pending[0]["item_id"] == "item-2"
    assert pending[0]["event_id"]


def test_partial_intended_consumer_coverage_is_degraded(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="fanout",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["distill", "persona", "scoring"],
    )
    production_event_id = ledger.record_produced(
        "fanout",
        source="producer",
        item_id="event-1",
        intended_consumers=["distill", "persona", "scoring"],
    )
    ledger.record_consumed(
        "fanout",
        source="distill",
        item_id="event-1",
        production_event_id=production_event_id,
    )

    health = build_runtime_producer_consumer_health(cfg, matrix_path=None)
    flow = health["flows"]["fanout"]

    assert health["status"] == "degraded"
    assert flow["observation_state"] == "partial"
    assert flow["intended_count"] == 3
    assert flow["terminal_consumer_count"] == 1
    assert flow["missing_consumers"] == ["persona", "scoring"]


def test_fresh_async_receipt_is_in_flight_until_deadline_then_degraded(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="async_projection",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["projector"],
        receipt_grace_seconds=60,
    )
    ledger.record_produced(
        "async_projection",
        source="producer",
        item_id="fresh-item",
        intended_consumers=["projector"],
    )

    fresh = build_runtime_producer_consumer_health(cfg, matrix_path=None)["flows"][
        "async_projection"
    ]

    assert fresh["status"] == "ok"
    assert fresh["observation_state"] == "in_flight"
    assert fresh["pending_count"] == 1
    assert fresh["in_flight_count"] == 1
    assert fresh["overdue_pending_count"] == 0
    assert fresh["missing_consumers"] == []
    assert build_runtime_producer_consumer_health(cfg, matrix_path=None)["counts"][
        "in_flight_receipts"
    ] == 1
    assert audit_runtime_producer_consumer_closure(
        cfg, strict=True, matrix_path=None
    ) == []

    overdue_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    ledger.record_produced(
        "async_projection",
        source="producer",
        item_id="overdue-item",
        intended_consumers=["projector"],
        created_at=overdue_at,
    )

    overdue = build_runtime_producer_consumer_health(cfg, matrix_path=None)["flows"][
        "async_projection"
    ]

    assert overdue["status"] == "degraded"
    assert overdue["observation_state"] == "partial"
    assert overdue["pending_count"] == 2
    assert overdue["in_flight_count"] == 1
    assert overdue["overdue_pending_count"] == 1
    assert overdue["missing_consumers"] == ["projector"]


def test_receipt_grace_rejects_negative_values_and_invalid_time_fails_closed(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    with pytest.raises(ValueError, match="receipt_grace_seconds must be non-negative"):
        ledger.register_flow(
            flow_id="invalid_grace",
            data_type="event",
            producer_refs=["producer"],
            consumer_refs=["consumer"],
            receipt_grace_seconds=-1,
        )

    ledger.register_flow(
        flow_id="invalid_time",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
        receipt_grace_seconds=60,
    )
    ledger.record_produced(
        "invalid_time",
        source="producer",
        item_id="bad-clock",
        intended_consumers=["consumer"],
        created_at="not-an-iso-timestamp",
    )

    flow = ledger.snapshot()["flows"]["invalid_time"]

    assert flow["status"] == "degraded"
    assert flow["observation_state"] == "stale"
    assert flow["overdue_pending_count"] == 1


def test_restart_preserves_generation_and_incomplete_state(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="restart",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
    )
    ledger.record_produced(
        "restart",
        source="producer",
        item_id="item-1",
        intended_consumers=["consumer"],
        generation_id="daemon-generation-1",
    )

    reopened = ProducerConsumerLedger(cfg, initialize=True)
    flow = reopened.snapshot()["flows"]["restart"]

    assert flow["generation_id"] == "daemon-generation-1"
    assert flow["observation_state"] == "partial"
    assert flow["missing_consumers"] == ["consumer"]


def test_stale_complete_flow_is_degraded(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="stale",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
        max_lag_seconds=60,
    )
    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    production_event_id = ledger.record_produced(
        "stale",
        source="producer",
        item_id="event-1",
        intended_consumers=["consumer"],
        created_at=stale_at,
    )
    ledger.record_consumed(
        "stale",
        source="consumer",
        item_id="event-1",
        production_event_id=production_event_id,
        created_at=stale_at,
    )

    flow = build_runtime_producer_consumer_health(cfg, matrix_path=None)["flows"]["stale"]

    assert flow["observation_state"] == "stale"
    assert flow["freshness_ok"] is False


def test_duplicate_terminal_receipt_is_idempotent_and_conflicting_terminal_is_rejected(
    tmp_path,
):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="idempotent",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
    )
    production_event_id = ledger.record_produced(
        "idempotent",
        source="producer",
        item_id="event-1",
        intended_consumers=["consumer"],
    )
    receipt_id = ledger.record_consumed(
        "idempotent",
        source="consumer",
        item_id="event-1",
        production_event_id=production_event_id,
        idempotency_key="consumer:event-1:consumed",
    )
    duplicate_id = ledger.record_consumed(
        "idempotent",
        source="consumer",
        item_id="event-1",
        production_event_id=production_event_id,
        idempotency_key="consumer:event-1:consumed",
    )
    with pytest.raises(ValueError, match="terminal_receipt_conflict"):
        ledger.record_dead_letter(
            "idempotent",
            source="consumer",
            item_id="event-1",
            production_event_id=production_event_id,
            idempotency_key="consumer:event-1:dead_letter",
        )

    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            "SELECT status FROM runtime_flow_receipts ORDER BY created_at, receipt_id"
        ).fetchall()

    assert duplicate_id == receipt_id
    assert rows == [("consumed",)]


@pytest.mark.parametrize(
    ("second_call"),
    [
        {
            "flow_id": "idempotency-collision",
            "item_id": "item-two",
            "generation_id": "generation-two",
            "event": "second",
            "status": "consumed",
            "metadata": {"proof": "one"},
        },
        {
            "flow_id": "idempotency-collision",
            "item_id": "item-one",
            "generation_id": "generation-two",
            "event": "second",
            "status": "consumed",
            "metadata": {"proof": "one"},
        },
        {
            "flow_id": "idempotency-collision",
            "item_id": "item-one",
            "generation_id": "generation-one",
            "event": "first",
            "status": "dead_letter",
            "metadata": {"proof": "one"},
        },
        {
            "flow_id": "idempotency-collision",
            "item_id": "item-one",
            "generation_id": "generation-one",
            "event": "first",
            "status": "consumed",
            "metadata": {"proof": "two"},
        },
    ],
)
def test_terminal_receipt_idempotency_key_rejects_effect_identity_collision(
    tmp_path: Path,
    second_call: dict[str, str | dict[str, str]],
) -> None:
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="idempotency-collision",
        data_type="event",
        producer_refs=["producer"],
        consumer_refs=["consumer"],
    )
    events = {
        "first": ledger.record_produced(
            "idempotency-collision",
            source="producer",
            item_id="item-one",
            generation_id="generation-one",
        ),
        "second": ledger.record_produced(
            "idempotency-collision",
            source="producer",
            item_id="item-two",
            generation_id="generation-two",
        ),
    }
    ledger.record_consumed(
        "idempotency-collision",
        source="consumer",
        item_id="item-one",
        generation_id="generation-one",
        production_event_id=events["first"],
        metadata={"proof": "one"},
        idempotency_key="shared-terminal-key",
    )

    method = (
        ledger.record_dead_letter
        if second_call["status"] == "dead_letter"
        else ledger.record_consumed
    )
    with pytest.raises(ValueError, match="idempotency_key_conflict"):
        method(
            str(second_call["flow_id"]),
            source="consumer",
            item_id=str(second_call["item_id"]),
            generation_id=str(second_call["generation_id"]),
            production_event_id=events[str(second_call["event"])],
            metadata=second_call["metadata"],
            idempotency_key="shared-terminal-key",
        )
    with sqlite3.connect(ledger.db_path) as connection:
        rows = connection.execute(
            """
            SELECT production_event_id, item_id, generation_id, status, metadata
            FROM runtime_flow_receipts
            WHERE idempotency_key='shared-terminal-key'
            """
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][:4] == (
        events["first"],
        "item-one",
        "generation-one",
        "consumed",
    )
    assert json.loads(rows[0][4]) == {"proof": "one"}


def test_strict_audit_flags_item_id_mismatch_even_when_counts_balance(tmp_path):
    cfg = _config(tmp_path)
    ledger = ProducerConsumerLedger(cfg, initialize=True)
    ledger.register_flow(
        flow_id="mismatched_flow",
        data_type="paired",
        producer_refs=["producer.py"],
        consumer_refs=["consumer.py"],
        pending_budget=0,
        dead_letter_budget=0,
    )

    ledger.record_produced("mismatched_flow", source="producer.py", item_id="produced-1")
    ledger.record_consumed("mismatched_flow", source="consumer.py", item_id="consumed-1")

    health = build_runtime_producer_consumer_health(cfg, matrix_path=None)
    flow = health["flows"]["mismatched_flow"]
    errors = audit_runtime_producer_consumer_closure(cfg, strict=True, matrix_path=None)

    assert health["status"] == "degraded"
    assert health["counts"]["item_mismatch_flows"] == 1
    assert flow["orphan_item_count"] == 1
    assert flow["no_source_item_count"] == 1
    assert any(
        "mismatched_flow: 1 produced item ids were not consumed" in error for error in errors
    )
    assert any("mismatched_flow: 1 consumed item ids had no producer" in error for error in errors)
